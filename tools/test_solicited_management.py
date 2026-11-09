"""Solicited management replies survive brief backpressure without stale timestamps."""
import copy
import errno
import socket
import struct
import unittest
from unittest.mock import patch

import frames
import sonosnet_link as sl
from test_multi_controller_link import STATE, AP, FIRST, SECOND, data, ethernet, management


CAPTURED_ASSOCIATION = bytes.fromhex('1003f401dd06000e58020101')
REASSOCIATION = CAPTURED_ASSOCIATION[:4] + AP + CAPTURED_ASSOCIATION[4:]


class Radio:
    def __init__(self): self.busy, self.sent = False, []
    def send(self, packet, flags=0):
        if self.busy: raise OSError(errno.EAGAIN, 'synthetic busy radio')
        self.sent.append(packet[len(frames.TX_RADIOTAP):])


class SolicitedManagement(unittest.TestCase):
    def setUp(self):
        clock = patch.object(sl.time, 'monotonic', return_value=100.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        tap, self.receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close)
        self.addCleanup(self.receiver.close)
        self.receiver.setblocking(False)
        self.radio = Radio()
        self.link = sl.Link(STATE, self.radio, tap.fileno(), lambda _: None)
        for station in self.link.stations.values():
            station.associated, station.last_rx = True, 100.0
        self.station = self.link.stations[FIRST]

    def service(self, now):
        self.clock.return_value = now
        with self.link.lock: self.link.service_stations(now)

    def test_busy_repeated_requests_coalesce_into_two_reply_types(self):
        self.radio.busy = True
        for _ in range(20):
            self.link.on_frame(management(FIRST, 0, CAPTURED_ASSOCIATION))
            self.link.on_frame(management(FIRST, 4))
        self.assertEqual(set(self.station.management_replies), {'association', 'probe'})
        self.assertEqual(self.radio.sent, [])
        self.radio.busy = False
        self.service(100.003)
        self.service(100.005)
        self.assertEqual([frame[0] >> 4 for frame in self.radio.sent], [1, 5])
        self.assertEqual([frame[4:10] for frame in self.radio.sent], [FIRST, FIRST])
        self.assertFalse(self.station.management_replies)
        self.assertEqual(self.station.stats['tx_mgmt_sent'], 2)
        self.assertFalse(self.link.stations[SECOND].management_replies)

    def test_probe_timestamp_is_rebuilt_when_a_deferred_reply_can_be_sent(self):
        self.radio.busy = True
        self.link.on_frame(management(FIRST, 4))
        self.radio.busy = False
        self.service(100.4)
        self.assertEqual(len(self.radio.sent), 1)
        self.assertEqual(struct.unpack_from('<Q', self.radio.sent[0], 24)[0], self.link.air_tsf())
        self.assertGreater(struct.unpack_from('<Q', self.radio.sent[0], 24)[0], 300000)

    def test_reply_expires_instead_of_transmitting_after_the_request_window(self):
        self.radio.busy = True
        self.link.on_frame(management(FIRST, 4))
        self.radio.busy = False
        self.service(100.0 + sl.MANAGEMENT_REPLY_TTL)
        self.assertEqual(self.radio.sent, [])
        self.assertFalse(self.station.management_replies)
        self.assertEqual(self.station.stats['tx_mgmt_expired'], 1)

    def test_association_and_reassociation_share_one_slot_and_departure_clears_it(self):
        self.radio.busy = True
        self.link.on_frame(management(FIRST, 0, CAPTURED_ASSOCIATION))
        self.link.on_frame(management(FIRST, 2, REASSOCIATION))
        self.assertEqual(self.station.management_replies, {'association': (100.0, 3)})
        self.link.on_frame(management(FIRST, 12, b'\x03\0'))
        self.assertFalse(self.station.management_replies)
        self.radio.busy = False
        self.service(100.002)
        # Proactive search may follow a genuine departure; the old solicited
        # association reply must never reappear.
        self.assertFalse(any(frame[0] >> 4 in (1, 3) for frame in self.radio.sent))

    def test_truncated_fixed_fields_or_information_elements_cannot_mutate_state(self):
        self.station.rx_last_pn = 100
        self.station.searching = True
        self.station.poll_pending_until = 100.04
        cases = [(0, CAPTURED_ASSOCIATION[:size]) for size in range(4)]
        cases += [(2, REASSOCIATION[:size]) for size in range(10)]
        cases += [(0, CAPTURED_ASSOCIATION[:4] + b'\xdd'),
                  (0, CAPTURED_ASSOCIATION[:-1]), (2, REASSOCIATION[:-1])]
        for subtype, body in cases:
            with self.subTest(subtype=subtype, length=len(body)):
                before = copy.deepcopy(self.station.__dict__)
                self.link.on_frame(management(FIRST, subtype, body))
                self.assertEqual(self.station.__dict__, before)
        self.assertEqual(self.radio.sent, [])

    def test_captured_vendor_only_body_and_fully_framed_optional_ies_are_accepted(self):
        for subtype, body in ((0, CAPTURED_ASSOCIATION), (2, REASSOCIATION),
                              (0, CAPTURED_ASSOCIATION + b'\x00\0')):
            with self.subTest(subtype=subtype):
                self.station.associated = False
                self.link.on_frame(management(FIRST, subtype, body))
                self.assertTrue(self.station.associated)
        self.assertEqual(self.station.stats['assoc_req'], 3)

    def test_malformed_association_does_not_reset_packet_replay_protection(self):
        packet = data(FIRST, ethernet(AP, source=FIRST), 9)
        self.link.on_frame(packet)
        self.receiver.recv(4096)
        self.link.on_frame(management(FIRST, 0))
        self.link.on_frame(packet)
        with self.assertRaises(BlockingIOError): self.receiver.recv(4096)
        self.assertEqual(self.station.rx_last_pn, 9)
        self.assertEqual(self.station.stats['rx_replay'], 1)

    def test_valid_legacy_association_still_allows_a_rebooted_counter_to_restart(self):
        self.link.on_frame(data(FIRST, ethernet(AP, b'before', source=FIRST), 99))
        self.receiver.recv(4096)
        self.link.on_frame(management(FIRST, 0, CAPTURED_ASSOCIATION))
        self.assertEqual(self.station.rx_last_pn, -1)
        self.link.on_frame(data(FIRST, ethernet(AP, b'after', source=FIRST), 1))
        self.assertEqual(self.receiver.recv(4096), ethernet(AP, b'after', source=FIRST))
        # This recovery mechanism is unauthenticated: a well-formed forged
        # association can also reset the replay epoch under the legacy key.


if __name__ == '__main__': unittest.main()
