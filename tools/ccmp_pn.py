"""Durable, AP-wide CCMP packet-number reservations for one radio process.

Use one PacketNumbers context for the lifetime of the radio link. Initialize its
TX counter from ``initial`` and call ``ensure(next_pn)`` before encrypting each
frame. Numbers are consumed even when transmission fails; callers serialize
ensure() with their existing radio lock.

The first use of a key seeds from wall-clock milliseconds unless ``minimum`` is
provided. This preserves the previous link's counter convention during migration,
but cannot reconstruct numbers sent before this file existed if the clock has
already rolled back. Once initialized, the durable high-water mark survives
clock rollback and reserves unused numbers across process restarts.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

MAX_PN = (1 << 48) - 1
RESERVATION_SIZE = 1 << 20
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')


class PacketNumberError(RuntimeError):
    """The durable counter cannot safely authorize a packet number."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise PacketNumberError('Duplicate key in CCMP packet-number state')
        result[key] = value
    return result


class PacketNumbers:
    def __init__(self, path, bssid, key, *, minimum=None, block_size=RESERVATION_SIZE):
        if not isinstance(bssid, bytes) or len(bssid) != 6:
            raise ValueError('CCMP packet numbers require a six-byte BSSID')
        if not isinstance(key, bytes) or len(key) != 16:
            raise ValueError('CCMP packet numbers require a sixteen-byte key')
        if type(block_size) is not int or not 1 <= block_size <= MAX_PN:
            raise ValueError('Invalid CCMP packet-number reservation size')
        if minimum is None: minimum = max(0, int(time.time() * 1000))
        if type(minimum) is not int or not 0 <= minimum <= MAX_PN:
            raise ValueError('CCMP packet-number minimum is outside the 48-bit range')
        self.path = Path(path)
        self.fingerprint = hashlib.sha256(bssid + key).hexdigest()
        self.block_size = block_size
        self._lock_fd, self._failed = None, False
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
            self._lock_fd = os.open(str(self.path) + '.lock', flags, 0o600)
            if not stat.S_ISREG(os.fstat(self._lock_fd).st_mode):
                raise PacketNumberError('CCMP packet-number lock is not a regular file')
            # A separate lock inode remains stable when the state file is renamed.
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self._lock_fd, 0o600)
            self._entries = self._read()
            self.initial = max(minimum, self._entries.get(self.fingerprint, 0))
            if self.initial == MAX_PN: raise PacketNumberError('CCMP packet numbers are exhausted')
            self._last = self.initial
            self._reserved = self.initial
            self._reserve(min(MAX_PN, self.initial + block_size))
        except BaseException:
            self.close()
            raise

    def _read(self):
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise PacketNumberError('CCMP packet-number state is not a regular file')
            try:
                state = json.load(source, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError) as error:
                raise PacketNumberError('Corrupt CCMP packet-number state') from error
        if not isinstance(state, dict) or set(state) != {'version', 'high_water'} \
                or type(state['version']) is not int or state['version'] != 1 \
                or not isinstance(state['high_water'], dict):
            raise PacketNumberError('Invalid CCMP packet-number state format')
        for fingerprint, value in state['high_water'].items():
            if not _FINGERPRINT.fullmatch(fingerprint) or type(value) is not int or not 0 <= value <= MAX_PN:
                raise PacketNumberError('Invalid CCMP packet-number high-water mark')
        return state['high_water']

    def _reserve(self, high_water):
        entries = {**self._entries, self.fingerprint: high_water}
        data = (json.dumps({'version': 1, 'high_water': entries}, sort_keys=True) + '\n').encode('utf-8')
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '.', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(fd, 'wb') as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            # Even if replacement succeeded, failure to fsync the directory means
            # this process must never authorize the new range for encryption.
            self._failed = True
            raise
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        self._entries, self._reserved = entries, high_water

    def ensure(self, pn):
        """Authorize one increasing PN only after its reservation is durable.

        A crash abandons the remainder of the reserved block. Retrying a frame
        still requires a new number, including after a busy or failed send.
        """
        if self._lock_fd is None: raise PacketNumberError('CCMP packet-number reservation is closed')
        if self._failed: raise PacketNumberError('CCMP packet-number reservation failed; restart is required')
        if type(pn) is not int or not 1 <= pn <= MAX_PN:
            raise PacketNumberError('CCMP packet number is outside the 48-bit range')
        if pn <= self._last: raise PacketNumberError('CCMP packet numbers must strictly increase')
        if pn > self._reserved:
            blocks = (pn - self._reserved + self.block_size - 1) // self.block_size
            self._reserve(min(MAX_PN, self._reserved + blocks * self.block_size))
        self._last = pn

    def close(self):
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            os.close(fd)

    def __enter__(self): return self

    def __exit__(self, *_): self.close()
