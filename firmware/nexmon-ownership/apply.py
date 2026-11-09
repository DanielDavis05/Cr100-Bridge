#!/usr/bin/env python3
"""Check or atomically apply the reviewed Nexmon ownership patch to a known image.

The manifest contains only reviewed byte replacements. Unknown firmware is never
modified. Symlinks remain intact: their final regular-file target is replaced.
Applying writes a private original backup and audit receipt before replacement.
This command does not reload a driver, restart services, or change running RAM.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
DEFAULT_FIRMWARE = Path('/lib/firmware/brcm/brcmfmac43455-sdio.raspberrypi,4-model-b.bin')
DEFAULT_BACKUPS = Path('/var/lib/cr100-bridge/firmware-backups')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def digest(data): return hashlib.sha256(data).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate firmware manifest key: ' + key)
        result[key] = value
    return result


def _validate_patches(patches):
    if not isinstance(patches, list) or not patches: raise ValueError('No firmware patches in manifest.')
    ranges = []
    for patch in patches:
        if not isinstance(patch, dict) or type(patch.get('offset')) is not int or patch['offset'] < 0:
            raise ValueError('Invalid firmware patch offset.')
        try:
            before = bytes.fromhex(patch['expected'])
            after = bytes.fromhex(patch['replacement'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Invalid firmware patch bytes.') from None
        if not before or len(before) != len(after): raise ValueError('Firmware patches must preserve image size.')
        end = patch['offset'] + len(before)
        if end > 1 << 32: raise ValueError('Firmware patch range exceeds the 32-bit image address space.')
        ranges.append((patch['offset'], end))
    ranges.sort()
    if any(previous[1] > current[0] for previous, current in zip(ranges, ranges[1:])):
        raise ValueError('Overlapping firmware patches.')


def read_manifest(path):
    manifest = json.loads(Path(path).read_text(), object_pairs_hook=_unique_object)
    if not isinstance(manifest, dict) or type(manifest.get('format')) is not int or manifest['format'] != 1:
        raise ValueError('Unsupported firmware patch manifest.')
    for field in ('base_sha256', 'patched_sha256'):
        if not isinstance(manifest.get(field), str) or not SHA256.fullmatch(manifest[field]):
            raise ValueError('Invalid manifest ' + field)
    if manifest['base_sha256'] == manifest['patched_sha256']:
        raise ValueError('The manifest must describe a changed firmware image.')
    name, stats = manifest.get('firmware_name'), manifest.get('stats')
    if not isinstance(name, str) or not name or Path(name).name != name or manifest.get('ram_base') != '0x198000':
        raise ValueError('Invalid firmware target metadata.')
    if not isinstance(stats, dict) or any(type(stats.get(key)) is not int for key in ('command', 'version', 'size')) \
            or (stats['command'], stats.get('magic'), stats['version'], stats['size']) != (430, '0x4f4e5243', 1, 48):
        raise ValueError('Unsupported ownership diagnostic ABI in manifest.')
    _validate_patches(manifest.get('patches'))
    upgrades = manifest.get('upgrades', [])
    if not isinstance(upgrades, list): raise ValueError('Invalid firmware upgrades list.')
    bases = {manifest['base_sha256']}
    for upgrade in upgrades:
        if not isinstance(upgrade, dict) or set(upgrade) != {'base_sha256', 'patches'}:
            raise ValueError('Invalid firmware upgrade entry.')
        base = upgrade['base_sha256']
        if not isinstance(base, str) or not SHA256.fullmatch(base):
            raise ValueError('Invalid firmware upgrade base_sha256.')
        if base in bases or base == manifest['patched_sha256']:
            raise ValueError('Firmware upgrade bases must be unique and differ from the target image.')
        bases.add(base)
        _validate_patches(upgrade['patches'])
    return manifest


def verify_final_bytes(data, manifest):
    """Every accepted route must satisfy the canonical target byte replacements."""
    for patch in manifest['patches']:
        expected = bytes.fromhex(patch['replacement'])
        if data[patch['offset']:patch['offset'] + len(expected)] != expected:
            raise ValueError('Patched firmware byte verification failed.')


def verify_image_ranges(size, manifest):
    # All routes preserve size and converge to the same whole-image checksum,
    # so their patch ranges must fit the same image extent, even when unselected.
    for route in [manifest, *manifest.get('upgrades', [])]:
        for patch in route['patches']:
            if patch['offset'] + len(bytes.fromhex(patch['expected'])) > size:
                raise ValueError('Firmware patch range exceeds the image size at offset %#x.' % patch['offset'])


def patched_image(data, manifest):
    checksum = digest(data)
    routes = [manifest, *manifest.get('upgrades', [])]
    route = next((route for route in routes if checksum == route['base_sha256']), None)
    if route is None:
        raise ValueError('Unknown firmware image; refusing to patch SHA256 ' + checksum)
    verify_image_ranges(len(data), manifest)
    result = bytearray(data)
    for patch in route['patches']:
        offset = patch['offset']
        before, after = bytes.fromhex(patch['expected']), bytes.fromhex(patch['replacement'])
        if data[offset:offset + len(before)] != before:
            raise ValueError('Firmware patch precondition failed at offset %#x.' % offset)
        result[offset:offset + len(after)] = after
    result = bytes(result)
    if len(result) != len(data) or digest(result) != manifest['patched_sha256']:
        raise ValueError('Patched firmware checksum does not match the reviewed image.')
    verify_final_bytes(result, manifest)
    return result


def inspect(path, manifest):
    requested = Path(path).absolute()
    target = requested.resolve(strict=True)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode): raise ValueError('Firmware target is not a regular file.')
    data = target.read_bytes()
    checksum = digest(data)
    if checksum == manifest['patched_sha256']:
        verify_image_ranges(len(data), manifest)
        verify_final_bytes(data, manifest)
        state, output = 'already_patched', data
    else:
        output = patched_image(data, manifest)
        state = 'patch_available'
    return {'requested': requested, 'target': target, 'info': info, 'data': data,
            'output': output, 'state': state, 'sha256': checksum}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def atomic_write(path, data, mode, owner=None):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.cr100-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            if owner is not None and os.geteuid() == 0:
                os.fchown(output.fileno(), *owner)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def summary(inspection):
    return {'state': inspection['state'], 'requested_path': str(inspection['requested']),
            'resolved_path': str(inspection['target']), 'sha256': inspection['sha256']}


def patch_file(path, manifest_path=HERE / 'manifest.json', backup_dir=DEFAULT_BACKUPS, apply=False):
    manifest_path, backup_dir = Path(manifest_path), Path(backup_dir)
    manifest = read_manifest(manifest_path)
    checked = inspect(path, manifest)
    if not apply or checked['state'] == 'already_patched': return summary(checked)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir.chmod(0o700)
    lock_path = backup_dir / (digest(os.fsencode(str(checked['target']))) + '.lock')
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked = inspect(path, manifest)
        if checked['state'] == 'already_patched': return summary(checked)
        backup = backup_dir / ('base-' + checked['sha256'] + '.bin')
        if backup.exists() or backup.is_symlink():
            if not stat.S_ISREG(backup.lstat().st_mode) or digest(backup.read_bytes()) != checked['sha256']:
                raise ValueError('Existing firmware backup is not the expected original image.')
            backup.chmod(0o600)
        else:
            atomic_write(backup, checked['data'], 0o600)
        audit = backup_dir / ('apply-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') +
                              '-' + uuid.uuid4().hex + '.json')
        receipt = {**summary(checked), 'state': 'prepared', 'backup_path': str(backup),
                   'base_sha256': checked['sha256'],
                   'patched_sha256': manifest['patched_sha256'],
                   'manifest_sha256': digest(manifest_path.read_bytes()),
                   'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        encode = lambda: (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode()
        atomic_write(audit, encode(), 0o600)
        # Check again after backup/audit I/O, so a package update or a changed
        # alternatives symlink cannot redirect this reviewed replacement.
        target, info = checked['target'], checked['info']
        current = target.stat()
        if Path(path).resolve(strict=True) != target or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) \
                or digest(target.read_bytes()) != checked['sha256']:
            raise ValueError('Firmware changed during patch preparation; no replacement performed.')
        atomic_write(target, checked['output'], stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))
        if digest(target.read_bytes()) != manifest['patched_sha256']:
            raise ValueError('Installed firmware failed checksum verification; inspect the retained backup.')
        receipt.update(state='applied', sha256=manifest['patched_sha256'])
        atomic_write(audit, encode(), 0o600)
        return {**receipt, 'audit_path': str(audit), 'reboot_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='read-only hash and byte-precondition verification')
    action.add_argument('--apply', action='store_true', help='back up and atomically patch a recognized image')
    parser.add_argument('--firmware', type=Path, default=DEFAULT_FIRMWARE)
    parser.add_argument('--manifest', type=Path, default=HERE / 'manifest.json')
    parser.add_argument('--backup-dir', type=Path, default=DEFAULT_BACKUPS)
    args = parser.parse_args()
    try:
        result = patch_file(args.firmware, args.manifest, args.backup_dir, args.apply)
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
