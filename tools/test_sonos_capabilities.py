"""Cached Sonos command routing is independent of descriptive About identities."""
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

from sonos_backend import SonosBackend, Speaker
from sonos_upnp import UPnPFault


A = 'RINCON_000E58AAAAAA01400'
B = 'RINCON_000E58BBBBBB01400'
BASE = 'http://10.0.0.30:1400'


class SonosCapabilities(unittest.TestCase):
    def setUp(self):
        self.inventory = SimpleNamespace(identity=lambda key: {
            'id': key, 'model': 'Sonos Test', 'ip': '203.0.113.200', 'software': '85.0-12345'})
        self.backend = SonosBackend(listener=SimpleNamespace(close=lambda: None),
                                    identity_inventory=self.inventory, log=lambda item: None)
        self.a = Speaker(A, BASE, 'Kitchen')
        self.b = Speaker(B, 'http://10.0.0.31:1400', 'Patio')
        self.backend.speakers.update({A: self.a, B: self.b})
        self.backend.groups = [(A, [A]), (B, [B])]
        self.a.avt = {'AVTransportURI': 'x-rincon-queue:' + A + '#0', 'TransportState': 'PLAYING',
                      'CurrentTransportActions': 'Play,Pause,Stop,Next,Previous,Seek'}
        self.b.avt = dict(self.a.avt)
        self.room = self.backend.room(A)

    def test_binding_and_capabilities_read_only_cached_physical_routing(self):
        with patch('sonos_backend.soap', side_effect=AssertionError('request-thread network')), \
                patch.object(self.backend, 'identity', side_effect=AssertionError('About is not routing identity')):
            binding = self.backend.binding(A)
            operations = self.room.operation_capabilities()
        self.assertEqual((binding.platform, binding.native_id, binding.native_url), ('sonos', A, BASE))
        self.assertTrue(binding.native_sonos)
        self.assertEqual(binding.group_key, self.backend.binding(B).group_key)
        self.assertNotIn('203.0.113.200', binding.native_url)
        with self.assertRaises(FrozenInstanceError): binding.native_url = 'http://example.invalid'
        self.assertTrue({'play', 'pause', 'stop', 'play_item', 'volume', 'mute', 'seek', 'queue_browse',
                         'queue_add', 'queue_remove', 'group', 'shuffle', 'repeat'} <= operations)
        self.assertFalse({'alarm', 'bass', 'balance', 'save_queue'} & operations)

    def test_known_about_identity_without_playback_device_is_not_a_native_binding(self):
        self.backend.speakers.clear()
        self.assertEqual(self.backend.identity(A)['model'], 'Sonos Test')
        binding = self.backend.binding(A)
        self.assertTrue(binding.is_sonos)
        self.assertFalse(binding.native_sonos)
        self.assertIsNone(binding.group_key)
        self.assertFalse(self.room.available)
        self.assertEqual(self.room.operation_capabilities(), frozenset())

    def test_invalid_and_bridge_synthetic_ids_cannot_acquire_native_routes(self):
        for key in ('unknown', 'RINCON_02690000000101400'):
            self.backend.speakers[key] = Speaker(key, BASE, 'Sonos named room')
            self.assertFalse(self.backend.binding(key).is_sonos)
            self.assertFalse(self.backend.binding(key).native_sonos)
            self.assertEqual(self.backend.room(key).operation_capabilities(), frozenset())

    def test_descriptive_endpoint_or_malformed_control_endpoint_is_not_used(self):
        self.a.base = 'http://user:password@10.0.0.30:1400'
        self.assertTrue(self.backend.identity(A))
        self.assertFalse(self.backend.binding(A).native_sonos)
        self.assertFalse(self.room.available)

    def test_group_key_requires_playback_topology_not_only_bonded_about_records(self):
        self.backend.groups = [(B, [B])]
        self.assertIsNone(self.backend.binding(A).group_key)
        self.assertNotIn('group', self.room.operation_capabilities())
        other = SonosBackend(listener=SimpleNamespace(close=lambda: None),
                             identity_inventory=self.inventory, log=lambda item: None)
        other.speakers[A] = self.a
        other.groups = [(A, [A])]
        self.backend.groups = [(A, [A])]
        self.assertNotEqual(self.backend.binding(A).group_key, other.binding(A).group_key)

    def test_radio_and_line_in_withdraw_inapplicable_transport_controls(self):
        for uri in ('x-sonosapi-stream:s123?sid=254', 'x-rincon-stream:' + B):
            self.a.avt.update(AVTransportURI=uri, CurrentTransportActions='Set,Stop,Play')
            ops = self.room.operation_capabilities()
            self.assertTrue({'play', 'stop', 'queue_use', 'queue_browse'} <= ops)
            self.assertFalse({'seek', 'next', 'previous', 'shuffle', 'repeat'} & ops)
            self.assertEqual('pause' in ops, uri.startswith('x-rincon-stream:'))
            self.assertEqual(self.room.capabilities(), frozenset())
        self.a.avt['AVTransportURI'] = 'x-rincon-queue:' + A + '#0'
        self.assertTrue({'shuffle', 'repeat'} <= self.room.operation_capabilities())

    def test_connected_input_is_required_to_advertise_line_in_selection(self):
        self.assertNotIn('line_in', self.room.operation_capabilities())
        self.b.audio_in['connected'] = True
        self.b.audio_subscription = SimpleNamespace(healthy=True)
        self.assertIn('line_in', self.room.operation_capabilities())
        self.b.audio_subscription.healthy = False
        self.assertNotIn('line_in', self.room.operation_capabilities())

    def test_line_in_uri_only_event_exposes_pause_as_stop_without_queue_controls(self):
        # CurrentTransportActions may arrive in a later event/read than the URI.
        self.a.avt['AVTransportURI'] = 'x-rincon-stream:' + B
        self.assertEqual(self.a.avt['CurrentTransportActions'], 'Play,Pause,Stop,Next,Previous,Seek')
        self.assertEqual(self.room.transport_actions(), 'Play,Stop,Pause')
        self.assertFalse({'seek', 'next', 'previous', 'shuffle', 'repeat'}
                         & self.room.operation_capabilities())
        with patch.object(self.room, 'transport_call') as call:
            self.room.require('pause')
            self.room.pause()
            call.assert_called_once_with('Stop')

    def test_persistent_failed_reads_withdraw_operations_but_preserve_about_then_event_recovers(self):
        self.a.audio_checked = True
        self.a.available = True
        changed = []
        self.backend.listeners.append(changed.append)
        with patch('sonos_backend.soap', side_effect=UPnPFault('GetTransportInfo', None, 'offline')), \
                patch('sonos_backend.time.monotonic') as clock:
            for now in (100, 110):
                clock.return_value = now
                self.backend.read(self.a)
                self.assertTrue(self.room.available)
                self.assertEqual(changed, [])
            clock.return_value = 120
            self.backend.read(self.a)
        self.assertEqual(changed, [A])
        self.assertFalse(self.room.available)
        self.assertEqual(self.room.operation_capabilities(), frozenset())
        self.assertEqual(self.backend.identity(A)['model'], 'Sonos Test')
        self.assertTrue(self.backend.binding(A).native_sonos)
        self.backend.on_avt(A, {'LastChange': '<Event><InstanceID val="0">'
                                          '<TransportState val="PLAYING"/></InstanceID></Event>'})
        self.assertTrue(self.room.available)
        self.assertIn('play', self.room.operation_capabilities())

    def test_coordinator_outage_withdraws_group_members_transport_routes(self):
        self.backend.groups = [(A, [A, B])]
        self.a.available = False
        self.assertFalse(self.backend.room(B).available)
        self.assertEqual(self.backend.room(B).operation_capabilities(), frozenset())


if __name__ == '__main__': unittest.main()
