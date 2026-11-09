"""Bridge build identity, available in checkouts and Git-free installations.

Distributions stamp build-info.json before leaving the checkout. At runtime a
real checkout reports its current Git state; installed/archive trees retain the
identity stamped by their builder rather than borrowing a parent repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
STAMP = 'build-info.json'


def _unknown():
    return {'version': 'dev', 'commit': '', 'short_commit': 'unknown',
            'description': 'unknown', 'dirty': False, 'tag': ''}


def _git(root):
    if not (root / '.git').exists(): return None
    env = {name: value for name, value in os.environ.items()
           if name not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE')}
    env['GIT_OPTIONAL_LOCKS'] = '0'

    def run(*args):
        result = subprocess.run(['git', '-C', str(root), *args], env=env,
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        top = run('rev-parse', '--show-toplevel')
        if not top or Path(top).resolve() != root: return None
        commit = run('rev-parse', '--verify', 'HEAD')
        if not commit or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit): return None
        status = run('status', '--porcelain', '--untracked-files=normal')
        if status is None: return None
        tag = run('describe', '--tags', '--abbrev=0') or ''
        description = run('describe', '--tags', '--always', '--abbrev=8') or commit[:8]
    except (OSError, subprocess.SubprocessError):
        return None
    dirty = bool(status)
    return {'version': tag or 'dev', 'commit': commit, 'short_commit': commit[:8],
            'description': description + ('-dirty' if dirty else ''), 'dirty': dirty, 'tag': tag}


def _stamped(root):
    try:
        path = root / STAMP
        if path.stat().st_size > 65536: return None
        value = json.loads(path.read_text())
        if not isinstance(value, dict): return None
        commit = value.get('commit', '')
        if not isinstance(commit, str) or (commit and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)):
            return None
        result = _unknown()
        for name in ('version', 'description', 'tag'):
            field = value.get(name, result[name])
            if not isinstance(field, str) or len(field) > 1024 or any(ord(c) < 32 for c in field): return None
            result[name] = field
        if not isinstance(value.get('dirty', False), bool): return None
        result.update(commit=commit, short_commit=commit[:8] if commit else 'unknown',
                      dirty=value.get('dirty', False))
        return result
    except (OSError, ValueError, TypeError):
        return None


def read(root=None):
    """JSON-safe build identity; no Git or network access is needed after install."""
    root = Path(root or ROOT).resolve()
    return _git(root) or _stamped(root) or _unknown()


def about(root=None):
    """GetZoneInfo fields for a virtual component; exact Git text goes in ExtraInfo.

    The CR100 removes '-' and '.' from SoftwareVersion when formatting a build,
    so retain the full describe/commit text separately in the appended details.
    """
    info = read(root)
    extra = 'Bridge build: ' + info['description']
    if info['commit']: extra += '\nGit commit: ' + info['commit']
    return {'software': info['short_commit'], 'display': info['version'],
            'hardware': 'Virtual component', 'extra': extra}
