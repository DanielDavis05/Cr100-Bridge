import hashlib
import types
import unittest
from unittest.mock import patch

from backends import BackendError
from radio_playback import CoverLookup, RadioPlayback, radio_id, radio_uri


class Room:
    def __init__(self):
        self.calls = []
        self.avt = {'AVTransportURI': ''}
        self.current = None
        self.queue = ['saved one', 'saved two']
    def require(self, op):
        if op != 'radio_play': raise BackendError(op)
    def supports(self, op): return op in ('pause', 'stop')
    def play_radio(self, uri, metadata):
        self.calls.append(('radio', uri, metadata)); self.avt['AVTransportURI'] = uri
    def play_item(self, node): self.calls.append(('episode', node.uri)); self.avt['AVTransportURI'] = node.uri
    def stop(self): self.calls.append(('stop',))
    def pause(self): self.calls.append(('pause',))
    def play(self): self.calls.append(('play',))
    def track(self): return self.current
    def transport_actions(self): return 'Play,Pause,Stop,Seek'


class Catalog:
    def __init__(self):
        self.item = types.SimpleNamespace(id='station:s1', title='Jazz station', kind='station',
                                         artist='', album='', image='https://example.org/logo.jpg',
                                         duration=None, metadata={})
        self.streams = [types.SimpleNamespace(url='https://example.org/live.mp3')]
    def metadata(self, id): return self.item
    def resolve(self, id, formats=()): return self.streams


class RadioPlaybackTest(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.player = types.SimpleNamespace(config=types.SimpleNamespace(udn='room1'),
                                            state=types.SimpleNamespace(backend=self.room), http_thread=None)
        self.house = types.SimpleNamespace(coordinator={'room1': 'room1', 'member': 'room1'},
                                          by_udn={'room1': self.player})
        self.catalog = Catalog()
        self.store = types.SimpleNamespace(settings=lambda: {'cover_lookup': False})
        self.playback = RadioPlayback(self.house, self.catalog, self.store, log=lambda _: None)
        # This fixture tests ownership/metadata; JPEG fetching and decoding have
        # real HTTP coverage in test_radio_integration. Avoid public network
        # work continuing in daemon threads after the unit-test process exits.
        self.art_paths = patch.object(self.playback.art, 'path_for',
            side_effect=lambda url, **kwargs: '/art/' + hashlib.sha256(str(url).encode()).hexdigest() + '.jpg')
        self.art_paths.start()
        self.profile = patch.object(self.playback, 'refresh_profile')
        self.profile.start()
    def tearDown(self): self.profile.stop(); self.playback.close(); self.art_paths.stop()
    def play(self):
        self.playback.play('room1', 'station:s1')
        return self.playback.session('room1')
    def test_live_play_preserves_queue_and_group_coordinator(self):
        self.playback.play('member', 'station:s1')
        self.assertEqual(self.room.queue, ['saved one', 'saved two'])
        self.assertEqual(self.room.calls[0][0], 'radio')
        self.assertIs(self.playback.session('member'), self.playback.session('room1'))
    def test_metadata_has_station_and_song_art_without_queue_transport(self):
        session = self.play()
        self.room.current = ('New song', 'Artist', 'Album', '/art/song.jpg', None)
        self.playback.refresh(session)
        values = self.playback.values('member', {}, 'http://192.0.2.1:1400')
        self.assertIn('New song', values['CurrentTrackMetaData'])
        self.assertIn('Artist', values['CurrentTrackMetaData'])
        self.assertIn('http://192.0.2.1:1400/art/song.jpg', values['CurrentTrackMetaData'])
        self.assertIn('audioBroadcast', values['AVTransportURIMetaData'])
        self.assertIn('Jazz station', values['AVTransportURIMetaData'])
        self.assertTrue(values['AVTransportURI'].startswith('x-rincon-mp3radio:'))
        self.assertEqual(values['RelTime'], 'NOT_IMPLEMENTED')
    def test_previous_source_metadata_not_displayed_during_transition(self):
        session = self.play()
        self.room.avt['AVTransportURI'] = 'spotify:old'
        self.room.current = ('Old song', 'Old artist', '', '/art/old.jpg', None)
        self.playback.refresh(session)
        self.assertEqual(session.title, 'Jazz station')
        self.assertNotIn('old.jpg', session.art)
    def test_external_takeover_removes_overlay(self):
        session = self.play(); session.started -= 10
        self.room.avt['AVTransportURI'] = 'spotify:new'
        self.playback.refresh(session)
        self.assertIsNone(self.playback.session('room1'))
    def test_live_pause_stops_and_restart_resolves_new_session(self):
        session = self.play()
        state = types.SimpleNamespace(config=types.SimpleNamespace(udn='room1'))
        self.playback.transport_action(state, 'AVTransport', 'Pause', {})
        self.assertEqual(self.room.calls[-1], ('stop',))
        self.assertEqual(session.state, 'STOPPED')
        self.playback.transport_action(state, 'AVTransport', 'Play', {})
        self.assertNotEqual(session.generation, self.playback.session('room1').generation)
    def test_live_seek_is_rejected(self):
        self.play()
        state = types.SimpleNamespace(config=types.SimpleNamespace(udn='room1'))
        with self.assertRaises(BackendError): self.playback.transport_action(state, 'AVTransport', 'Seek', {})
    def test_late_cover_does_not_change_new_station(self):
        old = self.play(); old.song_identity = ('old', 'artist', 'album')
        new = self.play()
        self.playback.covers = types.SimpleNamespace(lookup=lambda *args: 'https://example.org/old.jpg')
        self.playback.lookup_cover(old, old.song_identity)
        self.assertNotEqual(new.art, old.cover_art)
    def test_matched_cover_survives_subsequent_metadata_refresh(self):
        session = self.play()
        self.room.current = ('Song', 'Artist', 'Album', '', None)
        self.playback.refresh(session)
        self.playback.covers = types.SimpleNamespace(lookup=lambda *args: 'https://example.org/cover.jpg')
        self.playback.lookup_cover(session, session.song_identity)
        cover = session.art
        self.playback.refresh(session)
        self.assertEqual(session.art, cover)
    def test_changed_song_clears_previous_cover(self):
        session = self.play()
        self.room.current = ('Song', 'Artist', 'Album', '/art/cover.jpg', None)
        self.playback.refresh(session)
        self.room.current = ('Other song', 'Other artist', '', '', None)
        self.playback.refresh(session)
        self.assertNotEqual(session.art, '/art/cover.jpg')
    def test_folders_not_playable(self):
        self.catalog.item.kind = 'show'
        with self.assertRaises(BackendError): self.play()
    def test_episode_keeps_finite_controls(self):
        self.catalog.item.kind = 'episode'; self.catalog.item.duration = 123
        session = self.play()
        self.assertTrue(session.finite)
        self.assertEqual(self.room.calls[0][0], 'episode')
        self.assertEqual(self.playback.values('room1', {})['TrackDuration'], '0:02:03')
    def test_transport_ids_round_trip(self):
        self.assertEqual(radio_id(radio_uri('custom:contains / spaces')), 'custom:contains / spaces')
        self.assertEqual(radio_id('x-sonos-http:episode%3At123.mp3?sid=254&flags=0'), 'episode:t123')
        self.assertIsNone(radio_id('x-sonos-http:episode%3At123.mp3?sid=240&flags=0'))


class CoverLookupTest(unittest.TestCase):
    def test_exact_artist_title_and_single_release(self):
        def fetch(url):
            if 'musicbrainz.org' in url:
                return {'recordings': [{'title': 'Song', 'artist-credit': [{'name': 'Artist'}],
                                        'releases': [{'id': 'abc', 'title': 'Album'}]}]}
            return {'images': [{'front': True, 'approved': True, 'thumbnails': {'250': 'https://example.org/art.jpg'}}]}
        self.assertEqual(CoverLookup(fetch).lookup('Artist', 'Song', 'Album'), 'https://example.org/art.jpg')
    def test_ambiguous_recording_has_no_cover(self):
        fetch = lambda _: {'recordings': [{'title': 'Song', 'artist-credit': [{'name': 'Artist'}],
                                           'releases': [{'id': 'a'}, {'id': 'b'}]}]}
        self.assertEqual(CoverLookup(fetch).lookup('Artist', 'Song'), '')
    def test_wrong_artist_rejected(self):
        fetch = lambda _: {'recordings': [{'title': 'Song', 'artist-credit': [{'name': 'Wrong'}],
                                           'releases': [{'id': 'a'}]}]}
        self.assertEqual(CoverLookup(fetch).lookup('Artist', 'Song'), '')


if __name__ == '__main__': unittest.main()
