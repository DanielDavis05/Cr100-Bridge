#!/usr/bin/env python3
"""GitHub release checker, downloader, and atomic installer for CR100 Bridge self-updates."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
from urllib.parse import quote
import urllib.request

import build_info

DEFAULT_REPO = 'DanielDavis05/Cr100-Bridge'
DEFAULT_BRANCH = 'master'
DEFAULT_INSTALL_ROOT = Path('/opt/cr100-bridge')
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
CHECK_CACHE_TTL = 15.0
API_TIMEOUT = 5.0
DOWNLOAD_TIMEOUT = 30.0
REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
BRANCH_RE = re.compile(r'^[A-Za-z0-9_./-]+\Z')
SHA_RE = re.compile(r'^[0-9a-f]{40}\Z')


def _remove_group_other_write(root):
    for current, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(current) / name
            if path.is_symlink():
                continue
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o022)


def _extract_archive(archive_path, destination):
    """Safely extract a GitHub source tarball and return the directory with tools/distribution.py."""
    dest = Path(destination).resolve()
    total_bytes = 0
    with tarfile.open(archive_path, 'r:gz') as bundle:
        for member in bundle:
            if member.isdev() or member.isfifo() or member.islnk() or member.issym():
                raise ValueError('Unsupported archive entry type: ' + member.name)
            if not (member.isdir() or member.isfile()):
                raise ValueError('Unsupported archive entry: ' + member.name)
            rel = Path(member.name)
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('Unsafe archive path: ' + member.name)
            target = (dest / rel).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError('Archive entry escapes staging directory: ' + member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total_bytes += member.size
            if total_bytes > MAX_EXTRACTED_BYTES:
                raise ValueError('Extracted archive exceeds size limit')
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, 65536)
            mode = (member.mode & 0o777) or 0o644
            target.chmod(mode & ~0o022)
    candidates = [path for path in dest.iterdir()
                  if path.is_dir() and not path.is_symlink() and (path / 'tools/distribution.py').is_file()]
    if len(candidates) != 1:
        raise ValueError('Unexpected archive layout; missing tools/distribution.py')
    return candidates[0]


def _swap_current_symlink(install_root, release_dir):
    current_link = install_root / 'current'
    previous = current_link.resolve() if current_link.exists() else None
    temp_link = install_root / '.current-new'
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(release_dir)
    os.replace(temp_link, current_link)
    return previous


def _prune_releases(releases_dir, current_release, previous_release):
    protected = {current_release.resolve()}
    current_link = releases_dir.parent / 'current'
    if current_link.exists():
        protected.add(current_link.resolve())
    if previous_release is not None:
        protected.add(previous_release.resolve())
    candidates = sorted(
        (path for path in releases_dir.iterdir()
         if path.name.startswith('release-') and not path.is_symlink() and path.is_dir()
         and path.resolve() != current_release.resolve()),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    protected.update(path.resolve() for path in candidates[:2])
    for path in candidates:
        if path.resolve() not in protected:
            shutil.rmtree(path, ignore_errors=True)


def _default_restart():
    if sys.platform == 'linux' and os.geteuid() == 0 and Path('/run/systemd/system').is_dir():
        subprocess.Popen(
            ['systemctl', '--no-block', 'restart', 'cr100-webui.service', 'cr100-bridge.service'],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class _TokenJob:
    def __init__(self, target_commit):
        self.target_commit = target_commit
        self.condition = threading.Condition()
        self.download_state = 'pending'
        self.download_percent = 0
        self.install_state = 'pending'
        self.install_percent = 0
        self.staged_root = None
        self.source_dir = None
        self.error = None


class BridgeReleaseUpdater:
    """Checks GitHub for bridge updates, stages archives, and installs new releases."""

    def __init__(self, config_path=None, *, storage=None, install_root=DEFAULT_INSTALL_ROOT,
                 current_root=None, bin_dir=Path('/usr/local/bin'),
                 systemd_dir=Path('/etc/systemd/system'),
                 compatible_path=Path('/proc/device-tree/compatible'),
                 opener=urllib.request.urlopen, command_runner=subprocess.run,
                 restart_callback=_default_restart, clock=time.monotonic,
                 wall_clock=time.time, cache_ttl=CHECK_CACHE_TTL):
        self.config_path = Path(config_path).resolve() if config_path else None
        base = self.config_path.parent if self.config_path else Path.cwd()
        self.storage = Path(storage or base / 'controller-updates').resolve()
        self.install_root = Path(install_root)
        self.current_root = Path(current_root).resolve() if current_root else build_info.ROOT
        self.bin_dir = Path(bin_dir) if bin_dir else None
        self.systemd_dir = Path(systemd_dir) if systemd_dir else None
        self.compatible_path = Path(compatible_path) if compatible_path else None
        self.opener = opener
        self.command_runner = command_runner
        self.restart_callback = restart_callback
        self.clock = clock
        self.wall_clock = wall_clock
        self.cache_ttl = float(cache_ttl)
        self._lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._jobs = {}
        self._cached_check = None
        self._cached_repo = None
        self._cached_current = None
        self._cached_at = 0.0
        self.installed_commit = None
        self._restart_timer = None
        self._restart_pending = False
        self._closed = False

    def _settings_with_origin(self):
        repo, branch, explicit_branch = DEFAULT_REPO, DEFAULT_BRANCH, False
        if self.config_path and self.config_path.is_file():
            try:
                config = json.loads(self.config_path.read_text())
            except (OSError, ValueError):
                config = {}
            updates = config.get('updates') if isinstance(config, dict) else None
            if isinstance(updates, dict):
                raw_repo = updates.get('repo')
                raw_branch = updates.get('branch')
                if isinstance(raw_repo, str) and REPO_RE.fullmatch(raw_repo.strip()):
                    repo = raw_repo.strip()
                if isinstance(raw_branch, str):
                    candidate = raw_branch.strip()
                    if (BRANCH_RE.fullmatch(candidate) and not candidate.startswith('/')
                            and not candidate.endswith('/') and '..' not in candidate.split('/')):
                        branch = candidate
                        explicit_branch = True
        return repo, branch, explicit_branch

    def settings(self):
        repo, branch, _ = self._settings_with_origin()
        return repo, branch

    def current_commit(self):
        with self._lock:
            if self.installed_commit:
                return self.installed_commit
        return build_info.read(self.current_root).get('commit', '')

    def check_for_update(self, force=False):
        repo, branch, explicit_branch = self._settings_with_origin()
        current = self.current_commit()
        with self._lock:
            if (not force and self._cached_check is not None
                    and self._cached_repo == (repo, branch)
                    and self._cached_current == current
                    and self.clock() - self._cached_at < self.cache_ttl):
                return dict(self._cached_check)
        candidates = [branch] if explicit_branch else list(dict.fromkeys((branch, 'master', 'HEAD')))
        try:
            payload = None
            resolved_branch = branch
            for index, ref in enumerate(candidates):
                url = f'https://api.github.com/repos/{repo}/commits/{quote(ref, safe="/")}'
                req = urllib.request.Request(url, headers={
                    'Accept': 'application/vnd.github+json',
                    'User-Agent': 'cr100-bridge',
                })
                try:
                    with self.opener(req, timeout=API_TIMEOUT) as response:
                        payload = json.loads(response.read(1024 * 1024).decode('utf-8'))
                    resolved_branch = ref
                    break
                except urllib.error.HTTPError as http_error:
                    if http_error.code in (404, 422) and index + 1 < len(candidates):
                        continue
                    raise
            sha = payload.get('sha') if isinstance(payload, dict) else None
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                raise ValueError('Invalid commit SHA returned by GitHub')
            commit_obj = payload.get('commit') if isinstance(payload.get('commit'), dict) else {}
            raw_msg = commit_obj.get('message') if isinstance(commit_obj.get('message'), str) else ''
            summary = raw_msg.splitlines()[0].strip()[:120] if raw_msg.strip() else ''
            result = {
                'available': bool(sha and sha != current),
                'repo': repo,
                'branch': resolved_branch,
                'current_commit': current,
                'current_short': current[:8] if current else 'unknown',
                'target_commit': sha,
                'target_short': sha[:8],
                'summary': summary,
                'checked_at': self.wall_clock(),
            }
            with self._lock:
                self._cached_check = dict(result)
                self._cached_repo = (repo, branch)
                self._cached_current = current
                self._cached_at = self.clock()
            return result
        except (OSError, urllib.error.URLError, ValueError, TypeError) as error:
            return {
                'available': False,
                'repo': repo,
                'branch': branch,
                'current_commit': current,
                'current_short': current[:8] if current else 'unknown',
                'target_commit': '',
                'target_short': '',
                'summary': '',
                'checked_at': self.wall_clock(),
                'error': str(error)[:256],
            }

    def _job_for(self, token, target_commit):
        if not isinstance(target_commit, str) or not SHA_RE.fullmatch(target_commit):
            raise ValueError('Missing or invalid target commit SHA')
        with self._lock:
            job = self._jobs.get(token)
            if job is None:
                job = _TokenJob(target_commit)
                self._jobs[token] = job
            return job

    def _wait_phase(self, job, state_attr, percent_attr, progress, fail_message):
        emitted = 0
        progress(0)
        while True:
            with job.condition:
                state = getattr(job, state_attr)
                percent = getattr(job, percent_attr)
                error = job.error
                if state == 'running':
                    job.condition.wait(timeout=0.5)
            for milestone in (25, 75):
                if percent >= milestone > emitted:
                    progress(milestone)
                    emitted = milestone
            if state == 'done':
                if emitted < 100:
                    progress(100)
                return
            if state == 'failed':
                raise RuntimeError(error or fail_message)

    def download(self, token, offer, progress):
        target_commit = offer.get('target_commit')
        repo = offer.get('repo') or self.settings()[0]
        branch = offer.get('branch') or self.settings()[1]
        if not REPO_RE.fullmatch(repo):
            raise ValueError('Invalid repository name')
        job = self._job_for(token, target_commit)
        with job.condition:
            if job.download_state == 'done':
                leader = False
                already_done = True
            elif job.download_state == 'failed':
                raise RuntimeError(job.error or 'Bridge download failed')
            elif job.download_state == 'running':
                leader = False
                already_done = False
            else:
                job.download_state = 'running'
                job.download_percent = 0
                leader = True
                already_done = False
        if already_done:
            for percent in (0, 25, 75, 100):
                progress(percent)
            return
        if not leader:
            self._wait_phase(job, 'download_state', 'download_percent', progress, 'Bridge download failed')
            return
        staging_root = self.storage / 'bridge-staging'
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = Path(tempfile.mkdtemp(prefix='stage-', dir=str(staging_root)))
        archive_path = staged / 'source.tar.gz'
        try:
            def set_percent(value):
                with job.condition:
                    job.download_percent = value
                    job.condition.notify_all()
                progress(value)

            set_percent(0)
            url = f'https://codeload.github.com/{repo}/tar.gz/{target_commit}'
            req = urllib.request.Request(url, headers={'User-Agent': 'cr100-bridge'})
            emitted_25, emitted_75 = False, False
            with self.opener(req, timeout=DOWNLOAD_TIMEOUT) as response:
                raw_length = response.headers.get('Content-Length') if response.headers else None
                expected_length = int(raw_length) if raw_length and raw_length.isdigit() else None
                if expected_length is not None and expected_length > MAX_ARCHIVE_BYTES:
                    raise ValueError('Bridge archive exceeds maximum size')
                downloaded = 0
                with archive_path.open('wb') as out:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_ARCHIVE_BYTES:
                            raise ValueError('Bridge archive exceeds maximum size')
                        out.write(chunk)
                        if expected_length:
                            ratio = downloaded / expected_length
                            if ratio >= 0.25 and not emitted_25:
                                set_percent(25)
                                emitted_25 = True
                            if ratio >= 0.75 and not emitted_75:
                                set_percent(75)
                                emitted_75 = True
            if not emitted_25:
                set_percent(25)
            if not emitted_75:
                set_percent(75)
            extract_dir = staged / 'extracted'
            extract_dir.mkdir(mode=0o700)
            source_dir = _extract_archive(archive_path, extract_dir)
            archive_path.unlink(missing_ok=True)
            stamp = {
                'version': branch,
                'commit': target_commit,
                'short_commit': target_commit[:8],
                'description': f'{target_commit[:8]} ({branch})',
                'dirty': False,
                'tag': '',
            }
            (source_dir / build_info.STAMP).write_text(json.dumps(stamp, indent=2, sort_keys=True) + '\n')
            with job.condition:
                job.staged_root = staged
                job.source_dir = source_dir
                job.download_state = 'done'
                job.download_percent = 100
                job.condition.notify_all()
            progress(100)
        except Exception as error:
            shutil.rmtree(staged, ignore_errors=True)
            with job.condition:
                job.download_state = 'failed'
                job.error = str(error)[:256]
                job.condition.notify_all()
            raise

    def install(self, token, offer, progress):
        target_commit = offer.get('target_commit')
        job = self._job_for(token, target_commit)
        with job.condition:
            if job.download_state != 'done' or not job.source_dir:
                raise ValueError('Bridge update package was not downloaded')
            if job.install_state == 'done':
                leader = False
                already_done = True
            elif job.install_state == 'failed':
                raise RuntimeError(job.error or 'Bridge installation failed')
            elif job.install_state == 'running':
                leader = False
                already_done = False
            else:
                job.install_state = 'running'
                job.install_percent = 0
                leader = True
                already_done = False
        if already_done:
            for percent in (0, 25, 75, 100):
                progress(percent)
            return
        if not leader:
            self._wait_phase(job, 'install_state', 'install_percent', progress, 'Bridge installation failed')
            return
        releases_dir = self.install_root / 'releases'
        releases_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        release_dir = Path(tempfile.mkdtemp(prefix='release-', dir=str(releases_dir)))
        release_dir.chmod(0o755)
        activated = False
        try:
            def set_percent(value):
                with job.condition:
                    job.install_percent = value
                    job.condition.notify_all()
                progress(value)

            set_percent(0)
            source_dir = job.source_dir
            self.command_runner(
                [sys.executable, str(source_dir / 'tools/distribution.py'), '--copy', str(release_dir)],
                check=True,
                timeout=60,
            )
            _remove_group_other_write(release_dir)

            set_percent(25)
            venv_dir = release_dir / '.venv'
            venv_python = venv_dir / 'bin/python'
            self.command_runner([sys.executable, '-m', 'venv', str(venv_dir)], check=True, timeout=120)
            self.command_runner(
                [str(venv_python), '-m', 'pip', 'install', '--disable-pip-version-check',
                 '-r', str(release_dir / 'requirements.txt')],
                check=True,
                timeout=300,
            )

            set_percent(75)
            self.command_runner(
                [str(venv_python), '-c',
                 'import ccmp, ha_client, sonos_backend, spotify_service, netstart_pair, scapy.all, zoneplayers; '
                 'zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)'],
                cwd=str(release_dir / 'tools'),
                check=True,
                timeout=30,
            )
            self._maybe_patch_firmware(release_dir, venv_python)
            self._install_host_units(release_dir)
            previous = _swap_current_symlink(self.install_root, release_dir)
            activated = True
            _prune_releases(releases_dir, release_dir, previous)
            with self._lock:
                self.installed_commit = target_commit
                self._cached_check = None
            with job.condition:
                job.install_state = 'done'
                job.install_percent = 100
                job.condition.notify_all()
            progress(100)
        except Exception as error:
            if not activated:
                shutil.rmtree(release_dir, ignore_errors=True)
            with job.condition:
                job.install_state = 'failed'
                job.error = str(error)[:256]
                job.condition.notify_all()
            raise
        finally:
            if job.staged_root:
                shutil.rmtree(job.staged_root, ignore_errors=True)
                job.staged_root = None

    def _maybe_patch_firmware(self, release_dir, venv_python):
        if not self.compatible_path or not self.compatible_path.is_file():
            return
        try:
            board = self.compatible_path.read_bytes().split(b'\0')[0].decode('ascii', 'replace')
        except OSError:
            return
        if not board:
            return
        platform_script = release_dir / 'tools/radio_platform.py'
        if not platform_script.is_file():
            return
        probe = self.command_runner(
            [str(venv_python), str(platform_script), '--board', board, '--field', 'firmware_patch'],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if probe.returncode != 0:
            return
        patch_rel = (probe.stdout or '').strip()
        if not patch_rel:
            return
        patch_script = (release_dir / patch_rel).resolve()
        if release_dir.resolve() not in patch_script.parents or not patch_script.is_file():
            return
        self.command_runner([str(venv_python), str(patch_script), '--apply'], check=True, timeout=30)

    def _install_host_units(self, release_dir):
        if self.bin_dir and self.bin_dir.is_dir() and os.access(self.bin_dir, os.W_OK):
            cli_src = release_dir / 'scripts/cr100-bridge'
            if cli_src.is_file():
                target = self.bin_dir / 'cr100-bridge'
                fd, temp_name = tempfile.mkstemp(prefix='.cr100-bridge-', dir=str(self.bin_dir))
                try:
                    with os.fdopen(fd, 'wb') as out:
                        out.write(cli_src.read_bytes())
                        os.fchmod(out.fileno(), 0o755)
                    os.replace(temp_name, target)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
        if self.systemd_dir and self.systemd_dir.is_dir() and os.access(self.systemd_dir, os.W_OK):
            updated = False
            for service in sorted((release_dir / 'systemd').glob('*.service')):
                target = self.systemd_dir / service.name
                fd, temp_name = tempfile.mkstemp(prefix='.' + service.name + '-', dir=str(self.systemd_dir))
                try:
                    with os.fdopen(fd, 'wb') as out:
                        out.write(service.read_bytes())
                        os.fchmod(out.fileno(), 0o644)
                    os.replace(temp_name, target)
                    updated = True
                finally:
                    Path(temp_name).unlink(missing_ok=True)
            if updated and self.systemd_dir == Path('/etc/systemd/system') and Path('/run/systemd/system').is_dir():
                self.command_runner(['systemctl', 'daemon-reload'], check=True, timeout=15)

    def schedule_restart(self, delay=5.0):
        with self._restart_lock:
            if self._closed:
                return
            self._restart_pending = True
            self._arm_restart_timer_locked(delay)

    def expedite_restart(self, delay=1.0):
        with self._restart_lock:
            if self._closed or not self._restart_pending:
                return
            self._arm_restart_timer_locked(delay)

    def _arm_restart_timer_locked(self, delay):
        if self._restart_timer is not None:
            self._restart_timer.cancel()
        timer = threading.Timer(max(0.0, float(delay)), self._run_restart)
        timer.daemon = True
        self._restart_timer = timer
        timer.start()

    def _run_restart(self):
        with self._restart_lock:
            if self._closed or not self._restart_pending:
                return
            self._restart_pending = False
            self._restart_timer = None
            callback = self.restart_callback
        if callable(callback):
            callback()

    def close(self):
        with self._restart_lock:
            self._closed = True
            self._restart_pending = False
            if self._restart_timer is not None:
                self._restart_timer.cancel()
                self._restart_timer = None
