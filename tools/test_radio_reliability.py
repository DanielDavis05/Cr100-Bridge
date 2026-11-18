"""Radio failures stay observable and do not strand live-looking worker threads."""
import errno
import socket
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch

import ccmp
import frames
import sonosnet_link as sl
from test_multi_controller_link import AP, FIRST, KEYS, STATE, data, ethernet, null

KEY = KEYS[FIRST]   # the first controller's own link key


class Radio:
    def __init__(self):
        self.input, self.inject = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sent, self.error = [], None

    def fileno(self): return self.input.fileno()
    def recv(self, size): return self.input.recv(size)

    def send(self, packet, flags=0):
        if not flags & socket.MSG_DONTWAIT: raise AssertionError('Blocking radio write')
        if self.error is not None: raise OSError(self.error, 'Synthetic radio failure')
        self.sent.append(packet)

    def close(self):
        self.input.close()
        self.inject.close()


class RadioReliability(unittest.TestCase):
    def setUp(self):
        self.radio = Radio()
        self.addCleanup(self.radio.close)
        tap, self.tap_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close)
        self.addCleanup(self.tap_peer.close)
        self.logs = []
        self.link = sl.Link(STATE, self.radio, tap.fileno(), self.logs.append)
        self.station = self.link.stations[FIRST]
        self.inner = ethernet(AP, source=FIRST)

    def test_bad_capture_and_truncated_ccmp_do_not_stop_rx_worker(self):
        # Flags are advertised but missing. The next packet has a complete MAC
        # header but no CCMP body. Neither may hide the following good packet.
        self.radio.inject.send(struct.pack('<BBHI', 0, 0, 8, 2))
        good = data(FIRST, self.inner, 4)
        self.radio.inject.send(frames.MIN_RADIOTAP + good[:24])
        self.radio.inject.send(frames.MIN_RADIOTAP + good)
        with patch.object(self.link, 'tap_loop'):
            self.link.run(.08)
        self.tap_peer.settimeout(.5)
        self.assertEqual(self.tap_peer.recv(4096), self.inner)
        self.assertEqual(self.station.stats['rx_data'], 1)
        self.assertEqual(self.station.stats['rx_decrypt_fail'], 1)
        self.assertEqual(self.link.stats['rx_malformed_radiotap'], 1)
        self.assertFalse(any(record['event'] == 'worker-failed' for record in self.logs))

    def test_unexpected_frame_processor_exception_fails_main_loop(self):
        self.radio.inject.send(frames.MIN_RADIOTAP + null(FIRST))
        with patch.object(self.link, 'on_frame', side_effect=RuntimeError('unexpected processor bug')), \
                patch.object(self.link, 'tap_loop'):
            with self.assertRaisesRegex(RuntimeError, 'radio worker failed'):
                self.link.run(.5)
        errors = [record for record in self.logs if record['event'] == 'worker-failed']
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['worker'], 'radio')
        self.assertIn('unexpected processor bug', errors[0]['error'])
        self.assertTrue(self.link.stop.is_set())
        self.assertFalse(any(worker.is_alive() for worker in self.link.workers.values()))

    def test_tap_eof_fails_main_loop_instead_of_spinning(self):
        self.tap_peer.send(bytes(14))
        with patch.object(sl.os, 'read', return_value=b''), patch.object(self.link, 'radio_loop'):
            with self.assertRaisesRegex(RuntimeError, 'tap worker failed'):
                self.link.run(.5)
        self.assertEqual([record['worker'] for record in self.logs if record['event'] == 'worker-failed'], ['tap'])

    def test_tap_backpressure_is_counted_without_blocking_or_losing_radio_liveness(self):
        self.station.searching, self.station.check_from = True, 99
        with patch.object(sl.os, 'write', side_effect=BlockingIOError(errno.EAGAIN, 'TAP full')), \
                patch.object(sl.time, 'monotonic', return_value=100):
            self.link.on_frame(data(FIRST, self.inner, 4))
        self.assertEqual(self.station.stats['rx_tap_busy'], 1)
        self.assertEqual(self.station.stats['rx_data'], 0)
        self.assertEqual(self.station.last_rx, 100)
        self.assertFalse(self.station.searching)
        self.assertIsNone(self.station.check_from)

    def test_authenticated_replay_refreshes_hearing_but_never_reaches_tap_twice(self):
        packet = data(FIRST, self.inner, 4)
        with patch.object(sl.os, 'write', return_value=len(self.inner)) as write:
            with patch.object(sl.time, 'monotonic', return_value=100): self.link.on_frame(packet)
            self.station.searching, self.station.check_from = True, 101
            with patch.object(sl.time, 'monotonic', return_value=108): self.link.on_frame(packet)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.station.stats['rx_replay'], 1)
        self.assertEqual(self.station.stats['rx_authenticated'], 2)
        self.assertEqual(self.station.last_rx, 108)
        self.assertEqual(self.station.last_pm_rx, 108)
        self.assertTrue(self.station.dozing)
        self.assertFalse(self.station.searching)
        self.assertIsNone(self.station.check_from)

    def test_wrong_key_slot_and_bad_mic_do_not_refresh_hearing(self):
        packet = data(FIRST, self.inner, 4)
        wrong_slot = ccmp.encrypt(packet[:24], ccmp.SNAP_PREFIX + b'\x69\x69' + self.inner, KEY, 5, key_id=1)
        bad_mic = bytearray(packet)
        bad_mic[-1] ^= 1
        for candidate in (wrong_slot, bytes(bad_mic)):
            self.link.on_frame(candidate)
        self.assertEqual(self.station.last_rx, 0)
        self.assertFalse(self.station.associated)
        self.assertEqual(self.station.stats['rx_wrong_key'], 1)
        self.assertEqual(self.station.stats['rx_decrypt_fail'], 1)

    def test_radio_outage_keeps_queue_and_definite_frame_error_drops_only_one_frame(self):
        self.station.associated = True
        self.link.from_tap(ethernet(FIRST))
        self.link.radio_down = True
        self.link.flush(self.station)
        self.assertEqual(len(self.station.queue), 1)
        self.assertEqual(self.station.stats['tx_dropped'], 0)
        self.link.radio_down = False
        self.radio.error = errno.EMSGSIZE
        self.link.flush(self.station)
        self.assertEqual(len(self.station.queue), 0)
        self.assertEqual(self.station.stats['tx_dropped'], 1)
        self.assertFalse(self.link.radio_down)
        self.assertEqual(self.link.stats['tx_attempts'], 1)
        self.assertEqual(self.link.stats['tx_sent'], 0)
        self.assertEqual(self.link.stats['tx_send_failures'], 1)

    def test_device_io_error_retains_queue_and_reports_radio_lost_once(self):
        self.station.associated = True
        self.link.from_tap(ethernet(FIRST))
        self.radio.error = errno.EIO
        self.link.flush(self.station)
        self.link.flush(self.station)
        self.assertTrue(self.link.radio_down)
        self.assertEqual(len(self.station.queue), 1)
        self.assertEqual(self.station.stats['tx_dropped'], 0)
        self.assertEqual(self.link.stats['tx_attempts'], 1)
        self.assertEqual([record['event'] for record in self.logs], ['radio-lost'])

    def test_unknown_send_error_is_not_silently_swallowed(self):
        self.radio.error = errno.EPERM
        with self.assertRaises(OSError): self.link.send(b'frame')
        self.assertEqual(self.link.stats['tx_error_EPERM'], 1)

    def test_durable_packet_number_is_reserved_before_encryption_and_consumed_on_busy(self):
        events = []
        allocator = Mock(initial=1000)
        allocator.ensure.side_effect = lambda pn: events.append(('reserved', pn))
        link = sl.Link(STATE, self.radio, self.link.tap_fd, self.logs.append, pn_allocator=allocator)
        self.radio.error = errno.ENOBUFS
        real_encrypt = ccmp.encrypt
        def encrypt(*args):
            events.append(('encrypted', args[3]))
            return real_encrypt(*args)
        with patch.object(sl.ccmp, 'encrypt', side_effect=encrypt):
            result = link.send_data(link.stations[FIRST], self.inner, False)
        self.assertEqual(result, sl.BUSY)
        self.assertEqual(link.tx_pn, 1001)
        self.assertEqual(events, [('reserved', 1001), ('encrypted', 1001)])
        self.radio.error = None
        with patch.object(sl, 'PACE_WINDOW', 0): link.send_data(link.stations[FIRST], self.inner, False)
        self.assertEqual(ccmp.decrypt(self.radio.sent[-1][len(frames.TX_RADIOTAP):], KEY)[0], 1002)
        self.assertEqual(allocator.ensure.call_args_list[-1].args, (1002,))

    def test_packet_number_storage_error_prevents_encryption_and_transmission(self):
        self.link.pn_allocator = Mock()
        self.link.pn_allocator.ensure.side_effect = OSError(errno.ENOSPC, 'disk full')
        before = self.link.tx_pn
        with patch.object(sl.ccmp, 'encrypt') as encrypt:
            with self.assertRaises(OSError): self.link.send_data(self.station, self.inner, False)
        encrypt.assert_not_called()
        self.assertEqual(self.radio.sent, [])
        self.assertEqual(self.link.tx_pn, before)

    def test_packet_number_exhaustion_never_wraps_or_transmits(self):
        self.link.tx_pn = (1 << 48) - 1
        with patch.object(sl.ccmp, 'encrypt') as encrypt:
            with self.assertRaises(OverflowError): self.link.send_data(self.station, self.inner, False)
        encrypt.assert_not_called()
        self.assertEqual(self.radio.sent, [])

    def test_beacon_metrics_separate_attempts_socket_acceptance_and_backpressure(self):
        self.radio.error = errno.EAGAIN
        self.link.send_beacon()
        self.radio.error = None
        self.link.send_beacon()
        self.assertEqual(self.link.stats['beacon_attempts'], 2)
        self.assertEqual(self.link.stats['beacon_sent'], 1)
        self.assertEqual(self.link.stats['beacon_failed'], 1)
        self.assertEqual(self.link.stats['tx_attempts'], 2)
        self.assertEqual(self.link.stats['tx_sent'], 1)

    def test_worker_status_and_clean_shutdown_are_visible(self):
        def capture(record):
            self.logs.append(record)
            if record['event'] == 'stats': self.link.stop.set()
        with patch.object(self.link, 'radio_loop', side_effect=lambda: self.link.stop.wait()), \
                patch.object(self.link, 'tap_loop', side_effect=lambda: self.link.stop.wait()), \
                patch.object(self.link, 'log', side_effect=capture):
            # Stop at the observed snapshot. A 20 ms run could expire during
            # thread startup on a busy emulator host, before the first report.
            self.link.start = sl.time.monotonic()
            self.link.run(2)
        stats = next(record for record in self.logs if record['event'] == 'stats')
        self.assertEqual(stats['workers'], {'radio': True, 'tap': True})
        self.assertFalse(any(worker.is_alive() for worker in self.link.workers.values()))

    def test_reopen_timeout_is_counted_and_repeated_error_logging_is_bounded(self):
        error = subprocess.TimeoutExpired(['iw', 'dev', 'mon0', 'set', 'channel', '6'], sl.RADIO_COMMAND_TIMEOUT)
        with patch.object(sl.time, 'monotonic', return_value=100):
            self.link.reopen_failed(error)
            self.link.reopen_failed(error)
        with patch.object(sl.time, 'monotonic', return_value=111): self.link.reopen_failed(error)
        self.assertEqual(self.link.stats['radio_reopen_timeouts'], 3)
        self.assertEqual(self.link.stats['radio_reopen_failures'], 3)
        self.assertEqual(len(self.logs), 2)
        self.assertTrue(all(record['timeout'] for record in self.logs))


class RadioCommandBounds(unittest.TestCase):
    @staticmethod
    def command(command, **kwargs):
        return Mock(stdout='wiphy 7\n' + ('type monitor\n' if command[:3] == ['iw', 'dev', 'mon0'] else ''))

    def test_every_radio_recovery_command_has_a_timeout(self):
        radio = Mock()
        with patch.object(sl.os.path, 'exists', return_value=False), \
                patch.object(sl.subprocess, 'run', side_effect=self.command) as run, \
                patch.object(sl.socket, 'AF_PACKET', 17, create=True), \
                patch.object(sl.socket, 'socket', return_value=radio):
            self.assertIs(sl.open_radio('mon0', 6), radio)
        self.assertEqual(run.call_count, 6)
        self.assertTrue(all(call.kwargs['timeout'] == sl.RADIO_COMMAND_TIMEOUT for call in run.call_args_list))
        radio.bind.assert_called_once_with(('mon0', 0))
        radio.setblocking.assert_called_once_with(False)

    def test_failed_socket_bind_closes_candidate_socket(self):
        radio = Mock()
        radio.bind.side_effect = OSError(errno.ENODEV, 'Device disappeared')
        with patch.object(sl.os.path, 'exists', return_value=True), \
                patch.object(sl.subprocess, 'run', side_effect=self.command), \
                patch.object(sl.socket, 'AF_PACKET', 17, create=True), \
                patch.object(sl.socket, 'socket', return_value=radio):
            with self.assertRaises(OSError): sl.open_radio('mon0', 6)
        radio.close.assert_called_once()

    def test_existing_monitor_must_match_wlan0_phy_and_type_before_socket(self):
        for monitor in ('wiphy 8\ntype monitor\n', 'wiphy 7\ntype managed\n',
                        'wiphy 7\n', 'type monitor\n', 'wiphy 7\nwiphy 8\ntype monitor\n'):
            with self.subTest(monitor=monitor), patch.object(sl.os.path, 'exists', return_value=True), \
                    patch.object(sl.subprocess, 'run', side_effect=[Mock(stdout='wiphy 7\n'),
                                                                  Mock(stdout=monitor)]) as run, \
                    patch.object(sl.socket, 'socket') as create:
                with self.assertRaises(RuntimeError): sl.open_radio('mon0', 6)
                create.assert_not_called()
                self.assertTrue(all(call.args[0][-1] == 'info' for call in run.call_args_list))
                self.assertTrue(all(call.kwargs['timeout'] == sl.RADIO_COMMAND_TIMEOUT
                                    for call in run.call_args_list))

    def test_malformed_wlan0_metadata_fails_before_monitor_or_socket(self):
        with patch.object(sl.os.path, 'exists', return_value=True), \
                patch.object(sl.subprocess, 'run', return_value=Mock(stdout='type managed\n')) as run, \
                patch.object(sl.socket, 'socket') as create:
            with self.assertRaisesRegex(RuntimeError, 'PHY for wlan0'):
                sl.open_radio('mon0', 6)
        create.assert_not_called()
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__': unittest.main()
