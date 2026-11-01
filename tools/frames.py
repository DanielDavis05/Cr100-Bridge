"""SonosNet frame layouts for the bridge AP, copied from the ZP80.

Sources: 'Physical CR100 Boot (channel 6).pcap.pcap' beacon 1681, probe response
1649, association response 1667 and ZP80 -> CR100 data (FC 08 42 / 08 62).
"""
from __future__ import annotations

import ipaddress
import struct

BEACON_INTERVAL_TU = 100
BEACON_CAPABILITY = 0x0431  # ZP80 beacon: ESS, privacy, short preamble, short slot
PROBE_CAPABILITY = 0x0310   # ZP80 probe and association responses
RATES = bytes.fromhex('02040b160c121824')
EXT_RATES = bytes.fromhex('3048606c')
RSN = bytes.fromhex('0100000fac040100000fac040100000fac020000')
SONOS_IE = bytes.fromhex('000e58020101')
BROADCAST = b'\xff' * 6
# Version 0, length 8, nothing present: the firmware picks the rate. A zero length
# field makes nexmon drop the frame silently. The radiotap NOACK TX flag is ignored;
# the firmware still retries unicast ~7 times (measured 2026-10-31).
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
    bitmap = bytearray(8)
    if tim_aid: bitmap[tim_aid // 8] |= 1 << (tim_aid % 8)
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
    """Return (802.11 frame without FCS, rssi dBm or None) from a radiotap-framed capture."""
    if len(packet) < 8 or packet[0] != 0: raise ValueError('Not radiotap')
    length = struct.unpack_from('<H', packet, 2)[0]
    present, offset, words = [], 4, 0
    while True:
        word = struct.unpack_from('<I', packet, offset)[0]
        present.append(word); offset += 4; words += 1
        if not word & 0x80000000: break
    flags, rssi, bits = 0, None, present[0]
    # Fields in bit order with (alignment, size); stop after antenna signal (bit 5).
    for bit, (align, size) in enumerate(((8, 8), (1, 1), (1, 1), (2, 4), (1, 2), (1, 1))):
        if not bits & (1 << bit): continue
        offset = (offset + align - 1) & ~(align - 1)
        if bit == 1: flags = packet[offset]
        if bit == 5: rssi = struct.unpack_from('b', packet, offset)[0]
        offset += size
    frame = packet[length:]
    if flags & 0x10: frame = frame[:-4]  # FCS present
    return frame, rssi


def describe_inner(ethertype, payload):
    """Short summary of the tunnelled Ethernet payload for the log."""
    if ethertype != 0x0800 or len(payload) < 20: return {}
    ihl = (payload[0] & 0x0f) * 4
    proto = payload[9]
    out = {'ip_src': str(ipaddress.IPv4Address(payload[12:16])),
           'ip_dst': str(ipaddress.IPv4Address(payload[16:20])), 'ip_proto': proto}
    if proto == 17 and len(payload) >= ihl + 8:
        out['udp_sport'], out['udp_dport'] = struct.unpack_from('!HH', payload, ihl)
        dhcp = payload[ihl + 8:]
        if (out['udp_dport'] in (67, 68) or out['udp_sport'] in (67, 68)) and len(dhcp) > 240 \
                and dhcp[236:240] == b'\x63\x82\x53\x63':
            i = 240
            while i + 1 < len(dhcp) and dhcp[i] != 255:
                if dhcp[i] == 0: i += 1; continue
                if dhcp[i] == 53: out['dhcp_type'] = dhcp[i + 2]
                if dhcp[i] == 12: out['dhcp_hostname'] = dhcp[i + 2:i + 2 + dhcp[i + 1]].decode('latin1')
                i += 2 + dhcp[i + 1]
    elif proto == 1 and len(payload) >= ihl + 2:
        out['icmp_type'] = payload[ihl]
    return out
