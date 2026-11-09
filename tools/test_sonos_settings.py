"""Native settings: cache boundaries, physical/coordinator ownership and readback."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from backends import BackendError
from room_capabilities import RoomBinding
import sonos_settings as settings
from sonos_upnp import UPnPFault

FIXTURES = Path(__file__).parent / 'zp80' / 'fixtures' / 'xml'
A, B = 'RINCON_A01400', 'RINCON_B01400'
BASE_A, BASE_B = 'http://10.0.0.10:1400', 'http://10.0.0.11:1400'


class Native:
    def __init__(self):
        self.reads, self.calls, self.writes = [], [], []
        self.descriptions, self.documents, self.states = {}, {}, {}
        self.fail_getter, self.ignore_write, self.before_read = None, None, None
        for uuid, base in ((A, BASE_A), (B, BASE_B)):
            self.descriptions[base] = (FIXTURES / 'device_description.xml').read_text().replace('#UUID#', uuid).replace('#MODEL#', 'ZP80')
            self.states[base] = {
                'Bass': '1', 'Treble': '-2', 'Loudness': '1',
                'SupportsOutputFixed': '1', 'OutputFixed': '0', 'LEDState': 'On',
                'AudioInputName': 'Record player', 'Icon': 'x-rincon-roomicon:living',
                'LeftLineInLevel': '3', 'RightLineInLevel': '4',
                'VolumeChannels': {'LF': '100', 'RF': '80'},
                'MuteChannels': {'LF': '0', 'RF': '1'},
                'RemainingSleepTimerDuration': '', 'CurrentSleepTimerGeneration': '1',
            }

    def reader(self, url, timeout):
        self.reads.append(url)
        if url.endswith('/xml/device_description.xml'):
            return self.descriptions[url.removesuffix('/xml/device_description.xml')]
        name = url.rpartition('/')[2]
        return self.documents.get(url) or (FIXTURES / name).read_text()

    def soap(self, base, service, action, pairs=(), timeout=2):
        args = dict(pairs)
        self.calls.append((base, service, action, args))
        state = self.states[base]
        if self.before_read and action.startswith('Get'):
            callback, self.before_read = self.before_read, None
            callback()
        if action == self.fail_getter: raise UPnPFault(action, None, 'offline')
        if action.startswith('Set') or action == 'ConfigureSleepTimer':
            self.writes.append((base, service, action, args))
            if action == self.ignore_write: return {}
            if action in ('SetBass', 'SetTreble', 'SetLoudness'):
                name = action[3:]; state[name] = args['Desired' + name]
            elif action in ('SetVolume', 'SetMute'):
                name = action[3:]; state[name + 'Channels'][args['Channel']] = args['Desired' + name]
            elif action == 'SetLEDState': state['LEDState'] = args['DesiredLEDState']
            elif action == 'SetOutputFixed': state['OutputFixed'] = args['DesiredFixed']
            elif action == 'SetAudioInputAttributes':
                state.update(AudioInputName=args['DesiredName'])
                if args['DesiredIcon']: state['Icon'] = args['DesiredIcon']
            elif action == 'SetLineInLevel':
                state.update(LeftLineInLevel=args['DesiredLeftLineInLevel'], RightLineInLevel=args['DesiredRightLineInLevel'])
            elif action == 'ConfigureSleepTimer':
                state['RemainingSleepTimerDuration'] = args['NewSleepTimerDuration']
                state['CurrentSleepTimerGeneration'] = str(int(state['CurrentSleepTimerGeneration']) + 1)
            return {}
        if action in ('GetBass', 'GetTreble', 'GetLoudness'):
            name = action[3:]; return {'Current' + name: state[name]}
        if action in ('GetVolume', 'GetMute'):
            name = action[3:]; return {'Current' + name: state[name + 'Channels'][args['Channel']]}
        return {
            'GetSupportsOutputFixed': {'CurrentSupportsFixed': state['SupportsOutputFixed']},
            'GetOutputFixed': {'CurrentFixed': state['OutputFixed']},
            'GetLEDState': {'CurrentLEDState': state['LEDState']},
            'GetAutoplayVolume': {'CurrentVolume': state.get('AutoplayVolume', '50')},
            'GetUseAutoplayVolume': {'UseVolume': state.get('AutoplayUseVolume', '1')},
            'GetAutoplayLinkedZones': {'IncludeLinkedZones': state.get('AutoplayIncludeLinkedZones', '0')},
            'GetAutoplayRoomUUID': {'RoomUUID': state.get('AutoplayRoomUUID', '')},
            'GetAudioInputAttributes': {'CurrentName': state['AudioInputName'], 'CurrentIcon': state['Icon']},
            'GetLineInLevel': {'CurrentLeftLineInLevel': state['LeftLineInLevel'], 'CurrentRightLineInLevel': state['RightLineInLevel']},
            'GetRemainingSleepTimerDuration': {'RemainingSleepTimerDuration': state['RemainingSleepTimerDuration'],
                                               'CurrentSleepTimerGeneration': state['CurrentSleepTimerGeneration']},
        }[action]

    def remove_action(self, base, service, action):
        url = base + '/xml/' + service + '1.xml'
        root = ET.fromstring(self.documents.get(url) or (FIXTURES / (service + '1.xml')).read_text())
        for parent in root.iter():
            for child in list(parent):
                if settings.local(child.tag) == 'action' and settings.text(child, 'name') == action: parent.remove(child)
        self.documents[url] = ET.tostring(root)


class Settings(unittest.TestCase):
    def setUp(self):
        self.native, self.now, self.notices = Native(), 100, []
        self.bindings = {'member': RoomBinding(platform='sonos', native_id=A, native_url=BASE_A),
                         'leader': RoomBinding(platform='sonos', native_id=B, native_url=BASE_B)}
        self.coordinators = {'member': 'member', 'leader': 'leader'}
        self.provider = settings.SettingsProvider(self.bindings.get, self.notices.append,
            self.coordinators.get, lambda _: None, self.native.reader, self.native.soap, lambda: self.now)
        self.provider.register('member')
        self.addCleanup(self.provider.close)

    def warm(self):
        self.provider.refresh()
        self.provider.refresh()

    def action(self, service, action, args=None):
        return self.provider.action('member', service, action, args or {})

    def test_cache_only_getters_capabilities_and_copies(self):
        self.warm()
        before = (len(self.native.reads), len(self.native.calls))
        self.assertTrue({'bass', 'treble', 'loudness', 'channel_volume', 'channel_mute', 'led',
                         'output_fixed', 'sleep_timer', 'line_in_settings'} <= self.provider.capabilities('member'))
        self.assertIn('line_in_level', self.provider.capabilities('member'))
        self.assertEqual(self.action('RenderingControl', 'GetBass'), {'CurrentBass': '1'})
        self.assertEqual(self.action('AudioIn', 'GetLineInLevel'),
                         {'CurrentLeftLineInLevel': '3', 'CurrentRightLineInLevel': '4'})
        copied = self.provider.values('member'); copied['VolumeChannels']['LF'] = '0'
        self.assertEqual(self.provider.values('member')['VolumeChannels']['LF'], '100')
        self.assertEqual((len(self.native.reads), len(self.native.calls)), before)

    def test_identity_verification_precedes_actions_and_rejects_bridge_and_generic_bindings(self):
        self.native.descriptions[BASE_A] = self.native.descriptions[BASE_A].replace('uuid:' + A, 'uuid:' + B)
        self.warm()
        self.assertFalse(self.provider.capabilities('member'))
        self.assertFalse(self.native.calls)
        for binding in (RoomBinding(platform='cast', native_id=A, native_url=BASE_A),
                        RoomBinding(platform='sonos'),
                        RoomBinding(platform='sonos', native_id='RINCON_02690000000101400', native_url=BASE_A)):
            self.bindings['member'] = binding
            self.assertFalse(self.provider.capabilities('member'))
            with self.assertRaises(BackendError): self.action('RenderingControl', 'SetBass', {'DesiredBass': '2'})
        self.assertFalse(self.native.writes)

    def test_scpd_getter_and_setter_pair_is_required(self):
        self.native.remove_action(BASE_A, 'RenderingControl', 'SetBass')
        self.native.remove_action(BASE_A, 'DeviceProperties', 'GetLEDState')
        self.warm()
        self.assertNotIn('bass', self.provider.capabilities('member'))
        self.assertNotIn('led', self.provider.capabilities('member'))
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetBass', {'DesiredBass': '2'})
        self.assertFalse(self.native.writes)

    def test_settings_target_member_and_timer_targets_unselected_coordinator(self):
        self.coordinators['member'] = 'leader'
        self.warm()
        self.action('RenderingControl', 'SetBass', {'DesiredBass': '4'})
        self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:15:00'})
        self.assertEqual([(base, action) for base, _, action, _ in self.native.writes],
                         [(BASE_A, 'SetBass'), (BASE_B, 'ConfigureSleepTimer')])
        self.assertEqual(self.provider.values('member')['RemainingSleepTimerDuration'], '0:15:00')
        self.coordinators['member'] = None
        self.assertNotIn('sleep_timer', self.provider.capabilities('member'))
        with self.assertRaises(BackendError): self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': ''})

    def test_channel_changes_are_independent_and_master_is_not_routed(self):
        self.warm()
        self.action('RenderingControl', 'SetVolume', {'Channel': 'LF', 'DesiredVolume': '25'})
        self.assertEqual(self.native.states[BASE_A]['VolumeChannels'], {'LF': '25', 'RF': '80'})
        self.action('RenderingControl', 'SetMute', {'Channel': 'RF', 'DesiredMute': '0'})
        self.assertEqual(self.action('RenderingControl', 'GetMute', {'Channel': 'RF'}), {'CurrentMute': '0'})
        self.assertIsNone(self.action('RenderingControl', 'SetVolume', {'Channel': 'Master', 'DesiredVolume': '30'}))
        self.assertFalse(any(call[3].get('Channel') == 'Master' for call in self.native.writes))

    def test_fixed_output_hides_eq_but_can_be_restored(self):
        self.warm()
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        self.assertFalse({'bass', 'treble', 'loudness', 'channel_volume', 'channel_mute'} & self.provider.capabilities('member'))
        self.assertIn('output_fixed', self.provider.capabilities('member'))
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetBass', {'DesiredBass': '2'})
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '0'})
        self.assertIn('bass', self.provider.capabilities('member'))

    def test_fixed_output_support_is_not_inferred_from_model_or_action_presence(self):
        self.native.states[BASE_A]['SupportsOutputFixed'] = '0'
        self.warm()
        self.assertEqual(self.action('RenderingControl', 'GetSupportsOutputFixed'), {'CurrentSupportsFixed': '0'})
        self.assertNotIn('output_fixed', self.provider.capabilities('member'))

    def test_known_fixed_output_gate_survives_outage_but_not_endpoint_rebind(self):
        self.warm()
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        self.native.fail_getter = 'GetBass'
        self.now += settings.REFRESH_INTERVAL
        self.warm()
        self.assertFalse(self.provider.capabilities('member') - {'sleep_timer'})
        self.assertEqual(self.provider.values('member').get('OutputFixed'), '1')
        self.assertTrue(self.provider.volume_blocked('member'))
        with self.assertRaises(BackendError): self.action('RenderingControl', 'GetOutputFixed')
        self.bindings['member'] = self.bindings['leader']
        self.assertNotIn('OutputFixed', self.provider.values('member'))
        self.assertFalse(self.provider.volume_blocked('member'))

    def test_uncertain_fixed_write_blocks_volume_without_inventing_display_state(self):
        self.warm()
        self.assertFalse(self.provider.volume_blocked('member'))
        self.native.fail_getter = 'GetBass'
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        self.assertTrue(self.provider.volume_blocked('member'))
        self.assertNotIn('OutputFixed', self.provider.values('member'))  # previous confirmed value was variable
        self.native.fail_getter = None; self.now += settings.RETRY_INTERVAL; self.warm()
        self.assertTrue(self.provider.volume_blocked('member'))  # now confirmed fixed
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '0'})
        self.assertFalse(self.provider.volume_blocked('member'))

    def test_uncertain_fixed_write_timeout_stays_blocked_until_native_read_confirms(self):
        self.warm()
        original = self.provider.soap
        def timeout(base, service, action, args, timeout):
            if action == 'SetOutputFixed': raise UPnPFault(action, None, 'write timed out')
            return original(base, service, action, args, timeout)
        self.provider.soap = timeout
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        self.assertTrue(self.provider.volume_blocked('member'))
        before = len(self.native.calls)
        self.assertTrue(self.provider.volume_blocked('member'))
        self.assertEqual(len(self.native.calls), before)
        self.provider.soap = original; self.now += settings.RETRY_INTERVAL; self.warm()
        self.assertFalse(self.provider.volume_blocked('member'))

    def test_fixed_gate_survives_model_specific_fixed_getter_refusal(self):
        self.warm()
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        original = self.provider.soap
        def refused(base, service, action, args, timeout):
            if action == 'GetOutputFixed': raise UPnPFault(action, 401, 'unsupported now')
            return original(base, service, action, args, timeout)
        self.provider.soap = refused; self.now += settings.REFRESH_INTERVAL; self.warm()
        self.assertTrue(self.provider.volume_blocked('member'))
        self.assertFalse({'bass', 'treble', 'loudness', 'channel_volume', 'channel_mute'}
                         & self.provider.capabilities('member'))

    def test_same_native_binding_recovery_retains_fixed_guard_until_fresh_probe(self):
        self.warm()
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        original = self.bindings['member']
        self.bindings['member'] = RoomBinding()  # HA/transport outage hides native availability
        self.provider.refresh()
        self.assertFalse(self.provider.capabilities('member'))
        self.bindings['member'] = original
        self.assertFalse(self.provider.capabilities('member'))
        self.assertTrue(self.provider.volume_blocked('member'))
        with self.assertRaises(BackendError): self.action('RenderingControl', 'GetOutputFixed')
        self.warm()
        self.assertEqual(self.action('RenderingControl', 'GetOutputFixed'), {'CurrentFixed': '1'})

    def test_same_native_binding_recovery_retains_uncertain_write_guard(self):
        self.warm()
        self.native.fail_getter = 'GetBass'
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        original = self.bindings['member']
        self.bindings['member'] = RoomBinding(); self.provider.refresh()
        self.bindings['member'] = original
        self.assertTrue(self.provider.volume_blocked('member'))
        self.assertFalse(self.provider.capabilities('member'))
        self.bindings['member'] = self.bindings['leader']
        self.assertFalse(self.provider.volume_blocked('member'))

    def availability_gap(self):
        original = self.bindings['member']
        self.bindings['member'] = RoomBinding()
        self.assertFalse(self.provider.capabilities('member'))
        self.bindings['member'] = original
        self.assertFalse(self.provider.capabilities('member'))

    def test_inflight_read_cannot_restore_health_after_availability_gap(self):
        self.warm()
        self.action('RenderingControl', 'SetOutputFixed', {'DesiredFixed': '1'})
        descriptions = len(self.native.reads)
        self.native.before_read = self.availability_gap
        self.now += settings.REFRESH_INTERVAL
        self.provider.refresh()
        self.assertFalse(self.provider.capabilities('member'))
        self.assertTrue(self.provider.volume_blocked('member'))
        self.assertEqual(len(self.native.reads), descriptions)
        self.warm()
        self.assertGreater(len(self.native.reads), descriptions)
        self.assertIn('output_fixed', self.provider.capabilities('member'))

    def test_inflight_discovery_cannot_publish_metadata_after_availability_gap(self):
        original = self.provider.reader
        first = [True]
        def interrupted(url, timeout):
            if first[0]:
                first[0] = False
                self.availability_gap()
            return original(url, timeout)
        self.provider.reader = interrupted
        self.provider.refresh()
        self.assertFalse(self.provider.capabilities('member'))
        self.assertFalse(self.native.calls)  # stale discovery never reaches settings probes
        self.warm()
        self.assertIn('bass', self.provider.capabilities('member'))

    def test_invalid_values_and_unqualified_input_level_never_write(self):
        self.warm()
        cases = [('RenderingControl', 'SetBass', {'DesiredBass': '11'}),
                 ('RenderingControl', 'SetTreble', {'DesiredTreble': '1.2'}),
                 ('RenderingControl', 'SetLoudness', {'Channel': 'LF', 'DesiredLoudness': '1'}),
                 ('RenderingControl', 'SetVolume', {'Channel': 'LF', 'DesiredVolume': '-1'}),
                 ('DeviceProperties', 'SetLEDState', {'DesiredLEDState': 'Disabled'}),
                 ('AudioIn', 'SetLineInLevel', {'DesiredLeftLineInLevel': '8', 'DesiredRightLineInLevel': '4'}),
                 ('AudioIn', 'SetAudioInputAttributes', {'DesiredName': 'Bad\x00name', 'DesiredIcon': ''}),
                 ('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '24:00:00'})]
        for service, action, args in cases:
            with self.subTest(action=action):
                with self.assertRaises(BackendError): self.action(service, action, args)
        self.assertFalse(self.native.writes)

    def test_zp80_input_level_uses_native_ten_choice_gains_without_scpd_ranges(self):
        self.warm()
        self.assertIn('line_in_level', self.provider.capabilities('member'))
        for gain in (0, 1, 2, 3, 4, 5, 6, 7, 9, 12):
            self.action('AudioIn', 'SetLineInLevel',
                        {'DesiredLeftLineInLevel': str(gain), 'DesiredRightLineInLevel': str(gain)})
            self.assertEqual(self.action('AudioIn', 'GetLineInLevel'),
                             {'CurrentLeftLineInLevel': str(gain), 'CurrentRightLineInLevel': str(gain)})
        before = len(self.native.writes)
        for gain in (-1, 8, 10, 11, 13):
            with self.subTest(gain=gain), self.assertRaises(BackendError):
                self.action('AudioIn', 'SetLineInLevel',
                            {'DesiredLeftLineInLevel': str(gain), 'DesiredRightLineInLevel': str(gain)})
        self.assertEqual(len(self.native.writes), before)

    def test_custom_input_name_preserves_native_icon_and_requires_name_readback(self):
        self.warm()
        icon = self.native.states[BASE_A]['Icon']
        self.action('AudioIn', 'SetAudioInputAttributes', {'DesiredName': 'Sound Soother', 'DesiredIcon': ''})
        self.assertEqual(self.action('AudioIn', 'GetAudioInputAttributes'),
                         {'CurrentName': 'Sound Soother', 'CurrentIcon': icon})
        self.native.ignore_write = 'SetAudioInputAttributes'
        with self.assertRaisesRegex(BackendError, 'readback did not confirm'):
            self.action('AudioIn', 'SetAudioInputAttributes', {'DesiredName': 'Ignored change', 'DesiredIcon': ''})

    def test_input_level_without_range_requires_known_model_and_i4_type(self):
        url = BASE_A + '/xml/AudioIn1.xml'
        self.native.documents[url] = (FIXTURES / 'AudioIn1.xml').read_text().replace('<dataType>i4</dataType>', '<dataType>string</dataType>')
        self.warm()
        self.assertNotIn('line_in_level', self.provider.capabilities('member'))
        with self.assertRaises(BackendError):
            self.action('AudioIn', 'SetLineInLevel', {'DesiredLeftLineInLevel': '3', 'DesiredRightLineInLevel': '4'})
        self.assertFalse(self.native.writes)

    def test_unknown_model_requires_explicit_input_level_ranges(self):
        self.native.descriptions[BASE_A] = self.native.descriptions[BASE_A].replace(
            '<modelNumber>ZP80</modelNumber>', '<modelNumber>Unknown</modelNumber>')
        self.warm()
        self.assertNotIn('line_in_level', self.provider.capabilities('member'))
        with self.assertRaises(BackendError):
            self.action('AudioIn', 'SetLineInLevel', {'DesiredLeftLineInLevel': '3', 'DesiredRightLineInLevel': '4'})
        self.assertFalse(self.native.writes)

    def test_input_level_explicit_native_ranges_take_precedence(self):
        url = BASE_A + '/xml/AudioIn1.xml'
        root = ET.fromstring((FIXTURES / 'AudioIn1.xml').read_text())
        for variable in root.iter():
            if settings.local(variable.tag) != 'stateVariable': continue
            if settings.text(variable, 'name') not in ('LeftLineInLevel', 'RightLineInLevel'): continue
            bounds = ET.SubElement(variable, 'allowedValueRange')
            ET.SubElement(bounds, 'minimum').text = '0'
            ET.SubElement(bounds, 'maximum').text = '10'
            ET.SubElement(bounds, 'step').text = '1'
        self.native.documents[url] = ET.tostring(root)
        self.warm()
        self.assertIn('line_in_level', self.provider.capabilities('member'))
        with self.assertRaises(BackendError):
            self.action('AudioIn', 'SetLineInLevel', {'DesiredLeftLineInLevel': '5', 'DesiredRightLineInLevel': '11'})
        self.assertFalse(self.native.writes)
        self.action('AudioIn', 'SetLineInLevel', {'DesiredLeftLineInLevel': '5', 'DesiredRightLineInLevel': '6'})
        self.assertEqual(self.action('AudioIn', 'GetLineInLevel'),
                         {'CurrentLeftLineInLevel': '5', 'CurrentRightLineInLevel': '6'})

    def test_cancel_is_empty_immediate_timer_is_zero_and_countdown_is_cached(self):
        self.warm()
        self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:15:00'})
        self.now += 11
        self.assertEqual(self.action('AVTransport', 'GetRemainingSleepTimerDuration')['RemainingSleepTimerDuration'], '0:14:49')
        self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': ''})
        self.assertEqual(self.native.writes[-1][3]['NewSleepTimerDuration'], '')
        self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:00:00'})
        self.assertEqual(self.native.writes[-1][3]['NewSleepTimerDuration'], '0:00:00')

    def test_success_without_confirmed_effect_faults_and_withdraws_operations(self):
        self.warm(); self.native.ignore_write = 'SetBass'
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetBass', {'DesiredBass': '6'})
        self.assertFalse(self.provider.capabilities('member'))
        self.assertEqual(self.native.states[BASE_A]['Bass'], '1')

    def test_readback_outage_never_publishes_an_optimistic_value_and_recovers(self):
        self.warm(); self.native.fail_getter = 'GetBass'
        with self.assertRaises(BackendError): self.action('DeviceProperties', 'SetLEDState', {'DesiredLEDState': 'Off'})
        self.assertFalse(self.provider.values('member'))
        self.native.fail_getter = None; self.now += settings.RETRY_INTERVAL
        self.warm()
        self.assertEqual(self.action('DeviceProperties', 'GetLEDState'), {'CurrentLEDState': 'Off'})

    def test_endpoint_rebinding_and_read_race_cannot_expose_old_snapshot(self):
        self.warm()
        self.bindings['member'] = self.bindings['leader']
        self.assertFalse(self.provider.values('member'))
        self.warm()
        self.assertEqual(self.provider.values('member')['Bass'], '1')
        self.native.states[BASE_A]['Bass'] = '7'
        self.bindings['member'] = RoomBinding(platform='sonos', native_id=A, native_url=BASE_A)
        self.assertFalse(self.provider.capabilities('member'))
        self.warm()
        self.assertEqual(self.provider.values('member')['Bass'], '7')

    def test_rebinding_during_setter_readback_does_not_publish_success(self):
        self.warm()
        self.native.before_read = lambda: self.bindings.update(member=self.bindings['leader'])
        with self.assertRaises(BackendError): self.action('RenderingControl', 'SetBass', {'DesiredBass': '4'})
        self.assertFalse(self.provider.capabilities('member'))
        self.assertEqual(self.native.states[BASE_A]['Bass'], '4')  # write may have reached the old physical device

    def test_coordinator_change_during_timer_readback_does_not_publish_success(self):
        self.coordinators['member'] = 'leader'; self.warm()
        self.native.before_read = lambda: self.coordinators.update(member='member')
        with self.assertRaises(BackendError):
            self.action('AVTransport', 'ConfigureSleepTimer', {'NewSleepTimerDuration': '0:05:00'})
        self.assertEqual(self.native.states[BASE_B]['RemainingSleepTimerDuration'], '0:05:00')

    def test_native_outage_and_stale_cache_withdraw_settings(self):
        self.warm()
        self.now += settings.STALE_AFTER + 1
        self.assertFalse(self.provider.capabilities('member'))
        self.native.fail_getter = 'GetBass'; self.warm()
        self.assertFalse(self.provider.capabilities('member'))
        self.native.fail_getter = None; self.now += settings.RETRY_INTERVAL; self.warm()
        self.assertIn('bass', self.provider.capabilities('member'))

    def test_cross_origin_scpd_and_nonstandard_control_paths_are_not_followed(self):
        self.native.descriptions[BASE_A] = self.native.descriptions[BASE_A].replace(
            '/xml/RenderingControl1.xml', 'http://other.invalid/RenderingControl.xml')
        self.warm()
        self.assertNotIn('bass', self.provider.capabilities('member'))
        self.assertFalse(any('other.invalid' in url for url in self.native.reads))


if __name__ == '__main__': unittest.main()
