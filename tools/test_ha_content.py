#!/usr/bin/env python3
"""Offline tests for the Music menu from Home Assistant (ha_content.py): Browse over real
SOAP to virtual players, a fake Home Assistant media browser behind them, and the CR100's
play actions turned into play_media and sonos.* calls. Needs websockets and Pillow."""
import sys
import http.client
import io
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content
import fake_ha
import ha_backend
import zoneplayers
from fake_ha import media
from ha_client import HomeAssistant
from test_ha_backend import AVT, Events, field, playing_kitchen, wait_for
from test_ha_linein import HA as RegistryHA, Listener as InputListener, Inventory as IdentityInventory
from ha_linein import SonosLineIn
from test_zoneplayers import HOUSEHOLD, soap, zp

CD = '/MediaServer/ContentDirectory/Control'
DIDL = {'d': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/', 'dc': 'http://purl.org/dc/elements/1.1/',
        'upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/'}
TRACK = 'x-file-cifs://nas/Music/So%20What.mp3'


def tree():
    favourites = media('Favorites', '', 'favorites', 'directory', False, children=[
        media('Playlists', 'object.container.playlistContainer', 'favorites_folder', 'playlist', False,
              children=[media('Chill', 'SQ:2', 'favorite_item_id', 'playlist')]),
        media('Radio', 'object.item.audioItem.audioBroadcast', 'favorites_folder', 'genre', False,
              children=[media('WBGO', 'FV:2/23', 'favorite_item_id', 'genre', thumbnail='/api/brands/wbgo.png')]),
        media('Tracks', 'object.item.audioItem.musicTrack', 'favorites_folder', 'track', False,
              children=[media('So What', 'FV:2/20', 'favorite_item_id', 'track')])])
    album = media('Kind of Blue', 'A:ALBUM/Kind%20of%20Blue', 'album', 'album',
                  thumbnail='http://192.168.1.164:1400/getaa?u=x', children=[media('So What', TRACK, 'track', 'track')])
    library = media('Music Library', 'library', 'library', 'directory', False, children=[
        media('Albums', 'A:ALBUM', 'album', 'album', False, children=[album])])
    radio = media('Radio Browser', 'media-source://radio_browser', 'music', 'channel', False, children=[
        media('Station %d' % i, 'media-source://radio_browser/%d' % i, 'audio/mpeg', 'music') for i in range(250)])
    return [media('Sonos', '', 'root', 'directory', False, children=[favourites, library, radio]),
            favourites, library, radio]


def browse(player, object_id, flag='BrowseDirectChildren', start=0, count=500):
    status, body = soap(player, CD, 'ContentDirectory', 'Browse',
                        f'<ObjectID>{object_id}</ObjectID><BrowseFlag>{flag}</BrowseFlag><Filter>dc:title,res</Filter>'
                        f'<StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>')
    if status != 200: return status, field(body, '{urn:schemas-upnp-org:control-1-0}errorCode'), None
    result = ET.fromstring(field(body, 'Result'))
    return (int(field(body, 'NumberReturned')), int(field(body, 'TotalMatches')),
            [entry for entry in result if entry.tag in ('{%s}item' % DIDL['d'], '{%s}container' % DIDL['d'])])


def title(entry): return entry.findtext('dc:title', namespaces=DIDL)
def cls(entry): return entry.findtext('upnp:class', namespaces=DIDL)
def res(entry): return entry.findtext('d:res', namespaces=DIDL)


class Content(unittest.TestCase):
    def setUp(self):
        self.fake = fake_ha.FakeHA([playing_kitchen(), fake_ha.sonos_state('media_player.patio', 'Patio')])
        for node in tree(): self.fake.add_media(node)
        self.fake.queues['media_player.kitchen'] = [
            {'media_title': 'Track %d' % i, 'media_artist': 'Miles Davis', 'media_album_name': 'Kind of Blue',
             'media_content_id': 'x-file-cifs://nas/Music/%d.mp3' % i} for i in range(1, 10)]
        self.logs = []
        self.ha = HomeAssistant(self.fake.url, fake_ha.TOKEN, self.logs.append)
        self.ha.start()
        self.assertTrue(self.ha.connected.wait(5), self.logs)
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio', 'Spare'], ['127.0.0.1'] * 3,
                                           self.logs.append, port=0, backends=[ha_backend.HABackend(self.ha, self.logs.append)],
                                           refs=[('ha', 'media_player.kitchen'), ('ha', 'media_player.patio'), None])
        self.house.start('127.0.0.1', ssdp=False)
        self.kitchen, self.patio, self.spare = self.house.players
        self.events = Events()

    def tearDown(self):
        self.events.close()
        self.house.close()
        self.house.backends['ha'].close()
        self.fake.close()

    def play_calls(self):
        return [data for service, _, data in self.fake.calls if service == 'play_media']

    def test_line_in_source_browse_and_remote_playback_keep_the_destination_queue(self):
        entity, uuid = 'media_player.kitchen', 'RINCON_000E5800000101400'
        backend = self.house.backends['ha']
        backend.line_in.close()
        backend.line_in = SonosLineIn(self.ha, backend.line_in_changed, self.logs.append,
                                     listener=InputListener(),
                                     reader=lambda base, timeout: {'name': 'Audio Component'},
                                     inventory=IdentityInventory())
        registry = RegistryHA()
        with patch.object(self.ha, 'call', side_effect=registry.call):
            self.assertTrue(backend.line_in.refresh())
        self.fake.queues['media_player.patio'] = [{'media_content_id': TRACK, 'media_title': 'Saved queue track'}]
        returned, total, entries = browse(self.kitchen, 'AI:')
        self.assertEqual((returned, total, title(entries[0])), (1, 1, 'Turntable'))
        self.assertEqual(res(entries[0]), 'x-rincon-stream:' + self.kitchen.config.udn)
        self.assertEqual(browse(self.patio, 'AI:')[:2], (0, 0))
        status, body = soap(self.patio, AVT, 'AVTransport', 'SetAVTransportURI',
                           '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>'
                           '<CurrentURIMetaData></CurrentURIMetaData>' % res(entries[0]))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.play_calls()[-1], {'media_content_id': 'x-rincon-stream:' + uuid,
                                                 'media_content_type': 'music'})
        self.assertEqual(len(self.fake.queues['media_player.patio']), 1)
        self.assertTrue(wait_for(lambda: self.patio.state.backend.line_in_source() == entity))
        _, body = soap(self.patio, AVT, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'CurrentURI'), 'x-rincon-stream:' + self.kitchen.config.udn)
        _, body = soap(self.patio, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(title(ET.fromstring(field(body, 'TrackMetaData'))[0]), 'Turntable')
        self.assertEqual(field(body, 'RelTime'), 'NOT_IMPLEMENTED')
        # The bridge sends the requested member to HA. HA's Sonos integration
        # owns coordinator routing; the fake models its shared group media state.
        members = ['media_player.patio', entity]
        self.fake.set(entity, group_members=members)
        self.fake.set('media_player.patio', 'paused', group_members=members)
        self.assertTrue(wait_for(lambda: self.house.coordinator[self.kitchen.config.udn] == self.patio.config.udn))
        before = len(self.fake.calls)
        status, body = soap(self.kitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                           '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>'
                           '<CurrentURIMetaData></CurrentURIMetaData>' % res(entries[0]))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.fake.calls[before][1], entity)
        self.assertTrue(wait_for(lambda: all(self.ha.state(member).get('state') == 'playing' for member in members)))
        self.assertTrue(all(self.ha.state(member)['attributes']['media_content_id'] == 'x-rincon-stream:' + uuid
                            for member in members))

    def test_library_containers_and_tracks(self):
        returned, total, entries = browse(self.kitchen, 'A:ALBUM', 'BrowseMetadata')
        self.assertEqual((returned, total, title(entries[0]), entries[0].get('childCount')), (1, 1, 'Albums', '1'))
        _, total, (album,) = browse(self.kitchen, 'A:ALBUM')
        self.assertEqual((total, album.get('id'), title(album), cls(album)),
                         (1, 'A:ALBUM/Kind%20of%20Blue', 'Kind of Blue', 'object.container.album.musicAlbum'))
        self.assertEqual(res(album), 'x-rincon-playlist:%s#A:ALBUM/Kind%%20of%%20Blue' % self.kitchen.config.udn)
        self.assertTrue(album.findtext('upnp:albumArtURI', namespaces=DIDL).startswith('/art/'))
        _, _, (track,) = browse(self.kitchen, 'A:ALBUM/Kind%20of%20Blue')
        self.assertEqual((title(track), cls(track), res(track)), ('So What', 'object.item.audioItem.musicTrack', TRACK))
        self.assertTrue(track.get('id').startswith('A:ALBUM/Kind%20of%20Blue/'))

    def test_long_lists_are_paged_from_one_home_assistant_answer(self):
        returned, total, entries = browse(self.kitchen, 'R:0/0')
        self.assertEqual((returned, total, title(entries[0])), (content.PAGE_LIMIT, 250, 'Station 0'))
        returned, total, entries = browse(self.kitchen, 'R:0/0', start=200)
        self.assertEqual((returned, total, title(entries[0]), title(entries[-1])), (50, 250, 'Station 200', 'Station 249'))
        self.assertEqual(self.fake.browses.count(('media-source://radio_browser', 'music')), 1)
        # A station plays at once, by its Home Assistant media-source ID.
        station = entries[0]
        self.assertEqual(cls(station), 'object.item.audioItem.audioBroadcast')
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                         f'<InstanceID>0</InstanceID><CurrentURI>{res(station)}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        self.assertEqual(self.play_calls()[-1], {'media_content_id': 'media-source://radio_browser/200',
                                                 'media_content_type': 'audio/mpeg'})

    def test_favourites_are_one_list_and_play_at_once(self):
        _, total, entries = browse(self.kitchen, 'FV:2')
        self.assertEqual([title(e) for e in entries], ['Chill', 'WBGO', 'So What'])
        self.assertEqual({cls(e) for e in entries}, {'object.item.audioItem.audioBroadcast'})
        chill = entries[0]
        self.assertTrue(chill.get('id').startswith('FV:2/'))   # not the Sonos Playlists container SQ:2
        for entry, expected in ((entries[1], 'FV:2/23'), (chill, 'SQ:2')):
            status, _ = soap(self.kitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                             f'<InstanceID>0</InstanceID><CurrentURI>{res(entry)}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
            self.assertEqual(status, 200)
            self.assertEqual(self.play_calls()[-1], {'media_content_id': expected, 'media_content_type': 'favorite_item_id'})

    def test_add_to_queue_as_next_then_play_it(self):
        # Not browsed first (a bridge restart): the Sonos ID alone says it is an album.
        uri = 'x-rincon-playlist:%s#A:ALBUM/Kind%%20of%%20Blue' % self.kitchen.config.udn
        status, body = soap(self.kitchen, AVT, 'AVTransport', 'AddURIToQueue',
                            f'<InstanceID>0</InstanceID><EnqueuedURI>{uri}</EnqueuedURI><EnqueuedURIMetaData></EnqueuedURIMetaData>'
                            '<DesiredFirstTrackNumberEnqueued>4</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>1</EnqueueAsNext>')
        self.assertEqual(status, 200, body)
        self.assertEqual(self.play_calls()[-1], {'media_content_id': 'A:ALBUM/Kind%20of%20Blue',
                                                 'media_content_type': 'album', 'enqueue': 'next'})
        self.assertEqual((field(body, 'FirstTrackNumberEnqueued'), field(body, 'NumTracksAdded'),
                          field(body, 'NewQueueLength')), ('4', '1', '10'))
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'Seek',
                         '<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit><Target>4</Target>')
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ('play_queue', 'media_player.kitchen', {'queue_position': 3}))
        # A library track goes in by its own URI, at the end.
        soap(self.kitchen, AVT, 'AVTransport', 'AddURIToQueue',
             f'<InstanceID>0</InstanceID><EnqueuedURI>{TRACK}</EnqueuedURI><EnqueuedURIMetaData></EnqueuedURIMetaData>'
             '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>')
        self.assertEqual(self.play_calls()[-1], {'media_content_id': TRACK, 'media_content_type': 'music', 'enqueue': 'add'})

    def test_queue_is_browsable_and_edits_reach_the_cr100(self):
        self.assertEqual(self.events.subscribe(self.kitchen, '/MediaServer/ContentDirectory/Event'), 200)
        first = field(self.events.get(), 'ContainerUpdateIDs')
        returned, total, entries = browse(self.kitchen, 'Q:0')
        self.assertEqual((returned, total, entries[1].get('id'), title(entries[1])), (9, 9, 'Q:0/2', 'Track 2'))
        _, _, (meta,) = browse(self.kitchen, 'Q:0', 'BrowseMetadata')
        self.assertEqual(meta.get('childCount'), '9')
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'RemoveTrackFromQueue',
                         '<InstanceID>0</InstanceID><ObjectID>Q:0/2</ObjectID><UpdateID>0</UpdateID>')
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ('remove_from_queue', 'media_player.kitchen', {'queue_position': 1}))
        update = field(self.events.get(), 'ContainerUpdateIDs')
        self.assertNotEqual(update, first)
        self.assertTrue(update.startswith('Q:0,'))
        _, total, entries = browse(self.kitchen, 'Q:0')
        self.assertEqual((total, title(entries[1])), (8, 'Track 3'))
        # A change made elsewhere (the Sonos app) also moves the version.
        self.fake.queues['media_player.kitchen'].append({'media_title': 'New', 'media_content_id': 'x-file-cifs://nas/n.mp3'})
        self.fake.set('media_player.kitchen', queue_size=9)
        self.assertNotEqual(field(self.events.get(), 'ContainerUpdateIDs'), update)
        self.assertTrue(wait_for(lambda: browse(self.kitchen, 'Q:0')[1] == 9))

    def test_native_queue_art_reaches_hover_metadata_and_player_jpeg_proxy(self):
        from PIL import Image
        from sonos_queue_art import SonosQueueArt
        from test_sonos_queue_art import HA, Opener, response
        native = 'http://192.168.1.164:1400/getaa?u=track-one&v=1'
        uri = self.fake.queues['media_player.kitchen'][0]['media_content_id']
        registry = HA()
        opener = Opener(response([(1, uri, '/getaa?u=track-one&v=1')]))
        self.house.backends['ha'].content.queue_art = SonosQueueArt(registry, self.logs.append, opener)
        returned, total, (entry,) = browse(self.kitchen, 'Q:0/1', 'BrowseMetadata')
        self.assertEqual((returned, total, entry.get('id'), res(entry)), (1, 1, 'Q:0/1', uri))
        path = entry.findtext('upnp:albumArtURI', namespaces=DIDL)
        self.assertTrue(path.startswith('/art/'), path)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.fake.calls, [])  # metadata inspection made no service mutation
        with patch.object(self.ha, 'fetch', return_value=self.fake.picture) as fetch:
            connection = http.client.HTTPConnection('127.0.0.1', self.kitchen.config.port, timeout=5)
            connection.request('GET', path)
            reply = connection.getresponse()
            data = reply.read(); connection.close()
            self.assertEqual((reply.status, reply.getheader('Content-Type')), (200, 'image/jpeg'))
            fetch.assert_called_once_with(native)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.format, 'JPEG')
                self.assertLessEqual(max(image.size), 200)
                self.assertFalse(image.info.get('progressive') or image.info.get('progression'))

    def test_queue_art_looks_up_only_visible_rows_and_not_the_queue_header(self):
        helper = self.house.backends['ha'].content.queue_art = Mock()
        helper.lookup.return_value = {0: 'http://192.168.1.164:1400/getaa?u=first&v=1'}
        browse(self.kitchen, 'Q:0', 'BrowseMetadata')
        helper.lookup.assert_not_called()
        returned, total, entries = browse(self.kitchen, 'Q:0', start=2, count=2)
        self.assertEqual((returned, total), (2, 9))
        helper.lookup.assert_called_once_with('media_player.kitchen',
                                             [track['media_content_id'] for track in self.fake.queues['media_player.kitchen'][2:4]], 2)
        self.assertTrue(entries[0].findtext('upnp:albumArtURI', namespaces=DIDL).startswith('/art/'))
        self.assertIsNone(entries[1].findtext('upnp:albumArtURI', namespaces=DIDL))

    def test_queue_keeps_home_assistant_provided_per_item_art(self):
        helper = self.house.backends['ha'].content.queue_art = Mock()
        source = '/api/media_player_proxy/media_player.kitchen?token=queue-item&cache=1'
        self.fake.queues['media_player.kitchen'][0]['thumbnail'] = source
        _, _, (entry,) = browse(self.kitchen, 'Q:0/1', 'BrowseMetadata')
        path = entry.findtext('upnp:albumArtURI', namespaces=DIDL)
        self.assertTrue(path.startswith('/art/'))
        self.assertEqual(self.house.backends['ha'].art.sources[path[len('/art/'):-len('.jpg')]], source)
        helper.lookup.assert_not_called()

    def test_current_track_fallback_requires_matching_uri_and_does_not_leak_to_other_rows(self):
        helper = self.house.backends['ha'].content.queue_art = Mock()
        helper.lookup.return_value = {}
        tracks = self.fake.queues['media_player.kitchen']
        self.fake.set('media_player.kitchen', media_content_id=tracks[2]['media_content_id'])
        self.assertTrue(wait_for(lambda: self.ha.state('media_player.kitchen')['attributes'].get('media_content_id')
                                == tracks[2]['media_content_id']))
        _, _, entries = browse(self.kitchen, 'Q:0', count=5)
        self.assertEqual([i for i, entry in enumerate(entries)
                          if entry.findtext('upnp:albumArtURI', namespaces=DIDL)], [2])
        self.fake.set('media_player.kitchen', media_content_id=tracks[3]['media_content_id'], queue_position=4,
                      entity_picture='/api/media_player_proxy/media_player.kitchen?token=next&cache=2')
        self.assertTrue(wait_for(lambda: self.ha.state('media_player.kitchen')['attributes'].get('queue_position') == 4))
        _, _, entries = browse(self.kitchen, 'Q:0', count=5)
        self.assertEqual([i for i, entry in enumerate(entries)
                          if entry.findtext('upnp:albumArtURI', namespaces=DIDL)], [3])
        self.fake.set('media_player.kitchen', media_content_id='different-stream')
        self.assertTrue(wait_for(lambda: self.ha.state('media_player.kitchen')['attributes'].get('media_content_id') == 'different-stream'))
        _, _, (entry,) = browse(self.kitchen, 'Q:0/4', 'BrowseMetadata')
        self.assertIsNone(entry.findtext('upnp:albumArtURI', namespaces=DIDL))

    def test_unknown_objects_and_simulated_rooms(self):
        self.assertEqual(browse(self.kitchen, 'X:nothing')[:2], (500, '701'))
        self.assertEqual(browse(self.kitchen, 'AI:')[:2], (0, 0))
        self.assertEqual(browse(self.spare, 'A:ALBUM')[1], 1)    # household content, through a real room
        self.assertEqual(browse(self.spare, 'Q:0')[:2], (0, 0))  # but no queue of its own
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                         '<InstanceID>0</InstanceID><CurrentURI>x-sonos-dock:nothing</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 500)


if __name__ == '__main__':
    unittest.main(verbosity=2)
