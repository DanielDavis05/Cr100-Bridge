"""SonosNet frame layouts for the bridge AP, copied from the ZP80.

Sources: 'Physical CR100 Boot (channel 6).pcap.pcap' beacon 1681, probe response
1649, association response 1667 and ZP80 -> CR100 data (FC 08 42 / 08 62).
"""
from __future__ import annotations

import ipaddress
import struct


class BadFrameChecksum(ValueError):
    """The monitor metadata marks an over-the-air frame as corrupt."""


BEACON_INTERVAL_TU = 100
BEACON_CAPABILITY = 0x0431  # ZP80 beacon: ESS, privacy, short preamble, short slot
PROBE_CAPABILITY = 0x0310   # ZP80 probe and association responses
RATES = bytes.fromhex('02040b160c121824')
EXT_RATES = bytes.fromhex('3048606c')
RSN = bytes.fromhex('0100000fac040100000fac040100000fac020000')
SONOS_IE = bytes.fromhex('000e58020101')
BROADCAST = b'\xff' * 6
# Version 0, length 8, nothing present: the firmware picks the rate. A zero length
# field makes Nexmon drop the frame silently. Its injector ignores radiotap NOACK.
# The owned firmware separately marks valid unicast probe-response clones with the
# native NoAck tag; ordinary unicast retains hardware ACK/retry handling.
MIN_RADIOTAP = bytes.fromhex('0000080000000000')
# What the bridge transmits with: flags + rate, 24 Mb/s (48 x 500 kb/s). The firmware's own
# choice is 1 Mb/s (ACK rates from a neighbouring AP, rate_test.py ack, 2026-10-31), 24x the
# airtime per frame; the ZP80 sends everything to the CR100 at 24 (boot capture).
TX_RATE_MBPS = 24
TX_RADIOTAP = struct.pack('<BBHIBB', 0, 0, 10, 0x6, 0, TX_RATE_MBPS * 2)


def mac_bytes(text):
    return bytes.fromhex(text.replace(':', ''))


def mac_text(raw):
    return ':'.join(f'{b:02x}' for b in raw)


def ie(tag, value):
    return bytes((tag, len(value))) + value


def beacon_body(tsf, channel, tim_aid=None):
    """Build a TIM for one AID (legacy callers) or an iterable of station AIDs."""
    aids = [] if tim_aid is None else [tim_aid] if isinstance(tim_aid, int) else list(tim_aid)
    if any(isinstance(aid, bool) or not isinstance(aid, int) or not 1 <= aid <= 2007 for aid in aids):
        raise ValueError('TIM association IDs must be in 1..2007')
    # Retain the ZP80's eight-byte bitmap for small households; extend it for higher
    # AIDs. The full 2008-bit virtual bitmap fits in one information element.
    bitmap = bytearray(max(8, max(aids, default=0) // 8 + 1))
    for aid in aids: bitmap[aid // 8] |= 1 << (aid % 8)
    tim = bytes((0, 1, 0)) + bytes(bitmap)  # DTIM count 0, period 1, as the ZP80
    return (struct.pack('<QHH', tsf, BEACON_INTERVAL_TU, BEACON_CAPABILITY) + ie(0, b'\0') + ie(1, RATES)
            + ie(3, bytes((channel,))) + ie(5, tim) + ie(42, b'\0') + ie(50, EXT_RATES) + ie(48, RSN)
            + ie(221, SONOS_IE))


def probe_response_body(tsf, household):
    # The CR100 requires the SSID IE first, at frame offset 36, matching exactly.
    return struct.pack('<QHH', tsf, BEACON_INTERVAL_TU, PROBE_CAPABILITY) + ie(0, household)


def association_response_body(aid):
    return struct.pack('<HHH', PROBE_CAPABILITY, 0, 0xc000 | aid)


def mgmt_header(subtype, addr1, bssid, seq, from_ds=False):
    return bytes((subtype << 4, 0x02 if from_ds else 0)) + b'\0\0' + addr1 + bssid + bssid + struct.pack('<H', (seq & 0xfff) << 4)


def data_header(addr1, bssid, seq, more_data=False):
    """AP -> CR100 data: FromDS + Protected (+ MoreData), addr3 = AP, as the ZP80."""
    return bytes((0x08, 0x42 | (0x20 if more_data else 0))) + b'\0\0' + addr1 + bssid + bssid + struct.pack('<H', (seq & 0xfff) << 4)


def parse_radiotap(packet):
    """Return (802.11 frame without FCS, rssi dBm or None), or raise ValueError.

    Presence words and metadata must fit inside the declared radiotap header;
    bytes belonging to the 802.11 frame cannot complete truncated metadata.
    """
    if len(packet) < 8 or packet[0] != 0: raise ValueError('Not radiotap')
    length = struct.unpack_from('<H', packet, 2)[0]
    if not 8 <= length <= len(packet): raise ValueError('Invalid radiotap header length')
    present, offset = [], 4
    while True:
        if offset + 4 > length: raise ValueError('Truncated radiotap presence word')
        word = struct.unpack_from('<I', packet, offset)[0]
        present.append(word); offset += 4
        if not word & 0x80000000: break
    flags, rssi, bits = 0, None, present[0]
    # Fields in bit order with (alignment, size); stop after antenna signal (bit 5).
    for bit, (align, size) in enumerate(((8, 8), (1, 1), (1, 1), (2, 4), (1, 2), (1, 1))):
        if not bits & (1 << bit): continue
        offset = (offset + align - 1) & ~(align - 1)
        if offset + size > length: raise ValueError('Truncated radiotap field')
        if bit == 1: flags = packet[offset]
        if bit == 5: rssi = struct.unpack_from('b', packet, offset)[0]
        offset += size
    frame = packet[length:]
    if flags & 0x40: raise BadFrameChecksum('Radiotap reports an invalid frame checksum')
    if flags & 0x10:
        if len(frame) < 4: raise ValueError('Truncated radiotap FCS')
        frame = frame[:-4]
    return frame, rssi


def describe_inner(ethertype, payload):
    """Short summary of the tunnelled Ethernet payload for the log."""
    if ethertype != 0x0800 or len(payload) < 20 or payload[0] >> 4 != 4: return {}
    ihl = (payload[0] & 0x0f) * 4
    length = struct.unpack_from('!H', payload, 2)[0]
    if not 20 <= ihl <= length <= len(payload): return {}
    proto = payload[9]
    out = {'ip_src': str(ipaddress.IPv4Address(payload[12:16])),
           'ip_dst': str(ipaddress.IPv4Address(payload[16:20])), 'ip_proto': proto}
    # Later IP fragments do not start with a transport header.
    if struct.unpack_from('!H', payload, 6)[0] & 0x1fff: return out
    if proto == 17 and length >= ihl + 8:
        sport, dport, udp_length = struct.unpack_from('!HHH', payload, ihl)
        if not 8 <= udp_length <= length - ihl: return out
        out.update(udp_sport=sport, udp_dport=dport)
        dhcp = payload[ihl + 8:ihl + udp_length]
        if (out['udp_dport'] in (67, 68) or out['udp_sport'] in (67, 68)) and len(dhcp) >= 240 \
                and dhcp[236:240] == b'\x63\x82\x53\x63':
            i = 240
            while i < len(dhcp) and dhcp[i] != 255:
                if dhcp[i] == 0: i += 1; continue
                if i + 2 > len(dhcp): break
                tag, size = dhcp[i], dhcp[i + 1]
                end = i + 2 + size
                if end > len(dhcp): break
                if tag == 53 and size == 1: out['dhcp_type'] = dhcp[i + 2]
                if tag == 12: out['dhcp_hostname'] = dhcp[i + 2:end].decode('latin1')
                i = end
    elif proto == 1 and length >= ihl + 2:
        out['icmp_type'] = payload[ihl]
    return out
