"""Mixed-service discovery, household account events, routing and lifecycle checks."""
import http.client
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from music_services import MusicServices
from smapi_service import SmapiService
from smapi_stub import SmapiStub
from speaker_settings import SpeakerSettingsService
from spotify_service import SpotifyService
from test_backends import MemoryBackend
from test_smapi_stub import HOUSEHOLD, OTHER_HOUSEHOLD, IV, decode_accounts
from test_spotify_service import Client, TRACK
from test_zoneplayers import soap, zoneplayers, zp


def quiet(_):
    pass


class MixedServices(unittest.TestCase):
    def setUp(self):
        self.spotify = SpotifyService('127.0.0.1', 0, Client(), log=quiet)
        self.settings = SpeakerSettingsService('127.0.0.1', 0, lambda: [], log=quiet)
        self.services = MusicServices([self.spotify, self.settings])
        self.addCleanup(self.services.close)

    def test_merged_descriptor_contains_both_services_and_types(self):
        values = self.services.descriptor()
        children = list(ET.fromstring(values['AvailableServiceDescriptorList']))
        self.assertEqual([child.get('Name') for child in children], ['Spotify', 'Speaker Settings'])
        self.assertEqual([child.get('Id') for child in children], ['240', '244'])
        self.assertEqual([child.get('Uri') for child in children], [self.spotify.uri, self.settings.uri])
        self.assertEqual([child.get('Capabilities') for child in children], ['1', '3'])
        self.assertEqual(values['AvailableServiceTypeList'], '61447,62471')
        self.assertEqual(self.services.services, [self.spotify, self.settings])
        self.assertEqual(self.services.controller_services, [self.spotify])
        self.assertFalse(hasattr(self.services, 'controller_resolver'))
        self.assertTrue(self.services.preflight_playback)

    def test_version_is_repeatable_and_changes_with_each_child_descriptor(self):
        version = self.services.descriptor()['AvailableServiceListVersion']
        self.assertTrue(version.isdecimal())
        self.assertTrue(0 < int(version) <= 2147483647)
        self.assertEqual(self.services.descriptor()['AvailableServiceListVersion'], version)
        for service in self.services.services:
            original = service.descriptor()
            variants = [dict(original, AvailableServiceListVersion='changed'),
                        dict(original, AvailableServiceDescriptorList=
                             original['AvailableServiceDescriptorList'].replace('PollInterval="', 'Extra="yes" PollInterval="'))]
            for changed in variants:
                with self.subTest(service=service.name, descriptor=changed):
                    with patch.object(service, 'descriptor', return_value=changed):
                        self.assertNotEqual(self.services.descriptor()['AvailableServiceListVersion'], version)
            self.assertEqual(self.services.descriptor()['AvailableServiceListVersion'], version)

        # A fresh process configuration on the same ports gets the same token.
        ports = self.spotify.port, self.settings.port
        self.services.close()
        spotify = SpotifyService('127.0.0.1', ports[0], Client(), log=quiet)
        settings = SpeakerSettingsService('127.0.0.1', ports[1], lambda: [], log=quiet)
        rebuilt = MusicServices([spotify, settings])
        self.addCleanup(rebuilt.close)
        self.assertEqual(rebuilt.descriptor()['AvailableServiceListVersion'], version)

    def test_spotify_resolver_receives_the_controller_context_only_when_supplied(self):
        uri = f'x-sonos-http:spotify%3Atrack%3A{TRACK}.mp3?sid=240&flags=0'
        with patch.object(self.spotify, 'resolve_playback', wraps=self.spotify.resolve_playback) as resolve:
            self.assertEqual(self.services.resolve_playback(uri, controller_ip='192.0.2.10'),
                             ('spotify:track:' + TRACK, 'music'))
            resolve.assert_called_once_with(uri, '', controller_ip='192.0.2.10')
            resolve.reset_mock()
            self.services.resolve_playback(uri, '')
            resolve.assert_called_once_with(uri, '')
        self.assertTrue(self.services.owns(uri))
        self.assertFalse(self.services.owns('http://example.invalid/song.mp3'))

    def test_settings_unknown_and_conflicting_ownership_cannot_reach_spotify(self):
        settings_uri = 'x-sonos-http:ss%3Aaction%3Aexample.mp3?sid=244&flags=0'
        with patch.object(self.spotify, 'resolve_playback') as resolve:
            for uri, metadata in ((settings_uri, ''), ('http://example.invalid/song.mp3', ''),
                                  (settings_uri, 'SA_RINCON61447_bridge')):
                with self.subTest(uri=uri, metadata=metadata):
                    with self.assertRaises(ValueError):
                        self.services.resolve_playback(uri, metadata, controller_ip='192.0.2.10')
            resolve.assert_not_called()
        stub = SmapiStub('127.0.0.1', 0, service_id=245, log=quiet)
        diagnostic = MusicServices([stub])
        self.addCleanup(diagnostic.close)
        with self.assertRaisesRegex(ValueError, 'cannot be played'):
            diagnostic.resolve_playback('x-sonos-http:bt%3Atrack%3A1.mp3?sid=245')

    def test_household_preserves_both_accounts_in_its_encoded_event(self):
        expected = self.services.media_servers()
        accounts = list(ET.fromstring(expected))
        self.assertEqual([entry.get('UDN') for entry in accounts],
                         ['SA_RINCON61447_bridge', 'SA_RINCON62471_bridge'])
        self.assertEqual([entry.get('Username0') for entry in accounts], ['bridge', 'bridge'])
        wires = []
        for household in (HOUSEHOLD, OTHER_HOUSEHOLD):
            with patch('music_service_events.secrets.token_bytes', return_value=IV):
                house = zoneplayers.Household(zp, household, ['Den'], ['127.0.0.1'], quiet,
                                              port=0, music_service=self.services)
            self.addCleanup(house.close)
            event = ET.fromstring(house.players[0].state.propertyset('ZoneGroupTopology'))
            self.assertIsNone(event.find('.//ThirdPartyMediaServers'))
            wire = event.find('.//ThirdPartyMediaServersX').text
            self.assertEqual(decode_accounts(wire, household), expected)
            self.assertEqual(house.music_service_accounts, wire)
            self.assertEqual(ET.fromstring(house.players[0].state.propertyset('ZoneGroupTopology'))
                             .find('.//ThirdPartyMediaServersX').text, wire)
            wires.append(wire)
        self.assertNotEqual(*wires)
        with self.assertRaises(ValueError):
            decode_accounts(wires[1], HOUSEHOLD)

    def test_nonplayable_http_requests_preserve_transport_and_backend(self):
        backend = MemoryBackend('memory')
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], quiet, port=0,
                                     backends=[backend], refs=[('memory', 'den')], music_service=self.services)
        self.addCleanup(house.close)
        house.start('127.0.0.1', ssdp=False)
        player = house.players[0]
        state, room = player.state, player.state.backend
        state.uri, state.metadata, state.transport = 'preserved', '<preserved/>', 'PLAYING'
        room.transport = 'PLAYING'
        room.enqueue_provider, room.play_provider = Mock(), Mock()
        before = state.uri, state.metadata, state.transport, room.transport, list(backend.calls)
        uri = 'x-sonos-http:ss%3Aaction%3Aexample.mp3?sid=244&flags=0'
        for action, prefix in (('SetAVTransportURI', 'Current'), ('AddURIToQueue', 'Enqueued')):
            arguments = ('<InstanceID>0</InstanceID><%sURI>%s</%sURI>'
                         '<%sURIMetaData></%sURIMetaData>' % (prefix, escape(uri), prefix, prefix, prefix))
            if action == 'AddURIToQueue':
                arguments += '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>'
            with self.subTest(action=action):
                status, body = soap(player, '/MediaRenderer/AVTransport/Control', 'AVTransport', action, arguments)
                self.assertEqual(status, 500, body)
                self.assertIn(b'Speaker settings cannot be played', body)
                self.assertEqual((state.uri, state.metadata, state.transport, room.transport, backend.calls), before)
        room.enqueue_provider.assert_not_called()
        room.play_provider.assert_not_called()


class ValidationAndLifecycle(unittest.TestCase):
    def test_controller_policy_provider_does_not_require_spotify_notice_hook(self):
        service = SmapiService('127.0.0.1', 0, 245, 'Other Provider', log=quiet)
        service.controller_resolver = lambda address: None
        services = MusicServices([service])
        self.addCleanup(services.close)
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], quiet,
                                      port=0, music_service=services)
        self.addCleanup(house.close)
        self.assertIsNone(house.spotify_notices)
        self.assertFalse(hasattr(service, 'on_locked'))
        self.assertEqual(zoneplayers.music_service_controllers(services), [service])

    def services(self):
        first = SpotifyService('127.0.0.1', 0, Client(), log=quiet)
        second = SpeakerSettingsService('127.0.0.1', 0, lambda: [], log=quiet)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        return first, second

    def test_duplicate_ids_types_uris_and_listener_ports_are_rejected(self):
        for field in ('Id', 'type', 'Uri', 'port'):
            with self.subTest(field=field):
                first, second = self.services()
                values = second.descriptor()
                root = ET.fromstring(values['AvailableServiceDescriptorList'])
                child = root.find('Service')
                if field == 'Id':
                    child.set('Id', str(first.service_id))
                elif field == 'type':
                    values['AvailableServiceTypeList'] = str(first.service_type)
                elif field == 'Uri':
                    child.set('Uri', first.uri)
                else:
                    child.set('Uri', first.uri.replace('/smapi', '/other'))
                values['AvailableServiceDescriptorList'] = ET.tostring(root, encoding='unicode')
                with patch.object(second, 'descriptor', return_value=values):
                    with self.assertRaises(ValueError):
                        MusicServices([first, second])
                self.assertEqual(first.server.fileno(), -1)
                self.assertEqual(second.server.fileno(), -1)

    def test_close_unstarted_spotify_and_settings_never_calls_shutdown(self):
        first, second = self.services()
        services = MusicServices([first, second])
        with patch.object(first.server, 'shutdown') as a, patch.object(second.server, 'shutdown') as b:
            services.close()
            services.close()
            a.assert_not_called()
            b.assert_not_called()
        self.assertEqual([service.server.fileno() for service in (first, second)], [-1, -1])
        with self.assertRaises(RuntimeError):
            services.start()
        with self.assertRaises(RuntimeError):
            first.start()

    def test_started_servers_respond_and_close_once_without_thread_leaks(self):
        first, second = self.services()
        services = MusicServices([first, second])
        self.addCleanup(services.close)
        services.start()
        threads = first._http_thread, second._http_thread
        services.start()
        self.assertEqual(threads, (first._http_thread, second._http_thread))
        for service in (first, second):
            connection = http.client.HTTPConnection('127.0.0.1', service.port, timeout=3)
            connection.request('GET', '/missing')
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.close()
        with patch.object(first, 'close', wraps=first.close) as a, patch.object(second, 'close', wraps=second.close) as b:
            services.close()
            services.close()
            a.assert_called_once_with()
            b.assert_called_once_with()
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([service.server.fileno() for service in (first, second)], [-1, -1])

    def test_partial_start_failure_closes_started_failed_and_pending_children(self):
        first, second = self.services()
        pending = SmapiStub('127.0.0.1', 0, service_id=245, log=quiet)
        self.addCleanup(pending.close)
        services = MusicServices([first, second, pending])
        with patch.object(second, 'start', side_effect=RuntimeError('startup failed')), \
                patch.object(pending, 'start') as start:
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                services.start()
            start.assert_not_called()
        self.assertFalse(first._http_thread.is_alive())
        self.assertEqual([service.server.fileno() for service in services.services], [-1, -1, -1])
        services.close()

    def test_failure_after_thread_start_still_stops_that_server(self):
        first, second = self.services()
        services = MusicServices([first, second])
        second.log = Mock(side_effect=RuntimeError('logging failed'))
        with self.assertRaisesRegex(RuntimeError, 'logging failed'):
            services.start()
        self.assertFalse(first._http_thread.is_alive())
        self.assertFalse(second._http_thread.is_alive())
        self.assertEqual(first.server.fileno(), -1)
        self.assertEqual(second.server.fileno(), -1)

    def test_cleanup_failure_does_not_leave_other_servers_open(self):
        first, second = self.services()
        services = MusicServices([first, second])
        services.start()
        with patch.object(second, 'close', side_effect=RuntimeError('cleanup failed')):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                services.close()
        self.assertFalse(first._http_thread.is_alive())
        self.assertFalse(second._http_thread.is_alive())
        self.assertEqual(first.server.fileno(), -1)
        self.assertEqual(second.server.fileno(), -1)
        services.close()


class PlaybackPolicies(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.policy = Mock()
        self.policy.before_action.return_value = None
        self.policy.play_provider.return_value = True
        self.service = SpotifyService('127.0.0.1', 0, Client(), log=quiet, playback_policy=self.policy)
        self.services = MusicServices([self.service])
        self.addCleanup(self.services.close)
        self.backend = MemoryBackend('memory')
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall'], ['127.0.0.1'] * 2,
            self.logs.append, port=0, music_service=self.services, backends=[self.backend],
            refs=[('memory', 'den'), ('memory', 'hall')])
        self.addCleanup(self.house.close)
        self.den, self.hall = self.house.players
        self.room = self.den.state.backend
        self.room.play_provider = Mock()
        self.room.enqueue_provider = Mock(return_value={'FirstTrackNumberEnqueued': '2', 'NumTracksAdded': '1',
                                                       'NewQueueLength': '2'})
        self.uri = f'x-sonos-http:spotify%3Atrack%3A{TRACK}.mp3?sid=240&flags=0'

    def test_owner_policy_routes_validated_play_and_off_falls_back_to_existing_backend(self):
        self.assertIs(self.services.playback_policy_for(self.uri), self.policy)
        with self.assertRaises(ValueError):
            self.services.playback_policy_for('http://music.invalid/track.mp3')
        args = {'InstanceID': '0', 'CurrentURI': self.uri, 'CurrentURIMetaData': ''}
        self.den.state.action('AVTransport', 'SetAVTransportURI', args)
        self.policy.play_provider.assert_called_once_with(self.room, 'spotify:track:' + TRACK, 'music')
        self.room.play_provider.assert_not_called()
        self.policy.before_action.assert_called_once_with(self.room, 'AVTransport', 'SetAVTransportURI', args)
        self.policy.after_action.assert_not_called()
        self.policy.play_provider.return_value = False
        self.den.state.action('AVTransport', 'SetAVTransportURI', args)
        self.room.play_provider.assert_called_once_with('spotify:track:' + TRACK, 'music')

    def test_provider_queue_notification_is_canonical_once_and_targets_group_coordinator(self):
        self.house.coordinator[self.hall.config.udn] = self.den.config.udn
        args = {'InstanceID': '0', 'EnqueuedURI': self.uri, 'EnqueuedURIMetaData': '',
                'DesiredFirstTrackNumberEnqueued': '0', 'EnqueueAsNext': '1'}
        values, _ = self.hall.state.action('AVTransport', 'AddURIToQueue', args)
        self.room.enqueue_provider.assert_called_once_with('spotify:track:' + TRACK, 'music', True)
        self.assertEqual(values['FirstTrackNumberEnqueued'], '2')
        self.policy.queue_added.assert_called_once_with(self.room, 'spotify:track:' + TRACK,
                                                       self.room.enqueue_provider.return_value, as_next=True)
        self.policy.after_action.assert_not_called()
        self.policy.before_action.assert_called_once_with(self.room, 'AVTransport', 'AddURIToQueue', args)

    def test_validation_precedes_policy_callbacks(self):
        args = {'InstanceID': '0', 'CurrentURI': 'x-sonos-http:spotify%3Atrack%3Abad.mp3?sid=240',
                'CurrentURIMetaData': ''}
        with self.assertRaises(zp.UPnPError):
            self.den.state.action('AVTransport', 'SetAVTransportURI', args)
        self.policy.before_action.assert_not_called()
        self.policy.play_provider.assert_not_called()
        self.room.play_provider.assert_not_called()

    def test_intercepted_transport_command_does_not_reach_backend_or_get_duplicate_notification(self):
        self.room.next = Mock()
        self.policy.before_action.return_value = {}
        args = {'InstanceID': '0'}
        self.den.state.action('AVTransport', 'Next', args)
        self.room.next.assert_not_called()
        self.policy.before_action.assert_called_once_with(self.room, 'AVTransport', 'Next', args)
        self.policy.after_action.assert_not_called()

    def test_unowned_successful_transport_is_notified_once_and_read_only_is_not(self):
        args = {'InstanceID': '0', 'Speed': '1'}
        self.den.state.action('AVTransport', 'Play', args)
        self.policy.after_action.assert_called_once_with(self.room, 'AVTransport', 'Play', args, result={})
        self.policy.before_action.reset_mock()
        self.policy.after_action.reset_mock()
        self.den.state.action('AVTransport', 'GetTransportInfo', {'InstanceID': '0'})
        self.policy.before_action.assert_not_called()
        self.policy.after_action.assert_not_called()

    def test_notification_failure_cannot_turn_successful_action_into_retried_failure(self):
        self.policy.after_action.side_effect = RuntimeError('credential-like details must not be logged')
        self.den.state.action('AVTransport', 'Play', {'InstanceID': '0', 'Speed': '1'})
        self.assertEqual(self.room.transport, 'PLAYING')
        failures = [entry for entry in self.logs if entry.get('event') == 'playback-policy-update-failed']
        self.assertEqual(failures, [{'event': 'playback-policy-update-failed', 'error_type': 'RuntimeError'}])

    def test_household_closes_unique_policies_before_player_listeners(self):
        events = []
        self.policy.close.side_effect = lambda: events.append('policy')
        with patch.object(self.den.http, 'server_close', side_effect=lambda: events.append('listener')):
            self.house.close()
        self.assertEqual(events[0], 'policy')
        self.assertIn('listener', events)

    def test_service_policy_lifecycle_is_idempotent(self):
        self.service.start()
        self.service.start()
        self.policy.start.assert_called_once_with()
        self.service.close()
        self.service.close()
        self.policy.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
