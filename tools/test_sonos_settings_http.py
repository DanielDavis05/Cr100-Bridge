"""Native settings against real HTTP/SOAP fake devices, shared by both backends."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backends import BackendError
from fake_sonos import FakeHousehold
from room_capabilities import RoomBinding
from sonos_settings import SettingsProvider

A, B = 'RINCON_000E58AAAAAA01400', 'RINCON_000E58BBBBBB01400'


class NativeSettingsHTTP(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHousehold()
        self.a = self.fake.add(A, 'Coordinator', settings=True, line_in_connected=False)
        self.b = self.fake.add(B, 'Member', settings=True, line_in_connected=False)
        self.fake.coordinator[B] = A
        self.bindings = {
            'a': RoomBinding(platform='sonos', native_id=A, native_url=self.a.base),
            'b': RoomBinding(platform='sonos', native_id=B, native_url=self.b.base),
        }
        self.changed, self.logs = [], []
        self.provider = SettingsProvider(self.bindings.get, self.changed.append,
                                        coordinator_reader=lambda key: 'a' if key == 'b' else key,
                                        log=self.logs.append)
        self.provider.register('a'); self.provider.register('b')
        self.addCleanup(self.fake.close)
        self.addCleanup(self.provider.close)

    def read(self):
        self.provider.refresh()
        self.assertIn('bass', self.provider.capabilities('a'), self.logs)

    def action(self, room, service, action, **args):
        if service in ('RenderingControl', 'AVTransport'): args.setdefault('InstanceID', '0')
        return self.provider.action(room, service, action, args)

    @staticmethod
    def writes(player):
        return [(action, args) for action, args in player.calls
                if action.startswith(('Set', 'Configure'))]

    def test_physical_settings_and_balance_stay_on_addressed_member(self):
        self.read()
        self.action('b', 'RenderingControl', 'SetBass', DesiredBass='4')
        self.action('b', 'RenderingControl', 'SetTreble', DesiredTreble='-3')
        self.action('b', 'RenderingControl', 'SetLoudness', Channel='Master', DesiredLoudness='1')
        self.action('b', 'RenderingControl', 'SetVolume', Channel='LF', DesiredVolume='35')
        self.action('b', 'RenderingControl', 'SetMute', Channel='RF', DesiredMute='1')
        self.action('b', 'DeviceProperties', 'SetLEDState', DesiredLEDState='Off')
        self.assertEqual((self.b.bass, self.b.treble, self.b.loudness, self.b.led), (4, -3, True, 'Off'))
        self.assertEqual(self.b.channel_volumes, {'LF': 35, 'RF': 100})
        self.assertEqual(self.b.channel_mutes, {'LF': False, 'RF': True})
        self.assertEqual((self.b.volume, self.b.mute), (20, False))
        self.assertEqual(self.writes(self.a), [])
        self.assertEqual(self.action('b', 'RenderingControl', 'GetBass'), {'CurrentBass': '4'})
        self.assertEqual(self.action('b', 'RenderingControl', 'GetVolume', Channel='LF'), {'CurrentVolume': '35'})
        before = list(self.b.calls)
        with patch.object(self.provider, 'reader', side_effect=AssertionError('Getter discovery')), \
                patch.object(self.provider, 'soap', side_effect=AssertionError('Getter SOAP')):
            self.assertEqual(self.action('b', 'DeviceProperties', 'GetLEDState'), {'CurrentLEDState': 'Off'})
            self.provider.capabilities('b'); self.provider.values('b')
        self.assertEqual(self.b.calls, before)

    def test_native_sleep_uses_coordinator_and_distinguishes_zero_from_cancel(self):
        self.read()
        self.action('b', 'AVTransport', 'ConfigureSleepTimer', NewSleepTimerDuration='0:15:00')
        self.assertIsNotNone(self.a.sleep_deadline)
        self.assertIsNone(self.b.sleep_deadline)
        remaining = self.action('b', 'AVTransport', 'GetRemainingSleepTimerDuration')
        self.assertEqual(remaining['CurrentSleepTimerGeneration'], '1')
        self.assertIn(remaining['RemainingSleepTimerDuration'], ('0:15:00', '0:14:59'))
        self.action('b', 'AVTransport', 'ConfigureSleepTimer', NewSleepTimerDuration='0:00:00')
        self.assertIsNotNone(self.a.sleep_deadline)
        self.action('b', 'AVTransport', 'ConfigureSleepTimer', NewSleepTimerDuration='')
        self.assertIsNone(self.a.sleep_deadline)
        self.assertEqual(self.action('b', 'AVTransport', 'GetRemainingSleepTimerDuration'),
                         {'RemainingSleepTimerDuration': '', 'CurrentSleepTimerGeneration': '3'})
        self.assertEqual(self.writes(self.b), [])

    def test_fixed_output_withdraws_eq_and_channel_controls_until_restored(self):
        self.read()
        self.assertIn('output_fixed', self.provider.capabilities('b'))
        self.action('b', 'RenderingControl', 'SetOutputFixed', DesiredFixed='1')
        self.assertTrue(self.b.output_fixed)
        self.assertFalse({'bass', 'treble', 'loudness', 'channel_volume', 'channel_mute'}
                         & self.provider.capabilities('b'))
        before = self.writes(self.b)
        with self.assertRaises(BackendError):
            self.action('b', 'RenderingControl', 'SetBass', DesiredBass='5')
        self.assertEqual(self.writes(self.b), before)
        self.action('b', 'RenderingControl', 'SetOutputFixed', DesiredFixed='0')
        self.assertIn('bass', self.provider.capabilities('b'))

    def test_hardware_support_and_scpd_actions_gate_writes(self):
        self.b.supports_output_fixed = False
        self.b.disabled_settings_actions.add('SetBass')
        self.read()
        self.assertNotIn('output_fixed', self.provider.capabilities('b'))
        self.assertNotIn('bass', self.provider.capabilities('b'))
        before = self.writes(self.b)
        for action, args in (('SetBass', {'DesiredBass': '3'}), ('SetOutputFixed', {'DesiredFixed': '1'})):
            with self.subTest(action=action), self.assertRaises(BackendError):
                self.action('b', 'RenderingControl', action, **args)
        self.assertEqual(self.writes(self.b), before)

    def test_zp80_input_settings_belong_to_source_and_confirm_gain_without_scpd_range(self):
        self.read()
        self.assertIn('line_in_settings', self.provider.capabilities('b'))
        self.action('b', 'AudioIn', 'SetAudioInputAttributes', DesiredName='Vinyl & synth', DesiredIcon='Turntable')
        self.assertEqual((self.b.line_in_name, self.b.line_in_icon), ('Vinyl & synth', 'Turntable'))
        self.assertEqual(self.a.line_in_name, 'Line-In')
        self.assertEqual(self.action('b', 'AudioIn', 'GetLineInLevel'),
                         {'CurrentLeftLineInLevel': '1', 'CurrentRightLineInLevel': '1'})
        self.assertIn('line_in_level', self.provider.capabilities('b'))
        self.action('b', 'AudioIn', 'SetLineInLevel', DesiredLeftLineInLevel='12', DesiredRightLineInLevel='9')
        self.assertEqual(self.action('b', 'AudioIn', 'GetLineInLevel'),
                         {'CurrentLeftLineInLevel': '12', 'CurrentRightLineInLevel': '9'})
        self.assertEqual(self.a.line_in_levels, (1, 1))

    def test_native_write_fault_withdraws_settings_without_optimistic_readback(self):
        self.read()
        self.b.reject['SetTreble'] = 501
        with self.assertRaises(BackendError):
            self.action('b', 'RenderingControl', 'SetTreble', DesiredTreble='7')
        self.assertEqual(self.b.treble, 0)
        # The coordinator's native sleep capability remains independently usable.
        self.assertNotIn('treble', self.provider.capabilities('b'))
        self.assertEqual(self.provider.values('b').get('Treble'), None)
        self.assertIn('sleep_timer', self.provider.capabilities('b'))


if __name__ == '__main__': unittest.main()
