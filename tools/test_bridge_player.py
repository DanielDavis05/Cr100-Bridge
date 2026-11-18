#!/usr/bin/env python3
"""Offline tests for bridge-owned playback routes (Connect preferred, relay fallback)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_player
from backends import BackendError
from bridge_player import BridgePlayback, ConnectRoute, RelayRoute, spotify_connect_discovery
from bridge_queue import QueueEntry
from media_sources import AudioInput, MediaSources

URL = 'http://10.0.0.147:8200/zc'
RECEIVER = '037c4c0c3b1e4174712740d9e3a9e4e1c9ddc9e3'


def track(n, title=None):
    return QueueEntry('spotify', 'spotify:track:%022d' % n, title or 'Track %d' % n, 'Artist', 'Album',
                      'https://i.scdn.co/image/%d' % n, 180)


class FakeSource:
    name = 'spotify'

    def __init__(self, audio=True):
        self.audio, self.opened = audio, []

    def owns(self, uri): return uri.startswith('spotify:')
    def available(self): return True
    def audio_available(self): return self.audio

    def expand(self, uri, kind):
        if kind == 'album': return [track(1), track(2), track(3)]
        return [track(9, 'Single')]

    def open_audio(self, entry):
        self.opened.append(entry.ref)
        return AudioInput(url='http://example.invalid/%s' % entry.ref)


class FakeConnect:
    def __init__(self, available=True):
        self.ok, self.plays, self.commands = available, [], []

    def available(self): return self.ok

    def play_list(self, url, uris, index=0, position_ms=0, *, shuffle=False, repeat='off', autoplay=False):
        self.plays.append((url, list(uris), index, position_ms, shuffle, repeat))
        self.autoplay = autoplay
        return {'device_id': RECEIVER, 'activated': False}

    def command(self, receiver, endpoint, value=None):
        self.commands.append((receiver, endpoint, value))

    def receiver_info(self, url):
        if 'broken' in url: raise BackendError('no')
        return {'deviceID': RECEIVER, 'remoteName': 'Bose'}


class FakeStreamer:
    available = True

    def __init__(self): self.feeds = {}
    def register(self, feed): self.feeds['token'] = feed; return 'token'
    def unregister(self, token): self.feeds.pop(token, None)
    def url(self, token): return 'http://10.0.0.9:8404/relay/%s.mp3' % token
    def close(self): pass


class FakeSpeaker:
    name = 'Bose'

    def __init__(self, url=True):
        self.url_ok, self.calls = url, []
        self.state = {'available': True, 'state': 'off', 'source': '', 'content_id': '', 'title': '',
                      'artist': '', 'album': '', 'picture': '', 'duration': None, 'position': 0}

    def snapshot(self): return dict(self.state)
    def can_play_url(self): return self.url_ok
    def play_url(self, url): self.calls.append(('play_url', url)); self.state.update(state='playing', content_id=url)
    def pause(self): self.calls.append(('pause',)); self.state['state'] = 'paused'
    def resume(self): self.calls.append(('resume',)); self.state['state'] = 'playing'
    def stop(self): self.calls.append(('stop',)); self.state['state'] = 'idle'
    def wake(self): self.calls.append(('wake',))


def service(connect=None, streamer=None, urls=None, source=None, discover=None, relay=True, autoplay=None):
    return BridgePlayback(MediaSources([source or FakeSource()]), connect=connect, streamer=streamer,
                          connect_urls=urls or {}, discover=discover, relay=relay, autoplay=autoplay,
                          log=lambda record: None)


class RouteSelection(unittest.TestCase):
    def test_connect_is_preferred_and_sends_the_whole_queue(self):
        connect, speaker = FakeConnect(), FakeSpeaker()
        player = service(connect, FakeStreamer(), {'room': URL}).player('room', speaker)
        player.enqueue('spotify:album:%022d' % 1, 'album')
        player.select(2)
        player.play()
        self.assertIsInstance(player.route, ConnectRoute)
        self.assertEqual(connect.plays, [(URL, [track(n).ref for n in (1, 2, 3)], 1, 0, False, 'off')])
        self.assertEqual(player.position(), 0)        # the speaker has not started audio yet
        self.assertIn(('wake',), speaker.calls)
        self.assertTrue(player.active)
        self.assertEqual(player.queue.numbers(), (2, 3))

    def test_relay_when_speaker_is_not_a_connect_receiver(self):
        speaker = FakeSpeaker()
        player = service(FakeConnect(), FakeStreamer()).player('room', speaker)
        player.replace('spotify:album:%022d' % 1, 'album')
        self.assertIsInstance(player.route, RelayRoute)
        self.assertEqual(speaker.calls[-1], ('play_url', 'http://10.0.0.9:8404/relay/token.mp3'))
        self.assertEqual(player.transport(), 'TRANSITIONING')   # until the speaker connects

    def test_relay_disabled_or_unplayable_gives_a_clear_error(self):
        player = service(FakeConnect(), FakeStreamer(), relay=False).player('room', FakeSpeaker())
        with self.assertRaisesRegex(BackendError, 'cannot play'):
            player.replace('spotify:track:%022d' % 9, 'music')
        player = service(None, FakeStreamer()).player('room', FakeSpeaker(url=False))
        with self.assertRaisesRegex(BackendError, 'not a Spotify Connect speaker'):
            player.replace('spotify:track:%022d' % 9, 'music')
        player = service(None, FakeStreamer(), source=FakeSource(audio=False)).player('room', FakeSpeaker())
        with self.assertRaisesRegex(BackendError, 'sign-in'):
            player.replace('spotify:track:%022d' % 9, 'music')

    def test_discovery_matches_speaker_name(self):
        connect = FakeConnect()
        browse = lambda service, timeout: [
            {'instance': 'Bose', 'host': 'b.local', 'address': '10.0.0.147', 'port': 8200, 'txt': {'CPath': '/zc'}},
            {'instance': 'Other', 'host': 'o.local', 'address': '10.0.0.5', 'port': 1, 'txt': {'CPath': '/broken'}}]
        playback = service(connect, FakeStreamer(), discover=spotify_connect_discovery(browse, connect))
        self.assertEqual(playback.connect_url('room', ' bose '), URL)
        self.assertIsNone(playback.connect_url('room', 'Other'))
        playback.connect_urls['room'] = False
        self.assertIsNone(playback.connect_url('room', 'Bose'))


class Persistence(unittest.TestCase):
    def setUp(self):
        import tempfile
        no_transition(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'bridge-queues.json'

    def playback(self, connect):
        return BridgePlayback(MediaSources([FakeSource()]), connect=connect, streamer=FakeStreamer(),
                              connect_urls={'room': URL}, state_file=self.path, log=lambda record: None)

    def test_a_restart_resumes_the_connect_session(self):
        speaker = FakeSpeaker()
        first = self.playback(FakeConnect())
        player = first.player('room', speaker)
        player.replace('spotify:album:%022d' % 1, 'album')
        speaker.state.update(state='playing', source='SPOTIFY', title='Artist - Track 2')
        self.assertTrue(player.active)
        player.set_mode(False, 'all')
        position = player.position()
        first.close()                                       # the bridge process exits

        connect = FakeConnect()
        second = self.playback(connect)
        restored = second.player('room', speaker)
        self.assertEqual([entry.title for entry in restored.queue.snapshot()], ['Track 1', 'Track 2', 'Track 3'])
        self.assertEqual(restored.queue.numbers(), (2, 3))
        self.assertEqual(restored.queue.repeat, 'all')
        self.assertTrue(restored.active)
        self.assertEqual(restored.transport(), 'PLAYING')
        self.assertGreaterEqual(restored.position(), position)
        restored.seek(40)                                     # control works without a new Play
        self.assertEqual(connect.commands[-1][1:], ('seek_to', 40000))
        self.assertEqual(connect.plays, [])

    def test_queue_survives_without_a_session_and_bad_files_are_ignored(self):
        first = self.playback(FakeConnect())
        first.player('room', FakeSpeaker()).enqueue('spotify:album:%022d' % 1, 'album')
        first.write()
        second = self.playback(FakeConnect())
        player = second.player('room', FakeSpeaker())
        self.assertEqual(len(player.queue), 3)
        self.assertFalse(player.active)
        self.path.write_text('{"schema": 1, "rooms": {"room": {"entries": [{"source": 1}]}}}')
        third = self.playback(FakeConnect())
        self.assertEqual(len(third.player('room', FakeSpeaker()).queue), 0)
        self.path.write_text('not json')
        self.assertEqual(len(self.playback(FakeConnect()).player('room', FakeSpeaker()).queue), 0)


class TransitionEvents(unittest.TestCase):
    def test_seek_reports_transitioning_then_playing_with_an_event(self):
        # The CR100 restarts its progress bar only from the PLAYING event after a seek.
        import threading
        events = threading.Semaphore(0)
        speaker = FakeSpeaker()
        player = service(FakeConnect(), FakeStreamer(), {'room': URL}).player('room', speaker,
                                                                             on_change=events.release)
        with mock.patch.object(bridge_player, 'TRANSITION', 0.2):
            player.replace('spotify:album:%022d' % 1, 'album')
            speaker.state.update(state='playing', source='SPOTIFY', title='Track 1')
            self.assertEqual(player.transport(), 'TRANSITIONING')
            while events.acquire(timeout=1):
                if player.transport() == 'PLAYING': break
            self.assertEqual(player.transport(), 'PLAYING')
            player.seek(60)
            self.assertEqual(player.transport(), 'TRANSITIONING')
            self.assertTrue(events.acquire(timeout=1))               # the immediate event
            self.assertTrue(events.acquire(timeout=2))               # the timed PLAYING event
            self.assertEqual(player.transport(), 'PLAYING')


class SlowStart(unittest.TestCase):
    def test_polls_during_a_slow_connect_start_do_not_end_the_session(self):
        # The first Play after a restart signs in to Spotify (9 s on the Pi) while the
        # controller keeps polling position from other threads.
        import threading
        gate, entered = threading.Event(), threading.Event()
        connect = FakeConnect()
        original = connect.play_list
        def slow(*args, **kwargs):
            entered.set(); gate.wait(5)
            return original(*args, **kwargs)
        connect.play_list = slow
        player = service(connect, FakeStreamer(), {'room': URL}).player('room', FakeSpeaker())
        player.enqueue('spotify:album:%022d' % 1, 'album')
        worker = threading.Thread(target=player.play)
        worker.start()
        self.assertTrue(entered.wait(5))
        results = []
        poller = threading.Thread(target=lambda: results.append(player.active))
        poller.start()
        for _ in range(3): results.append(player.active)       # unlocked readers during the start
        gate.set(); worker.join(5); poller.join(5)
        self.assertTrue(player.active)
        self.assertIsInstance(player.route, ConnectRoute)
        self.assertIn('seek', player.capabilities())


def no_transition(case):
    patcher = mock.patch.object(bridge_player, 'TRANSITION', 0)
    patcher.start(); case.addCleanup(patcher.stop)


class ConnectControl(unittest.TestCase):
    def setUp(self):
        no_transition(self)
        self.connect, self.speaker = FakeConnect(), FakeSpeaker()
        self.player = service(self.connect, FakeStreamer(), {'room': URL}).player('room', self.speaker)
        self.player.replace('spotify:album:%022d' % 1, 'album')

    def test_transport_goes_to_the_receiver(self):
        self.player.next()
        self.player.pause()
        self.player.seek(30)
        self.player.set_mode(True, 'all')
        self.assertEqual([c[1:] for c in self.connect.commands], [
            ('skip_next', None), ('pause', None), ('seek_to', 30000), ('set_shuffling_context', True),
            ('set_repeating_context', True), ('set_repeating_track', False)])
        self.assertEqual(self.player.queue.numbers(), (2, 3))

    def test_speaker_reports_drive_cursor_and_takeover_ends_session(self):
        self.speaker.state.update(state='playing', source='SPOTIFY', title='Track 3', artist='Artist')
        self.assertEqual(self.player.transport(), 'PLAYING')
        self.assertEqual(self.player.queue.numbers(), (3, 3))
        self.assertEqual(self.player.track()[0], 'Track 3')
        self.speaker.state.update(title='Artist - Track 2')       # SoundTouch style
        self.assertTrue(self.player.active)                       # reads follow the speaker
        self.assertEqual(self.player.queue.numbers(), (2, 3))
        self.assertEqual(self.player.track()[:2], ('Track 2', 'Artist'))
        later = bridge_player.time.monotonic() + 30
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later):
            self.assertGreaterEqual(self.player.position(), 27)  # no reported position: track clock
        self.speaker.state.update(source='AUX')
        self.assertTrue(self.player.active)                       # a short gap is tolerated
        with mock.patch.object(bridge_player, 'LOST_GRACE', 0):
            self.assertFalse(self.player.active)
        self.assertIsNone(self.player.route)
        self.assertEqual(len(self.player.queue), 3)    # the queue survives another source

    def test_slow_speaker_reports_do_not_end_a_new_session(self):
        # A SoundTouch waking from standby: HA shows "unknown", then its old source, for a while.
        self.speaker.state.update(available=False, state='unknown', source='BLUETOOTH', title='')
        self.assertTrue(self.player.active)
        self.assertEqual(self.player.transport(), 'PLAYING')
        later = bridge_player.time.monotonic() + 20
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later):
            self.speaker.state.update(available=True, state='playing', title='Old Song')
            self.assertTrue(self.player.active)
            self.assertGreaterEqual(self.player.position(), 17)
            self.assertEqual(self.player.track()[0], 'Track 1')    # not the stale title
            self.player.seek(90)
            self.assertEqual(self.connect.commands[-1][1:], ('seek_to', 90000))
            self.assertEqual(round(self.player.position()), 90)   # counts once the speaker starts
            self.speaker.state.update(title='Artist - Track 1')
            self.assertTrue(self.player.active)
            self.assertTrue(self.player.route.confirmed)
            self.assertEqual(round(self.player.position()), 90)    # same track: clock kept
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later + 61):
            self.assertTrue(self.player.active)                   # confirmed sessions do not time out

    def test_autoplay_setting_and_tracks_past_the_queue(self):
        self.assertFalse(self.connect.autoplay)
        connect, speaker = FakeConnect(), FakeSpeaker()
        player = service(connect, FakeStreamer(), {'room': URL}, autoplay=lambda: True).player('room', speaker)
        player.replace('spotify:album:%022d' % 1, 'album')
        self.assertTrue(connect.autoplay)
        speaker.state.update(state='playing', source='SPOTIFY', title='Artist - Track 3')
        self.assertTrue(player.active)
        later = bridge_player.time.monotonic() + 100
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later):
            speaker.state.update(title='Someone - Related Song', artist='Someone')   # Spotify Autoplay
            self.assertTrue(player.active)
            self.assertEqual(player.queue.numbers(), (3, 3))
            self.assertEqual(player.track()[:2], ('Related Song', 'Someone'))
            self.assertLess(player.position(), 1)                  # a new track: clock restarted
            self.assertIn('next', player.capabilities())
            plays = len(connect.plays)
            player.enqueue('spotify:track:%022d' % 9, 'music')     # Add to Queue does not interrupt
            self.assertEqual(len(connect.plays), plays)
            self.assertEqual(player.queue.numbers(), (3, 4))
            player.select(4)                                       # Play Now does
            self.assertEqual(connect.plays[-1][2], 3)
            player.play()                                          # ...and its Play adds no restart
            self.assertEqual(len(connect.plays), plays + 1)

    def test_autoplay_status_follows_the_queue_and_tracks_past_it(self):
        connect, speaker = FakeConnect(), FakeSpeaker()
        player = service(connect, FakeStreamer(), {'room': URL}, autoplay=lambda: True).player('room', speaker)
        self.assertIsNone(player.autoplay_status())                     # nothing playing
        player.replace('spotify:album:%022d' % 1, 'album')
        speaker.state.update(state='playing', source='SPOTIFY', title='Artist - Track 3')
        self.assertEqual(player.autoplay_status(), 'armed')
        later = bridge_player.time.monotonic() + 100
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later):
            speaker.state.update(title='Someone - Related Song', artist='Someone')
            self.assertEqual(player.autoplay_status(), 'active')
            speaker.state.update(title='Artist - Track 2', artist='Artist')  # back on the queue
            self.assertEqual(player.autoplay_status(), 'armed')

    def test_relay_route_never_reports_autoplay(self):
        speaker = FakeSpeaker()
        player = service(None, FakeStreamer(), {}, autoplay=lambda: True).player('room', speaker)
        player.replace('spotify:album:%022d' % 1, 'album')
        self.assertEqual(player.route.name, 'relay')
        self.assertIsNone(player.autoplay_status())

    def test_late_track_reports_keep_the_clock(self):
        self.speaker.state.update(state='playing', source='SPOTIFY', title='Track 1', duration=180)
        self.assertTrue(self.player.active)
        start = bridge_player.time.monotonic()
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=start + 30):
            self.player.next()                                     # clock restarts at the command
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=start + 40):
            self.speaker.state.update(title='Track 2')             # reported 10 s later
            self.assertTrue(self.player.active)
            self.assertAlmostEqual(self.player.position(), 8, delta=0.5)
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=start + 40 + 175):
            self.assertAlmostEqual(self.player.position(), 3, delta=0.5)   # ran out: next track
            self.speaker.state.update(title='Track 3', duration=200)
            self.assertTrue(self.player.active)
            self.assertAlmostEqual(self.player.position(), 3, delta=0.5)   # its late report keeps it

    def test_commanded_pause_shows_before_the_speaker_reports_it(self):
        self.speaker.state.update(state='playing', source='SPOTIFY', title='Track 1')
        self.assertEqual(self.player.transport(), 'PLAYING')
        self.player.pause()
        self.speaker.state['state'] = 'playing'                   # Home Assistant has not polled yet
        self.assertEqual(self.player.transport(), 'PAUSED_PLAYBACK')
        position = self.player.position()
        later = bridge_player.time.monotonic() + 5
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=later):
            self.assertEqual(self.player.position(), position)    # frozen while paused
        self.speaker.state['state'] = 'paused'
        self.assertEqual(self.player.transport(), 'PAUSED_PLAYBACK')
        self.player.play()                                        # resume
        self.assertEqual(self.connect.commands[-1][1], 'resume')
        self.assertEqual(self.player.transport(), 'PLAYING')

    def test_unconfirmed_session_ends_after_waiting(self):
        self.speaker.state.update(state='playing', source='BLUETOOTH', title='Phone Song')
        with mock.patch.object(bridge_player.time, 'monotonic', return_value=bridge_player.time.monotonic() + 61):
            self.assertFalse(self.player.active)

    def test_adding_while_playing_restarts_list_in_place(self):
        self.speaker.state.update(state='playing', source='SPOTIFY', title='Track 2', position=42)
        self.player.enqueue('spotify:track:%022d' % 9, 'music')
        url, uris, index, position, *_ = self.connect.plays[-1]
        self.assertEqual((len(uris), index, position), (4, 1, 42000))

    def test_release_keeps_queue_but_not_play(self):
        self.player.release()
        self.assertFalse(self.player.active)
        self.assertFalse(self.player.queue_selected)
        self.assertEqual(self.player.capabilities(), frozenset(('play',)))


class RelayFeed(unittest.TestCase):
    def setUp(self):
        no_transition(self)
        self.source, self.speaker = FakeSource(), FakeSpeaker()
        self.player = service(None, FakeStreamer(), source=self.source).player('room', self.speaker)
        self.player.replace('spotify:album:%022d' % 1, 'album')
        self.route = self.player.route

    def test_segments_follow_the_queue_and_end_after_last_track(self):
        segment = self.route.segment()
        self.assertEqual(self.source.opened, [track(1).ref])
        self.assertEqual(self.player.transport(), 'PLAYING')
        self.route.progress(segment.generation, 12.5)
        self.assertEqual(self.player.position(), 12.5)
        self.route.finished(segment.generation)
        self.assertEqual(self.player.queue.numbers(), (2, 3))
        segment = self.route.segment()
        self.assertEqual(segment.start, 0)
        self.route.finished(segment.generation - 1)              # stale: ignored
        self.assertEqual(self.player.queue.numbers(), (2, 3))
        self.player.next()
        self.route.finished(self.route.segment().generation)
        self.assertIsNone(self.route.segment())
        self.assertEqual(self.route.state, 'STOPPED')

    def test_seek_and_previous_restart_audio(self):
        segment = self.route.segment()
        self.player.seek(60)
        self.assertNotEqual(self.route.generation, segment.generation)
        self.assertEqual(self.route.segment().start, 60)
        self.player.previous()                     # >3 s in: restart the track
        self.assertEqual(self.player.queue.numbers(), (1, 3))
        self.assertEqual(self.route.segment().start, 0)

    def test_pause_and_resume_reconnects_when_speaker_dropped_stream(self):
        self.route.segment()
        self.player.pause()
        self.assertEqual(self.speaker.calls[-1], ('pause',))
        self.route.disconnected()
        self.player.play()
        self.assertEqual(self.speaker.calls[-1][0], 'play_url')

    def test_repeated_audio_failures_stop(self):
        failures = []
        self.player.log = failures.append
        with mock.patch.object(self.source, 'open_audio', side_effect=BackendError('gone')):
            self.assertIsNone(self.route.segment())
        self.assertEqual(self.route.state, 'STOPPED')
        self.assertEqual(len(failures), 3)


if __name__ == '__main__':
    unittest.main()
