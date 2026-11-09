"""HTTP player routing with the real continuation policy and native Sonos adapter.

Only Spotify Connect itself is injected. Queue encodings, policy ownership,
virtual-player requests and native transport reads/writes use the actual bridge
code and loopback Sonos SOAP fixtures. No credentials or Spotify SDK are used.
"""
import copy
import html
import unittest
from unittest.mock import Mock, patch

import fake_sonos
import sonos_backend
import zoneplayers
from music_services import MusicServices
from spotify_autoplay import SpotifyAutoplay
from spotify_service import SpotifyService
from test_ha_backend import AVT, field, wait_for
from test_sonos_backend import KITCHEN, browse, calls
from test_spotify_autoplay import FakeClient, SEED, NEXT
from test_zoneplayers import HOUSEHOLD, soap, zp


LIBRARY = 'x-file-cifs://library.example/explicit.mp3'


class Connect:
    def __init__(self, player):
        self.player, self.calls = player, []

    def prepare(self):
        self.calls.append(('prepare',))

    def play_track(self, base, uri):
        self.calls.append(('play_track', base, uri))
        self.player.uri, self.player.uri_metadata = 'x-sonos-vli:finite-test-session', ''
        self.player.transport, self.player.position, self.player.track_number = 'PLAYING', '0:00:00', 1
        self.player.play_track_metadata(FakeClient.names[uri], 'Artist', duration='0:01:40')
        self.player.notify('AVTransport')

    def recommendations(self, seed, recent):
        self.calls.append(('recommendations', seed, list(recent)))
        return [NEXT]

    def close(self):
        pass


class AutoplayRouting(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.fake = fake_sonos.FakeHousehold()
        self.addCleanup(self.fake.close)
        self.speaker = self.fake.add(KITCHEN, 'Kitchen')
        get_position = self.speaker.do_GetPositionInfo
        def position_with_track_uri(args):
            values = get_position(args)
            index = self.speaker.track_number - 1
            values['TrackURI'] = self.speaker.queue[index][0] if self.speaker.uri.startswith('x-rincon-queue:') \
                and 0 <= index < len(self.speaker.queue) else self.speaker.uri
            return values
        # The general fixture omits TrackURI; this policy needs the actual
        # current resource to classify an explicit Spotify versus library tail.
        self.speaker.do_GetPositionInfo = position_with_track_uri
        self.fake.library['A:TRACKS'] = [fake_sonos.entry('A:TRACKS/explicit', 'Explicit library song',
            'A:TRACKS', res=LIBRARY, artist='Library artist')]
        self.backend = sonos_backend.SonosBackend([self.speaker.base], self.logs.append)
        self.addCleanup(self.backend.close)
        self.backend.start()
        self.assertTrue(self.backend.ready.wait(5), self.logs)
        self.assertTrue(wait_for(lambda: all(s.available and s.read_at for s in self.backend.speakers.values())),
                        self.logs)
        self.enabled, self.now = True, 1000
        self.client, self.connection = FakeClient(), Connect(self.speaker)
        self.connection_factory = Mock(return_value=self.connection)
        self.policy = SpotifyAutoplay(self.client, None, lambda: self.enabled, self.logs.append,
            connect_factory=self.connection_factory, clock=lambda: self.now, submit=lambda job: job())
        # Deterministic manual policy ticks while HTTP/SOAP/event transport stays real.
        self.policy.start = Mock()
        self.service = SpotifyService('127.0.0.1', 0, self.client, log=self.logs.append,
                                      playback_policy=self.policy)
        self.services = MusicServices([self.service])
        self.addCleanup(self.services.close)
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen'], ['127.0.0.1'], self.logs.append,
            port=0, backends=[self.backend], refs=[('sonos', KITCHEN)], music_service=self.services)
        self.addCleanup(self.house.close)
        self.services.start()
        self.house.start('127.0.0.1', ssdp=False)
        self.player = self.house.players[0]

    def wire(self, uri=SEED):
        return 'x-sonos-http:%s.mp3?sid=240&flags=0' % uri.replace(':', '%3A')

    def play(self, uri=SEED):
        status, body = soap(self.player, AVT, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>'
            % html.escape(self.wire(uri)))
        self.assertEqual(status, 200, body)

    def enqueue(self, uri, metadata='', as_next=False):
        status, body = soap(self.player, AVT, 'AVTransport', 'AddURIToQueue',
            '<InstanceID>0</InstanceID><EnqueuedURI>%s</EnqueuedURI>'
            '<EnqueuedURIMetaData>%s</EnqueuedURIMetaData><DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>'
            '<EnqueueAsNext>%d</EnqueueAsNext>' % (html.escape(uri), html.escape(metadata), as_next))
        self.assertEqual(status, 200, body)
        return body

    def tick(self, run, position, transport='PLAYING'):
        self.speaker.position, self.speaker.transport = position, transport
        self.now += 1
        self.policy._tick(run)

    def finish(self, run):
        self.tick(run, '0:01:39')
        self.tick(run, '0:00:00', 'PAUSED_PLAYBACK')
        self.tick(run, '0:00:00', 'PAUSED_PLAYBACK')

    def test_on_starts_finite_connect_outside_queue_off_uses_exact_legacy_native_encoding(self):
        self.play()
        self.assertEqual(self.speaker.queue, [])
        self.assertEqual(self.speaker.uri, 'x-sonos-vli:finite-test-session')
        self.assertEqual([c[2] for c in self.connection.calls if c[0] == 'play_track'], [SEED])
        self.assertEqual(calls(self.speaker, 'AddURIToQueue'), [])
        native_calls, catalogue_calls = copy.deepcopy(self.connection.calls), list(self.client.calls)
        self.enabled = False
        self.play()
        self.assertEqual(self.connection.calls, native_calls)
        self.assertEqual(self.client.calls, catalogue_calls)
        self.assertEqual(self.speaker.uri, 'x-rincon-queue:' + KITCHEN + '#0')
        added = calls(self.speaker, 'AddURIToQueue')[-1]
        self.assertEqual(added['EnqueuedURI'], SEED.replace(':', '%3a'))
        self.assertIn('SA_RINCON2311_X_#Svc2311-0-Token', added['EnqueuedURIMetaData'])
        self.assertEqual(len(self.speaker.queue), 1)
        self.assertEqual(self.speaker.track_number, 1)
        self.assertEqual(self.speaker.transport, 'PLAYING')

    def test_provider_add_reaches_actual_policy_once_without_generic_duplicate(self):
        self.play()
        run = next(iter(self.policy.runs.values()))
        with patch.object(self.policy, 'queue_added', wraps=self.policy.queue_added) as added, \
                patch.object(self.policy, 'after_action', wraps=self.policy.after_action) as after:
            body = self.enqueue(self.wire(NEXT), as_next=True)
        self.assertEqual(field(body, 'FirstTrackNumberEnqueued'), '1')
        added.assert_called_once()
        self.assertEqual(added.call_args.args[1], NEXT)
        self.assertTrue(added.call_args.kwargs['as_next'])
        after.assert_not_called()
        self.assertEqual(run.queue, [NEXT.replace(':', '%3a')])
        self.assertEqual(self.speaker.uri, 'x-sonos-vli:finite-test-session')
        self.assertEqual(self.speaker.transport, 'PLAYING')
        self.assertEqual([c[2] for c in self.connection.calls if c[0] == 'play_track'], [SEED])

    def test_native_library_add_has_priority_at_actual_policy_boundary_and_foreign_tail_stops(self):
        self.play()
        run = next(iter(self.policy.runs.values()))
        status, body = browse(self.player, 'A:TRACKS')
        self.assertEqual(status, 200, body)
        metadata = field(body, 'Result')
        self.assertIn('Explicit library song', metadata)
        with patch.object(self.policy, 'after_action', wraps=self.policy.after_action) as after:
            self.enqueue(LIBRARY, metadata)
        after.assert_called_once()
        self.assertEqual(run.queue, [LIBRARY])
        self.assertEqual(self.speaker.uri, 'x-sonos-vli:finite-test-session')
        self.finish(run)
        self.assertEqual(self.speaker.uri, 'x-rincon-queue:' + KITCHEN + '#0')
        self.assertEqual(self.speaker.track_number, 1)
        self.assertEqual(self.speaker.transport, 'PLAYING')
        self.assertEqual([c[2] for c in self.connection.calls if c[0] == 'play_track'], [SEED])
        self.speaker.play_track_metadata('Explicit library song', 'Library artist', duration='0:01:40')
        self.finish(run)
        self.assertTrue(run.foreign)
        self.assertTrue(run.cancelled.is_set())
        self.assertTrue(self.enabled)
        self.assertEqual(self.speaker.transport, 'PAUSED_PLAYBACK')
        self.assertEqual(self.speaker.queue[0][0], LIBRARY)  # retained history is not replayed
        self.assertEqual([c[2] for c in self.connection.calls if c[0] == 'play_track'], [SEED])


if __name__ == '__main__':
    unittest.main()
