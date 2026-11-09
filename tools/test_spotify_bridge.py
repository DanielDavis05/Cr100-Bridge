"""SMAPI -> original CR100 URI shapes -> native Sonos queue over real SOAP/HA WS."""
import html
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote
from unittest.mock import patch

import fake_ha
import ha_backend
from ha_client import HomeAssistant
from spotify_service import SpotifyService
from test_ha_backend import AVT, field, wait_for
from test_ha_content import browse
from test_smapi_stub import smapi, NS
from test_zoneplayers import HOUSEHOLD, soap, zp
import zoneplayers

TRACK_ID = '0123456789ABCDEFGHIJKL'
ALBUM_ID = 'abcdefghijklmnopqrstuv'
TRACK_URI = 'spotify:track:' + TRACK_ID
ALBUM_URI = 'spotify:album:' + ALBUM_ID


class Catalogue:
    def __init__(self): self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == '/search':
            return {'tracks': {'items': [{'id': TRACK_ID, 'uri': TRACK_URI, 'type': 'track',
                'name': 'A Real Search Result', 'duration_ms': 200000,
                'artists': [{'name': 'An Artist'}],
                'album': {'id': ALBUM_ID, 'uri': ALBUM_URI, 'name': 'An Album', 'images': []}}],
                'total': 1}}
        raise AssertionError('Unexpected catalogue request: ' + path)


def metadata(uri, prefix):
    item = prefix + quote(uri, safe='')
    return ('<DIDL-Lite><item id="%s" parentID="root"><desc id="cdudn">'
            'SA_RINCON61447_bridge</desc></item></DIDL-Lite>' % item)


def add(player, uri, meta='', next_=False):
    return soap(player, AVT, 'AVTransport', 'AddURIToQueue',
        '<InstanceID>0</InstanceID><EnqueuedURI>%s</EnqueuedURI>'
        '<EnqueuedURIMetaData>%s</EnqueuedURIMetaData>'
        '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>'
        '<EnqueueAsNext>%d</EnqueueAsNext>' % (html.escape(uri), html.escape(meta), next_))


class SpotifyBridge(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.fake = fake_ha.FakeHA([
            fake_ha.sonos_state('media_player.kitchen', 'Kitchen', state='playing',
                                queue_size=3, queue_position=2, media_title='Existing Track'),
            fake_ha.sonos_state('media_player.patio', 'Patio')])
        self.fake.queues['media_player.kitchen'] = [
            {'media_title': 'Existing %d' % i, 'media_content_id': 'http://music.invalid/%d.mp3' % i}
            for i in range(3)]
        self.ha = HomeAssistant(self.fake.url, fake_ha.TOKEN, self.logs.append)
        self.ha.start()
        self.assertTrue(self.ha.connected.wait(5), self.logs)
        self.client = Catalogue()
        self.service = SpotifyService('127.0.0.1', 0, self.client, log=self.logs.append)
        self.service.start()
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio', 'Spare'],
            ['127.0.0.1'] * 3, self.logs.append, port=0, music_service=self.service,
            backends=[ha_backend.HABackend(self.ha, self.logs.append)],
            refs=[('ha', 'media_player.kitchen'), ('ha', 'media_player.patio'), None])
        self.house.start('127.0.0.1', ssdp=False)
        self.kitchen, self.patio, self.spare = self.house.players

    def tearDown(self):
        self.house.close()
        self.service.close()
        self.house.backends['ha'].close()
        self.fake.close()

    def test_search_selection_appends_without_replacing_sonos_queue(self):
        status, doc = smapi(self.service, 'search', {'id': 'tracks', 'term': 'Artist', 'index': 0, 'count': 499})
        self.assertEqual(status, 200)
        self.assertEqual(doc.find('.//s:mediaMetadata/s:id', NS).text, TRACK_URI)
        uri = 'x-sonos-http:%s.mp3?sid=240&flags=0' % quote(TRACK_URI, safe='')
        status, body = add(self.kitchen, uri, metadata(TRACK_URI, '00030000'))
        self.assertEqual(status, 200, body)
        self.assertEqual([field(body, name) for name in
            ('FirstTrackNumberEnqueued', 'NumTracksAdded', 'NewQueueLength')], ['4', '1', '4'])
        self.assertIn(('play_media', 'media_player.kitchen',
            {'media_content_id': TRACK_URI, 'media_content_type': 'music', 'enqueue': 'add'}), self.fake.calls)
        self.assertEqual(self.fake.queues['media_player.kitchen'][0]['media_title'], 'Existing 0')
        self.assertEqual(browse(self.kitchen, 'Q:0')[1], 4)

    def test_play_next_and_native_queue_controls(self):
        uri = 'x-sonos-http:%s.mp3?sid=240&flags=0' % quote(TRACK_URI, safe='')
        status, body = add(self.kitchen, uri, metadata(TRACK_URI, '00030000'), next_=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'FirstTrackNumberEnqueued'), '3')
        self.assertEqual(self.fake.queues['media_player.kitchen'][2]['media_content_id'], TRACK_URI)
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'Seek',
            '<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit><Target>3</Target>')
        self.assertEqual(status, 200)
        self.assertIn(('play_queue', 'media_player.kitchen', {'queue_position': 2}), self.fake.calls)
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'RemoveTrackFromQueue',
            '<InstanceID>0</InstanceID><ObjectID>Q:0/3</ObjectID><UpdateID>0</UpdateID>')
        self.assertEqual(status, 200)
        self.assertEqual(len(self.fake.queues['media_player.kitchen']), 3)
        status, _ = soap(self.kitchen, AVT, 'AVTransport', 'RemoveAllTracksFromQueue', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.queues['media_player.kitchen'], [])

    def test_native_container_plays_on_selected_group_coordinator(self):
        self.house.coordinator[self.patio.config.udn] = self.kitchen.config.udn
        uri = 'x-rincon-cpcontainer:0004004c' + quote(ALBUM_URI, safe='')
        status, body = add(self.patio, uri, metadata(ALBUM_URI, '0004004c'))
        self.assertEqual(status, 200, body)
        self.assertIn(('play_media', 'media_player.kitchen',
            {'media_content_id': ALBUM_URI, 'media_content_type': 'album', 'enqueue': 'add'}), self.fake.calls)
        self.assertFalse(any(entity == 'media_player.patio' for _, entity, _ in self.fake.calls))

    def test_play_now_replaces_queue_and_invalid_items_do_not_touch_speakers(self):
        uri = 'x-sonos-http:%s.mp3?sid=240&flags=0' % quote(TRACK_URI, safe='')
        status, body = soap(self.kitchen, AVT, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData>%s</CurrentURIMetaData>'
            % (html.escape(uri), html.escape(metadata(TRACK_URI, '00030000'))))
        self.assertEqual(status, 200, body)
        self.assertIn(('play_media', 'media_player.kitchen',
            {'media_content_id': TRACK_URI, 'media_content_type': 'music', 'enqueue': 'replace'}), self.fake.calls)
        self.assertEqual(len(self.fake.queues['media_player.kitchen']), 1)
        calls = len(self.fake.calls)
        self.assertEqual(add(self.kitchen, 'x-sonos-http:spotify%3atrack%3abad.mp3?sid=240')[0], 500)
        self.assertEqual(add(self.spare, uri, metadata(TRACK_URI, '00030000'))[0], 500)
        self.fake.fail_services = True
        self.assertEqual(add(self.kitchen, uri, metadata(TRACK_URI, '00030000'))[0], 500)
        self.assertEqual(len(self.fake.queues['media_player.kitchen']), 1)
        self.assertEqual(len(self.fake.calls), calls + 1)  # only the attempted offline HA call


class Configuration(unittest.TestCase):
    def test_restart_generation_matches_discovery_and_topology(self):
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Probe'], ['127.0.0.1'],
                                      lambda r: None, port=0, boot_sequence=1234567)
        player = house.players[0]
        try:
            self.assertIn(b'X-RINCON-BOOTSEQ: 1234567', player.ssdp_packet(zp.ZONE_PLAYER, alive=True))
            self.assertIn('BootSeq="1234567"', house.topology())
        finally:
            player.http.server_close()

    def test_spotify_precedes_stub_and_relative_token_path_uses_config_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'bridge.json'
            settings = {'spotify_music_service': {'host': '127.0.0.1', 'port': 0,
                        'client_id': '', 'token_file': 'credentials/spotify.json'},
                        'music_service_stub': {'host': '127.0.0.1', 'port': 0},
                        'speaker_settings': {'enabled': False}}
            config.write_text(json.dumps(settings))
            with patch('spotify_client.SpotifyClient') as client:
                service = zoneplayers.load_music_service(config, '127.0.0.1', lambda r: None)
                self.assertIsInstance(service, SpotifyService)
                self.assertEqual(client.call_args.args[1], Path(folder).resolve() / 'credentials/spotify.json')
                self.assertEqual(service.name, 'Spotify')
                service.server.server_close()
            settings['spotify_music_service']['enabled'] = False
            config.write_text(json.dumps(settings))
            service = zoneplayers.load_music_service(config, '127.0.0.1', lambda r: None)
            self.assertEqual(service.name, 'Bridge Test')
            service.server.server_close()


if __name__ == '__main__': unittest.main()
