"""GitHub release checking, tarball extraction, atomic release swap, and restart timing."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock
import urllib.error

import bridge_updater
import build_info
import distribution


class _FakeResponse:
    def __init__(self, data, headers=None):
        self._stream = io.BytesIO(data)
        self.headers = headers or {'Content-Length': str(len(data))}

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _make_source_tarball(prefix='DanielDavis05-Cr100-Bridge-1234567'):
    """Build a valid GitHub-style tarball from the repository's distributable tree."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as bundle:
        for path in distribution.files():
            rel = path.relative_to(distribution.ROOT)
            bundle.add(path, arcname=str(Path(prefix) / rel), recursive=False)
    return buf.getvalue()


class BridgeUpdaterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'bridge-config.json'
        self.config.write_text('{}')
        self.install_root = self.root / 'opt/cr100-bridge'
        self.current_root = self.root / 'running'
        self.current_root.mkdir()
        self.bin_dir = self.root / 'usr/local/bin'
        self.bin_dir.mkdir(parents=True)
        self.systemd_dir = self.root / 'etc/systemd/system'
        self.systemd_dir.mkdir(parents=True)
        self.commit_a = 'a' * 40
        self.commit_b = 'b' * 40
        (self.current_root / build_info.STAMP).write_text(json.dumps({
            'version': 'master',
            'commit': self.commit_a,
            'short_commit': self.commit_a[:8],
            'description': self.commit_a[:8],
            'dirty': False,
            'tag': '',
        }))

    def _command_runner(self, cmd, **kwargs):
        if len(cmd) >= 3 and str(cmd[1]).endswith('tools/distribution.py'):
            return subprocess.run(cmd, **kwargs)
        if len(cmd) >= 3 and cmd[1:3] == ['-m', 'venv']:
            venv_python = Path(cmd[3]) / 'bin/python'
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text('#!/bin/sh\nexit 0\n')
            venv_python.chmod(0o755)
            return subprocess.CompletedProcess(cmd, 0)
        if len(cmd) >= 4 and cmd[2:4] == ['--board', 'raspberrypi,4-model-b']:
            return subprocess.CompletedProcess(cmd, 0, stdout='firmware/nexmon-ownership/apply.py\n')
        return subprocess.CompletedProcess(cmd, 0)

    def test_check_for_update_compares_github_commit_caches_and_handles_errors(self):
        now = [100.0]
        calls = []
        responses = [
            _FakeResponse(json.dumps({
                'sha': self.commit_b,
                'commit': {'message': 'Add GitHub bridge self-update\n\nBody text'},
            }).encode()),
            OSError('offline'),
            _FakeResponse(json.dumps({
                'sha': self.commit_a,
                'commit': {'message': 'Already current'},
            }).encode()),
        ]

        def opener(req, timeout=None):
            calls.append(req.full_url)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        updater = bridge_updater.BridgeReleaseUpdater(
            self.config, install_root=self.install_root, current_root=self.current_root,
            opener=opener, clock=lambda: now[0], wall_clock=lambda: 1700000000.0)
        self.addCleanup(updater.close)

        first = updater.check_for_update()
        self.assertTrue(first['available'])
        self.assertEqual(first['current_commit'], self.commit_a)
        self.assertEqual(first['target_commit'], self.commit_b)
        self.assertEqual(first['target_short'], 'bbbbbbbb')
        self.assertEqual(first['summary'], 'Add GitHub bridge self-update')
        self.assertEqual(first['repo'], 'DanielDavis05/Cr100-Bridge')
        self.assertEqual(first['branch'], 'master')

        # Cached within TTL
        second = updater.check_for_update()
        self.assertEqual(second['target_commit'], self.commit_b)
        self.assertEqual(len(calls), 1)

        # Past TTL -> error does not crash or poison cache
        now[0] += 20.0
        failed = updater.check_for_update()
        self.assertFalse(failed['available'])
        self.assertIn('offline', failed['error'])
        self.assertEqual(len(calls), 2)

        # Immediate retry succeeds when network returns
        current = updater.check_for_update()
        self.assertFalse(current['available'])
        self.assertEqual(current['target_commit'], self.commit_a)
        self.assertEqual(len(calls), 3)

    def test_config_overrides_repo_and_branch_and_rejects_unsafe_values(self):
        self.config.write_text(json.dumps({'updates': {'repo': 'other/fork', 'branch': 'release/v1'}}))
        updater = bridge_updater.BridgeReleaseUpdater(self.config, current_root=self.current_root)
        self.addCleanup(updater.close)
        self.assertEqual(updater.settings(), ('other/fork', 'release/v1'))

        self.config.write_text(json.dumps({'updates': {'repo': '../bad/repo', 'branch': '../main'}}))
        self.assertEqual(updater.settings(), (bridge_updater.DEFAULT_REPO, bridge_updater.DEFAULT_BRANCH))

    def test_download_and_install_stages_release_flips_symlink_and_prunes_old_releases(self):
        releases = self.install_root / 'releases'
        releases.mkdir(parents=True)
        old_targets = []
        for idx in range(4):
            d = releases / f'release-old0000{idx}'
            d.mkdir()
            old_targets.append(d)
        (self.install_root / 'current').symlink_to(old_targets[0])

        tarball = _make_source_tarball()
        commands = []

        def runner(cmd, **kwargs):
            commands.append(cmd)
            return self._command_runner(cmd, **kwargs)

        compatible = self.root / 'compatible'
        compatible.write_bytes(b'raspberrypi,4-model-b\0')

        updater = bridge_updater.BridgeReleaseUpdater(
            self.config, install_root=self.install_root, current_root=self.current_root,
            bin_dir=self.bin_dir, systemd_dir=self.systemd_dir, compatible_path=compatible,
            opener=lambda req, timeout=None: _FakeResponse(tarball),
            command_runner=runner)
        self.addCleanup(updater.close)

        offer = {
            'target_commit': self.commit_b,
            'current_commit': self.commit_a,
            'repo': 'DanielDavis05/Cr100-Bridge',
            'branch': 'master',
        }
        dl_progress = []
        updater.download('tok1', offer, dl_progress.append)
        self.assertEqual(dl_progress, [0, 25, 75, 100])

        inst_progress = []
        updater.install('tok1', offer, inst_progress.append)
        self.assertEqual(inst_progress, [0, 25, 75, 100])

        active = (self.install_root / 'current').resolve()
        self.assertTrue(active.is_dir())
        self.assertNotEqual(active, old_targets[0].resolve())
        self.assertEqual(build_info.read(active)['commit'], self.commit_b)
        self.assertEqual(build_info.read(active)['version'], 'master')
        self.assertEqual(updater.current_commit(), self.commit_b)
        self.assertTrue((self.bin_dir / 'cr100-bridge').is_file())
        self.assertTrue((self.systemd_dir / 'cr100-bridge.service').is_file())
        self.assertTrue(any('apply.py' in str(c) and '--apply' in c for c in commands))
        # Staging directory cleaned up; previous current + 2 recent releases retained
        self.assertEqual(list((updater.storage / 'bridge-staging').iterdir()), [])
        self.assertTrue(old_targets[0].exists())
        self.assertLessEqual(len(list(releases.iterdir())), 4)

    def test_rejects_unsafe_tar_members(self):
        for name, kind in (('../escape.txt', tarfile.REGTYPE), ('/etc/passwd', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE)):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as bundle:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = '/etc/passwd'
                    bundle.addfile(info)
                else:
                    data = b'bad'
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
            updater = bridge_updater.BridgeReleaseUpdater(
                self.config, install_root=self.install_root, current_root=self.current_root,
                opener=lambda req, timeout=None: _FakeResponse(buf.getvalue()))
            self.addCleanup(updater.close)
            with self.assertRaises(ValueError):
                updater.download('tok-bad', {'target_commit': self.commit_b}, lambda _: None)
            self.assertEqual(list((updater.storage / 'bridge-staging').iterdir()), [])

    def test_concurrent_players_download_and_install_only_once(self):
        tarball = _make_source_tarball()
        fetches = []
        pip_calls = []

        def opener(req, timeout=None):
            fetches.append(req.full_url)
            return _FakeResponse(tarball)

        def runner(cmd, **kwargs):
            if 'pip' in str(cmd):
                pip_calls.append(cmd)
            return self._command_runner(cmd, **kwargs)

        updater = bridge_updater.BridgeReleaseUpdater(
            self.config, install_root=self.install_root, current_root=self.current_root,
            bin_dir=None, systemd_dir=None, compatible_path=None,
            opener=opener, command_runner=runner)
        self.addCleanup(updater.close)

        offer = {'target_commit': self.commit_b, 'repo': 'DanielDavis05/Cr100-Bridge', 'branch': 'master'}
        results = [[], []]
        barrier = threading.Barrier(2)

        def worker(index):
            barrier.wait()
            updater.download('shared-tok', offer, lambda p: results[index].append(('D', p)))
            barrier.wait()
            updater.install('shared-tok', offer, lambda p: results[index].append(('I', p)))

        threads = [threading.Thread(target=worker, args=(0,)), threading.Thread(target=worker, args=(1,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(fetches), 1)
        self.assertEqual(len(pip_calls), 1)
        for series in results:
            self.assertEqual(series, [('D', 0), ('D', 25), ('D', 75), ('D', 100),
                                      ('I', 0), ('I', 25), ('I', 75), ('I', 100)])

    def test_restart_schedule_and_do_post_update_expedite(self):
        triggered = threading.Event()
        callback = Mock(side_effect=lambda: triggered.set())
        updater = bridge_updater.BridgeReleaseUpdater(
            self.config, current_root=self.current_root, restart_callback=callback)
        self.addCleanup(updater.close)

        # Expediting without an armed restart is a no-op
        updater.expedite_restart(delay=0.01)
        self.assertFalse(triggered.wait(0.05))

        updater.schedule_restart(delay=5.0)
        self.assertFalse(triggered.is_set())
        updater.expedite_restart(delay=0.02)
        self.assertTrue(triggered.wait(1.0))
        callback.assert_called_once_with()

    def test_default_branch_is_master_and_falls_back_to_the_repository_default_on_422(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url.endswith('/commits/master'):
                raise urllib.error.HTTPError(req.full_url, 422, 'Unprocessable Entity', {}, None)
            if req.full_url.endswith('/commits/HEAD'):
                return _FakeResponse(json.dumps({
                    'sha': self.commit_b,
                    'commit': {'message': 'Commit on the default branch'},
                }).encode())
            raise AssertionError('Unexpected URL: ' + req.full_url)

        updater = bridge_updater.BridgeReleaseUpdater(
            self.config, install_root=self.install_root, current_root=self.current_root,
            opener=opener)
        self.addCleanup(updater.close)

        self.assertEqual(bridge_updater.DEFAULT_BRANCH, 'master')
        result = updater.check_for_update()
        self.assertTrue(result['available'])
        self.assertEqual(result['branch'], 'HEAD')
        self.assertEqual(result['target_commit'], self.commit_b)
        self.assertEqual([url.rpartition('/commits/')[2] for url in calls], ['master', 'HEAD'])

        # Explicit branch overrides do not fall back
        self.config.write_text(json.dumps({'updates': {'branch': 'custom-branch'}}))
        calls.clear()

        def fail_404(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, None)

        updater.opener = fail_404
        explicit = updater.check_for_update(force=True)
        self.assertFalse(explicit['available'])
        self.assertIn('404', explicit['error'])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
