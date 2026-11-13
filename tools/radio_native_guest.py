#!/usr/bin/env python3
"""Synthetic upstream/backend fixtures driving the production native Radio stack.

Run only in a disposable emulator's private peer namespace. No physical devices,
public accounts or real audio streams are contacted. Native UI, protocol adapter,
catalog/store, RadioPlayback.values and JPEG transport are production code.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import sys
import time
import types
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, '/opt/cr100-lab')
try:
    import xml.etree.ElementTree as ET
except ImportError:
    import xml_compat as ET
    xml = types.ModuleType('xml'); etree = types.ModuleType('xml.etree')
    xml.etree = etree; etree.ElementTree = ET
    sys.modules.update({'xml': xml, 'xml.etree': etree, 'xml.etree.ElementTree': ET})

import zp80_peer as zp
from native_radio import NativeRadioService
from radio_catalog import RadioCatalog
from radio_store import RadioStore
from radiotime import RadioTimeClient, RadioStream
from radio_playback import RadioPlayback

BASE = Path('/opt/cr100-lab/radio-fixtures')
TRACK = json.loads((BASE / 'now-playing.json').read_text())
COVER = (BASE / 'cover-native.jpg').read_bytes()
IMAGE_URL = 'http://192.168.88.80:1400/fixture-cover.jpg'
log = lambda record: print(json.dumps(record), flush=True)


def station():
    return dict(element='outline', type='audio', text='Radio Lab Jazz', guide_id='s1', item='station',
                URL='http://opml.radiotime.com/Tune.ashx?id=s1', image=IMAGE_URL,
                current_song=TRACK['title'], current_artist=TRACK['artist'], current_album=TRACK['album'],
                current_album_art=IMAGE_URL, current_track='Evening Jazz', current_host='Radio Lab Host')


def link(title, guide):
    return dict(element='outline', type='link', text=title, guide_id=guide,
                URL='http://opml.radiotime.com/Browse.ashx?id=' + guide)


def transport(url, **kwargs):
    path, q = urlsplit(url).path, parse_qs(urlsplit(url).query)
    guide = q.get('id', [''])[0]; category = q.get('c', [''])[0]
    log({'event': 'fixture-upstream', 'path': path, 'query': q})
    if path.endswith('Describe.ashx'):
        row = station(); row.update(element='station', name='Radio Lab Jazz')
        if guide.startswith('p'): row.update(element='show', name='Jazz History', guide_id=guide)
        if guide.startswith('t'): row.update(element='topic', name=TRACK['title'], guide_id=guide)
        body = [row]
    elif path.endswith('Search.ashx'):
        body = [station()] if q.get('filter') == ['s'] else [dict(element='outline', type='link',
            guide_id='p77', text='Jazz History', URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p77')]
    elif category == 'music': body = [link('Jazz', 'g11')]
    elif category == 'talk': body = [link('News & Talk', 'g317')]
    elif category == 'sports': body = [link('Sports Talk', 'g323')]
    elif category == 'podcast': body = [dict(element='outline', type='link', text='Jazz History', guide_id='p77',
        URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p77')]
    elif category == 'pbrowse': body = [dict(element='outline', type='audio', text=TRACK['title'], guide_id='t88',
        item='topic', URL='http://opml.radiotime.com/Tune.ashx?id=t88', image=IMAGE_URL,
        duration=565, current_artist=TRACK['artist'], current_album=TRACK['album'])]
    elif guide == 'r0': body = [link('North America', 'r100')]
    elif guide == 'r100': body = [link('United States', 'r101')]
    elif guide == 'r101': body = [link('Boston', 'r102'), link('New York', 'r103')]
    elif category == 'lang': body = [dict(link('English', 'l1'), guide_id='l1')]
    elif category == 'local': body = [dict(element='outline', text='FM', children=[station()])]
    else: body = [station()]
    return dict(head={'status': '200', 'title': 'Radio Lab'}, body=body)


class Catalog(RadioCatalog):
    def resolve(self, identity, formats=None):
        return [RadioStream('http://fixture.invalid/radio.mp3', 'mp3', 128, 100)]


store = RadioStore('/opt/cr100-lab/radio-state.json')
store.update_settings({'cover_lookup': False})
catalog = Catalog(RadioTimeClient(transport, min_request_interval=0), store,
                  zip_resolver=lambda zipcode: dict(zip=zipcode, label='Boston, MA' if zipcode.startswith('0') else 'New York, NY',
                                                    latlon='42.3576,-71.0684' if zipcode.startswith('0') else '40.7484,-73.9967'))


class FixtureRoom:
    transport = 'STOPPED'
    avt = {}
    def require(self, action):
        if action != 'radio_play': raise ValueError('Unexpected fixture operation')
    def radio_formats(self): return ('mp3',)
    def play_radio(self, uri, metadata): self.avt = {'AVTransportURI': uri}; self.transport = 'PLAYING'
    def track(self): return (TRACK['title'], TRACK['artist'], TRACK['album'], song_art)
    def stop(self): self.transport = 'STOPPED'
    def play(self): self.transport = 'PLAYING'
    def transport_actions(self): return 'Play,Stop'
    def supports(self, action): return action in ('play', 'stop')

room = FixtureRoom()
config = zp.Config(bind='192.168.88.80', advertise_ip='192.168.88.80',
                   household='Sonos_TUNEIN_RADIO_LAB', mac='02:00:00:97:00:80',
                   room='Radio Acceptance', callback_network='192.168.88.0/24', boot_sequence=15)
peer = zp.ZP80Peer(config)
player = types.SimpleNamespace(config=config, state=types.SimpleNamespace(backend=room), http_thread=True,
                               publish_changed=lambda service: peer.publish(service))
household = types.SimpleNamespace(coordinator={config.udn: config.udn}, by_udn={config.udn: player})
playback = RadioPlayback(household, catalog, store, log=log)

# Host created this image with the production ArtStore.shrink path; prewarming
# the guest cache avoids requiring Pillow in its minimal SH4 Python installation.
plain_art = playback.art.path_for(IMAGE_URL, prefetch=False)
song_art = playback.art.path_for(IMAGE_URL, prefetch=False, revision=repr((TRACK['title'], TRACK['artist'], TRACK['album'])))
station_art = playback.art.path_for(IMAGE_URL, prefetch=False, revision='station:station:s1')
for image in (plain_art, song_art, station_art): playback.art.images[image.split('/')[-1][:-4]] = COVER


class Radio(NativeRadioService):
    def on_request(self, request, method, args, header):
        log({'event': 'production-radio-request', 'method': method, 'args': args})

radio = Radio('192.168.88.80', 8400, catalog, art=playback.art, log=log,
              on_change=lambda services: [peer.publish(service) for service in services])


class State(zp.PeerState):
    def last_change(self, rendering=False):
        if rendering: return super().last_change(True)
        values = playback.values(config.udn, {'TransportState': self.transport, 'TransportStatus': 'OK',
            'CurrentPlayMode': self.play_mode, 'AVTransportURI': self.uri, 'AVTransportURIMetaData': self.metadata,
            'CurrentTrackURI': self.uri, 'CurrentTrackMetaData': self.metadata,
            'TransportPlaySpeed': '1', 'CurrentValidPlayModes': 'NORMAL'}, 'http://192.168.88.80:1400')
        (BASE / 'last-production-values.json').write_text(json.dumps(values, indent=2))
        root = ET.Element('Event', xmlns='urn:schemas-upnp-org:metadata-1-0/AVT/')
        instance = ET.SubElement(root, 'InstanceID', val='0')
        for key, value in values.items(): ET.SubElement(instance, key, val=str(value))
        return ET.tostring(root, encoding='unicode')

    def event_values(self, service):
        values = super().event_values(service)
        if service == 'ContentDirectory': values.update(radio.event_values())
        if service == 'MusicServices': values['ServiceListVersion'] = '25415'
        if service == 'ZoneGroupTopology': values['ThirdPartyMediaServersX'] = (BASE / 'accounts-empty-x.txt').read_text().strip()
        return values

    def action(self, service, action, args):
        log({'event': 'native-soap', 'service': service, 'action': action, 'args': args})
        result = radio.native_action(service, action, args, self)
        if result is not None: return result, ['ContentDirectory'] if action in ('SetString', 'CreateObject', 'DestroyObject') else []
        result = playback.transport_action(self, service, action, args)
        if result is not None: return result, ['AVTransport']
        values, changed = super().action(service, action, args)
        if service == 'MusicServices' and action == 'ListAvailableServices': values.update(radio.descriptor())
        if service == 'AVTransport':
            overlay = playback.values(config.udn, values, 'http://192.168.88.80:1400')
            values = {key: overlay.get(key, value) for key, value in values.items()}
        return values, changed

peer.state = State(config)
original_get = zp.PeerHandler.do_GET

def get(self):
    path = urlsplit(self.path).path
    if path == '/fixture-cover.jpg': return self.reply(200, COVER, 'image/jpeg')
    match = re.fullmatch(r'/art/([0-9a-f]{16})\.jpg', path)
    if match:
        image = playback.art.get(match.group(1))
        log({'event': 'native-art-request', 'path': path, 'found': bool(image)})
        return self.reply(200 if image else 404, image or b'', 'image/jpeg')
    return original_get(self)

zp.PeerHandler.do_GET = get
radio.start(); peer.start()
log({'event': 'production-radio-ready', 'household': config.household, 'peer_mac': config.mac})
while True:
    time.sleep(.5)
    trigger = Path('/opt/cr100-lab/radio-play-trigger')
    if trigger.exists():
        trigger.unlink(); playback.play(config.udn, 'station:s1'); playback.refresh(playback.session(config.udn)); peer.publish('AVTransport')
