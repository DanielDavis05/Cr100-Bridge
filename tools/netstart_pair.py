#!/usr/bin/env python3
"""T2: pair a CR100 with the bridge over SonosNet Netstart (802.11 probe IEs).

Acts as the player: broadcasts ALIVE, answers the CR100's QUERY with a RESPONSE
carrying the bridge household and a 16-byte network key RSA-encrypted to the
CR100's public key, then saves the result. Frame layout matches the ZP80 in
'Physical CR100 Pairing (channel 1).pcap': broadcast probe request, SSID IE
'Rincon Netstart 1.0', payload split over IE 0x46 chunks, flags byte 2.

RESPONSE tag 6 is 1, so the CR100 skips the type 10/4 round (STATIC-FINDINGS §1).
Pairing replaces the CR100's household; rejoining the ZP80 needs mute + volume up.

Usage: sudo python3 netstart_pair.py --iface mon0 --mac dc:a6:32:e7:3b:0e --freq 2437
"""
from __future__ import annotations

import argparse
import json
import secrets
import string
import struct
import time
from pathlib import Path

NETSTART_SSID = b'Rincon Netstart 1.0'
ALIVE, QUERY, RESPONSE, ACK, TYPE10 = 0, 1, 2, 4, 10
VALID_FREQS = (2412, 2437, 2462)
PLAYER_PUBLIC_KEY = (Path(__file__).resolve().parent / 'synthetic_public_key.bin').read_bytes()


def encode(message_type, fields, flags=2):
    result = bytearray((flags, message_type))
    for tag, value in fields:
        result += struct.pack('!BH', tag, len(value)) + value
    return bytes(result)


def decode(payload):
    if len(payload) < 2: raise ValueError('Truncated header')
    fields, offset = {}, 2
    while offset < len(payload):
        if len(payload) - offset < 3: raise ValueError('Truncated field header')
        tag, size = struct.unpack_from('!BH', payload, offset); offset += 3
        if size > len(payload) - offset: raise ValueError('Truncated field')
        fields.setdefault(tag, payload[offset:offset + size]); offset += size
    return payload[0], payload[1], fields


def frame_body(payload):
    """Probe-request IEs: Netstart SSID, then the payload in IE 0x46 chunks."""
    if not 0 < len(payload) <= 1200: raise ValueError('Netstart payload must be 1..1200 bytes')
    body = bytearray((0, len(NETSTART_SSID))) + NETSTART_SSID
    for i in range(0, len(payload), 255):
        chunk = payload[i:i + 255]
        body += bytes((0x46, len(chunk))) + chunk
    return bytes(body)


def parse_body(body):
    """Return the Netstart payload from probe-request IEs, or None."""
    if body[:2 + len(NETSTART_SSID)] != bytes((0, len(NETSTART_SSID))) + NETSTART_SSID: return None
    payload, i = bytearray(), 2 + len(NETSTART_SSID)
    while i + 2 <= len(body):
        tag, size = body[i], body[i + 1]
        if i + 2 + size > len(body): break
        if tag == 0x46: payload += body[i + 2:i + 2 + size]
        i += 2 + size
    return bytes(payload) or None


def rsa_encrypt_key(public_key, message):
    """PKCS#1 v1.5 type-2 encryption to a 260-byte RSAref public key."""
    if len(public_key) != 260 or struct.unpack_from('<I', public_key)[0] != 1024:
        raise ValueError('Expected a 1024-bit RSAref public key')
    modulus = int.from_bytes(public_key[4:132], 'big')
    exponent = int.from_bytes(public_key[132:260], 'big')
    if modulus.bit_length() != 1024 or not exponent & 1: raise ValueError('Invalid RSA public parameters')
    padding = bytearray()
    while len(padding) < 125 - len(message):
        padding.extend(x for x in secrets.token_bytes(128) if x)
    encoded = b'\0\x02' + bytes(padding[:125 - len(message)]) + b'\0' + message
    return pow(int.from_bytes(encoded, 'big'), exponent, modulus).to_bytes(128, 'big')


def player_uuid(mac):
    return ('RINCON_' + mac.replace(':', '').upper() + '01400').encode()


def new_household():
    alphabet = string.ascii_letters + string.digits
    return ('Sonos_' + ''.join(secrets.choice(alphabet) for _ in range(26))).encode()


class Pairer:
    def __init__(self, uuid, household, network_key, freq, log=print):
        if len(uuid) != 24 or not 0 < len(household) <= 32 or len(network_key) != 16:
            raise ValueError('Need a 24-byte UUID, 1..32-byte household and 16-byte key')
        if freq not in VALID_FREQS: raise ValueError('CR100 accepts only 2412, 2437 or 2462 MHz')
        self.uuid, self.household, self.network_key, self.freq, self.log = uuid, household, network_key, freq, log
        self.responses = {}  # (cr100 mac, txid) -> RESPONSE payload, so repeats reuse one ciphertext
        self.paired = None

    def alive(self):
        return encode(ALIVE, [(0, self.uuid), (2, self.household), (6, struct.pack('!I', 1))])

    def handle(self, src, payload):
        """Return the payloads to send in reply to one received Netstart message."""
        _, kind, fields = decode(payload)
        if fields.get(0) != self.uuid: return []
        txid = fields.get(1, b'')
        if kind == QUERY:
            if len(txid) != 4: raise ValueError('QUERY transaction ID must be 4 bytes')
            key = (src, txid)
            if key not in self.responses:
                encrypted = rsa_encrypt_key(fields.get(3, b''), self.network_key)
                self.responses[key] = encode(RESPONSE, [(0, self.uuid), (1, txid), (2, self.household),
                    (3, PLAYER_PUBLIC_KEY), (4, encrypted), (5, struct.pack('!I', self.freq)),
                    (6, struct.pack('!I', 1))])
                self.log({'event': 'QUERY', 'from': src, 'txid': int.from_bytes(txid, 'big'),
                          'cr100_tag6': int.from_bytes(fields.get(6, b'\0'), 'big'), 'signed': 16 in fields})
                self.paired = {'cr100_mac': src, 'txid': int.from_bytes(txid, 'big'), 'time': time.time()}
            return [self.responses[key]]
        if kind == TYPE10:
            # Only reached if the CR100 saw tag 6 > 2; answer as the ZP80 did.
            self.log({'event': 'TYPE10', 'from': src})
            return [encode(ACK, [(0, self.uuid), (1, txid)])]
        return []


def run(args):
    from scapy.all import AsyncSniffer, Dot11, Dot11ProbeReq, RadioTap, Raw, conf

    state_path = Path(args.state)
    network_key = bytes.fromhex(args.key) if args.key else secrets.token_bytes(16)
    household = args.household.encode() if args.household else new_household()
    pairer = Pairer(player_uuid(args.mac), household, network_key, args.freq,
                    log=lambda record: print(json.dumps(record), flush=True))
    sock = conf.L2socket(iface=args.iface)
    seq = 0

    def send(payload, copies=1):
        nonlocal seq
        for _ in range(copies):
            sock.send(RadioTap() / Dot11(type=0, subtype=4, addr1='ff:ff:ff:ff:ff:ff', addr2=args.mac,
                                         addr3='ff:ff:ff:ff:ff:ff', SC=(seq & 0xfff) << 4)
                      / Dot11ProbeReq() / Raw(frame_body(payload)))
            seq += 1

    pending = []

    def on_frame(pkt):
        if not pkt.haslayer(Dot11ProbeReq) or pkt[Dot11].addr2 == args.mac: return
        payload = parse_body(bytes(pkt[Dot11ProbeReq].payload))
        if payload is None: return
        try:
            flags, kind, fields = decode(payload)
            print(json.dumps({'event': 'rx', 'from': pkt[Dot11].addr2, 'type': kind, 'flags': flags,
                              'tags': sorted(fields)}), flush=True)
            pending.extend(pairer.handle(pkt[Dot11].addr2, payload))
        except ValueError as error:
            print(json.dumps({'event': 'bad', 'from': pkt[Dot11].addr2, 'error': str(error)}), flush=True)

    sniffer = AsyncSniffer(iface=args.iface, filter='type mgt subtype probe-req', prn=on_frame, store=False)
    sniffer.start()
    print(json.dumps({'event': 'start', 'uuid': pairer.uuid.decode(), 'household': household.decode(),
                      'freq': args.freq, 'mac': args.mac}), flush=True)
    start, next_alive, done_at = time.monotonic(), 0.0, None
    try:
        while time.monotonic() - start < args.seconds:
            now = time.monotonic()
            while pending:
                send(pending.pop(0), copies=2)
            if pairer.paired and done_at is None:
                done_at = now
                state_path.write_text(json.dumps({'uuid': pairer.uuid.decode(), 'household': household.decode(),
                    'network_key': network_key.hex(), 'freq': args.freq, 'bssid': args.mac,
                    **pairer.paired}, indent=2) + '\n')
                print(json.dumps({'event': 'saved', 'state': str(state_path)}), flush=True)
            # Keep answering repeated QUERYs for a while after the first one.
            if done_at is not None and now - done_at > args.linger: break
            if done_at is None and now >= next_alive:
                send(pairer.alive())
                next_alive = now + args.alive_interval
            time.sleep(0.005)
    finally:
        sniffer.stop()
    print(json.dumps({'event': 'end', 'paired': pairer.paired is not None}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--iface', default='mon0')
    parser.add_argument('--mac', required=True, help='bridge MAC: ALIVE source and later BSSID')
    parser.add_argument('--freq', type=int, default=2437, help='MHz the CR100 should use after pairing')
    parser.add_argument('--household', help='default: new random Sonos_ + 26 characters')
    parser.add_argument('--key', help='network key as 32 hex digits (default: random)')
    parser.add_argument('--state', default='bridge-state.json')
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--alive-interval', type=float, default=0.1)
    parser.add_argument('--linger', type=float, default=20, help='seconds to keep answering after pairing')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
