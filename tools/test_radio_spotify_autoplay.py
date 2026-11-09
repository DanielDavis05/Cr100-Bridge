"""Radio and global alarms coexist with the deployed Spotify continuation path."""
import html
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from urllib.parse import urlsplit

from alarms import PlaybackRequest
from radio_catalog import RadioItem
from radio_playback import radio_uri
from radio_runtime import configure_sources
from radiotime import RadioTimeClient
import test_spotify_autoplay_integration as autoplay_fixture
from test_spotify_autoplay import SEED, NEXT
from test_ha_backend import AVT
from test_zoneplayers import soap


class RadioAutoplayCoexistence(unittest.TestCase):
    wire = autoplay_fixture.AutoplayRouting.wire
    play = autoplay_fixture.AutoplayRouting.play
    enqueue = autoplay_fixture.AutoplayRouting.enqueue
    tick = autoplay_fixture.AutoplayRouting.tick
    finish = autoplay_fixture.AutoplayRouting.finish

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        autoplay_fixture.AutoplayRouting.setUp(self)
        self.config = Path(self.temp.name) / 'bridge-config.json'
        self.original_config = {'spotify_music_service': {'enabled': True, 'autoplay': True},
                                'radio': {'port': 0}, 'alarms': {'timezone': 'America/New_York'}}
        self.config.write_text(json.dumps(self.original_config))

        def transport(url, **kwargs):
            if urlsplit(url).path.endswith('Describe.ashx'):
                body = [{'element': 'station', 'guide_id': 's1', 'name': 'Test Radio'}]
            elif urlsplit(url).path.endswith('Tune.ashx'):
                body = [{'element': 'audio', 'url': 'https://broadcast.test/live.mp3', 'media_type': 'mp3'}]
            else:
                body = []
            return {'head': {'status': '200'}, 'body': body}

        configure_sources(self.house, self.config, '127.0.0.1',
                          client=RadioTimeClient(transport, min_request_interval=0), native_port=0)
        self.house.radio_store.update_settings({'cover_lookup': False})

    def test_radio_composition_keeps_existing_autoplay_and_continuation(self):
        self.assertIs(self.house.music_service.services[0], self.service)
        self.assertEqual(self.house.playback_policies, [self.policy])
        self.assertEqual(json.loads(self.config.read_text()), self.original_config)
        self.assertEqual(len(ET.fromstring(self.house.radio_service.media_servers())), 0)
        self.play()
        run = next(iter(self.policy.runs.values()))
        self.finish(run)
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED, NEXT])

    def test_native_radio_selection_releases_autoplay_then_spotify_can_restart(self):
        self.play()
        run = next(iter(self.policy.runs.values()))
        status, body = soap(self.player, AVT, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>'
            % html.escape(radio_uri('station:s1')))
        self.assertEqual(status, 200, body)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(self.speaker.uri, 'https://broadcast.test/live.mp3')
        self.assertEqual(self.speaker.queue, [])
        self.assertTrue(self.enabled)
        self.play()
        restarted = next(iter(self.policy.runs.values()))
        self.assertIsNot(restarted, run)
        self.finish(restarted)
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'],
                         [SEED, SEED, NEXT])
        self.assertIsNone(self.house.radio_playback.session(self.player.config.udn))

    def test_radio_alarm_releases_spotify_continuation_without_changing_preference(self):
        self.play()
        run = next(iter(self.policy.runs.values()))
        context = {'room': self.player.config.udn, 'include_linked': False, 'volume': 25,
                   'media_ref': {'provider': 'radio', 'content_id': 'station:s1'}, 'run_id': 'coexistence'}
        handle = self.house.alarm_runtime.execute(PlaybackRequest('live', 'station:s1'), context)
        self.assertEqual(handle, 'coexistence')
        self.assertTrue(run.cancelled.is_set())
        self.assertTrue(self.house.alarm_runtime.owns(context, handle))
        self.assertEqual(self.speaker.uri, 'https://broadcast.test/live.mp3')
        self.assertTrue(self.enabled)
        self.assertEqual(json.loads(self.config.read_text()), self.original_config)

    def test_rejected_spotify_selection_preserves_radio_alarm_ownership(self):
        context = {'room': self.player.config.udn, 'include_linked': False, 'volume': 25,
                   'media_ref': {'provider': 'radio', 'content_id': 'station:s1'}, 'run_id': 'denied-selection'}
        handle = self.house.alarm_runtime.execute(PlaybackRequest('live', 'station:s1'), context)
        session = self.house.radio_playback.session(self.player.config.udn)
        with patch.object(self.house.music_service, 'resolve_playback', side_effect=ValueError('Account is locked')):
            status, body = soap(self.player, AVT, 'AVTransport', 'SetAVTransportURI',
                '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>'
                % html.escape(self.wire()))
        self.assertEqual(status, 500, body)
        self.assertIs(self.house.radio_playback.session(self.player.config.udn), session)
        self.assertTrue(self.house.alarm_runtime.owns(context, handle))
        self.assertEqual(self.speaker.uri, 'https://broadcast.test/live.mp3')

    def test_explicit_radio_episode_is_next_in_spotify_continuation_queue(self):
        self.play()
        run = next(iter(self.policy.runs.values()))
        with patch.object(self.house.radio_catalog, 'metadata',
                          return_value=RadioItem('episode:t1', 'Explicit episode', 'episode', duration=100)):
            self.enqueue(radio_uri('episode:t1'), as_next=True)
        self.assertEqual(run.queue, ['https://broadcast.test/live.mp3'])
        self.finish(run)
        self.assertEqual(self.speaker.uri, 'x-rincon-queue:' + self.speaker.uuid + '#0')
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED])


if __name__ == '__main__':
    unittest.main()
