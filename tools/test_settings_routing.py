"""Real CR100 SOAP routing for cached native settings and channel readback."""
import copy
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from backends import BackendError
import test_capability_routing as routing
from test_capability_routing import AVT, RC, DP, INSTANCE
from test_ha_backend import field
from test_zoneplayers import soap


class SettingsRouting(unittest.TestCase):
    snapshot = routing.CapabilityRouting.snapshot
    rejected = routing.CapabilityRouting.rejected

    def setUp(self):
        routing.CapabilityRouting.setUp(self)
        self.room.operations.update(('bass', 'treble', 'loudness', 'led', 'output_fixed',
                                     'channel_volume', 'channel_mute', 'sleep_timer',
                                     'line_in_settings'))
        self.native = Mock(return_value={})
        self.room.settings_action = self.native

    def test_cached_bass_and_fixed_output_readback_replaces_peer_defaults(self):
        self.room.position = Mock(side_effect=AssertionError('Settings GET triggered playback network path'))
        for action, expected, value in (
                ('GetBass', 'CurrentBass', '-6'),
                ('GetOutputFixed', 'CurrentFixed', '1'),
                ('GetSupportsOutputFixed', 'CurrentSupportsFixed', '1')):
            self.native.return_value = {expected: value}
            status, body = soap(self.den, RC, 'RenderingControl', action, INSTANCE)
            self.assertEqual(status, 200, body)
            self.assertEqual(field(body, expected), value)

    def test_channel_getter_is_not_overwritten_by_master_volume(self):
        self.native.return_value = {'CurrentVolume': '83'}
        status, body = soap(self.den, RC, 'RenderingControl', 'GetVolume', INSTANCE + '<Channel>LF</Channel>')
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'CurrentVolume'), '83')
        self.assertEqual(self.room.volume, 20)

    def test_channel_write_bypasses_master_hold_and_simulated_state(self):
        before = self.snapshot()
        status, body = soap(self.den, RC, 'RenderingControl', 'SetVolume',
                            INSTANCE + '<Channel>RF</Channel><DesiredVolume>73</DesiredVolume>')
        self.assertEqual(status, 200, body)
        self.native.assert_called_once_with('RenderingControl', 'SetVolume',
            {'InstanceID': '0', 'Channel': 'RF', 'DesiredVolume': '73'})
        self.assertEqual(self.room.held, {})
        self.assertEqual(self.snapshot(), before)

    def test_native_setting_failure_does_not_stage_a_peer_change(self):
        before = self.snapshot()
        self.native.side_effect = BackendError('Native readback failed')
        self.rejected(self.den, RC, 'RenderingControl', 'SetBass', INSTANCE + '<DesiredBass>7</DesiredBass>')
        self.assertEqual(self.snapshot(), before)

    def test_missing_capability_rejects_before_native_call(self):
        self.room.operations.discard('bass')
        self.rejected(self.den, RC, 'RenderingControl', 'SetBass', INSTANCE + '<DesiredBass>7</DesiredBass>')
        self.native.assert_not_called()

    def test_timer_and_input_settings_reach_the_settings_route(self):
        for path, service, action, args in (
                (AVT, 'AVTransport', 'ConfigureSleepTimer', INSTANCE + '<NewSleepTimerDuration></NewSleepTimerDuration>'),
                ('/AudioIn/Control', 'AudioIn', 'SetAudioInputAttributes',
                 '<DesiredName>Turntable</DesiredName><DesiredIcon>AudioComponent</DesiredIcon>'),
                (DP, 'DeviceProperties', 'SetLEDState', '<DesiredLEDState>Off</DesiredLEDState>')):
            status, body = soap(self.den, path, service, action, args)
            self.assertEqual(status, 200, body)
            self.assertEqual(self.native.call_args.args[:2], (service, action))
        self.assertEqual(self.den.state.led, 'On')

    def test_rendering_events_keep_balance_channels_separate_from_master(self):
        values = {'Bass': '-4', 'Treble': '2', 'Loudness': '0', 'OutputFixed': '1',
                  'LoudnessChannels': {'Master': '0'},
                  'VolumeChannels': {'LF': '78', 'RF': '100'},
                  'MuteChannels': {'LF': '1', 'RF': '0'}}
        self.room.settings_values = lambda: copy.deepcopy(values)
        event = ET.fromstring(self.den.state.last_change(rendering=True))
        variables = next(iter(event))
        volumes = {node.get('channel'): node.get('val') for node in variables if node.tag.endswith('Volume')}
        mutes = {node.get('channel'): node.get('val') for node in variables if node.tag.endswith('Mute')}
        self.assertEqual(volumes, {'Master': '20', 'LF': '78', 'RF': '100'})
        self.assertEqual(mutes, {'Master': '0', 'LF': '1', 'RF': '0'})
        self.assertEqual(next(node.get('val') for node in variables if node.tag.endswith('Bass')), '-4')
        self.assertEqual([node.get('channel') for node in variables if node.tag.endswith('Loudness')], ['Master'])

    def test_transport_event_exposes_native_sleep_generation(self):
        self.room.settings_values = lambda: {'CurrentSleepTimerGeneration': '14'}
        event = ET.fromstring(self.den.state.last_change())
        generation = next(node for node in next(iter(event)) if node.tag.endswith('SleepTimerGeneration'))
        self.assertEqual(generation.get('val'), '14')


if __name__ == '__main__': unittest.main()
