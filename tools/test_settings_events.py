"""Native setting events schedule readback and do not wait for the 30-second poll."""
import unittest
from unittest.mock import Mock

from fake_sonos import FakeHousehold
from sonos_backend import SonosBackend, Speaker
from test_ha_backend import wait_for
from test_sonos_settings import Native, A, BASE_A
from room_capabilities import RoomBinding
from sonos_settings import SettingsProvider

UUID = 'RINCON_000E58AAAAAA01400'


def rendering(variables):
    return {'LastChange': '<Event><InstanceID val="0">' + variables + '</InstanceID></Event>'}


class SettingEventRouting(unittest.TestCase):
    def setUp(self):
        self.backend = SonosBackend(settings=Mock(), listener=Mock())
        self.addCleanup(self.backend.close)
        self.speaker = Speaker(UUID, 'http://10.0.0.10:1400', 'Room')
        self.backend.speakers[UUID] = self.speaker
        self.backend.settings.values.return_value = {
            'Bass': '0', 'Treble': '0', 'Loudness': '1', 'OutputFixed': '0',
            'VolumeChannels': {'LF': '100', 'RF': '100'},
            'MuteChannels': {'LF': '0', 'RF': '0'},
            'AudioInputName': 'Turntable', 'Icon': 'AudioComponent',
            'LeftLineInLevel': '3', 'RightLineInLevel': '3'}

    def test_eq_only_and_balance_only_events_request_readback(self):
        for variable in ('<Bass val="4"/>', '<Treble val="-2"/>',
                         '<Loudness channel="Master" val="0"/>',
                         '<Volume channel="LF" val="70"/>', '<Mute channel="RF" val="1"/>'):
            with self.subTest(variable=variable):
                self.backend.settings.request_refresh.reset_mock()
                self.backend.on_rendering(UUID, rendering(variable))
                self.backend.settings.request_refresh.assert_called_once_with(UUID, output_fixed=None)

    def test_fixed_output_event_blocks_controls_until_confirmed(self):
        self.backend.on_rendering(UUID, rendering('<OutputFixed val="1"/>'))
        self.backend.settings.request_refresh.assert_called_once_with(UUID, output_fixed='1')

    def test_master_volume_and_unchanged_settings_do_not_trigger_full_settings_reads(self):
        self.backend.on_rendering(UUID, rendering('<Volume channel="Master" val="35"/>'
            '<Bass val="0"/><Volume channel="LF" val="100"/><Mute channel="RF" val="0"/>'))
        self.assertEqual(self.speaker.volume, 35)
        self.backend.settings.request_refresh.assert_not_called()
        self.backend.on_rendering(UUID, {})

    def test_malformed_rendering_event_preserves_cache_and_next_event_still_applies(self):
        before = self.speaker.volume
        self.backend.on_rendering(UUID, {'LastChange': '<Event><InstanceID val="0"><Bass val="4"/>'})
        self.assertEqual(self.speaker.volume, before)
        self.backend.settings.request_refresh.assert_not_called()
        self.backend.on_rendering(UUID, rendering('<Volume channel="Master" val="42"/>'))
        self.assertEqual(self.speaker.volume, 42)

    def test_level_only_and_name_events_refresh_settings_cache(self):
        for values in ({'LeftLineInLevel': '5'}, {'AudioInputName': 'New turntable'}):
            self.backend.settings.request_refresh.reset_mock()
            self.backend.on_audio_in(UUID, values)
            self.backend.settings.request_refresh.assert_called_once_with(UUID)


class SettingEventReadback(unittest.TestCase):
    def test_event_during_readback_is_not_lost_to_next_poll_deadline(self):
        native = Native()
        provider = SettingsProvider(lambda _: RoomBinding(platform='sonos', native_id=A, native_url=BASE_A),
                                    lambda _: None, log=lambda _: None,
                                    reader=native.reader, soap=native.soap, clock=lambda: 100)
        self.addCleanup(provider.close)
        provider.register('room'); provider.refresh()
        provider.request_refresh('room')
        native.before_read = lambda: provider.request_refresh('room', output_fixed='1')
        provider.refresh()
        self.assertTrue(provider.volume_blocked('room'))
        before = len(native.calls)
        provider.refresh()
        self.assertGreater(len(native.calls), before)
        self.assertFalse(provider.volume_blocked('room'))
        before = len(native.calls)
        provider.refresh()
        self.assertEqual(len(native.calls), before)

    def test_event_schedules_cached_readback_and_preserves_fixed_guard(self):
        native, notices = Native(), []
        provider = SettingsProvider(lambda _: RoomBinding(platform='sonos', native_id=A, native_url=BASE_A),
                                    notices.append, log=lambda _: None,
                                    reader=native.reader, soap=native.soap, clock=lambda: 100)
        self.addCleanup(provider.close)
        provider.register('room'); provider.refresh()
        native.states[BASE_A]['Bass'] = '6'
        native.states[BASE_A]['OutputFixed'] = '1'
        before = len(native.calls)
        provider.request_refresh('room', output_fixed='1')
        self.assertEqual(len(native.calls), before)
        self.assertTrue(provider.volume_blocked('room'))
        self.assertNotIn('bass', provider.capabilities('room'))
        provider.refresh()
        self.assertEqual(provider.values('room')['Bass'], '6')
        self.assertTrue(provider.volume_blocked('room'))
        native.states[BASE_A]['OutputFixed'] = '0'
        provider.request_refresh('room', output_fixed='0')
        self.assertTrue(provider.volume_blocked('room'))
        provider.refresh()
        self.assertFalse(provider.volume_blocked('room'))

    def test_real_sonos_event_updates_native_settings_before_periodic_poll(self):
        fake = FakeHousehold()
        self.addCleanup(fake.close)
        physical = fake.add(UUID, 'Room', settings=True)
        backend = SonosBackend([physical.base], log=lambda _: None)
        self.addCleanup(backend.close)
        room = backend.room(UUID)
        backend.start()
        self.assertTrue(backend.ready.wait(5))
        self.assertTrue(wait_for(lambda: 'bass' in room.operation_capabilities()))
        physical.bass, physical.channel_volumes['LF'] = 6, 65
        physical.notify('RenderingControl')
        self.assertTrue(wait_for(lambda: backend.settings.values(UUID).get('Bass') == '6'))
        self.assertEqual(backend.settings.values(UUID)['VolumeChannels']['LF'], '65')


if __name__ == '__main__': unittest.main()
