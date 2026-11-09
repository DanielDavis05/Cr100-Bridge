"""Keep the dedicated Nexmon radio awake independently of 802.11 station PS.

The brcmfmac firmware can leave minimum power consumption (MPC) enabled while
Linux power_save is off. Disabling MPC improved a measured controller handshake,
but searching later recurred with MPC still off; it is not a complete link fix.
Only the wlan0 MPC iovar is read here, and the only write sets it to zero.
"""
from __future__ import annotations

import errno
import fcntl
import importlib.util
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time


MPC_TIMEOUT = 4.0
MPC_LEASE = Path('/run/cr100-bridge/mpc-query.lock')
_RESPONSE = struct.Struct('<4sIIII')
_MAGIC = b'MPCO'


def _read_mpc():
    path = Path(__file__).resolve().parent.parent / 'firmware/nexmon-ownership/status.py'
    spec = importlib.util.spec_from_file_location('_nexmon_power_status', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = module._get_netlink('wlan0', 262, 32, b'mpc\0')
    return struct.unpack_from('<I', raw)[0]


def _set_mpc_off(socket_factory):
    # Official Nexmon NETLINK_USER ioctl transport. SET_VAR=263, set flag=1.
    payload = b'NEX\0' + struct.pack('<III', 0, 263, 1) + b'mpc\0' + struct.pack('<I', 0)
    size = (16 + len(payload) + 3) & ~3
    with socket_factory(16, socket.SOCK_RAW, 31) as control:
        control.settimeout(1)
        control.bind((0, 0))
        header = struct.pack('<IHHII', size, 0, 0, 1, control.getsockname()[0])
        control.sendto((header + payload).ljust(size, b'\0'), (0, 0))
        try:
            reply = control.recv(8192)
        except socket.timeout:
            # A lost ACK does not say whether a SET took effect. The mandatory
            # readback below decides; never enable injection on an assumption.
            return
    if len(reply) < 16:
        raise ValueError('Truncated Nexmon power-setting acknowledgement')
    length, kind, _, _, _ = struct.unpack_from('<IHHII', reply)
    if not 16 <= length <= len(reply):
        raise ValueError('Invalid Nexmon power-setting acknowledgement')
    if kind == 2:
        if length < 20:
            raise ValueError('Truncated Nexmon power-setting error')
        code = -struct.unpack_from('<i', reply, 16)[0]
        if code:
            raise OSError(code, 'Could not disable Nexmon firmware MPC')


def _disable_mpc_inline(*, getter=None, socket_factory=None):
    """Fixed GET/SET-zero/GET transaction, only inside a worker or unit harness."""
    getter = getter or _read_mpc
    previous = getter()
    if previous not in (0, 1):
        raise ValueError('Unexpected Nexmon firmware MPC value')
    if previous:
        _set_mpc_off(socket_factory or socket.socket)
        if getter() != 0:
            raise OSError(errno.EIO, 'Nexmon firmware MPC remained enabled')
    return previous


class _MpcQuery:
    """Bound startup IPC even if a driver callback pins its one child in the kernel.

    The child inherits the locked open-file description. Closing the parent's
    descriptor never unlocks a still-running child, including across supervisor
    restarts. A timed-out PID is retained and never replaced until poll reaps it.
    """
    def __init__(self, *, timeout=MPC_TIMEOUT, popen=None, lock_path=MPC_LEASE):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('MPC helper timeout must be finite and positive')
        self.timeout, self.lock_path = timeout, Path(lock_path)
        self._popen = popen or subprocess.Popen
        self._operation_lock, self._state_lock, self._pipe_lock = (threading.Lock() for _ in range(3))
        self._process = self._lease_fd = None
        self._retiring = self._closed = False

    def _lease(self):
        # The radio oneshot runs before the bridge unit creates RuntimeDirectory
        # on a fresh boot, so create the private lease directory if necessary.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = self.lock_path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) & 0o022):
            raise OSError(errno.EPERM, 'Unsafe MPC helper lease directory')
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                             | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()):
                raise OSError(errno.EPERM, 'Unsafe MPC helper lease file')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError(errno.EBUSY, 'Another MPC helper still owns the radio lease') from error
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _close_pipes(self, process):
        with self._pipe_lock:
            if process.stdout:
                try: process.stdout.close()
                except OSError: pass

    def _release_lease(self):
        with self._state_lock:
            descriptor, self._lease_fd = self._lease_fd, None
        if descriptor is not None:
            # No LOCK_UN or unlink: the inherited child descriptor owns the
            # same lease until the child really exits, even after SIGKILL.
            os.close(descriptor)

    def _retire(self):
        with self._state_lock:
            process = self._process
            self._retiring = True
        if process is not None:
            try: process.kill()
            except OSError: pass
            self._close_pipes(process)
            process.poll()  # WNOHANG only. Never wait() for a kernel-stuck PID.
        self._release_lease()

    def _spawn(self):
        with self._state_lock:
            if self._closed: raise OSError(errno.ECANCELED, 'MPC helper is closed')
            previous = self._process
        if previous is not None:
            if previous.poll() is None:
                raise OSError(errno.EBUSY, 'Previous MPC helper has not exited')
            self._close_pipes(previous)
            self._release_lease()
            with self._state_lock:
                self._process, self._retiring = None, False
        lease = self._lease()
        try:
            with self._state_lock:
                if self._closed: raise OSError(errno.ECANCELED, 'MPC helper is closed')
            process = self._popen([sys.executable, '-u', str(Path(__file__).resolve()), '--_mpc-worker'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  bufsize=0, close_fds=True, pass_fds=(lease,))
        except BaseException:
            os.close(lease)
            raise
        with self._state_lock:
            self._process, self._lease_fd = process, lease
            self._retiring = False
            closed = self._closed
        if closed:
            self._retire()
            raise OSError(errno.ECANCELED, 'MPC helper closed during startup')
        return process

    def _read_response(self, process, deadline):
        # Retain a duplicate through the read. Concurrent close() must not let
        # reuse of the original descriptor redirect the IPC to another file.
        with self._pipe_lock:
            os.set_blocking(process.stdout.fileno(), False)
            descriptor = os.dup(process.stdout.fileno())
        data = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise OSError(errno.ETIMEDOUT, 'MPC helper deadline expired')
                if not select.select([descriptor], [], [], remaining)[0]:
                    raise OSError(errno.ETIMEDOUT, 'MPC helper deadline expired')
                try: chunk = os.read(descriptor, _RESPONSE.size + 1 - len(data))
                except BlockingIOError: continue
                if not chunk: break
                data.extend(chunk)
                if len(data) > _RESPONSE.size: raise OSError(errno.EIO, 'Oversized MPC helper reply')
        finally:
            os.close(descriptor)
        if len(data) != _RESPONSE.size: raise OSError(errno.EIO, 'Incomplete MPC helper reply')
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise OSError(errno.ETIMEDOUT, 'MPC helper did not exit')
            time.sleep(min(.005, remaining))
        if time.monotonic() >= deadline:
            raise OSError(errno.ETIMEDOUT, 'MPC helper reply arrived after its deadline')
        magic, version, status, previous, current = _RESPONSE.unpack(data)
        if magic != _MAGIC or version != 1:
            raise OSError(errno.EIO, 'Malformed MPC helper reply')
        if process.returncode != 0 or status != 0 or previous not in (0, 1) or current != 0:
            code = status if 0 < status <= 4095 else errno.EIO
            raise OSError(code, 'MPC helper did not verify firmware power saving off')
        return previous

    def __call__(self):
        if not self._operation_lock.acquire(blocking=False):
            raise OSError(errno.EBUSY, 'MPC verification is already running')
        try:
            deadline = time.monotonic() + self.timeout
            process = self._spawn()
            previous = self._read_response(process, deadline)
            self._close_pipes(process)
            self._release_lease()
            with self._state_lock:
                if self._closed: raise OSError(errno.ECANCELED, 'MPC helper closed before verification completed')
                if time.monotonic() >= deadline: raise OSError(errno.ETIMEDOUT, 'MPC helper deadline expired')
                self._process = None
            return previous
        except BaseException:
            self._retire()
            raise
        finally:
            self._operation_lock.release()

    def close(self):
        with self._state_lock: self._closed = True
        self._retire()


_query = None
_query_lock = threading.Lock()


def disable_mpc(*, getter=None, socket_factory=None):
    """Verify MPC off in one fixed child, with a bounded parent IPC deadline.

    Explicit injected transports retain the inline unit-harness path. Production
    callers supply no arguments and never execute driver requests in the parent.
    """
    if getter is not None or socket_factory is not None:
        return _disable_mpc_inline(getter=getter, socket_factory=socket_factory)
    global _query
    with _query_lock:
        if _query is None: _query = _MpcQuery()
        query = _query
    return query()


def _mpc_worker(output=None):
    output = output or sys.stdout.buffer
    try:
        previous = _disable_mpc_inline()
        result, code = (_MAGIC, 1, 0, previous, 0), 0
    except Exception as error:
        failure = getattr(error, 'errno', None)
        failure = failure if type(failure) is int and 0 < failure <= 4095 else errno.EIO
        result, code = (_MAGIC, 1, failure, 0xffffffff, 0xffffffff), 1
    output.write(_RESPONSE.pack(*result))
    output.flush()
    return code


if __name__ == '__main__':
    raise SystemExit(_mpc_worker() if sys.argv[1:] == ['--_mpc-worker'] else 2)
