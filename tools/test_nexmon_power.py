"""MPC must be off before injection, including after a firmware reload."""
import errno
import io
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
from unittest.mock import Mock, patch

import nexmon_power


class FirmwarePower(unittest.TestCase):
    def connection(self, code=0):
        control = Mock()
        control.__enter__ = Mock(return_value=control)
        control.__exit__ = Mock(return_value=False)
        control.getsockname.return_value = (1234, 0)
        control.recv.return_value = struct.pack('<IHHIIi', 20, 2, 0, 1, 0, -code)
        return control, Mock(return_value=control)

    def test_already_disabled_needs_no_write(self):
        factory = Mock()
        self.assertEqual(nexmon_power.disable_mpc(getter=lambda: 0, socket_factory=factory), 0)
        factory.assert_not_called()

    def test_disables_only_mpc_and_checks_readback(self):
        control, factory = self.connection()
        getter = Mock(side_effect=[1, 0])
        self.assertEqual(nexmon_power.disable_mpc(getter=getter, socket_factory=factory), 1)
        packet, destination = control.sendto.call_args.args
        self.assertEqual(destination, (0, 0))
        self.assertEqual(packet[16:], b'NEX\0' + struct.pack('<III', 0, 263, 1) + b'mpc\0' + struct.pack('<I', 0))
        self.assertEqual(getter.call_count, 2)
        control.settimeout.assert_called_once_with(1)

    def test_driver_rejection_is_not_reported_as_success(self):
        control, factory = self.connection(errno.EOPNOTSUPP)
        with self.assertRaises(OSError):
            nexmon_power.disable_mpc(getter=lambda: 1, socket_factory=factory)

    def test_missing_ack_requires_successful_readback(self):
        control, factory = self.connection()
        control.recv.side_effect = socket.timeout()
        self.assertEqual(nexmon_power.disable_mpc(getter=Mock(side_effect=[1, 0]), socket_factory=factory), 1)
        with self.assertRaisesRegex(OSError, 'remained enabled'):
            nexmon_power.disable_mpc(getter=lambda: 1, socket_factory=factory)

    def test_unexpected_value_is_not_written(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            nexmon_power.disable_mpc(getter=lambda: 42, socket_factory=factory)
        factory.assert_not_called()


class BoundedPowerWorker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lease = self.root / 'run' / 'mpc-query.lock'
        self.children = []
        self.addCleanup(self.reap_test_children)

    def reap_test_children(self):
        # These are controlled Python test children, never kernel transactions.
        for child in self.children:
            if child.poll() is None: child.kill()
            child.wait(timeout=1)

    def factory(self, script):
        calls = []
        def launch(command, **options):
            calls.append((command, options))
            process = subprocess.Popen([sys.executable, '-u', '-c', script], **options)
            self.children.append(process)
            return process
        return launch, calls

    def success_script(self, previous=0):
        payload = nexmon_power._RESPONSE.pack(nexmon_power._MAGIC, 1, 0, previous, 0)
        return 'import os; os.write(1, bytes.fromhex(' + repr(payload.hex()) + '))'

    def test_fixed_worker_isolation_has_a_private_inherited_lease_and_releases_it_on_exit(self):
        factory, calls = self.factory(self.success_script(previous=1))
        query = nexmon_power._MpcQuery(timeout=.5, popen=factory, lock_path=self.lease)
        self.addCleanup(query.close)
        self.assertEqual(query(), 1)
        command, options = calls[0]
        self.assertEqual(command, [sys.executable, '-u', str(Path(nexmon_power.__file__).resolve()), '--_mpc-worker'])
        self.assertEqual(len(options['pass_fds']), 1)
        self.assertTrue(options['close_fds'])
        self.assertIs(options['stdin'], subprocess.DEVNULL)
        self.assertIs(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(self.lease.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.lease.parent.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(query._process)
        self.assertEqual(query(), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.lease.exists())  # The lease inode is never unlinked.

    def test_production_wrapper_uses_isolation_and_injected_transport_remains_inline(self):
        query = Mock(return_value=0)
        with patch.object(nexmon_power, '_query', None), \
                patch.object(nexmon_power, '_MpcQuery', return_value=query) as create:
            self.assertEqual(nexmon_power.disable_mpc(), 0)
            self.assertEqual(nexmon_power.disable_mpc(), 0)
        create.assert_called_once_with()
        self.assertEqual(query.call_count, 2)
        with patch.object(nexmon_power, '_MpcQuery') as create:
            self.assertEqual(nexmon_power.disable_mpc(getter=lambda: 0), 0)
        create.assert_not_called()

    def test_worker_emits_only_a_fixed_verified_result_or_a_failure(self):
        output = io.BytesIO()
        with patch.object(nexmon_power, '_disable_mpc_inline', return_value=1):
            self.assertEqual(nexmon_power._mpc_worker(output), 0)
        self.assertEqual(nexmon_power._RESPONSE.unpack(output.getvalue()), (b'MPCO', 1, 0, 1, 0))
        output = io.BytesIO()
        with patch.object(nexmon_power, '_disable_mpc_inline', side_effect=OSError(errno.EIO, 'private detail')):
            self.assertEqual(nexmon_power._mpc_worker(output), 1)
        response = nexmon_power._RESPONSE.unpack(output.getvalue())
        self.assertEqual(response[:3], (b'MPCO', 1, errno.EIO))
        self.assertNotIn(b'private detail', output.getvalue())

    def test_hanging_and_partially_written_responses_have_a_parent_deadline(self):
        for prefix in ('', 'import os; os.write(1, b"MPC"); '):
            with self.subTest(partial=bool(prefix)):
                factory, _ = self.factory(prefix + 'import time; time.sleep(3)')
                query = nexmon_power._MpcQuery(timeout=.08, popen=factory, lock_path=self.lease)
                started = time.monotonic()
                with self.assertRaises(OSError) as error: query()
                self.assertEqual(error.exception.errno, errno.ETIMEDOUT)
                self.assertLess(time.monotonic() - started, .8)
                query.close()
                self.children[-1].wait(timeout=1)

    def test_complete_reply_is_insufficient_without_eof_and_successful_exit(self):
        success = self.success_script()
        for suffix in ('; import time; time.sleep(3)', '; os.close(1); import time; time.sleep(3)'):
            with self.subTest(closed_output='os.close' in suffix):
                factory, _ = self.factory(success + suffix)
                query = nexmon_power._MpcQuery(timeout=.08, popen=factory, lock_path=self.lease)
                with self.assertRaises(OSError) as error: query()
                self.assertEqual(error.exception.errno, errno.ETIMEDOUT)
                query.close()
                self.children[-1].wait(timeout=1)

    def test_short_malformed_extra_or_failed_child_results_never_enable_injection(self):
        valid = nexmon_power._RESPONSE.pack(b'MPCO', 1, 0, 0, 0)
        cases = [(valid[:-1], 0), (valid + b'X', 0), (valid, 1),
                 (nexmon_power._RESPONSE.pack(b'FAKE', 1, 0, 0, 0), 0),
                 (nexmon_power._RESPONSE.pack(b'MPCO', 2, 0, 0, 0), 0),
                 (nexmon_power._RESPONSE.pack(b'MPCO', 1, 0, 2, 0), 0),
                 (nexmon_power._RESPONSE.pack(b'MPCO', 1, 0, 0, 1), 0),
                 (nexmon_power._RESPONSE.pack(b'MPCO', 1, errno.EPERM, 0, 0), 1)]
        for payload, exit_code in cases:
            with self.subTest(payload=payload.hex(), exit_code=exit_code):
                script = 'import os; os.write(1, bytes.fromhex(' + repr(payload.hex()) + ')); raise SystemExit(' + str(exit_code) + ')'
                factory, _ = self.factory(script)
                query = nexmon_power._MpcQuery(timeout=.5, popen=factory, lock_path=self.lease)
                with self.assertRaises(OSError): query()
                query.close()
                self.children[-1].wait(timeout=1)

    def test_late_eof_from_an_already_terminal_child_is_not_accepted(self):
        reader, writer = os.pipe()
        payload = nexmon_power._RESPONSE.pack(b'MPCO', 1, 0, 0, 0)
        os.write(writer, payload); os.close(writer)
        output = os.fdopen(reader, 'rb', buffering=0)
        self.addCleanup(output.close)
        process = Mock(stdout=output, returncode=0)
        process.poll.return_value = 0
        query = nexmon_power._MpcQuery(lock_path=self.lease)
        # The parent is descheduled after reading EOF; poll already sees exit.
        with patch.object(nexmon_power.time, 'monotonic', side_effect=[0.0, 0.0, 1.0]):
            with self.assertRaises(OSError) as error: query._read_response(process, .5)
        self.assertEqual(error.exception.errno, errno.ETIMEDOUT)

    def test_close_after_reading_a_valid_reply_cancels_publication(self):
        factory, _ = self.factory(self.success_script())
        query = nexmon_power._MpcQuery(timeout=.5, popen=factory, lock_path=self.lease)
        read = query._read_response
        def close_after_read(process, deadline):
            previous = read(process, deadline)
            query.close()
            return previous
        with patch.object(query, '_read_response', side_effect=close_after_read):
            with self.assertRaises(OSError) as error: query()
        self.assertEqual(error.exception.errno, errno.ECANCELED)

    def test_unsafe_or_redirected_lease_is_rejected_before_a_helper_starts(self):
        factory = Mock()
        self.lease.parent.mkdir(mode=0o700)
        self.lease.write_bytes(b''); self.lease.chmod(0o644)
        query = nexmon_power._MpcQuery(popen=factory, lock_path=self.lease)
        with self.assertRaises(OSError): query()
        factory.assert_not_called()
        self.lease.unlink()
        other = self.root / 'other'; other.write_bytes(b''); other.chmod(0o600)
        self.lease.symlink_to(other)
        with self.assertRaises(OSError): query()
        factory.assert_not_called()
        self.lease.unlink()
        self.lease.parent.chmod(0o777)
        with self.assertRaises(OSError): query()
        factory.assert_not_called()
        self.lease.parent.chmod(0o700)

    def stuck_factory(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        process = Mock(pid=4242, stdout=os.fdopen(reader, 'rb', buffering=0))
        process.poll.return_value = None
        inherited = []
        calls = []
        def launch(command, **options):
            calls.append(command)
            inherited.append(os.dup(options['pass_fds'][0]))
            return process
        return launch, calls, process, inherited

    def test_stuck_pid_and_inherited_lease_prevent_replacement_across_parent_close(self):
        factory, calls, process, inherited = self.stuck_factory()
        query = nexmon_power._MpcQuery(timeout=.02, popen=factory, lock_path=self.lease)
        try:
            with self.assertRaises(OSError) as error: query()
            self.assertEqual(error.exception.errno, errno.ETIMEDOUT)
            self.assertIs(query._process, process)
            with self.assertRaises(OSError) as error: query()
            self.assertEqual(error.exception.errno, errno.EBUSY)
            self.assertEqual(len(calls), 1)
            started = time.monotonic()
            query.close()
            self.assertLess(time.monotonic() - started, .1)
            replacement_factory = Mock()
            replacement = nexmon_power._MpcQuery(popen=replacement_factory, lock_path=self.lease)
            with self.assertRaises(OSError) as error: replacement()
            self.assertEqual(error.exception.errno, errno.EBUSY)
            replacement_factory.assert_not_called()
            process.wait.assert_not_called()
            process.communicate.assert_not_called()
        finally:
            query.close()
            for descriptor in inherited: os.close(descriptor)
        # The same stable lease file becomes usable only after the simulated
        # child really exits and its last inherited descriptor closes.
        recovered_factory, _ = self.factory(self.success_script())
        recovered = nexmon_power._MpcQuery(popen=recovered_factory, lock_path=self.lease)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered(), 0)

    def test_concurrent_close_keeps_ipc_on_its_duplicate_not_an_os_reused_descriptor(self):
        factory, _, process, inherited = self.stuck_factory()
        original_fd = process.stdout.fileno()
        duplicated, errors, results = threading.Event(), [], []
        query = nexmon_power._MpcQuery(timeout=.08, popen=factory, lock_path=self.lease)
        duplicate = os.dup
        def capture(descriptor):
            result = duplicate(descriptor)
            if descriptor == original_fd: duplicated.set()
            return result
        def run():
            try: results.append(query())
            except BaseException as error: errors.append(error)
        worker = threading.Thread(target=run)
        new_reader = new_writer = None
        try:
            with patch.object(nexmon_power.os, 'dup', side_effect=capture):
                worker.start()
                self.assertTrue(duplicated.wait(.5))
                query.close()
                new_reader, new_writer = os.pipe()
                self.assertEqual(new_reader, original_fd)
                payload = nexmon_power._RESPONSE.pack(b'MPCO', 1, 0, 0, 0)
                os.write(new_writer, payload)
                worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, [])
            self.assertTrue(errors)
            self.assertEqual(os.read(new_reader, len(payload)), payload)
        finally:
            query.close()
            worker.join(.5)
            for descriptor in inherited: os.close(descriptor)
            for descriptor in (new_reader, new_writer):
                if descriptor is not None: os.close(descriptor)


if __name__ == '__main__':
    unittest.main()
