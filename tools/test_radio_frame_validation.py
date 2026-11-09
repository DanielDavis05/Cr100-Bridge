"""Incomplete unprotected radio frames cannot change controller state."""
import copy
import unittest
from unittest.mock import patch

import sonosnet_link as sl
from test_multi_controller_link import STATE, FIRST, management, null, poll
from test_solicited_management import CAPTURED_ASSOCIATION, Radio


class StructuralRadioValidation(unittest.TestCase):
    def setUp(self):
        self.link = sl.Link(STATE, Radio(), -1, lambda _: None)
        self.link.start = 100
        self.station = self.link.stations[FIRST]
        self.station.associated, self.station.searching = True, True
        self.station.rx_last_pn = 99
        self.station.poll_pending_until = 100.04
        self.station.last_rx = 90

    def assert_ignored(self, packet):
        before = copy.deepcopy(self.station.__dict__)
        with patch.object(sl.time, 'monotonic', return_value=100): self.link.on_frame(packet)
        self.assertEqual(self.station.__dict__, before)

    def test_undefined_frame_control_versions_do_not_change_state(self):
        packets = (management(FIRST, 0, CAPTURED_ASSOCIATION), management(FIRST, 4),
                   null(FIRST), poll(FIRST, self.station.aid))
        for packet in packets:
            for version in (1, 2, 3):
                with self.subTest(frame=packet[0], version=version):
                    self.assert_ignored(bytes((packet[0] | version,)) + packet[1:])

    def test_departure_requires_a_complete_reason_code(self):
        for subtype in (10, 12):
            for body in (b'', b'\x03'):
                with self.subTest(subtype=subtype, body=body):
                    self.assert_ignored(management(FIRST, subtype, body))
        with patch.object(sl.time, 'monotonic', return_value=100):
            self.link.on_frame(management(FIRST, 12, b'\x03\0'))
        self.assertFalse(self.station.associated)
        self.assertIsNone(self.station.poll_pending_until)

    def test_probe_request_requires_complete_ies_but_no_standard_optional_tags(self):
        for body in (b'\x00', b'\x00\x05ab', b'\xdd\x06short'):
            with self.subTest(body=body): self.assert_ignored(management(FIRST, 4, body))
        for body in (b'', b'\x00\0', bytes.fromhex('dd06000e58020101')):
            with self.subTest(valid_body=body), patch.object(sl.time, 'monotonic', return_value=100):
                self.link.on_frame(management(FIRST, 4, body))
        self.assertEqual(self.station.stats['probe_req'], 3)

    def test_qos_null_needs_its_two_byte_qos_control_field(self):
        header = bytes((0xc8,)) + null(FIRST)[1:]
        for suffix in (b'', b'\0'):
            with self.subTest(length=len(header + suffix)): self.assert_ignored(header + suffix)
        with patch.object(sl.time, 'monotonic', return_value=100):
            self.link.on_frame(header + b'\0\0')
        self.assertEqual(self.station.last_rx, 100)
        self.assertFalse(self.station.searching)
        self.assertTrue(self.station.dozing)


if __name__ == '__main__': unittest.main()
