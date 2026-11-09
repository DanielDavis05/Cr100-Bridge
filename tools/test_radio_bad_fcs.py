"""Corrupt monitor frames must not reach the controller state machine."""
import struct
import unittest
from unittest.mock import Mock, patch

import frames
import sonosnet_link as sl
from test_multi_controller_link import STATE, FIRST, management


class BadFrameChecksum(unittest.TestCase):
    def packet(self, *, fcs_present=False):
        flags = 0x40 | (0x10 if fcs_present else 0)
        header = struct.pack('<BBHIB', 0, 0, 9, 2, flags)
        association = management(FIRST, 0, bytes.fromhex('1003f401dd06000e58020101'))
        return header + association + (bytes(4) if fcs_present else b'')

    def test_bad_checksum_is_rejected_with_or_without_a_captured_fcs(self):
        for present in (False, True):
            with self.subTest(fcs_present=present), self.assertRaisesRegex(ValueError, 'checksum'):
                frames.parse_radiotap(self.packet(fcs_present=present))

    def test_radio_loop_counts_bad_fcs_without_changing_association_state(self):
        radio = Mock()
        link = sl.Link(STATE, radio, -1, lambda _: None)
        def receive(_):
            link.stop.set()
            return self.packet()
        radio.recv.side_effect = receive
        with patch.object(sl.select, 'select', return_value=([radio], [], [])):
            link.radio_loop()
        self.assertFalse(link.stations[FIRST].associated)
        self.assertEqual(link.stations[FIRST].stats['assoc_req'], 0)
        self.assertEqual(link.stats['rx_bad_fcs'], 1)
        self.assertEqual(link.stats['rx_malformed_radiotap'], 0)


if __name__ == '__main__':
    unittest.main()
