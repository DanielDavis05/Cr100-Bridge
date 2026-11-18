#!/usr/bin/env python3
"""A non-Sonos Home Assistant player gets a bridge-owned queue (fake HA, fake Connect)."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_ha
import bridge_player
from unittest import mock
import ha_backend
from bridge_player import BridgePlayback
from ha_client import HomeAssistant
from media_sources import MediaSources
from test_bridge_player import URL, FakeConnect, FakeSource, FakeStreamer

BOSE = 'media_player.bose'
ALBUM = 'spotify:album:' + 'A' * 22


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(0.02)
    return False


class HABridgePlayback(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(bridge_player, 'TRANSITION', 0)
        patcher.start(); self.addCleanup(patcher.stop)
        # The SoundTouch integration's real feature set (no queue, no enqueue).
        state = fake_ha.sonos_state(BOSE, 'Bose', 'off', supported_features=151485, source_list=['AUX', 'BLUETOOTH'])
        state['attributes'].pop('group_members', None)
        self.fake = fake_ha.FakeHA([state])
        self.fake.platforms[BOSE] = 'soundtouch'
        self.logs = []
        self.ha = HomeAssistant(self.fake.url, fake_ha.TOKEN, self.logs.append)
        self.ha.start()
        self.assertTrue(self.ha.connected.wait(5), self.logs)
        self.backend = ha_backend.HABackend(self.ha, self.logs.append)
        self.connect = FakeConnect()
        self.playback = BridgePlayback(MediaSources([FakeSource()]), connect=self.connect, streamer=FakeStreamer(),
                                       connect_urls={BOSE: URL}, log=self.logs.append)
        self.room = self.backend.room(BOSE)
        self.assertTrue(wait_for(lambda: self.room.available))

    def tearDown(self):
        self.backend.close()
        self.fake.close()

    def test_without_bridge_playback_the_queue_is_unavailable(self):
        self.assertNotIn('provider_enqueue', self.room.operation_capabilities())
        self.assertNotIn('queue_browse', self.room.operation_capabilities())

    def test_spotify_album_queues_and_plays_through_connect(self):
        self.backend.attach_bridge_playback(self.playback)
        operations = self.room.operation_capabilities()
        for operation in ('provider_enqueue', 'provider_play', 'queue_browse', 'queue_play', 'queue_use', 'queue_clear'):
            self.assertIn(operation, operations)
        result = self.room.enqueue_provider(ALBUM, 'album')
        self.assertEqual(result, {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '3', 'NewQueueLength': '3'})
        self.assertEqual([node.title for node in self.backend.content.queue(BOSE)], ['Track 1', 'Track 2', 'Track 3'])
        self.assertIn('play', self.room.operation_capabilities())

        # The CR100's Play Now: Seek TRACK_NR, SetAVTransportURI x-rincon-queue, Play.
        self.room.play_track(2)
        self.room.use_queue()
        self.room.play()
        self.assertEqual(self.connect.plays[0][2], 1)
        self.assertTrue(wait_for(lambda: ('turn_on', BOSE) in [(s, e) for s, e, _ in self.fake.calls]))
        self.assertEqual(self.room.queue_numbers(), (2, 3))

        self.fake.set(BOSE, 'playing', source='SPOTIFY', media_title='Track 3', media_artist='Artist')
        self.assertTrue(wait_for(lambda: self.room.queue_numbers() == (3, 3)))
        self.assertEqual(self.room.transport, 'PLAYING')
        self.assertEqual(self.room.track()[0], 'Track 3')
        self.room.next()
        self.room.set_play_mode(False, 'all')
        self.assertEqual(self.connect.commands[0][1], 'skip_next')
        self.assertEqual(self.room.shuffle_repeat(), (False, 'all'))

    def test_unknown_speaker_state_still_accepts_the_bridge_queue(self):
        # A SoundTouch left with no source: Home Assistant reports "unknown".
        self.backend.attach_bridge_playback(self.playback)
        self.fake.set(BOSE, 'unknown', source='INVALID_SOURCE')
        self.assertTrue(wait_for(lambda: self.room.state.get('state') == 'unknown'))
        self.assertFalse(self.room.available)
        operations = self.room.operation_capabilities()
        self.assertIn('provider_enqueue', operations)
        self.assertNotIn('volume', operations)
        self.room.enqueue_provider(ALBUM, 'album')
        self.assertIn('play', self.room.operation_capabilities())
        self.room.use_queue()
        self.room.play()
        self.assertEqual(len(self.connect.plays), 1)
        self.assertIn('seek', self.room.operation_capabilities())

    def test_other_selections_release_the_queue(self):
        self.backend.attach_bridge_playback(self.playback)
        self.room.play_provider(ALBUM, 'album')
        self.assertTrue(self.playback.players[BOSE].active)
        self.room.play_radio('http://radio.example/stream.mp3')
        self.assertFalse(self.playback.players[BOSE].active)
        self.assertEqual(len(self.backend.content.queue(BOSE)), 3)
        self.room.clear_queue()
        self.assertEqual(self.backend.content.queue(BOSE), [])


if __name__ == '__main__':
    unittest.main()
