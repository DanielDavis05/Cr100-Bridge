#!/usr/bin/env python3
"""Offline tests for the one-shot mDNS browse used to find Spotify Connect speakers."""
import socket
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zeroconf_browse
from zeroconf_browse import A, PTR, SRV, TXT, _encode, browse, parse

SERVICE = '_spotify-connect._tcp.local'


def record(name, kind, body):
    return _encode(name) + struct.pack('!HHIH', kind, 1, 120, len(body)) + body


def response(instance='Bose', address='10.0.0.147', port=8200, path='/zc'):
    full = '%s.%s' % (instance, SERVICE)
    txt = b''.join(bytes([len(t)]) + t for t in (b'VERSION=1.0', ('CPath=' + path).encode()))
    answers = [record(SERVICE, PTR, _encode(full)),
               record(full, SRV, struct.pack('!HHH', 0, 0, port) + _encode(instance.lower() + '.local')),
               record(full, TXT, txt),
               record(instance.lower() + '.local', A, socket.inet_aton(address))]
    return struct.pack('!6H', 0, 0x8400, 0, len(answers), 0, 0) + b''.join(answers)


class FakeSocket:
    def __init__(self, packets):
        self.packets, self.sent = list(packets), []
    def setsockopt(self, *args): pass
    def sendto(self, data, address): self.sent.append((data, address))
    def settimeout(self, value): pass
    def close(self): pass
    def recvfrom(self, size):
        if not self.packets: raise socket.timeout()
        return self.packets.pop(0), ('10.0.0.147', 5353)


class ZeroconfBrowse(unittest.TestCase):
    def test_parse_and_join_records(self):
        records = parse(response())
        self.assertIn((SERVICE, PTR, 'Bose.' + SERVICE), records)
        sock = FakeSocket([response(), b'garbage', response('Public', '8.8.8.8')])
        found = browse(SERVICE, timeout=.5, sock_factory=lambda: sock)
        self.assertEqual(found, [{'instance': 'Bose', 'host': 'bose.local', 'address': '10.0.0.147',
                                  'port': 8200, 'txt': {'VERSION': '1.0', 'CPath': '/zc'}}])
        query, destination = sock.sent[0]
        self.assertEqual(destination, zeroconf_browse.MDNS)
        self.assertIn(_encode(SERVICE), query)

    def test_compressed_names(self):
        header = struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0)
        owner = _encode(SERVICE)
        pointer = struct.pack('!H', 0xC000 | 12)          # back to the owner name
        body = b'\x04Bose' + pointer
        packet = header + owner + struct.pack('!HHIH', PTR, 1, 120, len(body)) + body
        self.assertEqual(parse(packet), [(SERVICE, PTR, 'Bose.' + SERVICE)])


if __name__ == '__main__':
    unittest.main()
