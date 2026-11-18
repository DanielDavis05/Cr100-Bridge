"""Minimal one-shot mDNS/DNS-SD browse, without a system daemon.

Sends a legacy-unicast query (from an ephemeral port), so responders answer
directly to this socket (RFC 6762 section 6.7). Used to find speakers'
Spotify Connect (ZeroConf) endpoints. Returns plain data; no network writes
beyond the single query.
"""
from __future__ import annotations

import ipaddress
import random
import socket
import struct
import time

MDNS = ('224.0.0.251', 5353)
PTR, TXT, SRV, A = 12, 16, 33, 1


def _name(data, offset, depth=0):
    labels, jumped, end = [], False, offset
    while True:
        if offset >= len(data) or depth > 20: raise ValueError('bad name')
        length = data[offset]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack_from('!H', data, offset)[0] & 0x3FFF
            if not jumped: end = offset + 2
            offset, jumped, depth = pointer, True, depth + 1
            continue
        if length == 0:
            if not jumped: end = offset + 1
            return '.'.join(labels), end
        labels.append(data[offset + 1:offset + 1 + length].decode('utf-8', 'replace'))
        offset += 1 + length


def _encode(name):
    return b''.join(bytes([len(part)]) + part for part in (p.encode() for p in name.strip('.').split('.'))) + b'\0'


def parse(data):
    """[(name, type, value)] for every answer/additional record we understand."""
    ident, flags, qd, an, ns, ar = struct.unpack_from('!6H', data, 0)
    offset, records = 12, []
    for _ in range(qd):
        _, offset = _name(data, offset)
        offset += 4
    for _ in range(an + ns + ar):
        name, offset = _name(data, offset)
        kind, _, _, length = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        body, start = data[offset:offset + length], offset
        if kind == PTR: records.append((name, PTR, _name(data, start)[0]))
        elif kind == SRV:
            _, _, port = struct.unpack_from('!HHH', data, start)
            records.append((name, SRV, (_name(data, start + 6)[0], port)))
        elif kind == A and length == 4: records.append((name, A, socket.inet_ntoa(body)))
        elif kind == TXT:
            items, i = {}, 0
            while i < len(body):
                size = body[i]; text = body[i + 1:i + 1 + size].decode('utf-8', 'replace'); i += 1 + size
                key, _, value = text.partition('=')
                if key: items[key] = value
            records.append((name, TXT, items))
        offset += length
    return records


def browse(service, timeout=2.0, sock_factory=None):
    """[{'instance', 'host', 'address', 'port', 'txt'}] for a service like _spotify-connect._tcp.local."""
    sock = (sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))()
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        query = struct.pack('!6H', random.randrange(1, 65535), 0, 1, 0, 0, 0) + _encode(service) + struct.pack('!HH', PTR, 1)
        sock.sendto(query, MDNS)
        records, deadline = [], time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: break
            sock.settimeout(remaining)
            try: data, _ = sock.recvfrom(9000)
            except socket.timeout: break
            try: records += parse(data)
            except (ValueError, struct.error, IndexError): continue
    finally:
        sock.close()
    service = service.strip('.').lower()
    found = []
    for name, kind, value in records:
        if kind != PTR or name.lower() != service: continue
        srv = next((v for n, k, v in records if k == SRV and n.lower() == value.lower()), None)
        if srv is None: continue
        txt = next((v for n, k, v in records if k == TXT and n.lower() == value.lower()), {})
        address = next((v for n, k, v in records if k == A and n.lower() == srv[0].lower()), None)
        try:
            if address is None or not ipaddress.ip_address(address).is_private: continue
        except ValueError:
            continue
        instance = value[:-len(service) - 1] if value.lower().endswith('.' + service) else value
        if not any(item['instance'] == instance for item in found):
            found.append({'instance': instance, 'host': srv[0], 'address': address, 'port': srv[1], 'txt': txt})
    return found
