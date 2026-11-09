"""Management backpressure must not stall radio statistics or recovery."""
import errno
import socket
import threading
import unittest
from unittest.mock import patch

import frames
import sonosnet_link as sl
from test_link import STATE


class ManagementBackpressure(unittest.TestCase):
    def test_full_socket_buffer_does_not_block_management_or_hold_locks(self):
        radio, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(radio.close)
        self.addCleanup(receiver.close)
        radio.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        body = frames.beacon_body(0, 6)
        packet = frames.TX_RADIOTAP + frames.mgmt_header(8, frames.BROADCAST,
            frames.mac_bytes(STATE['bssid']), 1) + body
        # Use the same datagram size as the management send so the final packet
        # cannot fit in space left after filling the buffer.
        for _ in range(10000):
            try:
                radio.send(packet, socket.MSG_DONTWAIT)
            except OSError as error:
                if error.errno not in sl.RADIO_BUSY: raise
                break
        else:
            self.fail('Could not fill the local datagram send buffer')
        link = sl.Link(STATE, radio, -1, lambda _: None)
        done, errors = threading.Event(), []
        def transmit():
            try:
                link.send_mgmt(8, frames.BROADCAST, body)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=transmit, daemon=True)
        worker.start()
        try:
            self.assertTrue(done.wait(.5), 'A full radio buffer blocked management transmission')
            self.assertEqual(errors, [])
            self.assertEqual(link.stats['tx_busy'], 1)
            self.assertFalse(link.radio_down)
            for lock in (link.tx_lock, link.lock):
                self.assertTrue(lock.acquire(timeout=.2), 'Backpressure left a link lock held')
                lock.release()
        finally:
            # Release an old blocking implementation too, so a regression fails
            # promptly without leaving a stuck worker behind.
            receiver.setblocking(False)
            while True:
                try:
                    receiver.recv(65536)
                except BlockingIOError:
                    break
            worker.join(1)

    def test_busy_management_leaves_stats_and_radio_reopen_running(self):
        class Radio:
            def __init__(self, busy=False):
                self.busy, self.gone, self.attempts, self.sent = busy, False, 0, []
            def send(self, packet, flags=0):
                if not flags & socket.MSG_DONTWAIT:
                    raise AssertionError('Management send would block the main loop')
                self.attempts += 1
                if self.gone:
                    raise OSError(errno.ENETDOWN, 'Network is down')
                if self.busy:
                    code = errno.EAGAIN if self.attempts % 2 else errno.ENOBUFS
                    raise OSError(code, 'Transmit queue full')
                self.sent.append(packet[len(frames.TX_RADIOTAP):])
            def close(self): pass

        busy, fresh, logs = Radio(busy=True), Radio(), []
        link = sl.Link(STATE, busy, -1, logs.append, reopen=lambda: fresh)
        # Only the main loop is under test; the fake radio/TAP have no receiver.
        with patch.object(link, 'radio_loop'), patch.object(link, 'tap_loop'):
            link.run(seconds=.15)
            self.assertGreaterEqual(link.stats['tx_busy'], 2)
            self.assertFalse(link.radio_down)
            self.assertTrue(any(record['event'] == 'stats' and record.get('tx_busy', 0)
                                for record in logs))
            busy.gone = True
            link.send_mgmt(5, frames.mac_bytes(STATE['cr100_mac']), frames.probe_response_body(0, link.household))
            self.assertTrue(link.radio_down)
            link.run(seconds=.4)
        self.assertIs(link.radio, fresh)
        self.assertFalse(link.radio_down)
        self.assertTrue(any(frame[0] == 0x80 for frame in fresh.sent), 'Beacons did not resume')
        self.assertEqual([record['event'] for record in logs if record['event'].startswith('radio')],
                         ['radio-lost', 'radio-reopened'])


if __name__ == '__main__':
    unittest.main()
