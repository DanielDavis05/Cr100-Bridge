"""Model and transport boundaries for the read-only hardware clock helper."""
import errno
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import nexmon_clock as nc


def tsf_reply(value=123):
    return struct.pack('<6I', nc.TSF_MAGIC, 1, 24, 1, value & 0xffffffff, value >> 32)


def netlink_reply(data=None, *, command=433, set_flag=0, kind=3):
    body = b'NEX\0' + struct.pack('<III', 0, command, set_flag) + (data or tsf_reply())
    return struct.pack('<IHHII', 16 + len(body), kind, 0, 0, 0) + body


class ClockTransport(unittest.TestCase):
    def connection(self, reply=None, sender=(0, 0)):
        control = Mock()
        control.__enter__ = Mock(return_value=control)
        control.__exit__ = Mock(return_value=False)
        control.getsockname.return_value = (1234, 0)
        control.sendto.side_effect = lambda packet, destination: len(packet)
        control.recvfrom.return_value = (reply or netlink_reply(), sender)
        return control, Mock(return_value=control)

    def test_fixed_get_and_real_driver_zero_sequence(self):
        control, factory = self.connection(netlink_reply(tsf_reply(0x123456789a)))
        self.assertEqual(nc.read_tsf(socket_factory=factory), 0x123456789a)
        request, destination = control.sendto.call_args.args
        self.assertEqual(destination, (0, 0))
        self.assertEqual(struct.unpack('<IHHII', request[:16]), (56, 0, 0, 1, 1234))
        self.assertEqual(request[16:], b'NEX\0' + struct.pack('<III', 0, 433, 0) + bytes(24))
        self.assertEqual(control.recvfrom.call_args.args, (57,))
        for call in control.settimeout.call_args_list:
            self.assertTrue(0 < call.args[0] <= .075)

    def test_kernel_sender_required_despite_spoofed_header_pid(self):
        _, factory = self.connection(sender=(99, 0))
        with self.assertRaisesRegex(ValueError, 'kernel'):
            nc.read_tsf(socket_factory=factory)

    def test_rejects_wrong_command_set_size_kind_and_echo(self):
        valid = netlink_reply()
        variants = [netlink_reply(command=432), netlink_reply(set_flag=1),
                    netlink_reply(kind=0), valid[:-1], valid + b'\0',
                    valid[:32] + bytes(24), valid[:16] + b'BAD\0' + valid[20:]]
        for reply in variants:
            with self.subTest(reply=reply):
                _, factory = self.connection(reply)
                with self.assertRaises((ValueError, nc.ClockUnavailable)):
                    nc.read_tsf(socket_factory=factory)

    def test_strict_tsf_abi_status_and_uint64(self):
        self.assertEqual(nc.parse_tsf(tsf_reply(nc.TSF_MASK)), nc.TSF_MASK)
        for index, value in ((0, 0), (1, 2), (2, 28), (3, 0), (3, 2)):
            words = list(struct.unpack('<6I', tsf_reply()))
            words[index] = value
            with self.subTest(index=index, value=value):
                with self.assertRaises((ValueError, nc.ClockUnavailable)):
                    nc.parse_tsf(struct.pack('<6I', *words))
        with self.assertRaises(ValueError):
            nc.parse_tsf(tsf_reply() + b'\0')

    def test_one_deadline_includes_send_and_receive(self):
        control, factory = self.connection()
        moments = iter((0., .001, .02, .06, .076))
        with self.assertRaises(TimeoutError):
            nc.read_tsf(socket_factory=factory, monotonic=lambda: next(moments))
        self.assertLess(control.settimeout.call_args_list[-1].args[0], .02)

    def test_transport_errors_and_timeout_bounds(self):
        error = struct.pack('<IHHIIi', 20, 2, 0, 0, 0, -errno.EOPNOTSUPP)
        _, factory = self.connection(error)
        with self.assertRaises(OSError) as caught:
            nc.read_tsf(socket_factory=factory)
        self.assertEqual(caught.exception.errno, errno.EOPNOTSUPP)
        control, factory = self.connection()
        control.recvfrom.side_effect = socket.timeout()
        with self.assertRaises(TimeoutError):
            nc.read_tsf(socket_factory=factory)
        for timeout in (.049, .101, float('nan')):
            with self.assertRaises(ValueError):
                nc.read_tsf(timeout=timeout, socket_factory=factory)


class ProcessTransport(unittest.TestCase):
    SCRIPT = """
import struct, sys, time
{startup}
source, output = sys.stdin.buffer, sys.stdout.buffer
output.write(struct.pack('<4sI', b'CRCW', 1)); output.flush()
while True:
    request = source.read(8)
    if not request: break
    magic, sequence = struct.unpack('<4sI', request)
    if magic != b'TSFR': raise SystemExit(2)
    {query}
    before = time.monotonic_ns()
    raw = before // 1000
    after = time.monotonic_ns()
    output.write(struct.pack('<4sIIIQQQ', {magic}, 1, 1, sequence, raw, {before}, after))
    output.flush()
"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.lock_path = str(Path(temporary.name) / 'tsf-query.lock')

    def reader(self, *args, **kwargs):
        return nc._ProcessReader(*args, lock_path=self.lock_path, **kwargs)

    def factory(self, scripts, *, proxy=None):
        processes, requests = [], []
        created = threading.Event()

        def spawn(command, **kwargs):
            requests.append(command)
            script = scripts[min(len(processes), len(scripts) - 1)]
            real = subprocess.Popen([sys.executable, '-u', '-c', script], **kwargs)
            process = proxy(real) if proxy else real
            processes.append(process)
            created.set()
            return process

        def cleanup():
            for process in processes:
                real = getattr(process, 'real', process)
                real.kill()
                for stream in (real.stdin, real.stdout):
                    if stream:
                        stream.close()
                real.wait(timeout=1)

        self.addCleanup(cleanup)
        return spawn, processes, requests, created

    def script(self, *, startup='pass', query='pass', magic="b'TSFS'", before='before'):
        return self.SCRIPT.format(startup=startup, query=query, magic=magic, before=before)

    def test_persistent_child_and_startup_outside_calibration_rtt(self):
        factory, processes, commands, _ = self.factory([self.script(startup='time.sleep(.04)')])
        reader = self.reader(popen=factory)
        self.addCleanup(reader.close)
        clock = nc.HardwareClock(reader=reader)
        self.addCleanup(clock.close)
        started = time.monotonic()
        self.assertTrue(clock.bootstrap())
        self.assertGreater(time.monotonic() - started, .04)
        self.assertLess(clock.status()['rtt_us'], 4000)
        self.assertLess(abs(clock.tsf_us() - time.monotonic_ns() // 1000), 2000)
        self.assertTrue(clock._calibrate(1))
        self.assertEqual(len(processes), 1)
        self.assertEqual(commands[0][-2], '--_clock-worker')
        self.assertEqual(float(commands[0][-1]), .075)
        self.assertEqual(reader.status()['starts'], 1)

    def test_child_stall_times_out_and_exposes_fault(self):
        factory, processes, _, _ = self.factory([self.script(query='time.sleep(10)')])
        reader = self.reader(.05, popen=factory)
        self.addCleanup(reader.close)
        started = time.monotonic()
        with self.assertRaises(nc.ClockUnavailable):
            reader()
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(processes[0].stdin.closed and processes[0].stdout.closed)
        self.assertEqual(reader.status()['starts'], 1)
        self.assertIn('timed out', reader.status()['last_fault'])

    def test_nonterminal_killed_child_never_accumulates_replacements(self):
        class Nonterminal:
            def __init__(self, real):
                self.real = real
                self.stdin, self.stdout, self.pid = real.stdin, real.stdout, real.pid
                self.hold = True

            def kill(self):
                self.real.kill()

            def poll(self):
                return None if self.hold else self.real.poll()

        factory, processes, _, _ = self.factory(
            [self.script(query='time.sleep(10)'), self.script()], proxy=Nonterminal)
        reader = self.reader(.05, popen=factory)
        self.addCleanup(reader.close)
        with self.assertRaises(nc.ClockUnavailable):
            reader()
        for _ in range(5):
            with self.assertRaisesRegex(nc.ClockUnavailable, 'has not exited'):
                reader()
        self.assertEqual(len(processes), 1)
        self.assertTrue(reader.status()['awaiting_exit'])
        processes[0].real.wait(timeout=1)
        processes[0].hold = False
        self.assertIsInstance(reader(), nc._Sample)
        self.assertEqual(len(processes), 2)

    def test_inherited_lease_survives_parent_close_and_blocks_new_clock_process(self):
        class IgnoresKill:
            def __init__(self, real):
                self.real = real
                self.stdin, self.stdout, self.pid = real.stdin, real.stdout, real.pid

            def kill(self):
                pass  # Model SIGKILL pending on an uninterruptible kernel task.

            def poll(self):
                return self.real.poll()

        factory, processes, _, _ = self.factory(
            [self.script(query='time.sleep(10)'), self.script()], proxy=IgnoresKill)
        first = self.reader(.05, popen=factory)
        self.addCleanup(first.close)
        with self.assertRaises(nc.ClockUnavailable):
            first()
        first.close()
        self.assertFalse(first.status()['lease_held'])
        self.assertTrue(first.status()['awaiting_exit'])
        self.assertEqual(os.stat(self.lock_path).st_mode & 0o777, 0o600)
        second = self.reader(.05, popen=factory)
        self.addCleanup(second.close)
        with self.assertRaisesRegex(nc.ClockUnavailable, 'owns the radio lease'):
            second()
        self.assertEqual(len(processes), 1)
        check = """
import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit(0)
raise SystemExit(1)
"""
        command = [sys.executable, '-c', check, self.lock_path]
        self.assertEqual(subprocess.run(command, timeout=1).returncode, 0)
        processes[0].real.kill()
        processes[0].real.wait(timeout=1)
        self.assertEqual(subprocess.run(command, timeout=1).returncode, 1)
        self.assertIsInstance(second(), nc._Sample)
        self.assertEqual(len(processes), 2)

    def test_lease_rejects_symlink_nonregular_and_open_permissions(self):
        base = Path(self.lock_path).parent
        target = base / 'target'
        target.write_bytes(b'unchanged')
        symlink = base / 'symlink'
        symlink.symlink_to(target)
        insecure = base / 'insecure'
        insecure.write_bytes(b'')
        insecure.chmod(0o644)
        directory = base / 'directory'
        directory.mkdir()
        fifo = base / 'fifo'
        os.mkfifo(fifo)
        for path in (symlink, insecure, directory, fifo):
            factory = Mock()
            reader = nc._ProcessReader(lock_path=str(path), popen=factory)
            self.addCleanup(reader.close)
            with self.assertRaises(nc.ClockUnavailable):
                reader()
            factory.assert_not_called()
        self.assertEqual(target.read_bytes(), b'unchanged')

    def test_dead_malformed_and_startup_stalled_children_fail_closed(self):
        scripts = ['raise SystemExit(0)', self.script(magic="b'BAD!'"),
                   self.script(before='0'), 'import time; time.sleep(10)']
        for script in scripts:
            with self.subTest(script=script):
                factory, processes, _, _ = self.factory([script])
                reader = self.reader(.05, startup_timeout=.1, popen=factory)
                self.addCleanup(reader.close)
                with self.assertRaises(nc.ClockUnavailable):
                    reader()
                self.assertEqual(reader.status()['starts'], 1)
                self.assertIsNotNone(reader.status()['last_fault'])
                reader.close()
                processes[0].wait(timeout=1)

    def test_partial_startup_and_reply_have_one_deadline(self):
        startup = "import sys,time; sys.stdout.buffer.write(b'CRCW'); sys.stdout.buffer.flush(); time.sleep(10)"
        reply = """
import struct,sys,time
out=sys.stdout.buffer
out.write(struct.pack('<4sI',b'CRCW',1)); out.flush()
request=sys.stdin.buffer.read(8)
out.write(b'TSFS'+bytes(13)); out.flush(); time.sleep(10)
"""
        for script in (startup, reply):
            factory, processes, _, _ = self.factory([script])
            reader = self.reader(.05, startup_timeout=.1, popen=factory)
            self.addCleanup(reader.close)
            started = time.monotonic()
            with self.assertRaises(nc.ClockUnavailable):
                reader()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(processes[0].stdin.closed and processes[0].stdout.closed)
            reader.close()
            processes[0].wait(timeout=1)

    def test_full_request_pipe_cannot_block_parent_write(self):
        script = """
import struct,sys,time
sys.stdout.buffer.write(struct.pack('<4sI',b'CRCW',1)); sys.stdout.buffer.flush()
time.sleep(10)
"""
        factory, _, _, _ = self.factory([script])
        reader = self.reader(.05, popen=factory)
        self.addCleanup(reader.close)
        process = reader._ensure_process()
        filled = False
        for _ in range(256):
            try:
                os.write(process.stdin.fileno(), bytes(4096))
            except BlockingIOError:
                filled = True
                break
        self.assertTrue(filled)
        started = time.monotonic()
        with self.assertRaises(nc.ClockUnavailable):
            reader()
        self.assertLess(time.monotonic() - started, .2)
        self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_close_during_startup_is_nonblocking_and_rejects_result(self):
        factory, _, _, created = self.factory(['import time; time.sleep(10)'])
        reader = self.reader(.05, startup_timeout=.2, popen=factory)
        self.addCleanup(reader.close)
        errors = []

        def run():
            try:
                reader()
            except nc.ClockUnavailable:
                errors.append(True)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(created.wait(.2))
        before = time.monotonic()
        reader.close()
        self.assertLess(time.monotonic() - before, .05)
        worker.join(.3)
        self.assertEqual(errors, [True])
        self.assertTrue(reader.status()['closed'])
        with self.assertRaises(nc.ClockUnavailable):
            reader()

    def test_default_clock_owns_and_closes_its_helper(self):
        factory, _, _, _ = self.factory([self.script()])
        clock = nc.HardwareClock(lock_path=self.lock_path)
        self.addCleanup(clock.close)
        clock._process_reader._popen = factory
        self.assertTrue(clock.bootstrap())
        self.assertTrue(clock.status()['helper']['alive'])
        clock.close()
        self.assertTrue(clock.status()['helper']['closed'])
        self.assertFalse(clock.status()['ready'])


class FakeTime:
    def __init__(self, now_ns=100_000_000_000):
        self.ns = now_ns

    def now(self):
        return self.ns

    def advance(self, ns):
        self.ns += ns


class RadioModel:
    def __init__(self, clock, *, initial_tsf=12_345_678, latencies=None, ppm=0):
        self.clock = clock
        self.host_epoch_ns = clock.now()
        self.initial_tsf = initial_tsf
        self.latencies = iter(latencies or ())
        self.ppm = ppm
        self.calls = 0
        self.error = None

    def value(self, host_ns=None):
        elapsed_ns = (self.clock.now() if host_ns is None else host_ns) - self.host_epoch_ns
        return (self.initial_tsf + elapsed_ns * (1_000_000 + self.ppm) // 1_000_000_000) & nc.TSF_MASK

    def __call__(self):
        self.calls += 1
        latency = next(self.latencies, 200_000)
        self.clock.advance(latency // 2)
        value = self.value()
        self.clock.advance(latency - latency // 2)
        if self.error:
            raise self.error
        return value


class ClockModel(unittest.TestCase):
    def make(self, **radio_options):
        fake = FakeTime()
        radio = RadioModel(fake, **radio_options)
        clock = nc.HardwareClock(reader=radio, monotonic_ns=fake.now)
        self.addCleanup(clock.close)
        return clock, fake, radio

    def test_bootstrap_three_and_minimum_rtt_midpoint(self):
        clock, fake, radio = self.make(latencies=[1_000_000, 200_000, 600_000])
        self.assertTrue(clock.bootstrap())
        self.assertEqual(radio.calls, 3)
        self.assertEqual(clock.tsf_us(), radio.value())
        status = clock.status()
        self.assertEqual(status['rtt_us'], 200)
        self.assertGreaterEqual(status['uncertainty_us'], 101)
        self.assertLess(status['uncertainty_us'], 102)

    def test_hardware_epoch_and_grid_do_not_follow_process_start(self):
        clock, fake, radio = self.make()
        self.assertTrue(clock.bootstrap())
        first = clock.next_tbtt()
        target_ns = round(first * 1e9)
        self.assertLess(abs(radio.value(target_ns) % 102400), 1)
        self.assertTrue(0 < first - fake.now() / 1e9 <= .1024)
        fake.advance(1_000_000_000)
        second = nc.HardwareClock(reader=radio, monotonic_ns=fake.now)
        self.addCleanup(second.close)
        self.assertTrue(second.bootstrap())
        self.assertEqual(clock.tsf_us(), second.tsf_us())
        self.assertAlmostEqual(clock.next_tbtt(), second.next_tbtt(), places=8)

    def test_exact_grid_is_strictly_next_and_fractional_host_time(self):
        clock, fake, _ = self.make(initial_tsf=102400)
        self.assertTrue(clock.bootstrap())
        target = clock.next_tbtt()
        self.assertAlmostEqual(clock.next_tbtt(now=target) - target, .1024, places=8)
        self.assertGreater(clock.next_tbtt(now=target - .0000001), target - .0000001)
        for interval in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                clock.next_tbtt(interval_us=interval)
        for distant in (fake.now() / 1e9 + 4, fake.now() / 1e9 - 4):
            with self.assertRaises(nc.ClockUnavailable):
                clock.next_tbtt(now=distant)

    def test_uint32_and_uint64_rollover_remain_continuous(self):
        for initial in ((1 << 32) - 500_000, nc.TSF_MODULUS - 500_000):
            clock, fake, radio = self.make(initial_tsf=initial)
            self.assertTrue(clock.bootstrap())
            fake.advance(1_000_000_000)
            self.assertTrue(clock._calibrate(1))
            self.assertEqual(clock.tsf_us(), radio.value())
            self.assertEqual(clock.status()['resets'], 0)

    def test_grid_crosses_uint64_wrap_at_zero(self):
        clock, fake, radio = self.make(initial_tsf=nc.TSF_MODULUS - 10_000)
        self.assertTrue(clock.bootstrap())
        target = clock.next_tbtt()
        self.assertEqual(radio.value(round(target * 1e9)), 0)
        self.assertTrue(0 < target - fake.now() / 1e9 < .01)

    def test_bounded_drift_is_uncertainty_not_claimed_rate_fit(self):
        clock, fake, radio = self.make(ppm=100)
        self.assertTrue(clock.bootstrap())
        fake.advance(2_000_000_000)
        estimate = clock.tsf_us()
        self.assertLess(abs(estimate - radio.value()), clock.status()['uncertainty_us'])
        self.assertTrue(clock._calibrate(1))
        self.assertLessEqual(abs(clock.tsf_us() - radio.value()), 1)

    def test_asymmetric_query_timing_is_covered_by_half_rtt_uncertainty(self):
        for capture_at_end in (False, True):
            fake = FakeTime()
            radio = RadioModel(fake)

            def reader():
                start_value = radio.value()
                fake.advance(1_000_000)
                return radio.value() if capture_at_end else start_value

            clock = nc.HardwareClock(reader=reader, monotonic_ns=fake.now)
            self.addCleanup(clock.close)
            self.assertTrue(clock.bootstrap())
            self.assertEqual(abs(clock.tsf_us() - radio.value()), 500)
            self.assertGreater(clock.status()['uncertainty_us'], 500)

    def test_uncertainty_limit_expires_before_maximum_age(self):
        clock, fake, _ = self.make()
        clock.max_uncertainty_us = 125
        clock.refresh_interval = .005
        self.assertTrue(clock.bootstrap())
        fake.advance(200_000_000)
        self.assertLess(clock.status()['age_seconds'], 3)
        with self.assertRaises(nc.ClockUnavailable):
            clock.tsf_us()

    def test_marginal_refresh_rtt_cannot_replace_a_good_model_then_expire(self):
        clock, fake, radio = self.make()
        self.assertTrue(clock.bootstrap())
        previous = clock._model
        fake.advance(1_000_000_000)
        # Production failure: this RTT passed the immediate 2 ms uncertainty
        # check but left only ~0.38 s, shorter than the one-second refresh.
        radio.latencies = iter((3_845_740,))
        self.assertFalse(clock._calibrate(1))
        self.assertIs(clock._model, previous)
        self.assertIn('refresh headroom', clock.status()['last_error'])
        fake.advance(1_000_000_000)
        self.assertTrue(clock.status()['ready'])
        clock.tsf_us()
        self.assertTrue(clock._calibrate(1))
        self.assertTrue(clock.status()['ready'])

    def test_reset_or_excessive_drift_invalidates_then_rebootstraps(self):
        for reset in (True, False):
            clock, fake, radio = self.make()
            self.assertTrue(clock.bootstrap())
            fake.advance(1_000_000_000)
            if reset:
                radio.initial_tsf = 0
                radio.host_epoch_ns = fake.now()
            else:
                radio.ppm = 10_000
            self.assertFalse(clock._calibrate(1))
            self.assertFalse(clock.status()['ready'])
            self.assertEqual(clock.status()['resets'], 1)
            with self.assertRaises(nc.ClockUnavailable):
                clock.tsf_us()
            self.assertTrue(clock.bootstrap())

    def test_timeouts_retain_last_good_only_until_stale(self):
        clock, fake, radio = self.make()
        self.assertTrue(clock.bootstrap())
        radio.error = TimeoutError('test timeout')
        fake.advance(1_000_000_000)
        self.assertFalse(clock._calibrate(1))
        self.assertTrue(clock.status()['ready'])
        fake.advance(3_000_000_000)
        self.assertFalse(clock.status()['ready'])
        for call in (clock.tsf_us, clock.next_tbtt):
            with self.assertRaises(nc.ClockUnavailable):
                call()

    def test_missing_high_rtt_and_reset_inside_bootstrap_fail_closed(self):
        clock, fake, radio = self.make(latencies=[5_000_000] * 3)
        self.assertFalse(clock.bootstrap())
        self.assertFalse(clock.status()['ready'])
        for value in (-1, True, nc.TSF_MODULUS):
            other = nc.HardwareClock(reader=lambda: value, monotonic_ns=fake.now)
            self.addCleanup(other.close)
            self.assertFalse(other.bootstrap())
        values = iter((10_000_000, 1, 2))
        other = nc.HardwareClock(reader=lambda: next(values), monotonic_ns=fake.now)
        self.addCleanup(other.close)
        self.assertFalse(other.bootstrap())

    def test_startup_does_not_publish_an_already_expired_best_sample(self):
        fake = FakeTime()
        radio = RadioModel(fake, latencies=[2_500_000] * 3)
        clock = nc.HardwareClock(reader=radio, monotonic_ns=fake.now,
                                 refresh_interval=.001, max_age=.005)
        self.addCleanup(clock.close)
        self.assertFalse(clock.bootstrap())
        self.assertFalse(clock.status()['ready'])

    def test_invalidate_rejects_inflight_old_generation(self):
        entered, released = threading.Event(), threading.Event()
        fake = FakeTime()

        def reader():
            entered.set()
            released.wait(.5)
            return 1234

        clock = nc.HardwareClock(reader=reader, monotonic_ns=fake.now)
        self.addCleanup(clock.close)
        result = []
        worker = threading.Thread(target=lambda: result.append(clock.bootstrap()))
        worker.start()
        self.assertTrue(entered.wait(.2))
        clock.invalidate()
        released.set()
        worker.join(.2)
        self.assertEqual(result, [False])
        self.assertFalse(clock.status()['ready'])

    def test_background_query_never_blocks_clock_reads_and_close_rejects_result(self):
        clock, fake, radio = self.make()
        self.assertTrue(clock.bootstrap())
        entered, release = threading.Event(), threading.Event()

        def blocked_reader():
            entered.set()
            release.wait(.2)
            return radio.value()

        clock._reader = blocked_reader
        clock.start()
        self.assertTrue(entered.wait(.2))
        before = time.monotonic()
        self.assertEqual(clock.tsf_us(), radio.value())
        clock.next_tbtt()
        self.assertLess(time.monotonic() - before, .02)
        clock.close()
        release.set()
        clock._thread.join(.2)
        self.assertFalse(clock.status()['ready'])
        self.assertFalse(clock.status()['worker_alive'])
        with self.assertRaises(nc.ClockUnavailable):
            clock.start()


if __name__ == '__main__':
    unittest.main()
