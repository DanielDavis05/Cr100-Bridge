"""Shared native settings routes retain each backend's playback ownership."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backends import BackendError
from ha_backend import HABackend, PLAY, VOLUME_SET, VOLUME_MUTE
from room_capabilities import RoomBinding
from sonos_backend import SonosBackend, Speaker


A = 'RINCON_000E58AAAAAA01400'
B = 'RINCON_000E58BBBBBB01400'
COORDINATOR, MEMBER, GENERIC = 'media_player.leader', 'media_player.member', 'media_player.generic'


class Settings:
    """No network: the provider has already verified/cache-read these settings."""
    def __init__(self):
        self.registered, self.calls = [], []
        self.started, self.closed = 0, 0
        self.uncertain_fixed = False
        self.operations = {'bass', 'treble', 'led', 'output_fixed', 'sleep_timer', 'channel_volume'}
        self.data = {'Bass': '3', 'Treble': '-2', 'LEDState': 'On', 'OutputFixed': '0',
                     'SupportsOutputFixed': '1', 'VolumeChannels': {'LF': '100', 'RF': '70'}}

    def register(self, key): self.registered.append(key)
    def start(self): self.started += 1
    def close(self): self.closed += 1
    def capabilities(self, key): return frozenset(self.operations)
    def values(self, key): return copy.deepcopy(self.data)
    def volume_blocked(self, key): return self.uncertain_fixed or self.data.get('OutputFixed') == '1'
    def action(self, key, service, action, args):
        self.calls.append((key, service, action, dict(args)))
        if action == 'GetBass': return {'CurrentBass': self.data['Bass']}
        if action == 'GetSupportsOutputFixed': return {'CurrentSupportsFixed': self.data['SupportsOutputFixed']}
        return {}


def native(uuid, base):
    return RoomBinding(platform='sonos', native_id=uuid, native_url=base,
                       group_key=('sonos', 'example'))


class HA:
    def __init__(self):
        self.connected = threading.Event(); self.connected.set()
        self.listeners, self.calls = [], []
        self.states = {entity: {'state': 'playing', 'attributes': {
            'supported_features': PLAY | VOLUME_SET | VOLUME_MUTE, 'volume_level': .25,
            'group_members': [COORDINATOR, MEMBER] if entity != GENERIC else [GENERIC]}}
            for entity in (COORDINATOR, MEMBER, GENERIC)}

    def state(self, entity): return self.states.get(entity, {})
    def close(self): pass
    def call_service(self, domain, service, entity, data=None, **kwargs):
        self.calls.append((domain, service, entity, data))


class HABackendSettings(unittest.TestCase):
    def setUp(self):
        self.ha, self.settings, self.input_events = HA(), Settings(), []
        self.bindings = {
            COORDINATOR: native(A, 'http://10.0.0.30:1400'),
            MEMBER: native(B, 'http://10.0.0.31:1400'),
            GENERIC: RoomBinding(platform='cast', group_key=('cast', 'example'))}
        inputs = SimpleNamespace(start=lambda: None, close=lambda: None,
            binding=lambda key: self.bindings.get(key, RoomBinding()),
            sources=lambda: [], identity=lambda key: {'model': 'Sonos-looking description'},
            on_audio=lambda uuid, values: self.input_events.append((uuid, values)))
        self.backend = HABackend(self.ha, lambda _: None, line_in=inputs, settings=self.settings)
        self.addCleanup(self.backend.close)
        self.room = self.backend.room(MEMBER)
        self.generic = self.backend.room(GENERIC)

    def test_selected_rooms_register_without_registering_unselected_coordinator(self):
        self.assertEqual(self.settings.registered, [MEMBER, GENERIC])
        self.assertEqual(self.settings.started, 1)
        self.assertEqual(self.backend.settings_coordinator(MEMBER), COORDINATOR)
        self.assertEqual(self.backend.settings_binding(COORDINATOR).native_id, A)

    def test_native_eq_uses_shared_provider_while_master_volume_stays_with_ha(self):
        self.assertEqual(self.room.settings_action('RenderingControl', 'GetBass', {'InstanceID': '0'}),
                         {'CurrentBass': '3'})
        self.room.settings_action('RenderingControl', 'SetBass', {'InstanceID': '0', 'DesiredBass': '4'})
        self.assertEqual(self.settings.calls[-1][0], MEMBER)
        self.assertIsNone(self.room.settings_action('RenderingControl', 'SetVolume',
            {'InstanceID': '0', 'Channel': 'Master', 'DesiredVolume': '40'}))
        self.room.set_volume(40)
        self.assertEqual(self.ha.calls, [('media_player', 'volume_set', MEMBER, {'volume_level': .4})])
        self.assertIsNone(self.room.settings_action('AVTransport', 'Play', {'InstanceID': '0', 'Speed': '1'}))
        self.assertEqual(len(self.settings.calls), 2)

    def test_generic_and_registry_only_sonos_never_delegate_native_actions(self):
        self.ha.states[GENERIC]['attributes']['group_members'] = [COORDINATOR, GENERIC]
        self.assertIsNone(self.backend.settings_coordinator(GENERIC))
        for room in (self.generic, self.room):
            if room is self.room: self.bindings[MEMBER] = RoomBinding(platform='sonos')
            with self.subTest(entity=room.key):
                self.assertFalse({'bass', 'led', 'sleep_timer'} & room.operation_capabilities())
                self.assertEqual(room.settings_values(), {})
                with self.assertRaises(BackendError):
                    room.settings_action('RenderingControl', 'SetBass', {'DesiredBass': '2'})
                self.assertEqual(room.settings_action('RenderingControl', 'GetSupportsOutputFixed', {}),
                                 {'CurrentSupportsFixed': '0'})
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.ha.calls, [])

    def test_disconnect_and_missing_coordinator_withdraw_native_routes(self):
        self.ha.connected.clear()
        self.assertEqual(self.room.operation_capabilities(), frozenset())
        self.assertFalse(self.backend.settings_binding(MEMBER).native_sonos)
        self.assertIsNone(self.backend.settings_coordinator(MEMBER))
        with self.assertRaises(BackendError):
            self.room.settings_action('DeviceProperties', 'SetLEDState', {'DesiredLEDState': 'Off'})
        self.ha.connected.set()
        self.assertIn('led', self.room.operation_capabilities())
        self.bindings.pop(COORDINATOR)
        self.assertIsNone(self.backend.settings_coordinator(MEMBER))
        self.ha.states[MEMBER]['attributes'].pop('group_members')
        self.assertIsNone(self.backend.settings_coordinator(MEMBER))
        self.assertEqual(self.settings.calls, [])

    def test_fixed_output_blocks_ha_master_writes_then_restores_them(self):
        self.settings.data['OutputFixed'] = '1'
        for operation in ('volume', 'mute'): self.assertNotIn(operation, self.room.operation_capabilities())
        for send in (lambda: self.room.send_volume(30), lambda: self.room.send_mute(True)):
            with self.assertRaises(BackendError): send()
        self.assertEqual(self.ha.calls, [])
        self.settings.data['OutputFixed'] = '0'
        self.room.send_volume(30)
        self.assertEqual(self.ha.calls[-1], ('media_player', 'volume_set', MEMBER, {'volume_level': .3}))

    def test_cached_values_are_isolated_and_input_edits_refresh_existing_source_cache(self):
        values = self.room.settings_values()
        values['VolumeChannels']['LF'] = '0'
        self.assertEqual(self.room.settings_values()['VolumeChannels']['LF'], '100')
        self.settings.data.update(AudioInputName='Turntable', Icon='Phonograph')
        self.backend.settings_changed(MEMBER)
        self.assertEqual(self.input_events, [(B, {'AudioInputName': 'Turntable', 'Icon': 'Phonograph'})])

    def test_uncertain_fixed_write_blocks_ha_volume_without_claiming_new_readback(self):
        self.settings.uncertain_fixed = True
        self.assertEqual(self.room.settings_values()['OutputFixed'], '0')
        self.assertFalse({'volume', 'mute'} & self.room.operation_capabilities())
        with self.assertRaises(BackendError): self.room.send_volume(30)
        with self.assertRaises(BackendError): self.room.send_mute(True)
        self.assertEqual(self.ha.calls, [])
        self.settings.uncertain_fixed = False
        self.assertIn('volume', self.room.operation_capabilities())


class SonosBackendSettings(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        listener = SimpleNamespace(start=lambda: None, close=lambda: None)
        inventory = SimpleNamespace(identity=lambda key: {'id': key, 'model': 'Cached Sonos'})
        self.backend = SonosBackend(log=lambda _: None, listener=listener,
                                    identity_inventory=inventory, settings=self.settings)
        self.addCleanup(self.backend.close)
        self.backend.speakers = {A: Speaker(A, 'http://10.0.0.30:1400', 'Leader'),
                                 B: Speaker(B, 'http://10.0.0.31:1400', 'Member')}
        self.backend.groups = [(A, [A, B])]
        self.room = self.backend.room(B)

    def test_member_settings_are_native_and_timer_resolver_uses_known_coordinator(self):
        self.assertEqual(self.settings.registered, [B])
        self.assertEqual(self.backend.settings_binding(B).native_id, B)
        self.assertEqual(self.backend.settings_coordinator(B), A)
        self.assertIn('bass', self.room.operation_capabilities())
        self.room.settings_action('RenderingControl', 'SetBass', {'InstanceID': '0', 'DesiredBass': '4'})
        self.assertEqual(self.settings.calls[-1][0], B)
        self.backend.groups = []
        self.assertIsNone(self.backend.settings_coordinator(B))

    def test_coordinator_outage_withdraws_physical_settings_conservatively(self):
        self.backend.speakers[A].available = False
        self.assertEqual(self.room.operation_capabilities(), frozenset())
        self.assertFalse(self.backend.settings_binding(B).native_sonos)
        self.assertEqual(self.room.settings_values(), {})
        with self.assertRaises(BackendError):
            self.room.settings_action('RenderingControl', 'SetBass', {'DesiredBass': '4'})
        self.assertEqual(self.settings.calls, [])
        self.assertEqual(self.backend.identity(B)['model'], 'Cached Sonos')

    def test_channel_settings_do_not_enter_normal_master_rendering_path(self):
        self.assertIsNone(self.room.settings_action('RenderingControl', 'SetMute',
            {'InstanceID': '0', 'Channel': 'Master', 'DesiredMute': '1'}))
        self.room.settings_action('RenderingControl', 'SetVolume',
            {'InstanceID': '0', 'Channel': 'LF', 'DesiredVolume': '85'})
        self.assertEqual(self.settings.calls, [(B, 'RenderingControl', 'SetVolume',
            {'InstanceID': '0', 'Channel': 'LF', 'DesiredVolume': '85'})])
        self.settings.data['OutputFixed'] = '1'
        with patch('sonos_backend.soap') as native_call:
            with self.assertRaises(BackendError): self.room.send_volume(20)
            with self.assertRaises(BackendError): self.room.send_mute(True)
            native_call.assert_not_called()

    def test_input_name_readback_refreshes_all_receiving_room_menus(self):
        self.settings.data.update(AudioInputName='CD Player', Icon='CD')
        changed = []
        self.backend.listeners.append(changed.append)
        self.backend.settings_changed(B)
        self.assertEqual(self.backend.speaker(B).audio_in['name'], 'CD Player')
        self.assertEqual(set(changed), {A, B})

    def test_unproven_fixed_output_is_reported_false(self):
        self.settings.operations.discard('output_fixed')
        self.assertEqual(self.room.settings_action('RenderingControl', 'GetSupportsOutputFixed', {}),
                         {'CurrentSupportsFixed': '0'})
        self.assertEqual(self.settings.calls, [])

    def test_uncertain_fixed_write_blocks_native_master_volume_and_mute(self):
        self.settings.uncertain_fixed = True
        self.assertEqual(self.room.settings_values()['OutputFixed'], '0')
        with patch('sonos_backend.soap') as native_call:
            with self.assertRaises(BackendError): self.room.send_volume(30)
            with self.assertRaises(BackendError): self.room.send_mute(True)
            native_call.assert_not_called()
        self.settings.uncertain_fixed = False
        self.assertIn('volume', self.room.operation_capabilities())


if __name__ == '__main__': unittest.main()
