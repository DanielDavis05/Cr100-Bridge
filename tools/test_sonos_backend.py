#!/usr/bin/env python3
"""Offline tests for the Sonos backend (sonos_backend.py, sonos_content.py, sonos_upnp.py):
virtual players on loopback backed by fake Sonos players (fake_sonos.py), real HTTP, SOAP and
GENA both ways."""
import html
import http.client
import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_sonos
import sonos_backend
import sonos_upnp
import zoneplayers
from content import RADIO_PREFIX
from fake_sonos import entry
from sonos_upnp import DIDL_OPEN
from test_ha_backend import AVT, field, wait_for
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

KITCHEN, PATIO = 'RINCON_000E58AAAAAA01400', 'RINCON_000E58BBBBBB01400'
RC = '/MediaRenderer/RenderingControl/Control'
CD = '/MediaServer/ContentDirectory/Control'
STATION_MD = DIDL_OPEN + entry('R:0/0/1', 'Jazz FM', 'R:0/0', 'object.item.audioItem.audioBroadcast') + '</DIDL-Lite>'


def browse(player, object_id, flag='BrowseDirectChildren', start=0, count=500):
    status, body = soap(player, CD, 'ContentDirectory', 'Browse',
                        f'<ObjectID>{object_id}</ObjectID><BrowseFlag>{flag}</BrowseFlag><Filter>*</Filter>'
                        f'<StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>')
    return status, body


def calls(player, action):
    return [args for name, args in list(player.calls) if name == action]


class SonosHousehold(unittest.TestCase):
    def setUp(self):
        self.fake = fake_sonos.FakeHousehold()
        self.kitchen = self.fake.add(KITCHEN, 'Kitchen')
        self.patio = self.fake.add(PATIO, 'Patio', volume=35)
        self.fake.library['FV:2'] = [entry('FV:2/1', 'Jazz FM', 'FV:2', 'object.itemobject.item.sonos-favorite',
                                           res='x-sonosapi-stream:s123?sid=254', res_md=STATION_MD, art='/getaa?s=1&u=jazz')]
        self.fake.library['A:ALBUM'] = [entry('A:ALBUM/Album%d' % n, 'Album %d' % n, 'A:ALBUM',
                                              'object.container.album.musicAlbum', container=True,
                                              res='x-rincon-playlist:%s#A:ALBUM/Album%d' % (KITCHEN, n))
                                        for n in range(150)]
        self.logs = []
        self.backend = sonos_backend.SonosBackend([self.kitchen.base], self.logs.append)
        self.backend.start()
        self.assertTrue(self.backend.ready.wait(5), self.logs)
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio', 'Spare'], ['127.0.0.1'] * 3,
                                           self.logs.append, port=0, backends=[self.backend],
                                           refs=[('sonos', KITCHEN), ('sonos', PATIO), None])
        self.house.start('127.0.0.1', ssdp=False)
        self.vkitchen, self.vpatio, self.spare = self.house.players
        self.assertTrue(wait_for(lambda: all(s.healthy and s.read_at for s in self.backend.speakers.values())),
                        self.logs)

    def tearDown(self):
        self.house.close()
        self.backend.close()
        self.fake.close()

    def test_now_playing_from_the_player_with_its_art(self):
        self.kitchen.transport, self.kitchen.uri = 'PLAYING', 'x-rincon-queue:%s#0' % KITCHEN
        self.kitchen.queue = [('x-file-cifs://nas/%d.mp3' % n, '') for n in range(3)]
        self.kitchen.track_number, self.kitchen.position = 2, '0:01:00'
        self.kitchen.play_track_metadata('So What', 'Miles Davis', 'Kind of Blue', art='/getaa?u=so-what', duration='0:09:22')
        self.kitchen.notify('AVTransport')
        self.assertTrue(wait_for(lambda: field(soap(self.vkitchen, AVT, 'AVTransport', 'GetPositionInfo',
                                                    '<InstanceID>0</InstanceID>')[1], 'Track') == '2'))
        _, body = soap(self.vkitchen, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'TrackDuration'), '0:09:22')
        self.assertTrue('0:01:00' <= field(body, 'RelTime') <= '0:01:03', field(body, 'RelTime'))
        metadata = field(body, 'TrackMetaData')
        for text in ('So What', 'Miles Davis', 'Kind of Blue'): self.assertIn(text, metadata)
        art = re.search(r'<upnp:albumArtURI>([^<]+)<', metadata).group(1)
        connection = http.client.HTTPConnection('127.0.0.1', self.vkitchen.config.port, timeout=5)
        connection.request('GET', art); response = connection.getresponse(); data = response.read()
        self.assertEqual((response.status, data[:2]), (200, b'\xff\xd8'))
        _, body = soap(self.vkitchen, AVT, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'NrTracks'), '3')
        # A station: its stream title is artist - title, and the station shows as the album.
        self.kitchen.uri, self.kitchen.uri_metadata = 'x-sonosapi-stream:s123?sid=254', STATION_MD
        self.kitchen.play_track_metadata('s123', stream_content='Potlatch - After Rain', duration='0:00:00')
        self.kitchen.notify('AVTransport')
        self.assertTrue(wait_for(lambda: self.vkitchen.state.backend.track()[:3] == ('After Rain', 'Potlatch', 'Jazz FM')))

    def test_transport_goes_to_the_coordinator_and_volume_to_the_room(self):
        self.fake.regroup(PATIO, KITCHEN)
        self.assertTrue(wait_for(lambda: groups(self.house.topology()) == [['Kitchen', 'Patio'], ['Spare']]))
        self.kitchen.transport = 'PLAYING'
        self.kitchen.notify('AVTransport')
        self.assertTrue(wait_for(lambda: self.vpatio.state.backend.transport == 'PLAYING'))
        status, _ = soap(self.vpatio, AVT, 'AVTransport', 'Pause', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(len(calls(self.kitchen, 'Pause')), 1)
        self.assertEqual(calls(self.patio, 'Pause'), [])
        soap(self.vpatio, RC, 'RenderingControl', 'SetVolume',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>30</DesiredVolume>')
        self.assertTrue(wait_for(lambda: self.patio.volume == 30))
        self.assertEqual(calls(self.kitchen, 'SetVolume'), [])

    def test_grouping_goes_through_the_players(self):
        uri = 'x-rincon:' + self.vkitchen.config.udn
        status, _ = soap(self.vpatio, AVT, 'AVTransport', 'SetAVTransportURI',
                         f'<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        self.assertEqual(calls(self.patio, 'SetAVTransportURI')[-1]['CurrentURI'], 'x-rincon:' + KITCHEN)
        self.assertTrue(wait_for(lambda: groups(self.house.topology()) == [['Kitchen', 'Patio'], ['Spare']]))
        self.assertFalse(self.house.join(self.spare.config.udn, self.vkitchen.config.udn))   # simulated room
        status, _ = soap(self.vpatio, AVT, 'AVTransport', 'BecomeCoordinatorOfStandaloneGroup', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: groups(self.house.topology()) == [['Kitchen'], ['Patio'], ['Spare']]))

    def test_music_menu_pages_from_the_player_and_plays_as_sonos_does(self):
        status, body = browse(self.vkitchen, 'A:ALBUM')
        self.assertEqual((status, field(body, 'NumberReturned'), field(body, 'TotalMatches')), (200, '100', '150'))
        _, body = browse(self.vkitchen, 'A:ALBUM', start=100)
        self.assertEqual(field(body, 'NumberReturned'), '50')
        album = ET.fromstring(field(body, 'Result'))[0]
        res = album.find('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res').text
        self.assertEqual(res, 'x-rincon-playlist:%s#A:ALBUM/Album100' % self.vkitchen.config.udn)   # the virtual player's
        status, body = soap(self.vkitchen, AVT, 'AVTransport', 'AddURIToQueue',
                            f'<InstanceID>0</InstanceID><EnqueuedURI>{html.escape(res)}</EnqueuedURI><EnqueuedURIMetaData></EnqueuedURIMetaData>'
                            '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>')
        self.assertEqual((status, field(body, 'NewQueueLength')), (200, '1'))
        self.assertEqual(calls(self.kitchen, 'AddURIToQueue')[-1]['EnqueuedURI'],
                         'x-rincon-playlist:%s#A:ALBUM/Album100' % KITCHEN)              # the real player's
        _, body = browse(self.vkitchen, 'Q:0')
        self.assertEqual(field(body, 'TotalMatches'), '1')
        # A favourite plays at once, with the player's own URI and metadata.
        _, body = browse(self.vkitchen, 'FV:2')
        result = field(body, 'Result')
        self.assertIn('<upnp:albumArtURI>/art/', result)
        self.assertIn(RADIO_PREFIX, result)
        uri = re.search(r'<res protocolInfo="[^"]*">([^<]+)<', result).group(1)
        status, _ = soap(self.vkitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                         f'<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        played = calls(self.kitchen, 'SetAVTransportURI')[-1]
        self.assertEqual(played['CurrentURI'], 'x-sonosapi-stream:s123?sid=254')
        self.assertIn('R:0/0/1', played['CurrentURIMetaData'])
        self.assertEqual(len(calls(self.kitchen, 'Play')), 1)
        # The bridge's own top entries where a player has none.
        status, body = browse(self.vkitchen, 'AI:', 'BrowseMetadata')
        self.assertEqual(status, 200)
        self.assertIn('Line-In', field(body, 'Result'))

    def test_music_service_items_are_queued_as_share_links(self):
        room = self.vkitchen.state.backend
        room.play_provider('spotify:album:6wiUBliPe76YAVpNEdidpY', 'album')
        added = calls(self.kitchen, 'AddURIToQueue')[-1]
        self.assertEqual(added['EnqueuedURI'], 'x-rincon-cpcontainer:1004206cspotify%3aalbum%3a6wiUBliPe76YAVpNEdidpY')
        self.assertIn('id="00040000spotify%3aalbum%3a6wiUBliPe76YAVpNEdidpY"', added['EnqueuedURIMetaData'])
        self.assertIn('SA_RINCON2311_X_#Svc2311-0-Token', added['EnqueuedURIMetaData'])
        self.assertEqual([name for name, _ in self.kitchen.calls if name in ('AddURIToQueue', 'SetAVTransportURI', 'Seek', 'Play')],
                         ['AddURIToQueue', 'SetAVTransportURI', 'Seek', 'Play'])
        # Without an account on the first service number, the US one is tried.
        self.kitchen.refuse_service = 2311
        result = room.enqueue_provider('spotify:track:2qs5ZcLByNTctJKbhAZ9JE', 'music')
        tried = calls(self.kitchen, 'AddURIToQueue')[-2:]
        self.assertEqual([a['EnqueuedURI'] for a in tried], ['spotify%3atrack%3a2qs5ZcLByNTctJKbhAZ9JE'] * 2)
        self.assertIn('SA_RINCON3079_', tried[1]['EnqueuedURIMetaData'])
        self.assertEqual(result['NewQueueLength'], '2')

    def test_changes_made_elsewhere_arrive_as_events(self):
        self.kitchen.volume = 44
        self.kitchen.notify('RenderingControl')
        self.assertTrue(wait_for(lambda: field(soap(self.vkitchen, RC, 'RenderingControl', 'GetVolume',
                                                    '<InstanceID>0</InstanceID><Channel>Master</Channel>')[1],
                                               'CurrentVolume') == '44', timeout=3))
        version = self.vkitchen.state.backend.queue_version()
        self.kitchen.queue_update += 1
        self.kitchen.notify('ContentDirectory')
        self.assertTrue(wait_for(lambda: self.vkitchen.state.backend.queue_version() > version, timeout=3))


class Protocol(unittest.TestCase):
    def test_topology_leaves_out_hidden_players(self):
        groups_, zones = sonos_upnp.parse_topology(
            '<ZoneGroupState><ZoneGroups><ZoneGroup Coordinator="B" ID="B:1">'
            '<ZoneGroupMember UUID="A" Location="http://10.0.0.5:1400/xml/device_description.xml" ZoneName="Den"/>'
            '<ZoneGroupMember UUID="B" Location="http://10.0.0.6:1400/xml/device_description.xml" ZoneName="TV">'
            '<Satellite UUID="C" Location="http://10.0.0.7:1400/x.xml" ZoneName="TV" Invisible="1"/></ZoneGroupMember>'
            '<ZoneGroupMember UUID="D" Location="http://10.0.0.8:1400/x.xml" ZoneName="Sub" Invisible="1"/>'
            '</ZoneGroup><ZoneGroup Coordinator="E" ID="E:1"><ZoneGroupMember UUID="E" Location="http://10.0.0.9:1400/x.xml"'
            ' ZoneName="Boost" IsZoneBridge="1"/></ZoneGroup></ZoneGroups></ZoneGroupState>')
        self.assertEqual(groups_, [('B', ['B', 'A'])])
        self.assertEqual({u: z['base'] for u, z in zones.items()}, {'A': 'http://10.0.0.5:1400', 'B': 'http://10.0.0.6:1400'})

    def test_events_and_discovery_answers(self):
        self.assertEqual(sonos_upnp.last_change('<Event><InstanceID val="0"><Volume channel="Master" val="12"/>'
                                                '<Volume channel="LF" val="100"/><Mute channel="Master" val="1"/>'
                                                '</InstanceID></Event>'), {'Volume': '12', 'Mute': '1'})
        self.assertEqual(sonos_upnp.container_updates('Q:0,7,FV:2,3'), {'Q:0': 7, 'FV:2': 3})
        answer = 'HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.130:1400/xml/device_description.xml\r\nUSN: uuid:%s::urn:x\r\n'
        self.assertEqual(sonos_upnp.search_response(answer % KITCHEN), 'http://10.0.0.130:1400')
        self.assertIsNone(sonos_upnp.search_response(answer % 'RINCON_02690000000101400'))   # the bridge's own

    def test_config_names_rooms_from_the_players(self):
        fake = fake_sonos.FakeHousehold()
        fake.add(KITCHEN, 'Kitchen')
        try:
            config = Path(tempfile.mkdtemp()) / 'config.json'
            config.write_text(json.dumps({'sonos': {'hosts': [fake.players[KITCHEN].base]},
                                          'rooms': [{'backend': 'sonos', 'id': KITCHEN}, {'name': 'Spare'}],
                                          'music_menu': {'library': {'backend': 'sonos', 'id': KITCHEN}}}))
            names, refs, slots, connected = zoneplayers.load_rooms(config, lambda record: None)
            try:
                self.assertEqual((names, refs), (['Kitchen', 'Spare'], [('sonos', KITCHEN), None]))
                self.assertEqual(zoneplayers.library_ref(config), ('sonos', KITCHEN))
            finally:
                for backend in connected.values(): backend.close()
        finally:
            fake.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
