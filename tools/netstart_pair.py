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
from collections import OrderedDict
import errno
import json
import os
import secrets
import string
import struct
import tempfile
import threading
import time
from pathlib import Path

from controller_state import MAX_AID, merge_controller, normalize_mac, normalize_state

NETSTART_SSID = b'Rincon Netstart 1.0'
ALIVE, QUERY, RESPONSE, ACK, TYPE10 = 0, 1, 2, 4, 10
VALID_FREQS = (2412, 2437, 2462)
PLAYER_PUBLIC_KEY = (Path(__file__).resolve().parent / 'synthetic_public_key.bin').read_bytes()
MAX_NETSTART_PAYLOAD = 1200
RESPONSE_CACHE_LIMIT = 256
PENDING_REPLY_LIMIT = 128
REPLIES_PER_TICK = 8
REPLY_TICK_BUDGET = 0.02
TX_BUSY = {errno.EAGAIN, errno.ENOBUFS, errno.ENOMEM}


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
    if not 0 < len(payload) <= MAX_NETSTART_PAYLOAD: raise ValueError('Netstart payload must be 1..1200 bytes')
    body = bytearray((0, len(NETSTART_SSID))) + NETSTART_SSID
    for i in range(0, len(payload), 255):
        chunk = payload[i:i + 255]
        body += bytes((0x46, len(chunk))) + chunk
    return bytes(body)


def parse_body(body):
    """Return the Netstart payload from probe-request IEs, or None."""
    if body[:2 + len(NETSTART_SSID)] != bytes((0, len(NETSTART_SSID))) + NETSTART_SSID: return None
    payload, i = bytearray(), 2 + len(NETSTART_SSID)
    while i < len(body):
        if i + 2 > len(body): return None
        tag, size = body[i], body[i + 1]
        if i + 2 + size > len(body): return None
        if tag == 0x46: payload += body[i + 2:i + 2 + size]
        if len(payload) > MAX_NETSTART_PAYLOAD: return None
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
    def __init__(self, uuid, household, network_key, freq, log=print, *,
                 response_limit=RESPONSE_CACHE_LIMIT, controller_limit=MAX_AID):
        if len(uuid) != 24 or not 0 < len(household) <= 32 or len(network_key) != 16:
            raise ValueError('Need a 24-byte UUID, 1..32-byte household and 16-byte key')
        if freq not in VALID_FREQS: raise ValueError('CR100 accepts only 2412, 2437 or 2462 MHz')
        if type(response_limit) is not int or response_limit < 1:
            raise ValueError('Response cache limit must be positive')
        if type(controller_limit) is not int or not 1 <= controller_limit <= MAX_AID:
            raise ValueError('Invalid pairing controller limit')
        self.uuid, self.household, self.network_key, self.freq, self.log = uuid, household, network_key, freq, log
        self.response_limit, self.controller_limit = response_limit, controller_limit
        self.responses = OrderedDict()  # bounded (mac, txid) -> cached RESPONSE ciphertext
        self.cache_evictions = self.dropped_controllers = 0
        self.paired = None
        self.paired_controllers = {}

    def alive(self):
        return encode(ALIVE, [(0, self.uuid), (2, self.household), (6, struct.pack('!I', 1))])

    def handle(self, src, payload):
        """Return the payloads to send in reply to one received Netstart message."""
        src = normalize_mac(src)
        _, kind, fields = decode(payload)
        if fields.get(0) != self.uuid: return []
        txid = fields.get(1, b'')
        if kind == QUERY:
            if len(txid) != 4: raise ValueError('QUERY transaction ID must be 4 bytes')
            if src not in self.paired_controllers and len(self.paired_controllers) >= self.controller_limit:
                self.dropped_controllers += 1
                return []
            key = (src, txid)
            if key not in self.responses:
                encrypted = rsa_encrypt_key(fields.get(3, b''), self.network_key)
                self.responses[key] = encode(RESPONSE, [(0, self.uuid), (1, txid), (2, self.household),
                    (3, PLAYER_PUBLIC_KEY), (4, encrypted), (5, struct.pack('!I', self.freq)),
                    (6, struct.pack('!I', 1))])
                if len(self.responses) > self.response_limit:
                    self.responses.popitem(last=False)
                    self.cache_evictions += 1
                self.log({'event': 'QUERY', 'from': src, 'txid': int.from_bytes(txid, 'big'),
                          'cr100_tag6': int.from_bytes(fields.get(6, b'\0'), 'big'), 'signed': 16 in fields})
                self.paired = {'cr100_mac': src, 'txid': int.from_bytes(txid, 'big'), 'time': time.time()}
                self.paired_controllers[src] = self.paired
            self.responses.move_to_end(key)
            return [self.responses[key]]
        if kind == TYPE10:
            # Only reached if the CR100 saw tag 6 > 2; answer as the ZP80 did.
            self.log({'event': 'TYPE10', 'from': src})
            return [encode(ACK, [(0, self.uuid), (1, txid)])]
        return []


class PendingReplies:
    """A bounded, deduplicated FIFO shared by the sniffer and pairing loop."""
    def __init__(self, limit=PENDING_REPLY_LIMIT):
        if type(limit) is not int or limit < 1: raise ValueError('Pending reply limit must be positive')
        self.limit, self.dropped = limit, 0
        self._entries, self._lock = OrderedDict(), threading.Lock()

    def put_many(self, payloads):
        with self._lock:
            for payload in payloads:
                if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_NETSTART_PAYLOAD:
                    self.dropped += 1
                elif payload not in self._entries:
                    if len(self._entries) >= self.limit:
                        self.dropped += 1
                    else:
                        self._entries[payload] = None

    def __len__(self):
        with self._lock: return len(self._entries)

    def drain(self, send, deadline, *, limit=REPLIES_PER_TICK, budget=REPLY_TICK_BUDGET, clock=None):
        """Yield to stop/linger checks even if producers keep the queue nonempty."""
        clock = clock or time.monotonic
        stop_at, attempted = min(deadline, clock() + budget), 0
        for _ in range(limit):
            if clock() >= stop_at: break
            with self._lock:
                if not self._entries: break
                payload, _ = self._entries.popitem(last=False)
            send(payload)
            attempted += 1
        return attempted


def initial_state(args):
    """Reuse saved household credentials for both installed and direct pairing."""
    previous_path = Path(args.previous_state) if args.previous_state else Path(args.state)
    previous = None
    if previous_path.exists():
        from bridge_service import validate_state
        previous = validate_state(previous_path)
    elif args.previous_state:
        raise ValueError('Previous pairing state does not exist')
    requested = {'bssid': normalize_mac(args.mac) if args.mac else None,
                 'freq': args.freq, 'household': args.household,
                 'network_key': bytes.fromhex(args.key).hex() if args.key else None}
    if previous:
        for field, value in requested.items():
            if value is not None and value != previous[field]:
                raise ValueError('Pairing cannot change existing household ' + field)
        return previous
    if requested['bssid'] is None:
        raise ValueError('--mac is required for the first controller')
    return {'bssid': requested['bssid'], 'uuid': player_uuid(requested['bssid']).decode(),
            'freq': requested['freq'] if requested['freq'] is not None else 2437,
            'household': requested['household'] or new_household().decode(),
            'network_key': requested['network_key'] or secrets.token_bytes(16).hex()}


def save_pairing(path, state, controllers):
    """Atomically persist all paired controllers with private file permissions."""
    for controller in controllers:
        state = merge_controller(state, controller)
    state = normalize_state(state)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.pair-state-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(json.dumps(state, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return state


def run(args):
    state = initial_state(args)
    from scapy.all import AsyncSniffer, Dot11, Dot11ProbeReq, RadioTap, Raw, conf

    state_path = Path(args.state)
    mac, freq = state['bssid'], state['freq']
    network_key, household = bytes.fromhex(state['network_key']), state['household'].encode()
    pairer = Pairer(player_uuid(mac), household, network_key, freq,
                    log=lambda record: print(json.dumps(record), flush=True))
    sock = conf.L2socket(iface=args.iface)
    # Scapy's native Linux L2Socket sends through outs. Do not let an injection
    # backlog block the loop beyond its pairing or linger deadline.
    try: sock.outs.setblocking(False)
    except BaseException:
        sock.close()
        raise
    seq = 0
    tx_busy = 0

    def send(payload, copies=1, deadline=None):
        nonlocal seq, tx_busy
        for _ in range(copies):
            if deadline is not None and time.monotonic() >= deadline: return
            try:
                sock.send(RadioTap() / Dot11(type=0, subtype=4, addr1='ff:ff:ff:ff:ff:ff', addr2=mac,
                                             addr3='ff:ff:ff:ff:ff:ff', SC=(seq & 0xfff) << 4)
                          / Dot11ProbeReq() / Raw(frame_body(payload)))
            except OSError as error:
                if error.errno not in TX_BUSY: raise
                tx_busy += 1
                return  # the CR100 repeats its QUERY if the response was lost
            seq += 1

    pending = PendingReplies()

    def on_frame(pkt):
        if not pkt.haslayer(Dot11ProbeReq) or pkt[Dot11].addr2 == mac: return
        payload = parse_body(bytes(pkt[Dot11ProbeReq].payload))
        if payload is None: return
        try:
            flags, kind, fields = decode(payload)
            print(json.dumps({'event': 'rx', 'from': pkt[Dot11].addr2, 'type': kind, 'flags': flags,
                              'tags': sorted(fields)}), flush=True)
            pending.put_many(pairer.handle(pkt[Dot11].addr2, payload))
        except ValueError as error:
            print(json.dumps({'event': 'bad', 'from': pkt[Dot11].addr2, 'error': str(error)}), flush=True)

    sniffer = AsyncSniffer(iface=args.iface, filter='type mgt subtype probe-req', prn=on_frame, store=False)
    try: sniffer.start()
    except BaseException:
        sock.close()
        raise
    print(json.dumps({'event': 'start', 'uuid': pairer.uuid.decode(), 'household': household.decode(),
                      'freq': freq, 'mac': mac}), flush=True)
    start, next_alive, done_at = time.monotonic(), 0.0, None
    deadline = start + args.seconds
    saved = {}
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            stop_at = min(deadline, done_at + args.linger) if done_at is not None else deadline
            if now >= stop_at: break
            pending.drain(lambda payload: send(payload, copies=2, deadline=stop_at), stop_at)
            paired = pairer.paired_controllers.copy()
            if paired != saved:
                if done_at is None: done_at = now
                state = save_pairing(state_path, state, paired.values())
                saved = paired
                print(json.dumps({'event': 'saved', 'state': str(state_path)}), flush=True)
            # Keep answering repeated QUERYs for a while after the first one.
            now = time.monotonic()
            if now >= deadline or (done_at is not None and now - done_at >= args.linger): break
            if done_at is None and now >= next_alive:
                send(pairer.alive(), deadline=deadline)
                next_alive = now + args.alive_interval
            time.sleep(0.005)
    finally:
        sniffer.stop()
        sock.close()
    print(json.dumps({'event': 'end', 'paired': pairer.paired is not None, 'tx_busy': tx_busy,
                      'pending_dropped': pending.dropped, 'cache_evictions': pairer.cache_evictions,
                      'controllers_dropped': pairer.dropped_controllers}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--iface', default='mon0')
    parser.add_argument('--mac', help='bridge MAC: defaults to existing state; required for first pairing')
    parser.add_argument('--freq', type=int, help='MHz: defaults to existing state, otherwise 2437')
    parser.add_argument('--household', help='defaults to existing state, otherwise a new household')
    parser.add_argument('--key', help='network key: defaults to existing state, otherwise random')
    parser.add_argument('--state', default='bridge-state.json')
    parser.add_argument('--previous-state', help='read existing household here and write merged state to --state')
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--alive-interval', type=float, default=0.1)
    parser.add_argument('--linger', type=float, default=20, help='seconds to keep answering after pairing')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
