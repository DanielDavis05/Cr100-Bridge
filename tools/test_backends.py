#!/usr/bin/env python3
"""Offline tests for backends.py and content.py without Home Assistant: an in-memory backend
drives virtual players on loopback, so the household needs nothing beyond the Room API."""
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import content
import zoneplayers
from test_ha_backend import AVT, field, wait_for
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

RC = '/MediaRenderer/RenderingControl/Control'
CD = '/MediaServer/ContentDirectory/Control'
STATION = content.Node('FV:2/1', 'FV:2', 'Jazz FM', 'station:jazz', 'station', False, content.BROADCAST_CLASS,
                       uri='x-rincon-mp3radio://memory/jazz', thumbnail='jazz.png')


class MemoryContent(content.ContentSource):
    def __init__(self, art):
        super().__init__(art)
        self.browsed = []

    def children(self, key, object_id):
        self.browsed.append((key, object_id))
        if object_id != 'FV:2': raise content.ContentError(701, 'No such object: %s' % object_id)
        self.register(STATION)
        return [STATION]

    def metadata(self, key, object_id):
        raise content.ContentError(701, 'No such object: %s' % object_id)

    def resolve(self, uri, metadata=''):
        return STATION if uri == STATION.uri else None


class MemoryBackend(backends.Backend):
    def __init__(self, name):
        super().__init__(lambda record: None)
        self.name = name
        self.rooms, self.calls, self.groups = {}, [], {}
        self.art = backends.ArtStore(self)
        self.content = MemoryContent(self.art)

    def room(self, key):
        return self.rooms.setdefault(key, MemoryRoom(self, key))

    def display_name(self, key):
        return key.title()

    def fetch(self, picture):
        import io
        from PIL import Image
        out = io.BytesIO()
        Image.new('RGB', (600, 600), 'red').save(out, 'PNG')
        return out.getvalue()


class MemoryRoom(backends.Room):
    reported_volume = reported_mute = transport = None   # plain attributes here

    def __init__(self, backend, key):
        super().__init__(backend, key)
        self.reported_volume, self.reported_mute, self.transport, self.playing = 20, False, 'STOPPED', None

    def track(self):
        if not self.playing: return None
        return self.playing.title, '', '', self.backend.art.path_for(self.playing.thumbnail, prefetch=False), None

    def capabilities(self):
        return frozenset({'next'})

    def group_members(self):
        return self.backend.groups.get(self.key, [self.key])

    def record(self, *call):
        self.backend.calls.append((self.key,) + call)
        self.backend.changed(self.key)

    def play(self): self.transport = 'PLAYING'; self.record('play')
    def pause(self): self.transport = 'PAUSED_PLAYBACK'; self.record('pause')
    def play_item(self, node): self.playing, self.transport = node, 'PLAYING'; self.record('play_item', node.ref)
    def send_volume(self, value): self.reported_volume = value; self.record('volume', value)

    def join(self, member):
        members = self.group_members() + [member.key]
        for key in members: self.backend.groups[key] = members
        self.record('join', member.key)


class MemoryHousehold(unittest.TestCase):
    def setUp(self):
        self.one, self.two = MemoryBackend('one'), MemoryBackend('two')
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall', 'Shed'], ['127.0.0.1'] * 3,
                                           lambda record: None, port=0, backends=[self.one, self.two],
                                           refs=[('one', 'den'), ('one', 'hall'), ('two', 'shed')])
        self.house.start('127.0.0.1', ssdp=False)
        self.den, self.hall, self.shed = self.house.players

    def tearDown(self):
        self.house.close()

    def test_actions_and_state_go_through_the_room(self):
        status, body = soap(self.den, AVT, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
        self.assertEqual(status, 200)
        self.assertEqual(self.one.calls, [('den', 'play')])
        _, body = soap(self.den, AVT, 'AVTransport', 'GetTransportInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'CurrentTransportState'), 'PLAYING')
        # What the room cannot do is a UPnP error, and its actions list says so.
        status, _ = soap(self.den, AVT, 'AVTransport', 'Previous', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 500)
        _, body = soap(self.den, AVT, 'AVTransport', 'GetCurrentTransportActions', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'Actions'), 'Play,Stop,Pause,Next')
        soap(self.den, RC, 'RenderingControl', 'SetVolume',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>55</DesiredVolume>')
        self.assertTrue(wait_for(lambda: ('den', 'volume', 55) in self.one.calls))
        self.assertEqual(self.den.state.backend.volume, 55)

    def test_music_menu_plays_through_the_backend_with_its_art(self):
        status, body = soap(self.hall, CD, 'ContentDirectory', 'Browse',
                            '<ObjectID>FV:2</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter>'
                            '<StartingIndex>0</StartingIndex><RequestedCount>10</RequestedCount><SortCriteria></SortCriteria>')
        self.assertEqual((status, field(body, 'TotalMatches')), (200, '1'))
        self.assertIn('Jazz FM', field(body, 'Result'))
        self.assertEqual(self.one.content.browsed, [('hall', 'FV:2')])
        status, _ = soap(self.hall, AVT, 'AVTransport', 'SetAVTransportURI',
                         f'<InstanceID>0</InstanceID><CurrentURI>{STATION.uri}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        self.assertEqual(self.one.calls[-1], ('hall', 'play_item', 'station:jazz'))
        _, body = soap(self.hall, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        art = field(body, 'TrackMetaData').split('<upnp:albumArtURI>')[1].split('<')[0]
        # Any player serves any backend's art.
        c = http.client.HTTPConnection('127.0.0.1', self.shed.config.port, timeout=5)
        c.request('GET', art); r = c.getresponse(); data = r.read(); c.close()
        self.assertEqual((r.status, data[:2]), (200, b'\xff\xd8'))

    def test_groups_follow_the_backend_and_never_span_two(self):
        self.assertFalse(self.house.join(self.hall.config.udn, self.den.config.udn))
        self.assertEqual(self.one.calls[-1], ('den', 'join', 'hall'))
        self.assertTrue(wait_for(lambda: groups(self.house.topology()) == [['Den', 'Hall'], ['Shed']]))
        self.assertFalse(self.house.join(self.shed.config.udn, self.den.config.udn))
        self.assertFalse(self.house.join(self.den.config.udn, self.shed.config.udn))
        self.assertEqual(len(self.one.calls) + len(self.two.calls), 1)


class Config(unittest.TestCase):
    def test_room_refs_and_library(self):
        self.assertEqual(backends.room_ref({'entity': 'media_player.den'}), ('ha', 'media_player.den'))
        self.assertEqual(backends.room_ref({'backend': 'sonos', 'id': 'RINCON_1', 'entity': 'x'}), ('sonos', 'RINCON_1'))
        self.assertIsNone(backends.room_ref({'name': 'Spare'}))
        config = Path(tempfile.mkdtemp()) / 'config.json'
        config.write_text(json.dumps({'rooms': [], 'music_menu': {'library': {'backend': 'sonos', 'id': 'RINCON_1'},
                                                                  'library_entity': 'media_player.den'}}))
        self.assertEqual(zoneplayers.library_ref(config), ('sonos', 'RINCON_1'))
        config.write_text(json.dumps({'rooms': [{'backend': 'nope', 'id': 'x'}]}))
        with self.assertRaisesRegex(ValueError, 'Unknown backend: nope'):
            zoneplayers.load_rooms(config, lambda record: None)
        with self.assertRaises(ValueError):
            zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], port=0, refs=[('one', 'den')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
