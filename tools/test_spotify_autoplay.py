"""Deterministic continuation policy tests; no sockets, tokens, SDK or sleeps.

Model the speaker's actual finite-track and persistent-queue behavior. The
assertions exercise ownership, explicit queue priority and track-boundary policy
rather than reproducing native Connect's wire implementation.
"""
import copy
import unittest
from unittest.mock import Mock

from backends import BackendError
from room_capabilities import RoomBinding
from spotify_autoplay import Run, SpotifyAutoplay, target


SEED = 'spotify:track:' + 'A' * 22
NEXT = 'spotify:track:' + 'B' * 22
LATER = 'spotify:track:' + 'C' * 22
NATIVE = 'x-sonos-vli:bedroom-session'
LIBRARY = 'x-file-cifs://library/explicit-track.flac'


def speaker_state(*, title='Seed', state='PLAYING', position=0, duration=100,
                  source=NATIVE, uri=NATIVE, index=1):
    return {'state': state, 'source': source, 'uri': uri, 'title': title,
            'artist': 'Artist', 'duration': duration, 'position': position, 'index': index}


class FakeBackend:
    def __init__(self):
        self.rooms = {}

    def room(self, key):
        return self.rooms[key]


class FakeRoom:
    def __init__(self, backend, key='bedroom', ident='RINCON_BEDROOM'):
        self.backend, self.key = backend, key
        self.available = True
        self.binding = RoomBinding(platform='sonos', device_id=key, native_id=ident,
                                   native_url='http://%s.test:1400' % key, group_key=(key,))
        self.members = [key]
        self.provider_calls = []
        self.queue_edits = 0
        backend.rooms[key] = self

    def group_members(self):
        return list(self.members)

    def play_provider(self, uri, kind):
        self.provider_calls.append((uri, kind))

    def queue_edited(self):
        self.queue_edits += 1


class FakeTransport:
    def __init__(self, room, trace):
        self.uuid, self.base = target(room)
        self.room, self.trace = room, trace
        self.rows = []
        self.current = speaker_state()
        self.commands = []
        self.snapshot_reads = 0

    def snapshot(self):
        self.snapshot_reads += 1
        return copy.deepcopy(self.current)

    def queue(self):
        return list(self.rows)

    def clear(self):
        self.commands.append(('clear',))
        self.trace.append('clear')
        self.rows.clear()

    def play_queue(self, number):
        self.commands.append(('play_queue', number))
        self.trace.append(('play_queue', number))
        uri = self.rows[number - 1]
        self.current = speaker_state(title='Explicit library track', source='x-rincon-queue:' + self.uuid + '#0',
                                     uri=uri, index=number)

    def move_next(self, first, count, before):
        self.commands.append(('move_next', first, count, before))
        moving = self.rows[first - 1:first - 1 + count]
        del self.rows[first - 1:first - 1 + count]
        self.rows[before - 1:before - 1] = moving


class FakeClient:
    names = {SEED: 'Seed', NEXT: 'Recommendation', LATER: 'Another recommendation'}

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        uri = 'spotify:track:' + path.rpartition('/')[2]
        if not path.startswith('/tracks/') or uri not in self.names:
            raise AssertionError('Unexpected catalogue request: ' + path)
        return {'uri': uri, 'name': self.names[uri]}


class FakeConnect:
    def __init__(self, transport, trace):
        self.transport, self.trace = transport, trace
        self.calls = []
        self.prepare_error = None

    def prepare(self):
        self.calls.append(('prepare',))
        self.trace.append('prepare')
        if self.prepare_error:
            raise self.prepare_error

    def play_track(self, base, uri):
        self.calls.append(('play_track', base, uri))
        self.trace.append(('native_play', uri))
        self.transport.current = speaker_state(title=FakeClient.names[uri])

    def recommendations(self, seed, recent):
        self.calls.append(('recommendations', seed, list(recent)))
        return [NEXT, LATER]

    def close(self):
        self.calls.append(('close',))


class PolicyFixture(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.room = FakeRoom(self.backend)
        self.trace, self.logs = [], []
        self.transport = FakeTransport(self.room, self.trace)
        self.client = FakeClient()
        self.connection = FakeConnect(self.transport, self.trace)
        self.enabled = True
        self.now = 1000
        self.connect_factory = Mock(return_value=self.connection)
        def transport_factory(room):
            target(room)  # Retain the real transport factory's route validation.
            return self.transport
        self.transport_factory = Mock(side_effect=transport_factory)
        self.policy = SpotifyAutoplay(self.client, object(), lambda: self.enabled, self.logs.append,
            connect_factory=self.connect_factory, transport_factory=self.transport_factory, clock=lambda: self.now,
            submit=lambda job: job())
        self.policy.start = Mock()  # Drive actual policy ticks synchronously.
        self.addCleanup(self.policy.close)

    def start(self):
        self.assertTrue(self.policy.play_provider(self.room, SEED, 'music'))
        return next(iter(self.policy.runs.values()))

    def tick(self, run, **changes):
        self.transport.current.update(changes)
        self.now += 1
        self.policy._tick(run)

    def finish(self, run):
        self.tick(run, state='PLAYING', position=99)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)


class PlaybackPolicy(PolicyFixture):
    def test_off_declines_before_any_route_validation_or_native_api(self):
        self.enabled = False
        self.room.available = False
        self.assertFalse(self.policy.play_provider(self.room, SEED, 'music'))
        self.policy.queue_added(self.room, SEED, {'FirstTrackNumberEnqueued': '1'})
        self.assertIsNone(self.policy.before_action(self.room, 'AVTransport', 'Next', {}))
        self.connect_factory.assert_not_called()
        self.transport_factory.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.trace, [])

    def test_on_uses_finite_helper_and_related_tracks_stay_outside_queue(self):
        run = self.start()
        self.assertEqual(self.trace, ['prepare', 'clear', ('native_play', SEED)])
        self.finish(run)
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED, NEXT])
        self.assertEqual(self.transport.rows, [])
        self.assertEqual(self.transport.commands, [('clear',)])
        self.assertFalse(run.cancelled.is_set())
        self.assertEqual(run.recent, [SEED, NEXT])

    def test_authentication_failure_preserves_existing_sonos_queue(self):
        self.transport.rows = [LIBRARY]
        self.connection.prepare_error = BackendError('Streaming sign-in unavailable')
        with self.assertRaises(BackendError):
            self.policy.play_provider(self.room, SEED, 'music')
        self.assertEqual(self.transport.rows, [LIBRARY])
        self.assertEqual(self.transport.commands, [])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.policy.runs, {})

    def test_local_off_leaves_current_track_untouched_then_disarms_next(self):
        run = self.start()
        self.tick(run, position=30)
        calls, commands = copy.deepcopy(self.connection.calls), list(self.transport.commands)
        self.enabled = False
        self.tick(run, position=50)
        self.assertEqual(self.connection.calls, calls)
        self.assertEqual(self.transport.commands, commands)
        self.assertFalse(run.cancelled.is_set())
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(self.connection.calls, calls)
        self.assertEqual(self.transport.commands, commands)
        self.assertEqual(self.transport.current['state'], 'PAUSED_PLAYBACK')

    def test_explicit_library_queue_takes_priority_and_foreign_tail_stops_with_on(self):
        run = self.start()
        self.tick(run, position=10)
        self.transport.rows.append(LIBRARY)
        before = copy.deepcopy(self.transport.current)
        self.policy.after_action(self.room, 'AVTransport', 'AddURIToQueue', {},
                                 {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1'})
        self.assertEqual(self.transport.current, before)
        self.assertNotIn(('play_queue', 1), self.transport.commands)
        self.finish(run)
        self.assertEqual(self.transport.commands[-1], ('play_queue', 1))
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED])
        self.finish(run)
        self.assertTrue(run.foreign)
        self.assertTrue(run.cancelled.is_set())
        self.assertTrue(self.enabled)
        self.assertEqual(self.transport.rows, [LIBRARY])  # Played rows persist.

    def test_played_queue_history_is_not_replayed_before_new_pending_item(self):
        history = ['x-file-cifs://library/old-one.flac', 'x-file-cifs://library/old-two.flac']
        self.transport.rows = history + [LIBRARY]
        run = Run(self.room, self.transport, 'native', SEED, queue=list(history), cursor=2,
                  expected='Seed', recent=[SEED], started=self.now)
        self.policy._register(run)
        self.finish(run)
        self.assertEqual(self.transport.commands, [('play_queue', 3)])
        self.assertEqual(run.cursor, 2)
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(self.transport.commands, [('play_queue', 3)])

    def test_off_still_honors_an_explicit_pending_queue(self):
        run = self.start()
        self.tick(run, position=20)
        self.transport.rows = [LIBRARY]
        self.enabled = False
        self.finish(run)
        self.assertEqual(self.transport.commands[-1], ('play_queue', 1))
        self.assertFalse(run.cancelled.is_set())
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())

    def test_plain_album_uses_existing_finite_queue_playback(self):
        uri = 'spotify:album:' + 'D' * 22
        self.assertTrue(self.policy.play_provider(self.room, uri, 'album'))
        self.assertEqual(self.room.provider_calls, [(uri, 'album')])
        self.assertFalse(any(call[0] == 'play_track' for call in self.connection.calls))
        self.assertEqual(next(iter(self.policy.runs.values())).mode, 'queue')

    def test_add_next_moves_new_block_ahead_of_pending_but_after_history(self):
        old = 'x-file-cifs://library/played.flac'
        pending = 'x-file-cifs://library/pending.flac'
        self.transport.rows = [old, pending, LIBRARY]
        run = Run(self.room, self.transport, 'native', SEED, queue=[old, pending], cursor=1,
                  expected='Seed', started=self.now)
        self.policy._register(run)
        self.policy.queue_added(self.room, NEXT,
            {'FirstTrackNumberEnqueued': '3', 'NumTracksAdded': '1'}, as_next=True)
        self.assertEqual(self.transport.rows, [old, LIBRARY, pending])
        self.assertEqual(self.transport.commands, [('move_next', 3, 1, 2)])
        self.assertEqual(run.cursor, 1)

    def test_next_hands_off_to_pending_queue_without_double_next(self):
        run = self.start()
        self.tick(run, position=20)
        self.transport.rows = [LIBRARY]
        result = self.policy.before_action(self.room, 'AVTransport', 'Next', {})
        self.assertEqual(result, {})
        self.assertEqual(self.transport.commands[-1], ('play_queue', 1))
        self.assertEqual(run.mode, 'queue')

    def enqueue_during_candidate_metadata(self):
        original = self.client.get
        def get(path):
            result = original(path)
            if path == '/tracks/' + NEXT.rpartition(':')[2]:
                # Model the physical enqueue completing while its bridge
                # notification waits for this run's lock during metadata I/O.
                self.transport.rows.append(LIBRARY)
            return result
        self.client.get = get

    def test_enqueue_during_native_recommendation_metadata_lookup_has_priority(self):
        run = self.start()
        self.tick(run, position=30)
        self.enqueue_during_candidate_metadata()
        self.finish(run)
        self.assertEqual(self.transport.commands, [('clear',), ('play_queue', 1)])
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED])
        self.assertEqual(run.mode, 'queue')
        self.assertEqual(self.transport.rows, [LIBRARY])

    def test_enqueue_during_queue_tail_next_metadata_does_not_replay_the_current_row(self):
        self.transport.rows = [SEED]
        self.transport.current = speaker_state(source='x-rincon-queue:' + self.transport.uuid + '#0',
                                              uri=SEED, index=1, position=30)
        run = Run(self.room, self.transport, 'queue', SEED, queue=[SEED], started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.enqueue_during_candidate_metadata()
        result = self.policy.before_action(self.room, 'AVTransport', 'Next', {})
        self.assertEqual(result, {})
        self.assertEqual(self.transport.commands, [('play_queue', 2)])
        self.assertFalse(any(call[0] == 'play_track' for call in self.connection.calls))
        self.assertEqual(run.cursor, 1)
        self.assertEqual(self.transport.rows, [SEED, LIBRARY])

    def test_enqueue_during_naturally_finished_queue_metadata_starts_only_the_new_row(self):
        self.transport.rows = [SEED]
        self.transport.current = speaker_state(source='x-rincon-queue:' + self.transport.uuid + '#0',
                                              uri=SEED, index=1, position=30)
        run = Run(self.room, self.transport, 'queue', SEED, queue=[SEED], started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.enqueue_during_candidate_metadata()
        self.finish(run)
        self.assertEqual(self.transport.commands, [('play_queue', 2)])
        self.assertFalse(any(call[0] == 'play_track' for call in self.connection.calls))
        self.assertEqual(run.cursor, 1)


class OwnershipAndCancellation(PolicyFixture):
    def test_finished_shuffled_queue_does_not_replay_its_retained_tail(self):
        self.transport.rows = [SEED, NEXT, LATER]
        self.transport.current = speaker_state(source='x-rincon-queue:bedroom#0', uri=SEED, index=1)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.finish(run)
        self.assertEqual(run.cursor, 3)
        self.assertFalse(any(command[0] == 'play_queue' for command in self.transport.commands))
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [NEXT])

    def test_append_after_queue_stops_is_pending_even_with_retained_history(self):
        self.transport.rows = [SEED]
        self.transport.current = speaker_state(source='x-rincon-queue:bedroom#0', uri=SEED, index=1)
        run = Run(self.room, self.transport, 'queue', SEED, queue=[SEED], started=self.now)
        self.policy._register(run)
        self.tick(run, position=99)
        self.transport.rows.append(LIBRARY)
        self.tick(run, state='STOPPED', position=0)
        self.tick(run, state='STOPPED', position=0)
        self.assertEqual(self.transport.commands, [('play_queue', 2)])
        self.assertFalse(any(call[0] == 'play_track' for call in self.connection.calls))

    def test_unverified_non_sonos_route_declined_before_connection(self):
        # Non-Sonos rooms play the provider item through their bridge-owned queue.
        self.room.binding = RoomBinding(platform='generic', device_id='bedroom')
        self.assertFalse(self.policy.play_provider(self.room, SEED, 'music'))
        self.connect_factory.assert_not_called()
        self.assertEqual(self.transport.commands, [])

    def test_pause_suspends_and_play_resumes_without_advancing_immediately(self):
        run = self.start()
        self.tick(run, position=99)
        self.policy.before_action(self.room, 'AVTransport', 'Pause', {})
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertTrue(run.suspended)
        self.assertFalse(run.cancelled.is_set())
        self.assertEqual(len([call for call in self.connection.calls if call[0] == 'play_track']), 1)
        self.policy.after_action(self.room, 'AVTransport', 'Play', {})
        self.assertFalse(run.suspended)
        self.assertEqual(run.maximum, 0)

    def test_early_external_pause_is_not_natural_exhaustion(self):
        run = self.start()
        self.tick(run, position=25)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertTrue(run.suspended)
        self.assertFalse(run.cancelled.is_set())
        self.assertEqual(len([call for call in self.connection.calls if call[0] == 'play_track']), 1)

    def test_explicit_stop_and_source_selection_cancel_without_native_command(self):
        for action, args in [('Stop', {}), ('Previous', {}), ('SetAVTransportURI', {}),
                             ('Seek', {'Unit': 'TRACK_NR', 'Target': '2'})]:
            with self.subTest(action=action):
                run = self.start()
                before = len(self.connection.calls)
                self.policy.before_action(self.room, 'AVTransport', action, args)
                self.assertTrue(run.cancelled.is_set())
                self.assertEqual(len(self.connection.calls), before)

    def test_relative_seek_resets_end_guard_and_cannot_trigger_early_next(self):
        run = self.start()
        self.tick(run, position=99)
        self.policy.before_action(self.room, 'AVTransport', 'Seek', {'Unit': 'REL_TIME', 'Target': '0:00:05'})
        self.assertEqual(run.maximum, 0)
        self.tick(run, position=5)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertTrue(run.suspended)
        self.assertEqual(len([call for call in self.connection.calls if call[0] == 'play_track']), 1)

    def test_external_backward_seek_cannot_leave_stale_end_progress_and_autoplay_on_pause(self):
        run = self.start()
        self.tick(run, position=99)
        # Another controller seeks backwards; this bridge receives only state,
        # then observes an early pause whose position was reset by the receiver.
        self.tick(run, position=3)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertEqual(len([call for call in self.connection.calls if call[0] == 'play_track']), 1)
        self.assertTrue(run.cancelled.is_set() or run.suspended)

    def test_native_sleep_timer_cancels_continuation(self):
        run = self.start()
        self.tick(run, position=20)
        self.policy.before_action(self.room, 'AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:15:00'})
        self.policy.after_action(self.room, 'AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:15:00'})
        self.assertFalse(run.cancelled.is_set())
        before = len(self.connection.calls)
        self.now += 901
        self.tick(run, position=30)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(len(self.connection.calls), before)

    def test_stopped_queue_index_zero_uses_last_played_row_instead_of_replaying_history(self):
        encoded = 'x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22
        self.transport.rows = [encoded, encoded]
        self.transport.current = speaker_state(title='Seed', source='x-rincon-queue:' + self.transport.uuid + '#0',
                                              uri=encoded, index=2, position=99)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.tick(run, state='STOPPED', position=0, index=0, title='', artist='')
        self.tick(run, state='STOPPED', position=0, index=0, title='', artist='')
        self.assertEqual(run.cursor, 2)
        self.assertEqual(self.transport.commands, [])
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [NEXT])

    def queue_state(self, row, title, **changes):
        state = speaker_state(title=title, source='x-rincon-queue:' + self.transport.uuid + '#0',
                              uri=self.transport.rows[row - 1], index=row)
        state.update(changes)
        return state

    def test_play_now_adopts_after_the_player_finishes_transitioning(self):
        # A CR100 Play Now is Clear, Add, Seek TRACK_NR, Play; S1/S2 Spotify
        # then reports TRANSITIONING for several seconds.
        self.transport.rows = ['x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22]
        self.transport.current = self.queue_state(1, 'Seed', state='TRANSITIONING')
        self.policy.after_action(self.room, 'AVTransport', 'Play', {})
        self.assertEqual(self.policy.runs, {})
        self.policy._retry_adoptions()
        self.assertEqual(self.policy.runs, {})
        self.transport.current['state'] = 'PLAYING'
        self.now += 3
        self.policy._retry_adoptions()
        run = next(iter(self.policy.runs.values()))
        self.assertEqual((run.mode, run.seed), ('queue', SEED))
        self.assertEqual(self.policy.pending, {})

    def test_pending_adoption_expires_and_is_cancelled_by_stop(self):
        self.transport.rows = ['x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22]
        self.transport.current = self.queue_state(1, 'Seed', state='TRANSITIONING')
        self.policy.after_action(self.room, 'AVTransport', 'Play', {})
        self.now += 60
        self.transport.current['state'] = 'PLAYING'
        self.policy._retry_adoptions()
        self.assertEqual((self.policy.runs, self.policy.pending), ({}, {}))
        self.transport.current['state'] = 'TRANSITIONING'
        self.policy.after_action(self.room, 'AVTransport', 'Play', {})
        self.policy._register(Run(self.room, self.transport, 'queue', SEED, started=self.now))
        self.policy.before_action(self.room, 'AVTransport', 'Stop', {})
        self.assertEqual((self.policy.runs, self.policy.pending), ({}, {}))

    def test_finished_multi_row_queue_rewinds_to_row_one_then_continues(self):
        first = 'x-sonos-spotify:spotify%3atrack%3a' + 'C' * 22
        last = 'x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22
        self.transport.rows = [first, last]
        self.transport.current = self.queue_state(2, 'Seed', position=97, duration=100)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.tick(run, position=99)
        # Observed on S1 and S2: STOPPED, row 1, its own title and duration, 0:00.
        rewound = self.queue_state(1, 'Another recommendation', state='STOPPED', position=0, duration=257)
        self.transport.current = rewound
        self.tick(run)
        self.assertFalse(run.cancelled.is_set())
        self.tick(run)
        self.assertEqual(self.transport.commands, [])
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [NEXT])

    def test_stop_mid_track_then_rewound_row_is_not_mistaken_for_queue_end(self):
        first = 'x-sonos-spotify:spotify%3atrack%3a' + 'C' * 22
        last = 'x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22
        self.transport.rows = [first, last]
        self.transport.current = self.queue_state(2, 'Seed', position=40, duration=100)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.transport.current = self.queue_state(1, 'Another recommendation', state='STOPPED', position=0)
        self.tick(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertNotIn('play_track', [call[0] for call in self.connection.calls])

    def test_next_after_queue_tail_recommendation_advances_instead_of_replaying_the_tail(self):
        first = 'x-sonos-spotify:spotify%3atrack%3a' + 'C' * 22
        last = 'x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22
        self.transport.rows = [first, last]
        self.transport.current = self.queue_state(2, 'Seed', position=10)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.assertEqual(self.policy.before_action(self.room, 'AVTransport', 'Next', {}), {})
        self.tick(run, position=5)
        self.assertEqual(self.policy.before_action(self.room, 'AVTransport', 'Next', {}), {})
        self.assertEqual(self.transport.commands, [])
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [NEXT, LATER])

    def test_armed_run_claims_next_even_when_the_player_omits_it(self):
        self.assertFalse(self.policy.claims_action(self.room, 'AVTransport', 'Next', {}))
        run = self.start()
        self.assertTrue(self.policy.claims_action(self.room, 'AVTransport', 'Next', {}))
        self.assertFalse(self.policy.claims_action(self.room, 'AVTransport', 'Previous', {}))
        run.cancelled.set()
        self.assertFalse(self.policy.claims_action(self.room, 'AVTransport', 'Next', {}))

    def test_duplicate_queue_occurrence_resets_natural_end_progress(self):
        encoded = 'x-sonos-spotify:spotify%3atrack%3a' + 'A' * 22
        self.transport.rows = [encoded, encoded]
        self.transport.current = speaker_state(title='Seed', source='x-rincon-queue:' + self.transport.uuid + '#0',
                                              uri=encoded, index=1, position=99)
        run = Run(self.room, self.transport, 'queue', SEED, queue=list(self.transport.rows), started=self.now)
        self.policy._register(run)
        self.tick(run)
        self.tick(run, index=2, position=3)
        self.assertEqual(run.maximum, 3)
        self.assertEqual(run.cursor, 1)
        self.tick(run, state='STOPPED', position=0)
        self.assertTrue(run.suspended)
        self.assertFalse(any(call[0] == 'play_track' for call in self.connection.calls))

    def test_pending_metadata_fetch_cannot_start_a_new_song_after_off(self):
        pending = []
        self.policy.close()
        self.policy = SpotifyAutoplay(self.client, object(), lambda: self.enabled, self.logs.append,
            connect_factory=self.connect_factory, transport_factory=self.transport_factory,
            clock=lambda: self.now, submit=pending.append)
        self.policy.start = Mock()
        self.addCleanup(self.policy.close)
        run = self.start()
        self.tick(run, position=30)
        self.assertEqual(len(pending), 1)
        self.enabled = False
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        pending.pop()()
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == 'play_track'], [SEED])
        self.assertEqual(self.transport.commands, [('clear',)])

    def test_external_song_or_source_takeover_disarms_owned_run(self):
        for changes in ({'title': 'Someone else selected this song'},
                        {'source': 'x-rincon-stream:another-source'}):
            with self.subTest(changes=changes):
                run = self.start()
                self.tick(run, position=20)
                self.tick(run, **changes)
                self.assertTrue(run.cancelled.is_set())

    def test_coordinator_change_cancels_the_old_receiver_run(self):
        run = self.start()
        other = FakeRoom(self.backend, 'kitchen', 'RINCON_KITCHEN')
        self.room.members = [other.key, self.room.key]
        self.tick(run, position=20)
        self.assertTrue(run.cancelled.is_set())

    def test_group_membership_change_also_cancels_when_coordinator_is_unchanged(self):
        run = self.start()
        self.tick(run, position=20)
        other = FakeRoom(self.backend, 'kitchen', 'RINCON_KITCHEN')
        self.room.members = [self.room.key, other.key]
        self.room.binding = RoomBinding(platform='sonos', device_id=self.room.key,
            native_id=self.transport.uuid, native_url=self.transport.base,
            group_key=tuple(self.room.members))
        self.tick(run, position=30)
        self.assertTrue(run.cancelled.is_set())

    def test_enabling_never_adopts_an_external_autonomous_connect_session(self):
        self.transport.current = speaker_state(title='External Spotify session')
        self.policy.after_action(self.room, 'AVTransport', 'Play', {})
        self.assertEqual(self.policy.runs, {})
        self.connect_factory.assert_not_called()
        self.assertEqual(self.transport.commands, [])

    def test_unknown_edits_to_played_history_fail_before_replay(self):
        history = 'x-file-cifs://library/played.flac'
        self.transport.rows = ['x-file-cifs://library/different.flac', LIBRARY]
        run = Run(self.room, self.transport, 'native', SEED, queue=[history], cursor=1,
                  expected='Seed', started=self.now)
        self.policy._register(run)
        self.tick(run, position=99)
        self.tick(run, state='PAUSED_PLAYBACK', position=0)
        with self.assertRaises(BackendError):
            self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertEqual(self.transport.commands, [])


def numbered_track(number):
    return 'spotify:track:%022d' % number


def overlapping_batches():
    """50-row pages, including repeats much older than the request history."""
    return [
        [SEED] + [numbered_track(n) for n in range(1, 50)],
        [numbered_track(n) for n in range(25, 75)],
        [SEED, numbered_track(1)] + [numbered_track(n) for n in range(75, 123)],
        [numbered_track(n) for n in range(100, 150)],
        [numbered_track(2), numbered_track(3)] + [numbered_track(n) for n in range(150, 198)],
        [numbered_track(n) for n in range(175, 225)],
        [numbered_track(4), SEED] + [numbered_track(n) for n in range(225, 273)],
    ]


class LongSessionClient(FakeClient):
    names = {**FakeClient.names, **{numbered_track(n): 'Generated recommendation %d' % n for n in range(1, 301)}}


class ScriptedRecommendationStream:
    """An upstream stream can overlap both prior pages and explicit queue songs."""
    def __init__(self, batches):
        self.batches = copy.deepcopy(batches)
        self.calls = []

    def next_batch(self, *, recent=()):
        self.calls.append(list(recent))
        return self.batches.pop(0) if self.batches else []


class PagedConnect(FakeConnect):
    def __init__(self, transport, trace, batches):
        super().__init__(transport, trace)
        self.batches, self.streams = batches, []

    def recommendation_stream(self, seed, *args, **kwargs):
        self.calls.append(('recommendation_stream', seed))
        stream = ScriptedRecommendationStream(self.batches)
        self.streams.append(stream)
        return stream

    def recommendations(self, seed, recent):
        raise AssertionError('A stream-capable connection must not restart the first recommendation page')

    def play_track(self, base, uri):
        self.calls.append(('play_track', base, uri))
        self.trace.append(('native_play', uri))
        self.transport.current = speaker_state(title=LongSessionClient.names[uri])


class LongSessionContinuation(PolicyFixture):
    def setUp(self):
        super().setUp()
        self.client = LongSessionClient()
        self.connection = PagedConnect(self.transport, self.trace, overlapping_batches())
        self.connect_factory.return_value = self.connection
        self.policy.client = self.client

    def played(self):
        return [call[2] for call in self.connection.calls if call[0] == 'play_track']

    def test_more_than_150_tracks_continue_across_overlapping_pages_without_repeats(self):
        run = self.start()
        for transition in range(200):
            self.finish(run)
            self.assertFalse(run.cancelled.is_set(), 'Ended after transition %d' % transition)
        played = self.played()
        self.assertEqual(len(played), 201)
        self.assertEqual(len(set(played)), len(played))
        self.assertEqual(played.count(SEED), 1)
        self.assertEqual(played.count(numbered_track(1)), 1)
        self.assertEqual(played.count(numbered_track(2)), 1)
        self.assertEqual(len(self.connection.streams), 1)
        self.assertGreaterEqual(len(self.connection.streams[0].calls), 5)
        self.assertTrue(all(len(recent) <= 100 for recent in self.connection.streams[0].calls))
        self.assertEqual(self.transport.rows, [])
        self.assertEqual(self.transport.commands, [('clear',)])

    def test_stale_and_duplicate_candidates_are_rechecked_immediately_before_dispatch(self):
        run = self.start()
        for _ in range(75):
            self.finish(run)
        stale = numbered_track(1)
        self.assertIn(stale, self.played())
        self.assertNotIn(stale, run.recent[-30:])
        # Simulate candidates cached before an overlapping page or queue edit.
        # Hold any later metadata fetch to isolate the already-cached choices.
        pending = []
        self.policy.submit = pending.append
        run.candidates = [stale, SEED, numbered_track(271), numbered_track(271),
                          numbered_track(272), numbered_track(272)]
        self.finish(run)
        self.finish(run)
        self.assertEqual(self.played()[-2:], [numbered_track(271), numbered_track(272)])
        self.assertEqual(len(self.played()), len(set(self.played())))

    def test_stream_exhaustion_stops_without_replaying_or_looping_first_page(self):
        self.connection.batches = [[numbered_track(1), numbered_track(2)], []]
        run = self.start()
        self.finish(run)
        self.finish(run)
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(self.played(), [SEED, numbered_track(1), numbered_track(2)])
        calls_at_end = len(self.connection.streams[0].calls)
        for _ in range(20):
            self.tick(run, state='PAUSED_PLAYBACK', position=0)
        self.assertEqual(len(self.connection.streams), 1)
        self.assertEqual(len(self.connection.streams[0].calls), calls_at_end)
        self.assertEqual(self.transport.commands, [('clear',)])

    def test_foreign_queue_keeps_priority_while_the_next_page_is_pending(self):
        self.connection.batches = [[numbered_track(1)], [numbered_track(2)]]
        run = self.start()
        self.finish(run)
        self.assertEqual(self.played(), [SEED, numbered_track(1)])
        pending = []
        self.policy.submit = pending.append
        self.tick(run, position=30)
        self.assertEqual(len(pending), 1)
        self.transport.rows.append(LIBRARY)
        self.policy.after_action(self.room, 'AVTransport', 'AddURIToQueue', {},
                                 {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1'})
        self.finish(run)
        self.assertEqual(self.transport.commands[-1], ('play_queue', 1))
        pending.pop()()
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertTrue(run.foreign)
        self.assertEqual(self.played(), [SEED, numbered_track(1)])

    def test_off_while_next_page_is_pending_finishes_current_without_starting_it(self):
        self.connection.batches = [[numbered_track(1)], [numbered_track(2)]]
        run = self.start()
        self.finish(run)
        pending = []
        self.policy.submit = pending.append
        self.tick(run, position=30)
        self.assertEqual(len(pending), 1)
        before = copy.deepcopy(self.transport.current)
        commands = list(self.transport.commands)
        self.enabled = False
        self.tick(run, position=40)
        self.assertEqual(self.transport.current['title'], before['title'])
        self.assertEqual(self.transport.current['source'], before['source'])
        self.assertEqual(self.transport.commands, commands)
        pending.pop()()
        self.finish(run)
        self.assertTrue(run.cancelled.is_set())
        self.assertEqual(self.played(), [SEED, numbered_track(1)])

    def test_explicit_queue_duplicates_play_normally_but_are_excluded_from_autoplay(self):
        self.connection.batches = [[numbered_track(1), numbered_track(2)]]
        run = self.start()
        self.tick(run, position=30)
        self.transport.rows = [numbered_track(1), numbered_track(1)]
        self.policy.after_action(self.room, 'AVTransport', 'AddURIToQueue', {},
                                 {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '2'})
        self.finish(run)
        self.assertEqual(self.transport.commands[-1], ('play_queue', 1))
        # The real Sonos queue advances between both user-selected occurrences;
        # observing a duplicate must not delete it, skip it or replace it.
        self.tick(run, position=99)
        self.tick(run, index=2, position=0)
        self.assertFalse(run.cancelled.is_set())
        self.assertEqual(self.transport.rows, [numbered_track(1), numbered_track(1)])
        self.assertEqual(self.transport.commands, [('clear',), ('play_queue', 1)])
        self.finish(run)
        self.assertEqual(self.played(), [SEED, numbered_track(2)])
        self.assertFalse(run.foreign)

    def test_old_page_completion_cannot_replace_candidates_after_queued_spotify_seed_change(self):
        self.connection.batches = [[numbered_track(1)], [numbered_track(2)]]
        run = self.start()
        self.finish(run)
        pending = []
        self.policy.submit = pending.append
        self.tick(run, position=30)
        self.assertEqual(len(pending), 1)
        self.transport.rows = [numbered_track(250)]
        self.policy.after_action(self.room, 'AVTransport', 'AddURIToQueue', {},
                                 {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1'})
        self.finish(run)
        self.tick(run, position=10)
        self.assertEqual(run.seed, numbered_track(250))
        # The replacement station has a different next track. A delayed result
        # for the old seed must not become that station's upcoming selection.
        self.connection.batches = [[numbered_track(299)]]
        pending.pop(0)()
        self.tick(run, position=20)
        self.assertEqual(len(pending), 1)
        pending.pop(0)()
        self.finish(run)
        self.assertEqual(self.played(), [SEED, numbered_track(1), numbered_track(299)])


if __name__ == '__main__':
    unittest.main()
