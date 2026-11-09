"""A busy shared transmitter must not consume a sleeping station's poll reply."""
import errno
import unittest
from unittest.mock import patch

import frames
import sonosnet_link as sl
from test_multi_controller_link import STATE, FIRST, SECOND, decode, ethernet, null, poll


class Radio:
    def __init__(self):
        self.sent, self.error = [], None

    def send(self, packet, flags=0):
        if self.error is not None: raise OSError(self.error, 'synthetic backpressure')
        self.sent.append(packet[len(frames.TX_RADIOTAP):])


class PollDelivery(unittest.TestCase):
    def setUp(self):
        clock = patch.object(sl.time, 'monotonic', return_value=100.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.radio = Radio()
        self.link = sl.Link(STATE, self.radio, -1, lambda _: None)
        for station in self.link.stations.values():
            station.associated = True
            station.last_rx = 100.0
        self.station = self.link.stations[FIRST]

    def service(self, now):
        self.clock.return_value = now
        with self.link.lock: self.link.service_stations(now)

    def test_poll_after_beacon_waits_for_pacer_then_sends_only_one_frame(self):
        for marker in (b'first', b'second'):
            self.link.from_tap(ethernet(FIRST, marker))
        self.link.send_beacon()
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertEqual(self.station.stats['ps_poll'], 1)
        self.assertEqual(self.station.stats['tx_data'], 0)
        self.assertEqual(len(self.station.queue), 2)
        self.assertIsNotNone(self.station.poll_pending_until)
        self.assertTrue(self.station.dozing)
        self.service(100.002)
        self.assertEqual(self.station.stats['tx_data'], 1)
        self.assertEqual(len(self.station.queue), 1)
        self.assertIsNone(self.station.poll_pending_until)
        self.assertTrue(self.station.dozing)
        sent = [decode(frame) for frame in self.radio.sent if frame[0] == 8]
        self.assertEqual([(dst, more) for dst, _, more, _ in sent], [(FIRST, True)])
        self.service(100.004)
        self.assertEqual(self.station.stats['tx_data'], 1)

    def test_socket_backpressure_preserves_the_grant_until_one_reply_is_sent(self):
        self.link.from_tap(ethernet(FIRST))
        self.radio.error = errno.EAGAIN
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertIsNotNone(self.station.poll_pending_until)
        self.assertEqual(len(self.station.queue), 1)
        self.radio.error = None
        self.service(100.002)
        self.assertEqual(self.station.stats['tx_data'], 1)
        self.assertIsNone(self.station.poll_pending_until)

    def test_expired_grant_does_not_send_late_or_get_extended_by_repeated_polls(self):
        self.link.from_tap(ethernet(FIRST))
        self.radio.error = errno.EAGAIN
        self.link.on_frame(poll(FIRST, self.station.aid))
        deadline = self.station.poll_pending_until
        self.clock.return_value = 100.02
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertEqual(self.station.poll_pending_until, deadline)
        self.radio.error = None
        self.service(deadline)
        self.assertEqual(self.station.stats['tx_data'], 0)
        self.assertEqual(self.station.stats['tx_poll_expired'], 1)
        self.assertIsNone(self.station.poll_pending_until)
        self.service(deadline + .01)
        self.assertEqual(self.station.stats['tx_data'], 0)
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertEqual(self.station.stats['tx_data'], 1)

    def test_explicit_power_state_supersedes_pending_poll(self):
        self.link.from_tap(ethernet(FIRST))
        self.radio.error = errno.EAGAIN
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertIsNotNone(self.station.poll_pending_until)
        self.clock.return_value = 100.001
        self.link.on_frame(null(FIRST, pm=True))
        self.assertIsNone(self.station.poll_pending_until)
        self.radio.error = None
        self.service(100.002)
        self.assertEqual(self.station.stats['tx_data'], 0)

    def test_poll_grant_does_not_release_another_stations_queue(self):
        for mac in (FIRST, SECOND): self.link.from_tap(ethernet(mac))
        self.link.send_beacon()
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.service(100.002)
        self.assertEqual(self.station.stats['tx_data'], 1)
        self.assertEqual(self.link.stations[SECOND].stats['tx_data'], 0)
        self.assertEqual(len(self.link.stations[SECOND].queue), 1)
        self.assertIsNone(self.link.stations[SECOND].poll_pending_until)

    def test_empty_poll_does_not_authorize_new_traffic_arriving_later(self):
        self.link.on_frame(poll(FIRST, self.station.aid))
        self.assertIsNone(self.station.poll_pending_until)
        self.link.from_tap(ethernet(FIRST))
        self.service(100.002)
        self.assertEqual(self.station.stats['tx_data'], 0)
        self.assertEqual(len(self.station.queue), 1)


if __name__ == '__main__':
    unittest.main()
