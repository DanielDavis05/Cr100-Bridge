"""Diagnostic ABI, GET-only requests, reply validation, and driver deadlines."""
import ctypes
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('ownership_status', Path(__file__).with_name('status.py'))
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)


def counters(**changes):
    values = dict.fromkeys(status.STATS_FIELDS, 0)
    values.update(magic=status.STATS_MAGIC, version=1, size=status.STATS_SIZE, seen=10, cloned=9,
                  source_freed=10, alloc_fail=1, ioctl_reads=3)
    values.update(changes)
    return struct.pack('<12I', *(values[field] for field in status.STATS_FIELDS))


def beacon_offsets(state=1, offsets=(0, 0, 0, 0)):
    return struct.pack('<4I4Q', status.SHM_MAGIC, 1, status.SHM_SIZE, state, *offsets)


def beacon_stamp(**changes):
    values = dict.fromkeys(status.STAMP_FIELDS, 0)
    values.update(magic=status.STAMP_MAGIC, version=1, size=status.STAMP_SIZE, capabilities=1)
    values.update(changes)
    return struct.pack('<20I', *(values[field] for field in status.STAMP_FIELDS))


class Diagnostic(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.lock_path = Path(folder.name) / 'runtime' / 'firmware-query.lock'

    def read_status(self, *args, **kwargs):
        kwargs.setdefault('lock_path', self.lock_path)
        return status.read_status(*args, **kwargs)

    def test_native_pointer_layout_and_get_request_match_official_libnexio(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            expected = {'cmd': 0, 'buf': 8, 'len': 16, 'set': 20, 'used': 24, 'needed': 28, 'driver': 32}
            self.assertEqual(ctypes.sizeof(status.NexIoctl), 40)
            self.assertEqual(ctypes.sizeof(status.Ifreq), 40)
        else:
            expected = {'cmd': 0, 'buf': 4, 'len': 8, 'set': 12, 'used': 16, 'needed': 20, 'driver': 24}
            self.assertEqual(ctypes.sizeof(status.NexIoctl), 28)
            self.assertEqual(ctypes.sizeof(status.Ifreq), 32)
        for field, offset in expected.items(): self.assertEqual(getattr(status.NexIoctl, field).offset, offset)
        self.assertEqual(status.Ifreq.value.offset, 16)
        control = Mock()
        control.__enter__ = Mock(return_value=control)
        control.__exit__ = Mock(return_value=False)
        control.fileno.return_value = 42
        def ioctl(fd, command, argument):
            self.assertEqual((fd, command), (42, 0x89F0))
            ifreq = ctypes.cast(argument, ctypes.POINTER(status.Ifreq)).contents
            self.assertEqual(ifreq.name, b'wlan0')
            request = ctypes.cast(ifreq.value.data, ctypes.POINTER(status.NexIoctl)).contents
            self.assertEqual((request.cmd, request.len, request.set, request.driver), (430, 48, False, 0x14E46C77))
            ctypes.memmove(request.buf, counters(), 48)
            request.used = 48
            return 0
        result = status._get('wlan0', 430, 48, ioctl=ioctl, socket_factory=Mock(return_value=control))
        self.assertEqual(status.parse_stats(result)['seen'], 10)

    def test_query_rejects_arbitrary_command_or_iovar_before_ioctl(self):
        call = Mock()
        for command, value in ((408, b''), (262, b'country\0')):
            with self.assertRaises(ValueError): status._get('wlan0', command, 48, value, ioctl=call)
        call.assert_not_called()

    def test_stats_abi_rejects_truncated_wrong_firmware_and_unknown_versions(self):
        for raw in (counters()[:-1], counters(magic=0), counters(version=2), counters(size=44)):
            with self.assertRaises(ValueError): status.parse_stats(raw)

    def test_netlink_request_uses_official_magic_and_get_only_wire_format(self):
        control = Mock()
        control.__enter__ = Mock(return_value=control)
        control.__exit__ = Mock(return_value=False)
        control.getsockname.return_value = (1234, 0)
        payload = b'NEX\0' + struct.pack('<III', 0, 430, 0) + counters()
        control.sendto.side_effect = lambda packet, destination: len(packet)
        control.recvfrom.return_value = (struct.pack('<IHHII', 16 + len(payload), 3, 0, 0, 0) + payload, (0, 0))
        raw = status._get_netlink('wlan0', 430, 48, socket_factory=Mock(return_value=control))
        self.assertEqual(raw.transport, 'netlink')
        self.assertEqual(status.parse_stats(raw)['seen'], 10)
        sent, destination = control.sendto.call_args.args
        self.assertEqual(destination, (0, 0))
        self.assertEqual(sent[16:32], b'NEX\0' + struct.pack('<III', 0, 430, 0))
        self.assertEqual(struct.unpack_from('<IHHII', sent)[-1], 1234)
        control.settimeout.assert_called_once_with(1.0)

    def test_valid_candidate_counters_do_not_require_optional_version_support(self):
        def get(interface, command, size, initial=b''):
            if command == 430: return counters()
            raise OSError(errno.EOPNOTSUPP, 'version query unsupported')
        result = status.collect(get=get)
        self.assertTrue(result['ok'])
        self.assertIn('error', result['firmware'])
        self.assertEqual(result['ownership']['version'], 1)

    def test_stock_firmware_version_fallback_preserves_missing_patch_error(self):
        def get(interface, command, size, initial=b''):
            if command == 413: raise OSError(errno.EOPNOTSUPP, 'unsupported')
            if command == 262:
                self.assertEqual(initial, b'ver\0')
                return b'wl0: 7.45.206 (nexmon.org: base)\0'
            if command == 430: raise OSError(errno.EOPNOTSUPP, 'not patched')
            self.fail('Unexpected driver command')
        result = status.collect(get=get)
        self.assertFalse(result['ok'])
        self.assertEqual(result['firmware']['command'], 262)
        self.assertIn('7.45.206', result['firmware']['version'])
        self.assertIn('not patched', result['ownership']['error'])

    def test_candidate_counters_and_version_only_do_not_issue_writes(self):
        get = Mock(side_effect=lambda interface, command, size: b'7.45.206\0' if command == 413 else counters())
        result = status.collect(get=get)
        self.assertTrue(result['ok'])
        self.assertEqual(result['ownership']['source_freed'], 10)
        self.assertEqual([call.args[1] for call in get.call_args_list], [413, 430])
        get.reset_mock()
        self.assertTrue(status.collect(version_only=True, get=get)['ok'])
        self.assertEqual([call.args[1] for call in get.call_args_list], [413])
        get.reset_mock()
        self.assertTrue(status.collect(ownership_only=True, get=get)['ok'])
        self.assertEqual([call.args[1] for call in get.call_args_list], [430])

    def test_driver_deadline_kills_worker_and_reaping_is_also_bounded(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired('worker', 1)
        result = self.read_status(timeout=1, popen=Mock(return_value=process))
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['error'])
        process.kill.assert_called_once()
        self.assertEqual([call.kwargs['timeout'] for call in process.communicate.call_args_list], [1, .2])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()


    def test_radio_only_requests_fixed_gets_and_preserves_default(self):
        noack = struct.pack('<10I', status.NOACK_MAGIC, 1, 40, 1, 20, 19, 0, 1, 7, 3)
        clock = struct.pack('<6I', status.TSF_MAGIC, 1, 24, 1, 0xabcdef01, 3)
        get = Mock(side_effect=lambda interface, command, size: {
            432: noack, 433: clock, 435: beacon_offsets(), 436: beacon_stamp()}[command])
        result = status.collect(radio_only=True, get=get)
        self.assertTrue(result['ok'])
        self.assertEqual([(call.args[1], call.args[2]) for call in get.call_args_list],
                         [(432, 40), (433, 24), (435, 48), (436, 80)])
        self.assertEqual(result['clock']['tsf_us'], (3 << 32) | 0xabcdef01)
        self.assertEqual(result['noack']['capabilities'], 1)
        self.assertEqual(result['beacon_offsets']['offsets_unsigned'], [0, 0, 0, 0])
        self.assertEqual(result['beacon_stamp']['last_valid'], 0)
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(result), '')
        popen = Mock(return_value=process)
        self.assertEqual(self.read_status(radio_only=True, popen=popen), result)
        self.assertIn('--radio-only', popen.call_args.args[0])

    def test_radio_abi_strict_size_magic_version_flags_and_down_clock(self):
        noack = [status.NOACK_MAGIC, 1, 40, 1, 0, 0, 0, 0, 0, 0]
        clock = [status.TSF_MAGIC, 1, 24, 1, 0, 0]
        for words, parse, positions in ((noack, status.parse_noack, (0, 1, 2, 3)), (clock, status.parse_clock, (0, 1, 2, 3))):
            fmt = '<%dI' % len(words)
            for index in positions:
                bad = words.copy(); bad[index] = 9
                with self.assertRaises(ValueError): parse(struct.pack(fmt, *bad))
            for raw in (struct.pack(fmt, *words)[:-1], struct.pack(fmt, *words) + b'\0'):
                with self.assertRaises(ValueError): parse(raw)
        self.assertFalse(status.parse_clock(struct.pack('<6I', status.TSF_MAGIC, 1, 24, 0, 0, 0))['ready'])
        with self.assertRaises(ValueError): status.parse_clock(struct.pack('<6I', status.TSF_MAGIC, 1, 24, 0, 1, 0))
        get = Mock(side_effect=lambda interface, command, size: struct.pack('<10I', *noack) if command == 432 else struct.pack('<6I', status.TSF_MAGIC, 1, 24, 0, 0, 0))
        self.assertFalse(status.collect(radio_only=True, get=get)['ok'])
        for command, size in ((431, 24), (434, 240), (432, 48), (433, 40), (435, 16), (436, 84)):
            with self.assertRaises(ValueError): status._get_netlink('wlan0', command, size)
        for timeout in (float('nan'), float('inf'), 0):
            with self.assertRaises(ValueError): self.read_status(timeout=timeout)

    def test_fixed_offset_and_stamp_queries_have_no_address_or_payload_permission(self):
        for command, size in ((435, 48), (436, 80)):
            status._validate_get(command, size, b'')
            for initial in (b'\0' * 4, b'address', struct.pack('<I', 0xeee)):
                with self.assertRaises(ValueError): status._validate_get(command, size, initial)

    def test_beacon_offsets_abi_and_zero_readiness(self):
        parsed = status.parse_beacon_offsets(beacon_offsets(offsets=(0, 1 << 63, 0, 0)))
        self.assertEqual(parsed['offsets_signed'], [0, -(1 << 63), 0, 0])
        self.assertFalse(status.parse_beacon_offsets(beacon_offsets(state=0))['ready'])
        with self.assertRaises(ValueError):
            status.parse_beacon_offsets(beacon_offsets(state=0, offsets=(1, 0, 0, 0)))
        for index in range(4):
            words = list(struct.unpack('<12I', beacon_offsets()))
            words[index] = 9
            with self.assertRaises(ValueError): status.parse_beacon_offsets(struct.pack('<12I', *words))
        for raw in (beacon_offsets()[:-1], beacon_offsets() + b'\0'):
            with self.assertRaises(ValueError): status.parse_beacon_offsets(raw)
        base = {432: struct.pack('<10I', status.NOACK_MAGIC, 1, 40, 1, 0, 0, 0, 0, 0, 0),
                433: struct.pack('<6I', status.TSF_MAGIC, 1, 24, 1, 5, 0), 436: beacon_stamp()}
        for reply in (beacon_offsets(state=0), beacon_offsets(offsets=(0, 0, 1, 0))):
            get = lambda interface, command, size: reply if command == 435 else base[command]
            self.assertFalse(status.collect(radio_only=True, get=get)['ok'])

    def test_stamp_capability_is_valid_before_traffic_and_metadata_is_guarded_afterward(self):
        self.assertEqual(status.parse_beacon_stamp(beacon_stamp())['last_valid'], 0)
        # Historical counters and an invalid/rejected last snapshot do not make
        # the firmware capability disappear or permanently block future startup.
        status.parse_beacon_stamp(beacon_stamp(rejected_headers=1, last_prefix=7, last_length_before=10))
        live = {'last_valid': 1, 'last_low': 0x4a00, 'last_high_before': 2, 'last_high_after': 0x82,
                'last_bss_index': 0, 'last_prefix': 4, 'last_length_before': 104, 'last_length_after': 232}
        for prefix in (0, 4, 16):
            values = {**live, 'last_prefix': prefix, 'last_length_after': 104 + 124 + prefix}
            self.assertEqual(status.parse_beacon_stamp(beacon_stamp(**values))['last_prefix'], prefix)
        for field, bad in (('magic', 0), ('version', 2), ('size', 76), ('capabilities', 0),
                           ('sequence', 1), ('reserved', 1), ('last_valid', 2),
                           ('last_low', 0x4a01), ('last_low', 0x4a80), ('last_low', 0x10000),
                           ('last_high_before', 0x10002), ('last_high_after', 2),
                           ('last_high_after', 0x10082), ('last_bss_index', 4), ('last_prefix', 8),
                           ('last_length_before', 37), ('last_length_before', 513), ('last_length_after', 231)):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                status.parse_beacon_stamp(beacon_stamp(**{**live, field: bad}))
        for raw in (beacon_stamp()[:-1], beacon_stamp() + b'\0'):
            with self.assertRaises(ValueError): status.parse_beacon_stamp(raw)

    def test_radio_preflight_rejects_old_clock_only_firmware(self):
        def get(interface, command, size):
            if command == 432: return struct.pack('<10I', status.NOACK_MAGIC, 1, 40, 1, 0, 0, 0, 0, 0, 0)
            if command == 433: return struct.pack('<6I', status.TSF_MAGIC, 1, 24, 1, 5, 0)
            raise OSError(errno.EOPNOTSUPP, 'Missing hardware beacon stamp support')
        result = status.collect(radio_only=True, get=get)
        self.assertFalse(result['ok'])
        self.assertIn('error', result['beacon_offsets'])
        self.assertIn('error', result['beacon_stamp'])

    def test_radio_netlink_requires_kernel_done_zero_sequence_and_clean_envelope(self):
        payload = b'NEX\0' + struct.pack('<III', 0, 433, 0) + struct.pack('<6I', status.TSF_MAGIC, 1, 24, 1, 5, 0)
        valid = struct.pack('<IHHII', 56, 3, 0, 0, 0) + payload
        variants = [(valid, (42, 0)), (valid + b'\0', (0, 0)),
                    (struct.pack('<IHHII', 56, 0, 0, 0, 0) + payload, (0, 0)),
                    (struct.pack('<IHHII', 56, 3, 1, 0, 0) + payload, (0, 0))]
        for answer, sender in variants:
            control = Mock(); control.__enter__ = Mock(return_value=control); control.__exit__ = Mock(return_value=False)
            control.getsockname.return_value = (123, 0); control.sendto.side_effect = lambda packet, destination: len(packet)
            control.recvfrom.return_value = answer, sender
            with self.assertRaises(ValueError): status._get_netlink('wlan0', 433, 24, socket_factory=Mock(return_value=control))

    def test_bundled_power_mpc_read_uses_exact_allowed_get_and_kernel_reply(self):
        path = Path(__file__).resolve().parents[2] / 'tools/nexmon_power.py'
        spec = importlib.util.spec_from_file_location('bundled_power_radio_status_test', path)
        power = importlib.util.module_from_spec(spec); spec.loader.exec_module(power)
        control = Mock(); control.__enter__ = Mock(return_value=control); control.__exit__ = Mock(return_value=False)
        control.getsockname.return_value = (1234, 0)
        control.sendto.side_effect = lambda packet, destination: len(packet)
        body = b'NEX\0' + struct.pack('<III', 0, 262, 0) + struct.pack('<I', 0) + bytes(28)
        control.recvfrom.return_value = (struct.pack('<IHHII', 64, 3, 0, 0, 0) + body, (0, 0))
        with patch('socket.socket', return_value=control):
            self.assertEqual(power._read_mpc(), 0)
        request, destination = control.sendto.call_args.args
        self.assertEqual(request[16:32], b'NEX\0' + struct.pack('<III', 0, 262, 0))
        self.assertEqual(request[32:], b'mpc\0' + bytes(28))
        self.assertEqual(destination, (0, 0))
        for size, initial in ((48, b'mpc\0'), (32, b'country\0'), (32, b'mpc\0extra')):
            with self.assertRaises(ValueError): status._validate_get(262, size, initial)

    def test_secure_first_runtime_directory_and_launch_failure_release_lease(self):
        self.assertFalse(self.lock_path.parent.exists())
        def launch_failure(command, **kwargs):
            descriptor, = kwargs['pass_fds']
            self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            self.assertTrue(kwargs['close_fds'])
            with self.assertRaisesRegex(ValueError, 'has not exited'):
                status._acquire_query_lease(self.lock_path)
            raise OSError(errno.ENOENT, 'missing helper')
        failed = Mock(side_effect=launch_failure)
        result = self.read_status(popen=failed)
        self.assertFalse(result['ok'])
        self.assertIn('launch failed', result['error'])
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.lock_path.parent.stat().st_mode & 0o022, 0)
        lease = status._acquire_query_lease(self.lock_path)
        os.close(lease)
        self.assertTrue(self.lock_path.exists())  # Persistent inode, never unlink.

    def test_unsafe_lease_paths_and_busy_lock_fail_before_launch(self):
        self.lock_path.parent.mkdir(mode=0o700)
        base = self.lock_path.parent
        target = base / 'target'; target.write_bytes(b'unchanged')
        target.chmod(0o600)
        symlink = base / 'symlink'; symlink.symlink_to(target)
        directory = base / 'directory'; directory.mkdir()
        fifo = base / 'fifo'; os.mkfifo(fifo)
        insecure = base / 'insecure'; insecure.write_bytes(b''); insecure.chmod(0o644)
        link_parent = base / 'link-parent'; link_parent.symlink_to(base, target_is_directory=True)
        for path in (symlink, directory, fifo, insecure, link_parent / 'query.lock'):
            spawn = Mock()
            result = self.read_status(lock_path=path, popen=spawn)
            self.assertFalse(result['ok'])
            self.assertIn('lease unavailable', result['error'])
            spawn.assert_not_called()
        with patch.object(status.os, 'geteuid', return_value=os.geteuid() + 1):
            spawn = Mock()
            self.assertFalse(self.read_status(popen=spawn)['ok'])
            spawn.assert_not_called()
        base.chmod(0o777)
        try:
            spawn = Mock()
            self.assertFalse(self.read_status(popen=spawn)['ok'])
            spawn.assert_not_called()
        finally:
            base.chmod(0o700)
        lease = status._acquire_query_lease(self.lock_path)
        try:
            spawn = Mock()
            result = self.read_status(popen=spawn)
            self.assertFalse(result['ok'])
            self.assertIn('has not exited', result['error'])
            spawn.assert_not_called()
        finally:
            os.close(lease)
        self.assertEqual(target.read_bytes(), b'unchanged')

    def test_real_child_timeout_retains_lease_until_child_is_terminal(self):
        processes = []
        class PendingKill:
            def __init__(self, real):
                self.real = real
                self.stdout, self.stderr = real.stdout, real.stderr
            def communicate(self, **kwargs):
                return self.real.communicate(**kwargs)
            def kill(self):
                pass  # Model SIGKILL pending on an uninterruptible driver call.
        def spawn(command, **kwargs):
            self.assertIn('--radio-only', command)
            real = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            processes.append(real)
            return PendingKill(real)
        try:
            started = time.monotonic()
            result = self.read_status(timeout=.02, radio_only=True, popen=spawn)
            self.assertFalse(result['ok'])
            self.assertLess(time.monotonic() - started, .6)
            self.assertIsNone(processes[0].poll())
            for _ in range(3):
                rejected = Mock()
                blocked = self.read_status(timeout=.02, popen=rejected)
                self.assertFalse(blocked['ok'])
                self.assertIn('has not exited', blocked['error'])
                rejected.assert_not_called()
            check = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            self.assertNotEqual(subprocess.run([sys.executable, '-c', check, str(self.lock_path)],
                                              capture_output=True, timeout=1).returncode, 0)
            processes[0].kill(); processes[0].wait(timeout=1)
            lease = status._acquire_query_lease(self.lock_path); os.close(lease)
            self.assertEqual(len(processes), 1)
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=1)

    def test_inherited_lease_survives_original_parent_process_exit(self):
        launcher = r"""
import importlib.util,json,subprocess,sys
spec=importlib.util.spec_from_file_location('lease_status',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
workers=[]
class PendingKill:
    def __init__(self,real): self.real=real;self.stdout=real.stdout;self.stderr=real.stderr
    def communicate(self,**kwargs): return self.real.communicate(**kwargs)
    def kill(self): pass
def spawn(command,**kwargs):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],**kwargs)
    workers.append(child)
    return PendingKill(child)
result=m.read_status(timeout=.01,lock_path=sys.argv[2],popen=spawn)
print(json.dumps({'pid':workers[0].pid,'ok':result['ok']}),flush=True)
"""
        command = [sys.executable, '-c', launcher, str(Path(status.__file__)), str(self.lock_path)]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=2)
        record = json.loads(result.stdout)
        self.assertFalse(record['ok'])
        child = record['pid']
        try:
            spawn = Mock()
            blocked = self.read_status(popen=spawn)
            self.assertFalse(blocked['ok'])
            self.assertIn('has not exited', blocked['error'])
            spawn.assert_not_called()
        finally:
            os.kill(child, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while True:
            try:
                lease = status._acquire_query_lease(self.lock_path)
                os.close(lease)
                break
            except ValueError:
                if time.monotonic() >= deadline: raise
                time.sleep(.01)

if __name__ == '__main__': unittest.main()
