"""CR100-specific IP/UDP Netstart responder for an isolated software player.

No CA identity is forged: the controller's public key is used only to encrypt
a synthetic network key. The peer public-key field contains a newly generated
public key, with no certificate/signature. Static analysis of the CR100 join
path found this signature optional and unused; live compatibility is checked
by the original controller runtime, not assumed by this codec.
"""
from __future__ import annotations

from pathlib import Path
import secrets
import socket
import struct
import threading
import time

PUBLIC_KEY = (Path(__file__).resolve().parent / 'fixtures/synthetic_public_key.bin').read_bytes()
NETWORK_KEY = bytes.fromhex('00112233445566778899aabbccddeeff')
RESPONSE_CACHE_LIMIT = 8
RESPONSE_CACHE_TTL = 30.0
RESPONSE_CACHE_PACKET_LIMIT = 4096


def encode(message_type, fields, header=1):
    result = bytearray((header, message_type))
    for tag, value in fields:
        result += struct.pack('!BH', tag, len(value)) + value
    return bytes(result)


def decode(packet):
    if len(packet) < 2: raise ValueError('Truncated header')
    fields, offset = {}, 2
    while offset < len(packet):
        if len(packet) - offset < 3: raise ValueError('Truncated field header')
        tag, size = struct.unpack_from('!BH', packet, offset); offset += 3
        if size > len(packet) - offset or tag in fields: raise ValueError('Truncated or duplicate field')
        fields[tag] = packet[offset:offset + size]; offset += size
    return packet[0], packet[1], fields


def rsa_encrypt_key(public_key, message):
    if len(public_key) != 260 or struct.unpack_from('<I', public_key)[0] != 1024:
        raise ValueError('Expected a 1024-bit RSAref public key')
    modulus = int.from_bytes(public_key[4:132], 'big')
    exponent = int.from_bytes(public_key[132:260], 'big')
    if modulus.bit_length() != 1024 or not 3 <= exponent <= 0xffffffff or not exponent & 1:
        raise ValueError('Invalid RSA public parameters')
    if len(message) > 117: raise ValueError('Plaintext exceeds RSA PKCS#1 v1.5 capacity')
    padding = bytearray()
    while len(padding) < 125 - len(message):
        padding.extend(x for x in secrets.token_bytes(128) if x)
    encoded = b'\0\x02' + bytes(padding[:125 - len(message)]) + b'\0' + message
    return pow(int.from_bytes(encoded, 'big'), exponent, modulus).to_bytes(128, 'big')


class NetstartResponder:
    def __init__(self, identity, household, bind='0.0.0.0', port=6969,
                 broadcast='255.255.255.255', network_key=NETWORK_KEY, log=None, response_port=6969):
        self.identity = identity.encode('ascii')
        self.household = household.encode('ascii')
        if len(self.identity) != 24 or not 0 < len(self.household) <= 32:
            raise ValueError('Netstart requires UUID24 and household1..32')
        if len(network_key) != 16: raise ValueError('Network key must contain 16 bytes')
        self.bind, self.port, self.broadcast = bind, port, broadcast
        self.response_port = response_port
        self.network_key, self.log = network_key, log or (lambda record: None)
        self.stop_event = threading.Event()
        self.socket, self.thread = None, None
        self._response_cache = {}

    def alive(self):
        # Omitting optional MAC fields avoids the firmware's radio MAC filters.
        return encode(0, [(0, self.identity), (2, self.household), (6, struct.pack('!I', 1))])

    def respond(self, packet):
        _, kind, fields = decode(packet)
        if kind != 1 or fields.get(0) != self.identity: return None
        if len(fields.get(1, b'')) != 4: raise ValueError('QUERY transaction ID must contain4bytes')
        now = time.monotonic()
        for key, (deadline, _) in list(self._response_cache.items()):
            if deadline <= now:
                del self._response_cache[key]
        # Retransmit the exact encrypted payload for an identical request.
        # Include response configuration so a changed key or household cannot
        # reuse an older answer. Expiry is fixed, rather than extended by hits.
        cache_key = (bytes(packet), bytes(self.identity), bytes(self.household),
                     bytes(self.network_key), bytes(PUBLIC_KEY))
        cached = self._response_cache.get(cache_key)
        if cached is not None:
            self.log({'netstart': 'QUERY accepted', 'transaction': int.from_bytes(fields[1], 'big'),
                      'signed_query': 16 in fields, 'response_cached': True})
            return cached[1]
        encrypted = rsa_encrypt_key(fields.get(3, b''), self.network_key)
        self.log({'netstart': 'QUERY accepted', 'transaction': int.from_bytes(fields[1], 'big'),
                  'signed_query': 16 in fields})
        # Keep tag6=1, verified in the captured native join. Values above2
        # trigger an additional exchange; their full interoperability is untested.
        response = encode(2, [(0, self.identity), (1, fields[1]), (2, self.household),
            (3, PUBLIC_KEY), (4, encrypted), (5, struct.pack('!I', 2412)), (6, struct.pack('!I', 1))])
        if len(packet) <= RESPONSE_CACHE_PACKET_LIMIT:
            if len(self._response_cache) >= RESPONSE_CACHE_LIMIT:
                del self._response_cache[next(iter(self._response_cache))]
            # RSA can itself take seconds in the browser: start the lifetime
            # after it completes so queued retransmissions can reuse the work.
            self._response_cache[cache_key] = (time.monotonic() + RESPONSE_CACHE_TTL, response)
        return response

    def loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind, self.port)); self.port = sock.getsockname()[1]
            sock.settimeout(.2)
            next_alive = 0
            while not self.stop_event.is_set():
                if time.monotonic() >= next_alive:
                    sock.sendto(self.alive(), (self.broadcast, self.port))
                    next_alive = time.monotonic() + .5
                try: packet, source = sock.recvfrom(4096)
                except socket.timeout: continue
                try: response = self.respond(packet)
                except ValueError as exc:
                    self.log({'netstart': 'discarded', 'reason': str(exc)}); continue
                if response:
                    # Native QUERY uses an ephemeral sender socket but listens
                    # for the reply on a different socket bound to UDP6969.
                    sock.sendto(response, (source[0], self.response_port))
                    self.log({'netstart': 'RESPONSE sent', 'destination': source[0], 'bytes': len(response)})
        except OSError as exc:
            if not self.stop_event.is_set(): self.log({'netstart_error': str(exc)})
        finally: sock.close(); self.socket = None

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='zp80-netstart', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)
