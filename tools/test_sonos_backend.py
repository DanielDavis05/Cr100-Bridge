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
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
from backends import ArtStore
from backends import BackendError
from provider_playback import QueueItem

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

    def test_all_sonos_players_unreachable_exposes_bridge_until_a_player_recovers(self):
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio'], ['127.0.0.1'] * 2,
            lambda _: None, port=0, backends=[self.backend], refs=[('sonos', KITCHEN), ('sonos', PATIO)])
        try:
            self.assertNotIn(['CR100 Bridge'], groups(house.topology()))
            with self.backend.lock:
                for speaker in self.backend.speakers.values(): speaker.available = False
                house.refresh_identities()
                self.assertIn(['CR100 Bridge'], groups(house.topology()))
                self.backend.speakers[KITCHEN].available = True
                house.refresh_identities()
                self.assertNotIn(['CR100 Bridge'], groups(house.topology()))
        finally:
            house.close()

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

    def test_another_provider_plays_and_enqueues_through_its_registered_adapter(self):
        prepared = []
        class LibraryAdapter:
            def prepare(self, uri, kind):
                prepared.append((uri, kind))
                if not uri.startswith('library:album:') or kind != 'album':
                    raise BackendError('Expected a library album')
                return [QueueItem('x-library:' + uri.removeprefix('library:'),
                                  '<library-account>example</library-account>')]

        self.backend.playback_adapters.register('library', LibraryAdapter())
        room = self.vkitchen.state.backend
        self.kitchen.queue = [('http://music.invalid/existing.mp3', '')]
        room.play_provider('library:album:blue', 'album')
        self.assertEqual(self.kitchen.queue, [('x-library:album:blue',
                                              '<library-account>example</library-account>')])
        self.assertEqual(self.kitchen.transport, 'PLAYING')
        result = room.enqueue_provider('library:album:green', 'album', as_next=True)
        self.assertEqual(result, {'FirstTrackNumberEnqueued': '2', 'NumTracksAdded': '1', 'NewQueueLength': '2'})
        self.assertEqual(calls(self.kitchen, 'AddURIToQueue')[-1]['EnqueueAsNext'], '1')
        self.assertEqual(self.kitchen.queue[-1][0], 'x-library:album:green')
        self.assertEqual(prepared, [('library:album:blue', 'album'), ('library:album:green', 'album')])

    def test_invalid_or_unsupported_provider_items_preserve_the_existing_queue(self):
        room = self.vkitchen.state.backend
        original = [('http://music.invalid/existing.mp3', 'existing metadata')]
        self.kitchen.queue = list(original)
        self.kitchen.transport = 'PAUSED_PLAYBACK'
        mutations = {'RemoveAllTracksFromQueue', 'AddURIToQueue', 'SetAVTransportURI', 'Seek', 'Play'}
        for uri, kind, message in (
                ('unknown:album:blue', 'album', 'not supported'),
                ('spotify:artist:example', 'music', 'Not a supported Spotify item'),
                ('spotify:album:example', 'music', 'does not match')):
            for operation in (room.play_provider, room.enqueue_provider):
                with self.subTest(uri=uri, operation=operation.__name__):
                    with self.assertRaisesRegex(BackendError, message): operation(uri, kind)
                    self.assertEqual(self.kitchen.queue, original)
                    self.assertEqual(self.kitchen.transport, 'PAUSED_PLAYBACK')
        self.assertEqual([name for name, _ in self.kitchen.calls if name in mutations], [])

    def test_adapter_validation_finishes_before_replacing_the_queue(self):
        class BrokenAdapter:
            def prepare(self, uri, kind):
                yield QueueItem('http://music.invalid/first.mp3', '')
                raise BackendError('The rest of this album cannot be resolved')

        self.backend.playback_adapters.register('broken', BrokenAdapter())
        self.kitchen.queue = [('http://music.invalid/existing.mp3', '')]
        with self.assertRaisesRegex(BackendError, 'cannot be resolved'):
            self.vkitchen.state.backend.play_provider('broken:album:example', 'album')
        self.assertEqual(self.kitchen.queue, [('http://music.invalid/existing.mp3', '')])
        self.assertEqual(calls(self.kitchen, 'RemoveAllTracksFromQueue'), [])
        self.assertEqual(calls(self.kitchen, 'AddURIToQueue'), [])

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

    def connect_line_in(self):
        self.kitchen.line_in_connected = True
        self.kitchen.line_in_name = 'Turntable & CD'
        self.backend.speaker(KITCHEN).audio_checked = False
        self.backend.read_audio_in(self.backend.speaker(KITCHEN))
        self.assertTrue(wait_for(lambda: len(self.backend.line_in_sources()) == 1), self.logs)

    def test_line_in_discovery_tracks_the_connection_and_name(self):
        self.connect_line_in()
        self.assertEqual(self.backend.line_in_sources(), [
            {'id': KITCHEN, 'name': 'Turntable & CD', 'room_name': 'Kitchen',
             'icon': 'AudioComponent', 'uri': 'x-rincon-stream:' + KITCHEN}])
        self.kitchen.line_in_name = 'Tape Deck'
        self.kitchen.notify('AudioIn')
        self.assertTrue(wait_for(lambda: self.backend.line_in_sources()[0]['name'] == 'Tape Deck'))
        # A Browse item and a named input still exist after unplugging; GENA is authoritative.
        self.kitchen.line_in_connected = False
        self.kitchen.notify('AudioIn')
        self.assertTrue(wait_for(lambda: self.backend.line_in_sources() == []))
        self.assertTrue(sonos_upnp.read_line_in(self.kitchen.base)['name'])

    def test_line_in_browsing_and_playback_translate_the_source_uuid(self):
        self.connect_line_in()
        uri = 'x-rincon-stream:' + self.vkitchen.config.udn
        status, body = browse(self.vkitchen, 'AI:')
        self.assertEqual((status, field(body, 'NumberReturned'), field(body, 'TotalMatches')), (200, '1', '1'))
        node = sonos_upnp.parse_didl(field(body, 'Result'))[0]
        self.assertEqual(node['res'], uri)
        self.assertEqual(node['id'], 'AI:0')
        self.assertEqual(node['title'], 'Turntable & CD')
        self.assertEqual(node['class'], 'object.item.audioItem')
        status, body = browse(self.vpatio, 'AI:')
        self.assertEqual((status, field(body, 'TotalMatches')), (200, '0'))
        for player in (self.vkitchen, self.vpatio):
            status, _ = soap(player, AVT, 'AVTransport', 'SetAVTransportURI',
                             '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData/>' % uri)
            self.assertEqual(status, 200)
            real = self.kitchen if player is self.vkitchen else self.patio
            self.assertEqual(calls(real, 'SetAVTransportURI')[-1]['CurrentURI'], 'x-rincon-stream:' + KITCHEN)
            self.backend.read(self.backend.speaker(real.uuid))
            _, position = soap(player, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
            self.assertEqual(field(position, 'TrackURI'), uri)
            self.assertEqual(field(position, 'RelTime'), 'NOT_IMPLEMENTED')
            self.assertEqual(sonos_upnp.parse_didl(field(position, 'TrackMetaData'))[0]['title'], 'Turntable & CD')
        self.assertEqual(self.vkitchen.state.event_values('AudioIn')['Playing'], '1')
        self.assertEqual(self.vpatio.state.event_values('AudioIn')['LineInConnected'], '0')

    def test_line_in_pause_key_stops_and_play_resumes_the_selected_input(self):
        self.connect_line_in()
        uri = 'x-rincon-stream:' + self.vkitchen.config.udn
        status, body = soap(self.vpatio, AVT, 'AVTransport', 'SetAVTransportURI',
                            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData/>' % uri)
        self.assertEqual(status, 200, body)
        self.backend.read(self.backend.speaker(PATIO))
        status, body = soap(self.vpatio, AVT, 'AVTransport', 'GetCurrentTransportActions', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200, body)
        self.assertIn('Pause', field(body, 'Actions').split(','))
        status, body = soap(self.vpatio, AVT, 'AVTransport', 'Pause', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200, body)
        self.assertTrue(calls(self.patio, 'Stop'))
        self.assertEqual(calls(self.patio, 'Pause'), [])
        self.backend.read(self.backend.speaker(PATIO))
        self.assertEqual(self.vpatio.state.backend.transport, 'STOPPED')
        self.assertEqual(self.vpatio.state.backend.line_in_source(), KITCHEN)
        status, body = soap(self.vpatio, AVT, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
        self.assertEqual(status, 200, body)
        self.assertTrue(calls(self.patio, 'Play'))
        self.assertEqual(calls(self.patio, 'RemoveAllTracksFromQueue'), [])

    def test_expired_audio_in_subscription_hides_the_source_until_recovered(self):
        self.connect_line_in()
        subscription = self.backend.speaker(KITCHEN).audio_subscription
        subscription.expires = time.monotonic() - 1
        self.assertEqual(self.backend.line_in_sources(), [])
        subscription.expires = time.monotonic() + 1800
        self.assertEqual(len(self.backend.line_in_sources()), 1)

    def test_line_in_without_track_metadata_and_playing_on_another_player(self):
        self.connect_line_in()
        for player, room in ((self.kitchen, self.vkitchen.state.backend),
                             (self.patio, self.vpatio.state.backend)):
            player.uri = 'x-rincon-stream:' + KITCHEN
            player.transport = 'PLAYING'
            player.track_metadata, player.track_duration, player.position = 'NOT_IMPLEMENTED', 'NOT_IMPLEMENTED', 'NOT_IMPLEMENTED'
            player.notify('AVTransport')
            self.assertTrue(wait_for(lambda: room.line_in_source() == KITCHEN))
            self.assertEqual(room.track(), ('Turntable & CD', '', 'Kitchen', '', None))
            self.assertEqual(room.position(), 0)
            self.assertEqual(room.capabilities(), frozenset())
            self.assertEqual(room.queue_numbers(), (1, 1))
            room.pause()
            self.assertEqual(player.calls[-1][0], 'Stop')

    def test_tv_input_shows_tv_not_the_player_uuid(self):
        # A soundbar playing its TV input reports its own UUID as the track title.
        room = self.vkitchen.state.backend
        self.kitchen.uri = 'x-sonos-htastream:' + KITCHEN + ':spdif'
        self.kitchen.transport = 'PLAYING'
        self.kitchen.actions = 'Set, Stop, Play'
        self.kitchen.track_metadata = DIDL_OPEN + entry('-1', KITCHEN, '-1', 'object.item.audioItem') + '</DIDL-Lite>'
        self.kitchen.track_duration = '0:00:00'
        self.kitchen.notify('AVTransport')
        self.assertTrue(wait_for(lambda: room.tv_source() == KITCHEN), self.logs)
        self.assertEqual(room.track(), ('TV', '', 'Kitchen', '', None))
        self.assertEqual(room.position(), 0)
        self.assertEqual(room.queue_numbers(), (1, 1))
        self.assertNotIn('seek', room.capabilities())
        self.assertNotIn(KITCHEN, room.values(self.vkitchen.config.udn)['CurrentTrackMetaData'])
        room.pause()
        self.assertEqual(self.kitchen.calls[-1][0], 'Stop')

    def test_select_line_in_uses_the_source_uuid_and_destination_coordinator(self):
        self.connect_line_in()
        self.fake.regroup(PATIO, KITCHEN)
        self.assertTrue(wait_for(lambda: self.backend.coordinator_of(PATIO) == KITCHEN))
        self.vpatio.state.backend.play_line_in(KITCHEN)
        self.assertEqual(calls(self.kitchen, 'SetAVTransportURI')[-1],
                         {'InstanceID': '0', 'CurrentURI': 'x-rincon-stream:' + KITCHEN, 'CurrentURIMetaData': ''})
        self.assertEqual(calls(self.patio, 'SetAVTransportURI'), [])
        self.assertEqual(calls(self.kitchen, 'AddURIToQueue'), [])
        self.assertEqual(len(calls(self.kitchen, 'Play')), 1)
        self.kitchen.line_in_connected = False
        self.kitchen.notify('AudioIn')
        self.assertTrue(wait_for(lambda: self.backend.line_in_sources() == []))
        with self.assertRaises(sonos_backend.BackendError): self.vpatio.state.backend.play_line_in(KITCHEN)

    def test_removed_input_player_is_no_longer_discovered(self):
        self.connect_line_in()
        self.backend.apply_topology([(PATIO, [PATIO])],
                                    {PATIO: {'base': self.patio.base, 'name': 'Patio', 'icon': ''}})
        self.assertEqual(self.backend.line_in_sources(), [])
        self.assertIsNone(self.backend.speaker(KITCHEN))


class AlbumArtFetch(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(sonos_backend.SonosBackend)

    def test_non_http_schemes_and_missing_hosts_never_reach_the_opener(self):
        for picture in ('file:///etc/hostname', 'ftp://speaker/picture', 'data:image/png,test',
                        'http:///picture', '/getaa', 'https:picture'):
            with self.subTest(picture=picture), patch('sonos_backend.urllib.request.urlopen') as open_url:
                with self.assertRaises(ValueError): self.backend.fetch(picture)
                open_url.assert_not_called()

    def test_http_and_https_art_keep_the_existing_limit_and_timeout(self):
        for picture in ('http://speaker/getaa', 'https://speaker/getaa'):
            with self.subTest(picture=picture), patch('sonos_backend.urllib.request.urlopen') as open_url:
                open_url.return_value.__enter__.return_value.read.return_value = b'picture'
                self.assertEqual(self.backend.fetch(picture), b'picture')
                open_url.assert_called_once_with(picture, timeout=10)
                open_url.return_value.__enter__.return_value.read.assert_called_once_with(sonos_backend.ART_LIMIT)


class SpeakerReadFailures(unittest.TestCase):
    def setUp(self):
        self.fake = fake_sonos.FakeHousehold()
        self.real = self.fake.add(KITCHEN, 'Kitchen')
        self.addCleanup(self.fake.close)
        self.logs = []
        self.backend = sonos_backend.SonosBackend(log=self.logs.append)
        self.backend.listener.start()
        self.addCleanup(self.backend.close)
        self.speaker = sonos_backend.Speaker(KITCHEN, self.real.base, 'Kitchen')
        self.speaker.audio_checked = True
        self.backend.speakers[KITCHEN] = self.speaker
        self.backend.groups = [(KITCHEN, [KITCHEN])]
        self.backend.read(self.speaker)
        self.real.reject['GetTransportInfo'] = 501

    def read_at(self, now):
        with patch('sonos_backend.time.monotonic', return_value=now):
            self.backend.read(self.speaker)

    def test_one_failed_read_keeps_the_selected_room(self):
        self.read_at(100)
        available, pending = self.backend.selection_snapshot()
        self.assertEqual(available, {'sonos:' + KITCHEN: 'Kitchen'})
        self.assertEqual(pending, {})
        self.assertEqual(self.speaker.failures, 1)
        self.assertEqual(len([entry for entry in self.logs if entry['event'] == 'sonos-read-failed']), 1)

    def test_withdrawal_requires_both_the_count_and_duration(self):
        for now in (100, 101, 102, 119):
            self.read_at(now)
            self.assertTrue(self.speaker.available)
        self.read_at(120)
        self.assertFalse(self.speaker.available)
        self.assertEqual(self.backend.selection_snapshot(), ({}, {}))

    def test_two_failures_over_twenty_seconds_keep_the_room(self):
        self.read_at(100)
        self.read_at(125)
        self.assertTrue(self.speaker.available)
        self.read_at(126)
        self.assertFalse(self.speaker.available)

    def test_a_success_resets_the_failure_window(self):
        self.read_at(100)
        self.read_at(110)
        self.real.reject.clear()
        self.read_at(120)
        self.assertEqual((self.speaker.failures, self.speaker.failed_since), (0, None))
        self.real.reject['GetTransportInfo'] = 501
        self.read_at(121)
        self.read_at(130)
        self.assertTrue(self.speaker.available)
        self.assertEqual(self.speaker.failed_since, 121)

    def test_events_reset_the_failure_window(self):
        for callback, values in (
                (self.backend.on_avt, {'LastChange': '<Event><InstanceID val="0"><TransportState val="PLAYING"/></InstanceID></Event>'}),
                (self.backend.on_rendering, {'LastChange': '<Event><InstanceID val="0"><Volume channel="Master" val="20"/></InstanceID></Event>'}),
                (self.backend.on_content, {'ContainerUpdateIDs': 'Q:0,1'}),
                (self.backend.on_audio_in, {'LineInConnected': '0'})):
            with self.subTest(event=callback.__name__):
                self.read_at(100)
                self.read_at(110)
                callback(KITCHEN, values)
                self.assertEqual((self.speaker.failures, self.speaker.failed_since), (0, None))
                self.read_at(125)
                self.assertTrue(self.speaker.available)

    def test_topology_events_reset_the_reporting_players_failure_window(self):
        self.read_at(100)
        self.read_at(110)
        self.backend.topology_base = self.real.base
        self.backend.on_topology({'ZoneGroupState': self.fake.zone_group_state()})
        self.assertEqual((self.speaker.failures, self.speaker.failed_since), (0, None))
        self.read_at(125)
        self.assertTrue(self.speaker.available)

    def test_failing_players_are_polled_even_with_healthy_subscriptions(self):
        self.speaker.subscriptions = [SimpleNamespace(healthy=True)]
        self.addCleanup(self.speaker.subscriptions.clear)
        self.read_at(100)
        with patch('sonos_backend.time.monotonic', return_value=104):
            self.assertEqual(self.backend.due(), [self.speaker])

    def test_topology_removal_is_immediate_during_the_grace_period(self):
        self.read_at(100)
        self.backend.apply_topology([], {})
        self.assertIsNone(self.backend.speaker(KITCHEN))
        self.assertEqual(self.backend.selection_snapshot(), ({}, {}))


class ConnectState(unittest.TestCase):
    """Connect keeps a session URI while GetPositionInfo changes the song."""

    def setUp(self):
        self.backend = object.__new__(sonos_backend.SonosBackend)
        self.backend.lock, self.backend.wake = threading.RLock(), threading.Event()
        self.backend.listeners, self.backend.log = [], lambda record: None
        self.backend.groups = [(KITCHEN, [KITCHEN])]
        self.backend.settings = None
        self.speaker = sonos_backend.Speaker(KITCHEN, 'http://127.0.0.1:1400', 'Kitchen')
        self.backend.speakers = {KITCHEN: self.speaker}
        self.speaker.position_at = time.monotonic()
        self.speaker.volume, self.speaker.available = 20, True
        self.uri = 'x-sonos-vli:RINCON_SESSION:2,spotify,context-hash'
        self.speaker.avt = {
            'TransportState': 'PLAYING', 'AVTransportURI': self.uri,
            'CurrentTrackURI': self.uri, 'CurrentTrack': '1', 'NumberOfTracks': '1',
            'CurrentTrackDuration': '0:03:00', 'CurrentPlayMode': 'NORMAL',
            'CurrentTransportActions': 'Play,Pause,Stop,Next,Previous,Seek',
            'AVTransportURIMetaData': DIDL_OPEN + entry('context', 'Spotify', '-1',
                'object.item.audioItem.linein') + '</DIDL-Lite>',
            'CurrentTrackMetaData': self.metadata('First song', 'First artist', 'First album'),
        }
        # Exercise the real revision-aware URL mapping without starting a fetch.
        art = ArtStore(self.backend)
        self.backend.art = SimpleNamespace(path_for=lambda picture, *, revision=None:
            art.path_for(picture, prefetch=False, revision=revision))
        self.room = sonos_backend.SonosRoom(self.backend, KITCHEN)

    @staticmethod
    def metadata(title, artist, album):
        return DIDL_OPEN + entry('-1', title, '-1', artist=artist, album=album,
            art='/getaa?spotify=current') + '</DIDL-Lite>'

    def event(self, **values):
        change = '<Event><InstanceID val="0">%s</InstanceID></Event>' % ''.join(
            '<%s val="%s"/>' % (key, html.escape(str(value), quote=True)) for key, value in values.items())
        self.backend.on_avt(KITCHEN, {'LastChange': change})

    def test_connect_song_metadata_wins_over_spotify_context_and_changes_art_revision(self):
        first = self.room.track()
        self.assertEqual(first[:3], ('First song', 'First artist', 'First album'))
        first_values = self.room.values('VIRTUAL')
        metadata = self.metadata('Second song', 'Second artist', 'Second album')
        self.event(CurrentTrackMetaData=metadata)
        second = self.room.track()
        self.assertEqual(second[:3], ('Second song', 'Second artist', 'Second album'))
        self.assertEqual(self.room.avt['CurrentTrackURI'], self.uri)
        self.assertNotEqual(first[3], second[3])  # same source art URL, new song
        second_values = self.room.values('VIRTUAL')
        self.assertNotEqual(first_values['CurrentTrackURI'], second_values['CurrentTrackURI'])
        self.assertIn('Second artist', second_values['CurrentTrackMetaData'])
        self.assertNotIn('First song', second_values['CurrentTrackMetaData'])

    def test_position_reconciliation_recovers_missing_track_event_without_reentry(self):
        # A phone advances the track, but no GENA metadata event arrives.
        metadata = self.metadata('Recovered song', 'Recovered artist', 'Recovered album')
        response = {'Track': '1', 'TrackDuration': '0:04:00', 'TrackURI': self.uri,
                    'TrackMetaData': metadata, 'RelTime': '0:00:12'}
        notifications = []
        self.backend.listeners.append(lambda key: notifications.append((key, self.room.position())))
        with patch('sonos_backend.soap', return_value=response) as read:
            self.backend.read_position(self.speaker)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.room.track()[:3], ('Recovered song', 'Recovered artist', 'Recovered album'))
            self.assertEqual(self.room.avt['CurrentTrackDuration'], '0:04:00')
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0][0], KITCHEN)
            self.assertTrue(12 <= notifications[0][1] < 13)
            # A position-only change must not generate an endless state publish.
            response['RelTime'] = '0:00:13'
            self.backend.read_position(self.speaker)
            self.assertEqual(len(notifications), 1)

    def test_real_player_dlna_seek_action_enables_seek(self):
        # Exact strings read from an S1 ZP80 and an S2 player on a Spotify queue.
        for actions, expected in (
                ('Set, Stop, Pause, Play, X_DLNA_SeekTime, Next, X_DLNA_SeekTrackNr', {'seek', 'next'}),
                ('Set, Stop, Pause, Play, X_DLNA_SeekTime, Previous, X_DLNA_SeekTrackNr', {'seek', 'previous'}),
                ('Set, Stop, Pause, Play, X_DLNA_SeekTime, Next, Previous, X_DLNA_SeekTrackNr',
                 {'seek', 'next', 'previous'}),
                ('Set, Stop, Play', set())):
            with self.subTest(actions=actions):
                self.speaker.avt['CurrentTransportActions'] = actions
                self.assertEqual(self.room.capabilities(), frozenset(expected))
                self.assertEqual('Seek' in self.room.transport_actions().split(','), 'seek' in expected)

    def test_partial_track_transitions_coalesce_a_prompt_metadata_read(self):
        for index in range(2, 20):
            self.event(CurrentTrack=index)
        self.assertEqual(len(self.speaker.refresh_at), 1)
        self.assertLessEqual(self.speaker.refresh_at[0] - time.monotonic(), sonos_backend.REFRESH_AFTER[0])
        self.assertTrue(self.backend.wake.is_set())


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
