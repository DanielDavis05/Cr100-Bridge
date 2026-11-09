"""Malformed capture metadata and packet summaries must not terminate a radio worker."""
import random
import struct
import unittest

import frames

FRAME = bytes.fromhex('a4110100020000000001020000000002')
COOKIE = bytes(236) + b'\x63\x82\x53\x63'
IP_SUMMARY = {'ip_src': '192.0.2.1', 'ip_dst': '255.255.255.255', 'ip_proto': 17}


def radiotap(frame=FRAME, fcs=False, extended=False):
    # TSFT, flags, rate, channel, and antenna signal, with alignment relative to
    # the start of the radiotap header even when another presence word precedes them.
    presence = 0x2f | (0x80000000 if extended else 0)
    fields = bytearray(struct.pack('<I', presence))
    if extended: fields += struct.pack('<I', 0)
    fields += bytes(-(4 + len(fields)) % 8)
    fields += struct.pack('<QBBHHb', 123456, 0x10 if fcs else 0, 48, 2437, 0x00c0, -52)
    return struct.pack('<BBH', 0, 0, 4 + len(fields)) + fields + frame + (b'FCS!' if fcs else b'')


def ipv4(body, protocol=17, options=b'', fragment=0, total_length=None):
    header = bytearray(20 + len(options))
    header[0], header[8], header[9] = 0x40 | len(header) // 4, 64, protocol
    struct.pack_into('!H', header, 2, len(header) + len(body) if total_length is None else total_length)
    struct.pack_into('!H', header, 6, fragment)
    header[12:16], header[16:20], header[20:] = bytes((192, 0, 2, 1)), b'\xff' * 4, options
    return bytes(header) + body


def udp(body, sport=68, dport=67, length=None):
    return struct.pack('!HHHH', sport, dport, 8 + len(body) if length is None else length, 0) + body


def dhcp(options):
    return ipv4(udp(COOKIE + options))


class RadiotapBounds(unittest.TestCase):
    def test_valid_metadata_alignment_and_fcs_preserve_frame_and_signal(self):
        for extended in (False, True):
            for fcs in (False, True):
                self.assertEqual(frames.parse_radiotap(radiotap(fcs=fcs, extended=extended)), (FRAME, -52))
        self.assertEqual(frames.parse_radiotap(frames.MIN_RADIOTAP + FRAME), (FRAME, None))

    def test_declared_header_bounds_cannot_be_completed_by_frame_bytes(self):
        packet = radiotap(extended=True)
        header_length = struct.unpack_from('<H', packet, 2)[0]
        for length in range(header_length):
            malformed = bytearray(packet)
            struct.pack_into('<H', malformed, 2, length)
            with self.subTest(length=length), self.assertRaises(ValueError):
                frames.parse_radiotap(malformed)
        for length in (len(packet) + 1, 0xffff):
            malformed = bytearray(packet)
            struct.pack_into('<H', malformed, 2, length)
            with self.subTest(length=length), self.assertRaises(ValueError):
                frames.parse_radiotap(malformed)

    def test_missing_flags_and_truncated_extended_presence_raise_value_error(self):
        malformed = [struct.pack('<BBHI', 0, 0, 8, 2),
                     struct.pack('<BBHI', 0, 0, 8, 0x80000000),
                     struct.pack('<BBHI', 0, 0, 11, 0x80000000) + bytes(3),
                     struct.pack('<BBHII', 0, 0, 12, 0x80000000, 0x80000000)]
        for packet in malformed:
            # Appending a complete 802.11 frame cannot complete header metadata.
            for suffix in (b'', FRAME):
                with self.subTest(packet=packet.hex(), suffix=len(suffix)), self.assertRaises(ValueError):
                    frames.parse_radiotap(packet + suffix)

    def test_advertised_fcs_must_fit_in_captured_frame(self):
        header = struct.pack('<BBHIB', 0, 0, 9, 2, 0x10)
        for length in range(4):
            with self.subTest(length=length), self.assertRaises(ValueError):
                frames.parse_radiotap(header + bytes(length))
        self.assertEqual(frames.parse_radiotap(header + FRAME + b'FCS!'), (FRAME, None))

    def test_all_truncations_and_small_metadata_fuzz_have_bounded_error_outcomes(self):
        fixtures = [radiotap(), radiotap(fcs=True), radiotap(extended=True), frames.MIN_RADIOTAP + FRAME]
        cases = [packet[:end] for packet in fixtures for end in range(len(packet) + 1)]
        rng = random.Random(0x6969)
        for _ in range(1000):
            packet = bytearray(rng.choice(fixtures))
            for _ in range(rng.randrange(1, 5)):
                packet[rng.randrange(len(packet))] = rng.randrange(256)
            cases.append(bytes(packet[:rng.randrange(len(packet) + 1)]))
        cases.extend(rng.randbytes(rng.randrange(96)) for _ in range(500))
        for index, packet in enumerate(cases):
            try:
                frame, signal = frames.parse_radiotap(packet)
            except ValueError:
                continue
            with self.subTest(index=index):
                self.assertIsInstance(frame, bytes)
                self.assertTrue(signal is None or isinstance(signal, int) and -128 <= signal <= 127)


class PacketSummaryBounds(unittest.TestCase):
    def test_valid_dhcp_with_ip_options_padding_and_end_marker_keeps_summary(self):
        options = b'\0\x35\x01\x01\x0c\x06living\xff\x35\x01\x02'
        packet = ipv4(udp(COOKIE + options), options=b'\x01\x01\0\0') + b'ethernet-padding'
        self.assertEqual(frames.describe_inner(0x0800, packet),
                         {**IP_SUMMARY, 'udp_sport': 68, 'udp_dport': 67,
                          'dhcp_type': 1, 'dhcp_hostname': 'living'})

    def test_invalid_ip_headers_and_udp_lengths_do_not_read_other_fields(self):
        valid = ipv4(udp(b''))
        malformed = [b'', valid[:19]]
        for first in (0x65, 0x40, 0x44, 0x4f):
            malformed.append(bytes((first,)) + valid[1:])
        for length in (0, 19, 29, 0xffff):
            packet = bytearray(valid)
            struct.pack_into('!H', packet, 2, length)
            malformed.append(packet)
        for packet in malformed:
            with self.subTest(packet=bytes(packet).hex()):
                self.assertEqual(frames.describe_inner(0x0800, packet), {})
        for length in (0, 1, 7, 9, 0xffff):
            with self.subTest(udp_length=length):
                self.assertEqual(frames.describe_inner(0x0800, ipv4(udp(b'', length=length))), IP_SUMMARY)
        self.assertEqual(frames.describe_inner(0x0806, valid), {})

    def test_fragment_offsets_and_transport_length_exclude_false_udp_and_icmp(self):
        self.assertEqual(frames.describe_inner(0x0800, ipv4(udp(COOKIE), fragment=1)), IP_SUMMARY)
        packet = ipv4(b'\x08\0', protocol=1)
        self.assertEqual(frames.describe_inner(0x0800, packet), {**IP_SUMMARY, 'ip_proto': 1, 'icmp_type': 8})
        # An Ethernet pad byte after the declared IPv4 packet is not ICMP data.
        packet = ipv4(b'\x08', protocol=1) + b'\0'
        self.assertEqual(frames.describe_inner(0x0800, packet), {**IP_SUMMARY, 'ip_proto': 1})

    def test_dhcp_truncated_or_invalid_option_values_are_not_reported(self):
        invalid = [b'\x35', b'\x35\x01', b'\x35\0', b'\x35\x02\x01',
                   b'\x35\x02\x01\x02\xff', b'\x0c', b'\x0c\xffshort']
        for options in invalid:
            with self.subTest(options=options.hex()):
                self.assertEqual(frames.describe_inner(0x0800, dhcp(options)),
                                 {**IP_SUMMARY, 'udp_sport': 68, 'udp_dport': 67})
        # Retain complete earlier values when a later option is truncated.
        result = frames.describe_inner(0x0800, dhcp(b'\x35\x01\x02\x0c\x06short'))
        self.assertEqual(result['dhcp_type'], 2)
        self.assertNotIn('dhcp_hostname', result)

    def test_dhcp_options_are_bounded_by_udp_length_not_trailing_ip_payload(self):
        body = COOKIE + b'\x35\x01\x02\x0c\x04fake'
        packet = ipv4(udp(body, length=8 + len(COOKIE)))
        self.assertEqual(frames.describe_inner(0x0800, packet),
                         {**IP_SUMMARY, 'udp_sport': 68, 'udp_dport': 67})

    def test_every_option_truncation_and_small_dhcp_fuzz_stays_safe(self):
        options = b'\0\x35\x01\x01\x0c\x06living\xff'
        for end in range(len(options) + 1):
            result = frames.describe_inner(0x0800, dhcp(options[:end]))
            self.assertEqual(result.get('dhcp_type'), 1 if end >= 4 else None)
            self.assertEqual(result.get('dhcp_hostname'), 'living' if end >= 12 else None)
        # Include every possible option tag and pathological length, rather than
        # relying on random inputs to reach the DHCP parser behind its cookie.
        cases = [bytes((tag, length)) + b'x' * available
                 for tag in range(256) for length in (0, 1, 255) for available in (0, 1)]
        rng = random.Random(0x43523130)
        cases.extend(rng.randbytes(rng.randrange(65)) for _ in range(1000))
        for options in cases:
            self.assertIsInstance(frames.describe_inner(0x0800, dhcp(options)), dict)
        for _ in range(500):
            self.assertIsInstance(frames.describe_inner(0x0800, rng.randbytes(rng.randrange(300))), dict)


if __name__ == '__main__': unittest.main()
