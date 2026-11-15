"""Authenticated CR100 package staging shared by the web UI and player process.

Model-2 UPDs with unverified signatures require explicit consent. The controller runs its
own installer; this module persists selections and short-lived compatibility
prompts, and authorizes delivery to the selected controller. It never executes
an installer or treats a successful HTTP download as a completed installation.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import tempfile
import time
import zlib

from controller_state import normalize_mac

MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
STAGED_TTL = 7 * 24 * 3600
DEFAULT_TTL = 3600
DOWNLOAD_TTL = 1800
PROMPT_TTL = 120
UPD_MAGIC = 0x35167F49
MD5_DIGEST_INFO = bytes.fromhex('3020300c06082a864886f70d020505000410')
# Public firmware verification key from the stock CR100 /etc/upgradekey.pub.
# This is the vendor signing PUBLIC modulus, not a device identity/decryption key.
VENDOR_MODULUS = int(
    '9ff7ec436b11a752bb9ab2fce9359ac79ed983434ea024044e2ad98717d831b63'
    'caea335d40f72584be29d7724a8e494e27e0136f819b6ae358f529f60a3a98e'
    'bfd6af5daa73a5b08d948bd722b3205c976234152f385bfb5184d27a7a01c8cd'
    'e38052a8a5645e136af54560541b10b07d9d115317fe8b651d336e551be16275', 16)
VENDOR_EXPONENT = 65537
_ACTIVE = frozenset(('staged', 'armed', 'downloading'))
_VERSION = re.compile(r'(\d{1,3})\.(\d{1,3})-(\d{1,10})\Z')
FORCE_UNAVAILABLE = ('Automatic update prompts are supported only on a controller confirmed to run 4.3. '
                     'Use Settings > Online Updates on 4.3.2 or when its version is unknown.')
CFW_BUILD_ID = re.compile(r'cfw3:[0-9a-f]{64}\Z')


class CramFS:
    """Bounded exact-path reader for a CR100 root filesystem image.

    No host extraction or optional dependencies. The image checksum is verified
    once; each read walks one exact path and refuses links, padding tricks,
    ambiguous names and oversized files.
    """
    def __init__(self, data):
        if (len(data) < 76 or struct.unpack_from('<4I', data) != (0x28cd3d45, len(data), 3, 0)
                or data[16:32] != b'Compressed ROMFS'):
            raise ValueError('Unsupported CramFS')
        checked = bytearray(data)
        checksum = struct.unpack_from('<I', checked, 32)[0]
        struct.pack_into('<I', checked, 32, 0)
        if zlib.crc32(checked) & 0xffffffff != checksum:
            raise ValueError('Invalid CramFS checksum')
        self.data = data

    def _inode(self, position):
        if not 0 <= position <= len(self.data) - 12:
            raise ValueError('Invalid CramFS inode')
        a, b, c = struct.unpack_from('<3I', self.data, position)
        return a & 0xffff, b & 0xffffff, (c >> 6) * 4, (c & 63) * 4

    def _children(self, node):
        data = self.data
        mode, size, offset, _ = node
        if not stat.S_ISDIR(mode) or offset + size > len(data) or size > 8192 * 264:
            raise ValueError('Invalid CramFS directory')
        end, children = offset + size, []
        while offset < end:
            child = self._inode(offset)
            padded = child[3]
            if not padded or offset + 12 + padded > end:
                raise ValueError('Invalid CramFS name')
            raw = data[offset + 12:offset + 12 + padded].split(b'\0', 1)[0]
            if not raw or b'/' in raw or raw in (b'.', b'..'):
                raise ValueError('Invalid CramFS name')
            children.append((raw, child))
            offset += 12 + padded
        if offset != end:
            raise ValueError('Invalid CramFS directory')
        return children

    def _lookup(self, path):
        node = self._inode(64)
        for name in path.encode().split(b'/'):
            found = [child for raw, child in self._children(node) if raw == name]
            if len(found) != 1:
                raise ValueError('Missing or ambiguous CramFS path %s' % path)
            node = found[0]
        return node

    def listdir(self, path):
        """Names in one directory, in image order."""
        return [raw.decode('utf-8', 'replace') for raw, _ in self._children(self._lookup(path))]

    def read(self, path, limit=4 * 1024 * 1024):
        """One regular file's bytes; links and files over limit are refused."""
        data = self.data
        mode, size, offset, _ = self._lookup(path)
        if not stat.S_ISREG(mode) or not 0 < size <= limit:
            raise ValueError('Invalid CramFS file %s' % path)
        count = (size + 4095) // 4096
        position = offset + 4 * count
        if position > len(data):
            raise ValueError('Invalid CramFS block table')
        result = []
        for index, end in enumerate(struct.unpack_from('<%dI' % count, data, offset)):
            if not position <= end <= len(data):
                raise ValueError('Invalid CramFS block')
            expected = min(4096, size - index * 4096)
            if end == position:
                chunk = bytes(expected)
            else:
                decoder = zlib.decompressobj()
                chunk = decoder.decompress(data[position:end], expected + 1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError('Invalid compressed CramFS block')
            if len(chunk) != expected:
                raise ValueError('Wrong CramFS block size')
            result.append(chunk)
            position = end
        return b''.join(result)


def _mainui_from_cramfs(data):
    return CramFS(data).read('opt/swf/MainUI.swf')


def upd_records(data):
    """(kind, offset, payload, flags) for each record of a UPD; structure only, no signature check."""
    records, offset = [], 0
    while offset < len(data):
        if len(records) >= 128 or len(data) - offset < 16:
            raise UpdateError('The firmware UPD contains invalid or truncated records.')
        magic, kind, length, flags = struct.unpack_from('<4I', data, offset)
        if magic != UPD_MAGIC or length < 16 or length > len(data) - offset:
            raise UpdateError('The firmware UPD contains invalid or truncated records.')
        records.append((kind, offset, data[offset + 16:offset + length], flags))
        offset += length
    return records


def upd_rootfs(data):
    """The single root filesystem record of a UPD."""
    roots = [payload for kind, _, payload, _ in upd_records(data) if kind == 4]
    if len(roots) != 1:
        raise UpdateError('The CR100 firmware must contain its root filesystem and kernel.')
    return roots[0]


def firmware_build_id(rootfs):
    """Read only the canonical build literal in the package's actual MainUI.

    Older images and unsupported/malformed filesystems provide no independent
    evidence. They remain installable under the existing validation policy.
    """
    try:
        movie = _mainui_from_cramfs(rootfs)
        if movie[:3] != b'FWS' or len(movie) < 8 or struct.unpack_from('<I', movie, 4)[0] != len(movie):
            return None
        matches = re.findall(rb'\x00(cfw3:[0-9a-f]{64})\x00', movie)
        if len(matches) == 1 and movie.count(b'cfw3:') == 1:
            return matches[0].decode('ascii')
    except (OSError, ValueError, KeyError, TypeError, IndexError, struct.error, zlib.error, RecursionError):
        pass
    return None


def _invalid_json_number(value):
    raise ValueError('Nonfinite number in update receipt')


class UpdateError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def version_tuple(value):
    match = _VERSION.fullmatch(value or '') if isinstance(value, str) else None
    if not match:
        raise UpdateError('Use an internal firmware version such as 25.2-50130.')
    values = tuple(map(int, match.groups()))
    if not 0 < values[0] < 256 or values[1] > 255 or values[2] > 0xffffffff:
        raise UpdateError('Firmware version is outside the supported range.')
    return values


def force_prompt_supported(current_version):
    if current_version == '4.3':
        return True
    try:
        return version_tuple(current_version)[:2] == (24, 1)
    except UpdateError:
        return False


def _read_upload(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        with Path(source).open('rb') as stream:
            data = stream.read(MAX_FIRMWARE_BYTES + 1)
    elif hasattr(source, 'read'):
        data = source.read(MAX_FIRMWARE_BYTES + 1)
    else:
        raise UpdateError('Upload a CR100 firmware .upd file.')
    if not isinstance(data, bytes) or not 16 <= len(data) <= MAX_FIRMWARE_BYTES:
        raise UpdateError('Firmware must be a nonempty UPD no larger than 16 MiB.')
    return data


def validate_firmware(source, *, allow_unverified=False):
    """Read bounded bytes and validate UPD structure, model, version and signature.

    The legacy vendor format signs all records preceding the final signature
    record with RSA-1024/PKCS#1-v1.5/MD5. A SHA-256 receipt identifies the uploaded
    bytes; MD5 is used solely to verify that existing vendor signature.
    Unverified signatures can be returned as warning metadata for explicit review.
    """
    data = _read_upload(source)
    records = upd_records(data)
    signatures = [r for r in records if r[0] == 0xffffffff]
    signature_valid = False
    if len(signatures) == 1 and records[-1][0] == 0xffffffff and len(signatures[0][2]) == 128:
        signature = signatures[0]
        number = int.from_bytes(signature[2], 'big')
        digest = hashlib.md5(data[:signature[1]], usedforsecurity=False).digest()
        info = MD5_DIGEST_INFO + digest
        expected = b'\0\1' + b'\xff' * (128 - len(info) - 3) + b'\0' + info
        signature_valid = 0 < number < VENDOR_MODULUS and hmac.compare_digest(
            pow(number, VENDOR_EXPONENT, VENDOR_MODULUS).to_bytes(128, 'big'), expected)
    signature_warning = '' if signature_valid else (
        'The firmware vendor signature could not be verified. Only continue if you trust this image. '
        'Installing untrusted firmware could damage the controller. '
        'The controller may still reject it with its own signature check.')
    if not signature_valid and not allow_unverified:
        raise UpdateError(signature_warning)
    versions = [r for r in records if r[0] == 1]
    if len(versions) != 1 or len(versions[0][2]) != 12:
        raise UpdateError('Firmware needs one unambiguous version record.')
    major, minor, build = struct.unpack('<3I', versions[0][2])
    version = '%d.%d-%05d' % (major, minor, build)
    version_tuple(version)
    compatible = []
    for kind, _, payload, _ in records:
        if kind != 2:
            continue
        if len(payload) != 28:
            raise UpdateError('Invalid firmware compatibility record.')
        manufacturer, model, sub_min, sub_max, *_ = struct.unpack('<7I', payload)
        if manufacturer == 1:
            if model != 2:
                raise UpdateError('This firmware is for a player or another controller, not a CR100.')
            if sub_min > sub_max or sub_max > 0xffff:
                raise UpdateError('Invalid CR100 hardware compatibility range.')
            compatible.append((sub_min, sub_max))
    if len(compatible) != 1:
        raise UpdateError('Firmware must explicitly support Sonos model 2 (CR100).')
    if sum(r[0] == 4 for r in records) != 1 or not any(r[0] == 6 for r in records):
        raise UpdateError('The CR100 firmware must contain its root filesystem and kernel.')
    build_id = firmware_build_id(next(r[2] for r in records if r[0] == 4))
    return data, dict(version=version, model=2, manufacturer=1,
        submodel_min=compatible[0][0], submodel_max=compatible[0][1],
        size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        signature_valid=signature_valid, signature_warning=signature_warning,
        cfw_build_id=build_id,
        signature='Sonos RSA-1024 PKCS#1 v1.5 MD5')


def _atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class UpdateManager:
    def __init__(self, config_path=None, *, state_path=None, storage=None,
                 clock=time.time, ttl=DEFAULT_TTL):
        base = Path(config_path).resolve().parent if config_path else Path.cwd()
        self.storage = Path(storage or base / 'controller-updates').resolve()
        self.state_path = Path(state_path or self.storage / 'state.json').resolve()
        self.path = self.state_path
        self.clock = clock
        if not math.isfinite(float(ttl)) or not 1 <= float(ttl) <= STAGED_TTL:
            raise UpdateError('Update expiry must be between one second and seven days.')
        self.ttl = float(ttl)

    @contextmanager
    def _state(self):
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.state_path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if self.state_path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError('Oversized update state')
                state = json.loads(self.state_path.read_text(), parse_constant=_invalid_json_number)
                if state.get('schema') != 1 or not isinstance(state.get('controllers'), dict):
                    raise ValueError('Invalid update state')
            except FileNotFoundError:
                state = dict(schema=1, controllers={})
            except (ValueError, TypeError, AttributeError):
                # Preserve the damaged receipt for diagnosis; clear temporary
                # compatibility overrides so a broken state file cannot lock out
                # controllers indefinitely or resurrect an unauthenticated file.
                damaged = self.state_path.with_name(self.state_path.name + '.corrupt-' + secrets.token_hex(6))
                os.replace(self.state_path, damaged)
                state = dict(schema=1, controllers={})
            before = json.dumps(state, sort_keys=True)
            self._expire(state)
            try:
                yield state
            finally:
                if json.dumps(state, sort_keys=True) != before or not self.state_path.exists():
                    _atomic_bytes(self.state_path, (json.dumps(state, sort_keys=True, indent=1) + '\n').encode())

    def _expire(self, state):
        now = self.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise UpdateError('The bridge clock is unavailable.', 503)
        for mac, record in list(state['controllers'].items()):
            if not isinstance(record, dict):
                del state['controllers'][mac]
                continue
            expiry = record.get('expires_at', 0)
            if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                record.update(status='failed', force_prompt=False, updated_at=now,
                              expires_at=now, prompt_acknowledged=True,
                              last_error='The update receipt is invalid. Upload the firmware again.')
                continue
            if record.get('status') in _ACTIVE and expiry <= now:
                record.update(status='expired', force_prompt=False, updated_at=now,
                              last_error='The pending update expired. Stage or arm it again to retry.')
            if record.get('status') == 'armed' and record.get('force_prompt'):
                if not force_prompt_supported(record.get('current_version')):
                    record.update(force_prompt=False, prompt_acknowledged=True,
                                  prompt_release_reason=FORCE_UNAVAILABLE)
                    continue
                prompt_expiry = record.get('prompt_expires_at', 0)
                if (not isinstance(prompt_expiry, (int, float))
                        or not math.isfinite(prompt_expiry) or prompt_expiry <= now):
                    record.update(prompt_acknowledged=True, prompt_release_reason='Prompt lease expired')

    @staticmethod
    def _public(record, mac=None):
        if not record:
            return dict(mac=mac, status='none', armed=False, force_prompt=False, force_active=False)
        result = {key: value for key, value in record.items()
                  if key not in ('token', 'stored_name', 'package_path')}
        result['armed'] = record.get('status') == 'armed'
        result['force_prompt'] = bool(record.get('force_prompt') and result['armed'])
        result['force_active'] = bool(result['force_prompt'] and not record.get('prompt_acknowledged'))
        return result

    def status(self, mac=None):
        mac = normalize_mac(mac) if mac is not None else None
        with self._state() as state:
            if mac is not None:
                return self._public(state['controllers'].get(mac), mac)
            return {key: self._public(value) for key, value in state['controllers'].items()}

    def bridge_status(self):
        """Read the transport's atomic job receipt without exposing bearer data."""
        result = dict(mode='stub', stub=True, automatic_installation=False,
            status='unavailable', available=False, controllers={},
            message='Online Updates requires an armed controller package; bridge installation is not implemented.')
        path = self.storage / 'bridge.json'
        try:
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                return result
            state = json.loads(path.read_text(), parse_constant=_invalid_json_number)
            if not isinstance(state, dict) or not isinstance(state.get('controllers'), dict):
                return result
            fields = frozenset(('mac', 'kind', 'stub', 'status', 'phase', 'percent',
                'version', 'target_version', 'current_version', 'created_at',
                'expires_at', 'completed_at', 'restart_at', 'error'))
            active, latest, latest_at = None, None, -1
            for mac, offer in state['controllers'].items():
                try:
                    mac = normalize_mac(mac)
                except ValueError:
                    continue
                if not isinstance(offer, dict):
                    continue
                public = {key: (value[:512] if isinstance(value, str) else value)
                          for key, value in offer.items() if key in fields
                          and (value is None or isinstance(value, (str, bool, int, float)))}
                public['mac'] = mac
                result['controllers'][mac] = public
                if state.get('active') and offer.get('token') == state['active']:
                    active = public
                changed = max((value for value in (offer.get('completed_at'), offer.get('created_at'))
                               if isinstance(value, (int, float))), default=0)
                if changed >= latest_at:
                    latest, latest_at = public, changed
            selected = active or latest
            result.update(available=True, status='idle')
            for key in ('installed_version', 'updated_at'):
                value = state.get(key)
                if value is None or isinstance(value, (str, int, float)):
                    result[key] = value
            if selected:
                for key in ('status', 'phase', 'percent', 'mac', 'version', 'target_version', 'current_version'):
                    if key in selected:
                        result[key] = selected[key]
                if selected.get('error'):
                    result['last_error'] = selected['error']
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return result

    def stage_controller(self, mac, upload, *, filename=None, force_prompt=False,
                         current_version=None, arm=True):
        mac = normalize_mac(mac)
        data, metadata = validate_firmware(upload, allow_unverified=not arm)
        if type(force_prompt) is not bool:
            raise UpdateError('Force prompt must be true or false.')
        if current_version:
            current_version = self._version_or_none(current_version)
        force_unavailable = bool(arm and force_prompt and not force_prompt_supported(current_version))
        if force_unavailable:
            arm = False
        now = self.clock()
        name = Path(filename or getattr(upload, 'name', 'controller.upd')).name
        name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:160] or 'controller.upd'
        stored_name = metadata['sha256'] + '.upd'
        record = dict(metadata, mac=mac, filename=name, stored_name=stored_name,
            token=secrets.token_urlsafe(32), status='armed' if arm else 'staged',
            force_prompt=bool(force_prompt and arm), force_requested=bool(force_prompt),
            prompt_acknowledged=False,
            current_version=current_version, install_from_version=current_version,
            created_at=now, updated_at=now,
            expires_at=now + (self.ttl if arm else STAGED_TTL),
            offer_expires_at=now + self.ttl if arm else None,
            prompt_expires_at=now + min(PROMPT_TTL, self.ttl),
            last_error=FORCE_UNAVAILABLE if force_unavailable else '')
        with self._state() as state:
            if state['controllers'].get(mac, {}).get('status') == 'downloading':
                raise UpdateError('This controller update has already started. Cancel it before staging another image.', 409)
            _atomic_bytes(self.storage / stored_name, data)
            previous = state['controllers'].get(mac, {}).get('stored_name')
            state['controllers'][mac] = record
            if previous and previous != stored_name and not any(
                    r.get('stored_name') == previous for r in state['controllers'].values()):
                self._package_path(previous).unlink(missing_ok=True)
        if force_unavailable:
            raise UpdateError(FORCE_UNAVAILABLE)
        return self._public(record)

    @staticmethod
    def _version_or_none(value):
        try:
            return '%d.%d-%05d' % version_tuple(value)
        except UpdateError:
            # UI diagnostics sometimes supply display versions ("4.3.2").
            return {'4.3': '24.1-75101', '4.3.2': '25.2-50130'}.get(value)

    def arm_controller(self, mac, *, force_prompt=False, current_version=None, expected_sha256=None,
                       allow_unverified=False):
        mac = normalize_mac(mac)
        with self._state() as state:
            record = state['controllers'].get(mac)
            if not record:
                raise UpdateError('Upload controller firmware before starting an update.', 404)
            if record.get('status') == 'downloading':
                raise UpdateError('This controller update has already started.', 409)
            if expected_sha256 is not None and expected_sha256 != record.get('sha256'):
                record.update(status='staged', force_prompt=False, prompt_acknowledged=True,
                              updated_at=self.clock(), last_error='The staged image changed. Review it before starting the update.')
                raise UpdateError('The staged image changed. Review it before starting the update.', 409)
            if type(force_prompt) is not bool:
                record.update(status='staged', force_prompt=False, prompt_acknowledged=True,
                              updated_at=self.clock(), last_error='Force prompt must be true or false.')
                raise UpdateError('Force prompt must be true or false.')
            # Explicit unknown diagnostics must not reuse a previously cached
            # version to activate a compatibility override on another firmware.
            if force_prompt and not force_prompt_supported(current_version):
                record.update(status='staged', force_prompt=False, prompt_acknowledged=True,
                              updated_at=self.clock(), last_error=FORCE_UNAVAILABLE)
                raise UpdateError(FORCE_UNAVAILABLE)
            try:
                path = self._package_path(record.get('stored_name'))
                _, metadata = validate_firmware(path, allow_unverified=True)
            except (OSError, UpdateError):
                record.update(status='failed', force_prompt=False, updated_at=self.clock(),
                              prompt_acknowledged=True,
                              last_error='Stored firmware failed verification before arming.')
                raise UpdateError('Stored firmware failed verification. Upload the image again.') from None
            if metadata['sha256'] != record['sha256'] or metadata['version'] != record['version']:
                record.update(status='failed', force_prompt=False, prompt_acknowledged=True,
                              updated_at=self.clock(), last_error='Stored firmware changed before arming.')
                raise UpdateError('The stored firmware changed. Upload it again.')
            if type(allow_unverified) is not bool:
                raise UpdateError('Continue anyway must be true or false.')
            if not metadata['signature_valid'] and (not allow_unverified or expected_sha256 != metadata['sha256']):
                raise UpdateError('Review the signature warning and choose Continue anyway before installing.', 409)
            current = self._version_or_none(current_version) if current_version else record.get('current_version')
            now = self.clock()
            for field in ('download_started_at', 'download_from_version', 'completed_at',
                          'checked_at', 'prompt_release_reason'):
                record.pop(field, None)
            record.update(status='armed', allow_unverified=allow_unverified,
                cfw_build_id=metadata.get('cfw_build_id'),
                force_prompt=bool(force_prompt), force_requested=bool(force_prompt),
                token=secrets.token_urlsafe(32), current_version=current,
                install_from_version=current, expires_at=now + self.ttl,
                offer_expires_at=now + self.ttl,
                updated_at=now, last_error='', prompt_acknowledged=False,
                prompt_expires_at=now + min(PROMPT_TTL, self.ttl))
            result = self._public(record)
        return result

    arm = arm_controller

    def cancel_controller(self, mac, reason='cancelled'):
        mac = normalize_mac(mac)
        with self._state() as state:
            record = state['controllers'].get(mac)
            if record:
                record.update(status='cancelled', force_prompt=False, updated_at=self.clock(),
                              last_error='' if reason == 'cancelled' else str(reason)[:300])
            return self._public(record, mac)

    cancel = cancel_controller

    def _package_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{64}\.upd', name):
            raise UpdateError('Invalid stored firmware identity.')
        path = self.storage / name
        if path.is_symlink():
            raise UpdateError('Stored firmware must be a regular file.')
        return path

    def packages(self):
        """(mac, sha256, path) of every stored upload still on disk, newest first.

        Read-only: listing never changes a record, expires nothing that a read
        would not, and returns no token.
        """
        with self._state() as state:
            found = []
            for mac, record in state['controllers'].items():
                try:
                    path = self._package_path(record.get('stored_name'))
                except UpdateError:
                    continue
                if path.is_file():
                    found.append((record.get('updated_at') or 0, mac, record['stored_name'][:64], path))
        found.sort(key=lambda item: item[0] if isinstance(item[0], (int, float)) else 0, reverse=True)
        return [(mac, sha, path) for _, mac, sha, path in found]

    def selection(self, mac, current_version=None):
        mac = normalize_mac(mac)
        if current_version:
            self.report_controller(mac, current_version)
        with self._state() as state:
            record = state['controllers'].get(mac)
            if not record or record.get('status') not in ('armed', 'downloading'):
                return None
            return dict(record, kind='controller', package_path=str(self._package_path(record['stored_name'])))

    def compatibility_selection(self, mac, token=None):
        """Authorize pending virtual-player work after controller-first boot.

        This is not a controller package selection. Independently completed
        builds retain only the old player protocol lease, bounded by both the
        original offer and download expiry and invalidated by record replacement.
        """
        with self._state() as state:
            record = state['controllers'].get(normalize_mac(mac))
            if (not record or not isinstance(record.get('token'), str)
                    or (token is not None and (not isinstance(token, str)
                        or not hmac.compare_digest(record['token'], token)))):
                return None
            if record.get('status') in ('armed', 'downloading'):
                return dict(record, kind='controller')
            expected = record.get('cfw_build_id')
            limits = [record.get('expires_at')]
            limits.extend(record[name] for name in ('compatibility_expires_at', 'offer_expires_at')
                          if record.get(name) is not None)
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in limits):
                return None
            expiry = min(limits)
            build_proof = (record.get('completion_evidence') == 'installed-movie-build'
                and isinstance(expected, str) and CFW_BUILD_ID.fullmatch(expected)
                and record.get('installed_cfw_build_id') == expected)
            baselines = [record.get(name) for name in ('install_from_version', 'download_from_version')]
            version_proof = all(isinstance(value, str) and self._version_or_none(value)
                and value != record.get('version') for value in baselines)
            if (record.get('status') != 'completed'
                    or not (build_proof or version_proof)
                    or record.get('current_version') != record.get('version')
                    or type(expiry) not in (int, float) or not math.isfinite(expiry)
                    or self.clock() >= expiry):
                return None
            return dict(record, kind='controller-recovery')

    def compatibility_floor(self, mac, default='24.1-00000'):
        try:
            mac = normalize_mac(mac)
            with self._state() as state:
                record = state['controllers'].get(mac)
                if (not record or record.get('status') != 'armed' or not record.get('force_prompt')
                        or record.get('prompt_acknowledged')
                        or not force_prompt_supported(record.get('current_version'))):
                    return default
                current = self._version_or_none(record.get('current_version'))
                if current:
                    major, minor, build = version_tuple(current)
                    # Increasing the current build is sufficient for the controller
                    # comparison and avoids an implausible 99.x floor.
                    if build < 0xffffffff:
                        return '%d.%d-%05d' % (major, minor, build + 1)
                return '25.2-50131'
        except (OSError, ValueError, TypeError):
            # Discovery must remain compatible if storage becomes unavailable.
            return default

    floor = compatibility_floor

    def release_prompts(self, reason='Bridge restarted'):
        """Called by protocol startup, never by constructors or UI status reads."""
        released = 0
        with self._state() as state:
            for record in state['controllers'].values():
                if record.get('status') == 'armed' and record.get('force_prompt'):
                    record.update(prompt_acknowledged=True, updated_at=self.clock(),
                                  prompt_release_reason=str(reason)[:160])
                    released += 1
        return released

    def acknowledge_prompt(self, mac, token=None):
        """Release a one-shot prompt when checking starts, including later Cancel.

        Stock confirmation Cancel has no network callback. Retaining an override
        until package download would strand a user who cancels that screen.
        The staged selection remains armed for a later manual retry.
        """
        mac = normalize_mac(mac)
        with self._state() as state:
            record = state['controllers'].get(mac)
            if record and record.get('status') == 'armed':
                if token is not None:
                    self._authorized(state, token, mac)
                record.update(prompt_acknowledged=True, checked_at=self.clock(), updated_at=self.clock())
            return self._public(record, mac)

    def _authorized(self, state, token, mac):
        record = state['controllers'].get(normalize_mac(mac))
        if (not record or record.get('status') not in ('armed', 'downloading')
                or not isinstance(token, str) or not isinstance(record.get('token'), str)
                or not hmac.compare_digest(record['token'], token)):
            raise UpdateError('This update is not active for this controller.', 403)
        return record

    def authorize_download(self, token, mac):
        with self._state() as state:
            record = self._authorized(state, token, mac)
            # Verify at delivery too, so a changed file can never be served as
            # the authenticated staged image after a restart or disk mutation.
            try:
                path = self._package_path(record['stored_name'])
                _, metadata = validate_firmware(path, allow_unverified=record.get('allow_unverified') is True)
            except (OSError, UpdateError):
                record.update(status='failed', force_prompt=False, prompt_acknowledged=True, updated_at=self.clock(),
                              last_error='Stored firmware failed verification before delivery.')
                raise UpdateError('Stored firmware failed verification. Upload the image again.') from None
            if metadata['sha256'] != record['sha256']:
                record.update(status='failed', force_prompt=False, updated_at=self.clock(),
                              last_error='Stored firmware changed before delivery.')
                raise UpdateError('Stored firmware changed. Upload the image again.')
            return path

    def controller_started(self, token, mac):
        with self._state() as state:
            record = self._authorized(state, token, mac)
            now = self.clock()
            if record.get('status') != 'downloading':
                record['download_from_version'] = record.get('current_version')
            record.update(status='downloading', force_prompt=False, updated_at=now,
                          download_started_at=record.get('download_started_at', now),
                          expires_at=record.get('download_started_at', now) + DOWNLOAD_TTL)
            return self._public(record)

    def fail_controller(self, token, mac, error):
        with self._state() as state:
            record = self._authorized(state, token, mac)
            record.update(status='failed', force_prompt=False, updated_at=self.clock(), last_error=str(error)[:300])
            return self._public(record)

    def report_controller(self, mac, current_version):
        mac = normalize_mac(mac)
        current = self._version_or_none(current_version)
        with self._state() as state:
            record = state['controllers'].get(mac)
            if not record or not current:
                return self._public(record, mac)
            record['current_version'] = current
            if record.get('status') == 'armed' and record.get('force_prompt') and not force_prompt_supported(current):
                record.update(force_prompt=False, prompt_acknowledged=True, updated_at=self.clock(),
                              prompt_release_reason=FORCE_UNAVAILABLE)
            # The stock version is shared by custom builds. Preserve the
            # original pre-attempt version independently of ordinary reports;
            # a same-version report (or missing baseline) proves no install.
            baseline = record.get('install_from_version')
            download_baseline = record.get('download_from_version')
            if (record.get('status') == 'downloading' and current == record['version']
                    and baseline and baseline != record['version']
                    and download_baseline and download_baseline != record['version']):
                record.update(status='completed', force_prompt=False, updated_at=self.clock(),
                              completed_at=self.clock(), last_error='',
                              completion_evidence='observed-version-transition',
                              compatibility_expires_at=min(record['expires_at'],
                                  record.get('offer_expires_at') or record['expires_at']))
            return self._public(record)

    def report_build(self, mac, build_id, current_version=None):
        """Accept an installed movie's package-specific startup evidence.

        A well-formed report is acknowledged even with no pending installation,
        allowing the movie's once-per-start reporter to stop. Only a matching
        downloading record may transition to completed.
        """
        mac = normalize_mac(mac)
        if not isinstance(build_id, str) or not CFW_BUILD_ID.fullmatch(build_id):
            raise UpdateError('Invalid controller build evidence.')
        with self._state() as state:
            record = state['controllers'].get(mac)
            if not record or record.get('status') != 'downloading':
                return self._public(record, mac)
            current = self._version_or_none(current_version) if current_version else record.get('current_version')
            expected = record.get('cfw_build_id')
            if (not expected or not hmac.compare_digest(expected, build_id)
                    or current != record['version']):
                raise UpdateError('Controller build does not match the downloading firmware.', 409)
            now = self.clock()
            record.update(status='completed', force_prompt=False, prompt_acknowledged=True,
                          updated_at=now, completed_at=now, last_error='',
                          completion_evidence='installed-movie-build', installed_cfw_build_id=build_id,
                          compatibility_expires_at=min(record['expires_at'],
                              record.get('offer_expires_at') or record['expires_at']))
            return self._public(record)
