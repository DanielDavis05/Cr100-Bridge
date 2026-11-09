#!/usr/bin/env python3
"""Read Nexmon firmware/version and ownership counters without changing the radio.

Transport follows official utilities/libnexio/libnexio.c at Nexmon commit
68720406f54ff16cad28fd3c51f2392aefad9fb7: native-pointer nex_ioctl inside Linux
ifreq, WLC_IOCTL_MAGIC, SIOCDEVPRIVATE. The public entry point isolates driver
calls in a child process and bounds parent waits; every request has set=false.
An inherited exclusive lease prevents replacement workers while a timed-out
child is still alive, even after its original caller exits.

Usage on the Pi: sudo python3 status.py [--interface wlan0] [--version-only]
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys

DEFAULT_LOCK_PATH = '/run/cr100-bridge/firmware-query.lock'
SIOCDEVPRIVATE = 0x89F0
WLC_IOCTL_MAGIC = 0x14E46C77
NEX_GET_VERSION_STRING = 413
NEX_OWNERSHIP_STATS = 430
NEX_NOACK_STATS = 432
NEX_HARDWARE_TSF = 433
NEX_BEACON_OFFSETS = 435
NEX_BEACON_STAMP = 436
NOACK_MAGIC = 0x414E5243
NOACK_SIZE = 40
NOACK_FIELDS = ('magic', 'version', 'size', 'capabilities', 'probe_seen', 'probe_tagged',
                'probe_rejected', 'probe_alloc_fail', 'other_frames', 'ioctl_reads')
TSF_MAGIC = 0x51465354
TSF_SIZE = 24
SHM_MAGIC = 0x514D4853
SHM_SIZE = 48
STAMP_MAGIC = 0x51545342
STAMP_SIZE = 80
STAMP_FIELDS = ('magic', 'version', 'size', 'capabilities', 'sequence', 'calls',
                'beacon_candidates', 'stamped', 'already_stamped', 'rejected_headers',
                'ioctl_reads', 'last_valid', 'last_low', 'last_high_before', 'last_high_after',
                'last_bss_index', 'last_prefix', 'last_length_before', 'last_length_after', 'reserved')
WLC_GET_VAR = 262
NETLINK_USER = 31
AF_NETLINK = getattr(socket, 'AF_NETLINK', 16)
STATS_MAGIC = 0x4F4E5243
STATS_VERSION = 1
STATS_FIELDS = ('magic', 'version', 'size', 'seen', 'cloned', 'source_freed',
                'alloc_fail', 'malformed', 'host_link_nonzero', 'forwarded',
                'ioctl_reads', 'clone_free_on_error')
STATS_SIZE = 4 * len(STATS_FIELDS)


class NexIoctl(ctypes.Structure):
    _fields_ = [('cmd', ctypes.c_uint32), ('buf', ctypes.c_void_p),
                ('len', ctypes.c_uint32), ('set', ctypes.c_bool),
                ('used', ctypes.c_uint32), ('needed', ctypes.c_uint32),
                ('driver', ctypes.c_uint32)]


class IfreqData(ctypes.Union):
    # Linux ifreq's largest union member is struct ifmap: 24 bytes on LP64,
    # 16 on ILP32. Keep its pointer alignment, not a packed wire structure.
    _fields_ = [('data', ctypes.c_void_p),
                ('storage', ctypes.c_ubyte * (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16))]


class Ifreq(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char * 16), ('value', IfreqData)]


class Reply(bytes):
    def __new__(cls, data, transport):
        result = super().__new__(cls, data)
        result.transport = transport
        return result


def _validate_get(command, size, initial):
    if (command, size, initial) == (WLC_GET_VAR, 32, b'mpc\0'):
        return  # Exact existing radio power-management read; no other iovars.
    sizes = {NEX_GET_VERSION_STRING: 256, NEX_OWNERSHIP_STATS: STATS_SIZE,
             NEX_NOACK_STATS: NOACK_SIZE, NEX_HARDWARE_TSF: TSF_SIZE,
             NEX_BEACON_OFFSETS: SHM_SIZE, NEX_BEACON_STAMP: STAMP_SIZE, WLC_GET_VAR: 1024}
    if command not in sizes or size != sizes[command]:
        raise ValueError('Only fixed read-only version, ownership, radio capability and clock queries are allowed.')
    if initial != (b'ver\0' if command == WLC_GET_VAR else b''):
        raise ValueError('Unexpected read-only query input.')


def _get_ioctl(interface, command, size, initial=b'', *, ioctl=None, socket_factory=socket.socket):
    _validate_get(command, size, initial)
    encoded = interface.encode('ascii')
    if not encoded or len(encoded) >= 16 or b'\0' in encoded:
        raise ValueError('Use an interface name of 1–15 ASCII characters.')
    if not 0 < size <= 1500 or len(initial) > size:
        raise ValueError('Invalid diagnostic buffer size.')
    if ioctl is None:
        if not sys.platform.startswith('linux'):
            raise OSError(errno.ENOSYS, 'Nexmon diagnostics require Linux.')
        libc = ctypes.CDLL(None, use_errno=True)
        ioctl = libc.ioctl
        ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        ioctl.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(size)
    if initial: ctypes.memmove(buffer, initial, len(initial))
    request = NexIoctl(command, ctypes.addressof(buffer), size, False, 0, 0, WLC_IOCTL_MAGIC)
    ifreq = Ifreq()
    ifreq.name = encoded
    ifreq.value.data = ctypes.addressof(request)
    with socket_factory(socket.AF_INET, socket.SOCK_DGRAM, 0) as control:
        result = ioctl(control.fileno(), SIOCDEVPRIVATE, ctypes.byref(ifreq))
    if result < 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, 'Nexmon GET %d failed: %s' % (command, errno.errorcode.get(code, 'driver error')))
    # Some driver versions leave used=0 even on success; firmware headers and
    # NUL termination validate the actual reply independently of that field.
    data = buffer.raw[:request.used] if 0 < request.used <= size else buffer.raw
    return Reply(data, 'ioctl')


def _get_netlink(interface, command, size, initial=b'', *, socket_factory=socket.socket):
    """Official Nexmon NETLINK_USER transport for drivers without private ioctl."""
    _validate_get(command, size, initial)
    if interface != 'wlan0':
        raise ValueError('The Nexmon netlink fallback addresses the global wlan0 interface only.')
    frame = b'NEX\0' + struct.pack('<III', 0, command, 0) + initial.ljust(size, b'\0')
    message_size = (16 + len(frame) + 3) & ~3
    with socket_factory(AF_NETLINK, socket.SOCK_RAW, NETLINK_USER) as control:
        control.settimeout(1.0)
        control.bind((0, 0))
        pid = control.getsockname()[0]
        header = struct.pack('<IHHII', message_size, 0, 0, 1, pid)
        sent = control.sendto((header + frame).ljust(message_size, b'\0'), (0, 0))
        if sent != message_size: raise OSError(errno.EIO, 'Incomplete Nexmon query send.')
        answer, sender = control.recvfrom(message_size + 1)
    if not sender or sender[0] != 0: raise ValueError('Nexmon reply did not come from the kernel.')
    if len(answer) < 16: raise ValueError('Truncated Nexmon netlink header.')
    length, kind, flags, _, _ = struct.unpack_from('<IHHII', answer)
    if length < 16 or length != len(answer): raise ValueError('Invalid Nexmon netlink message length.')
    payload = answer[16:length]
    if kind == 2:  # NLMSG_ERROR
        code = -struct.unpack_from('<i', payload)[0] if len(payload) >= 4 else errno.EIO
        raise OSError(code or errno.EIO, 'Nexmon netlink GET failed.')
    if kind != 3 or flags or length != message_size:
        raise ValueError('Invalid Nexmon netlink envelope.')
    if len(payload) != 16 + size or payload[:4] != b'NEX\0':
        raise ValueError('Invalid Nexmon netlink reply.')
    if struct.unpack_from('<III', payload, 4) != (0, command, 0):
        raise ValueError('Nexmon netlink reply does not match the GET request.')
    return Reply(payload[16:16 + size], 'netlink')


def _get(interface, command, size, initial=b'', **kwargs):
    try:
        return _get_ioctl(interface, command, size, initial, **kwargs)
    except OSError as error:
        if error.errno not in (errno.EOPNOTSUPP, errno.ENOTTY, errno.ENOSYS): raise
        if not sys.platform.startswith('linux'): raise
        return _get_netlink(interface, command, size, initial)


def parse_stats(data):
    if len(data) < STATS_SIZE:
        raise ValueError('Truncated ownership counter reply.')
    values = dict(zip(STATS_FIELDS, struct.unpack_from('<12I', data)))
    if values['magic'] != STATS_MAGIC:
        raise ValueError('Ownership counter signature does not match the candidate firmware.')
    if values['version'] != STATS_VERSION or values['size'] != STATS_SIZE:
        raise ValueError('Unsupported ownership counter ABI.')
    return values


def parse_noack(data):
    if len(data) != NOACK_SIZE:
        raise ValueError('Invalid radio capability reply size.')
    values = dict(zip(NOACK_FIELDS, struct.unpack('<10I', data)))
    if (values['magic'], values['version'], values['size'], values['capabilities']) != (NOACK_MAGIC, 1, NOACK_SIZE, 1):
        raise ValueError('Unsupported radio capability signature/version/size/flags.')
    return values


def parse_clock(data):
    if len(data) != TSF_SIZE:
        raise ValueError('Invalid hardware clock reply size.')
    magic, version, size, state, low, high = struct.unpack('<6I', data)
    if (magic, version, size) != (TSF_MAGIC, 1, TSF_SIZE) or state not in (0, 1):
        raise ValueError('Unsupported hardware clock signature/version/size/status.')
    if state == 0 and (low or high):
        raise ValueError('Unavailable hardware clock returned nonzero time.')
    return {'magic': magic, 'version': version, 'size': size, 'status': state,
            'ready': bool(state), 'tsf_us': low | high << 32}


def parse_beacon_offsets(data):
    """Fixed four-entry public clock-offset table; never accepts an address."""
    if len(data) != SHM_SIZE:
        raise ValueError('Invalid beacon clock-offset reply size.')
    magic, version, size, state = struct.unpack_from('<4I', data)
    if (magic, version, size) != (SHM_MAGIC, 1, SHM_SIZE) or state not in (0, 1):
        raise ValueError('Unsupported beacon clock-offset signature/version/size/status.')
    offsets = list(struct.unpack_from('<4Q', data, 16))
    if not state and any(offsets):
        raise ValueError('Unavailable beacon clock offsets contain nonzero data.')
    return {'magic': magic, 'version': version, 'size': size, 'status': state,
            'ready': bool(state), 'offsets_unsigned': offsets,
            'offsets_signed': [value if value < 1 << 63 else value - (1 << 64) for value in offsets]}


def parse_beacon_stamp(data):
    """Validate capability before traffic, and stamp metadata when a sample exists."""
    if len(data) != STAMP_SIZE:
        raise ValueError('Invalid beacon hardware-stamp reply size.')
    values = dict(zip(STAMP_FIELDS, struct.unpack('<20I', data)))
    if ((values['magic'], values['version'], values['size'], values['capabilities']) !=
            (STAMP_MAGIC, 1, STAMP_SIZE, 1) or values['sequence'] & 1 or values['reserved'] or
            values['last_valid'] not in (0, 1)):
        raise ValueError('Unsupported beacon hardware-stamp ABI/snapshot.')
    if values['last_valid'] and (
            values['last_low'] > 0xffff or values['last_low'] & 0x81 or
            values['last_high_before'] > 0xffff or values['last_high_after'] > 0xffff or
            values['last_high_after'] != (values['last_high_before'] | 0x80) or
            values['last_bss_index'] > 3 or values['last_prefix'] not in (0, 4, 16) or
            not 38 <= values['last_length_before'] <= 512 or
            values['last_length_after'] != values['last_length_before'] + 124 + values['last_prefix']):
        raise ValueError('Invalid beacon hardware-stamp metadata.')
    return values


def collect(interface='wlan0', version_only=False, get=_get, ownership_only=False, radio_only=False):
    result = {'interface': interface, 'ok': False}
    if radio_only:
        for name, command, size, parse in (('noack', NEX_NOACK_STATS, NOACK_SIZE, parse_noack),
                                           ('clock', NEX_HARDWARE_TSF, TSF_SIZE, parse_clock),
                                           ('beacon_offsets', NEX_BEACON_OFFSETS, SHM_SIZE, parse_beacon_offsets),
                                           ('beacon_stamp', NEX_BEACON_STAMP, STAMP_SIZE, parse_beacon_stamp)):
            try:
                raw = get(interface, command, size)
                result[name] = {**parse(raw), 'transport': getattr(raw, 'transport', 'ioctl')}
            except (OSError, ValueError) as error: result[name] = {'error': str(error)}
        result['ok'] = (all('error' not in result[name] for name in
                            ('noack', 'clock', 'beacon_offsets', 'beacon_stamp')) and
                        result['clock'].get('ready') is True and result['beacon_offsets'].get('ready') is True and
                        result['beacon_offsets'].get('offsets_unsigned') == [0, 0, 0, 0])
        return result
    if ownership_only:
        try:
            raw = get(interface, NEX_OWNERSHIP_STATS, STATS_SIZE)
            result['ownership'] = {**parse_stats(raw), 'transport': getattr(raw, 'transport', 'ioctl')}
            result['ok'] = True
        except (OSError, ValueError) as error: result['ownership'] = {'error': str(error)}
        return result
    try:
        try:
            raw = get(interface, NEX_GET_VERSION_STRING, 256)
            version = raw.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()
            if not version: raise ValueError('The firmware returned an empty version.')
            result['firmware'] = {'version': version, 'command': NEX_GET_VERSION_STRING,
                                  'transport': getattr(raw, 'transport', 'ioctl')}
        except (OSError, ValueError) as unsupported:
            # 413 is defined by Nexmon but absent from its stock 7_45_206 hook.
            raw = get(interface, WLC_GET_VAR, 1024, b'ver\0')
            version = raw.split(b'\0', 1)[0].decode('utf-8', 'replace').strip()
            if not version or version == 'ver': raise ValueError('The firmware returned an empty version.')
            result['firmware'] = {'version': version, 'command': WLC_GET_VAR,
                                  'transport': getattr(raw, 'transport', 'ioctl'),
                                  'version_413_error': str(unsupported)}
    except (OSError, ValueError) as error:
        result['firmware'] = {'error': str(error)}
    if not version_only:
        try:
            raw = get(interface, NEX_OWNERSHIP_STATS, STATS_SIZE)
            result['ownership'] = {**parse_stats(raw), 'transport': getattr(raw, 'transport', 'ioctl')}
        except (OSError, ValueError) as error: result['ownership'] = {'error': str(error)}
    result['ok'] = 'error' not in result['firmware'] if version_only else 'error' not in result['ownership']
    return result


def _acquire_query_lease(path):
    """Hold one open-file-description flock through the real child lifetime.

    The radio oneshot can run before systemd creates /run/cr100-bridge. Create
    just that missing parent, then validate/open it without following its final
    symlink. openat keeps file creation anchored to that checked directory.
    No caller unlinks the file or explicitly unlocks the shared description.
    """
    path = Path(path)
    try:
        os.mkdir(path.parent, 0o755)
    except FileExistsError:
        pass
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    descriptor = None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise ValueError('Unsafe firmware-query lease directory.')
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                             | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0), 0o600,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()):
            raise ValueError('Unsafe firmware-query lease file.')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another firmware diagnostic worker has not exited.') from error
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def read_status(interface='wlan0', timeout=3.0, version_only=False, *, ownership_only=False,
                radio_only=False, popen=subprocess.Popen, lock_path=DEFAULT_LOCK_PATH):
    """Bound parent waits; retain an inherited lease for any kernel-stuck child."""
    if not math.isfinite(timeout) or timeout <= 0: raise ValueError('The diagnostic timeout must be finite and positive.')
    command = [sys.executable, str(Path(__file__).resolve()), '--_worker', '--interface', interface]
    if version_only: command.append('--version-only')
    if ownership_only: command.append('--ownership-only')
    if radio_only: command.append('--radio-only')
    try:
        lease = _acquire_query_lease(lock_path)
    except (OSError, ValueError) as error:
        return {'interface': interface, 'ok': False, 'error': 'Firmware diagnostic lease unavailable: ' + str(error)}
    try:
        try:
            process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            close_fds=True, pass_fds=(lease,))
        except OSError as error:
            return {'interface': interface, 'ok': False, 'error': 'Nexmon diagnostic worker launch failed: ' + str(error)}
        try:
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: process.kill()
            except ProcessLookupError: pass
            try: process.communicate(timeout=.2)
            except subprocess.TimeoutExpired:
                # An uninterruptible kernel ioctl can outlive SIGKILL. Closing
                # these pipes and our lease copy does not release the child's
                # inherited flock, so later callers cannot accumulate workers.
                for stream in (process.stdout, process.stderr):
                    if stream: stream.close()
            return {'interface': interface, 'ok': False, 'error': 'Nexmon diagnostic timed out after %.1f seconds.' % timeout}
        if process.returncode:
            return {'interface': interface, 'ok': False, 'error': errors.strip() or 'Nexmon diagnostic worker failed.'}
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool: raise ValueError
            return result
        except ValueError:
            return {'interface': interface, 'ok': False, 'error': 'Invalid diagnostic worker response.'}
    finally:
        # Never LOCK_UN: child and parent share the same flock description.
        # SIGKILL may remain pending until a firmware/bus callback returns.
        os.close(lease)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--interface', default='wlan0')
    parser.add_argument('--timeout', type=float, default=3.0)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--version-only', action='store_true')
    selection.add_argument('--ownership-only', action='store_true')
    selection.add_argument('--radio-only', action='store_true', help='Read probe NoAck capability and coherent hardware clock only')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0: parser.error('--timeout must be finite and positive')
    if args._worker:
        print(json.dumps(collect(args.interface, args.version_only, ownership_only=args.ownership_only, radio_only=args.radio_only), sort_keys=True))
        return 0
    result = read_status(args.interface, args.timeout, args.version_only, ownership_only=args.ownership_only, radio_only=args.radio_only)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__': raise SystemExit(main())
