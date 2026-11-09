"""Legacy input settings read verified Autoplay fields independently of volume."""
import unittest
from unittest.mock import patch

from backends import BackendError
import test_sonos_settings as fixtures
import test_sonos_settings_http as http_fixtures

BASE_A, BASE_B = fixtures.BASE_A, fixtures.BASE_B


class CachedAutoplay(unittest.TestCase):
    setUp = fixtures.Settings.setUp
    warm = fixtures.Settings.warm
    action = fixtures.Settings.action

    def test_cached_reads_preserve_independent_values_and_do_not_enable_writes(self):
        self.native.states[BASE_A].update(AutoplayVolume='53', AutoplayUseVolume='0',
            AutoplayIncludeLinkedZones='1', AutoplayRoomUUID='RINCON_B01400')
        self.warm()
        before = (len(self.native.reads), len(self.native.calls))
        for action, expected in (
                ('GetAutoplayVolume', {'CurrentVolume': '53'}),
                ('GetUseAutoplayVolume', {'UseVolume': '0'}),
                ('GetAutoplayLinkedZones', {'IncludeLinkedZones': '1'}),
                ('GetAutoplayRoomUUID', {'RoomUUID': 'RINCON_B01400'})):
            self.assertEqual(self.action('DeviceProperties', action), expected)
        self.assertEqual((len(self.native.reads), len(self.native.calls)), before)
        self.assertNotIn('line_in_autoplay_read', self.provider.capabilities('member'))
        self.assertIsNone(self.action('DeviceProperties', 'SetAutoplayVolume', {'Volume': '90'}))
        self.assertEqual(self.native.writes, [])

    def test_optional_missing_or_invalid_reads_do_not_withdraw_eq(self):
        self.native.remove_action(BASE_A, 'DeviceProperties', 'GetAutoplayVolume')
        self.native.states[BASE_A].update(AutoplayUseVolume='maybe', AutoplayRoomUUID='bad\x00uuid')
        self.warm()
        self.assertIn('bass', self.provider.capabilities('member'))
        for action in ('GetAutoplayVolume', 'GetUseAutoplayVolume', 'GetAutoplayRoomUUID'):
            with self.subTest(action=action), self.assertRaises(BackendError):
                self.action('DeviceProperties', action)
        self.assertEqual(self.action('DeviceProperties', 'GetAutoplayLinkedZones'), {'IncludeLinkedZones': '0'})

    def test_autoplay_reads_follow_physical_room_while_sleep_follows_coordinator(self):
        self.provider.register('leader')
        self.coordinators['member'] = 'leader'
        self.native.states[BASE_A]['AutoplayVolume'] = '53'
        self.native.states[BASE_B]['AutoplayVolume'] = '65'
        self.warm()
        self.assertEqual(self.action('DeviceProperties', 'GetAutoplayVolume'), {'CurrentVolume': '53'})


class NativeHTTPAutoplay(unittest.TestCase):
    setUp = http_fixtures.NativeSettingsHTTP.setUp
    read = http_fixtures.NativeSettingsHTTP.read
    action = http_fixtures.NativeSettingsHTTP.action

    def test_real_http_reads_native_autoplay_volume_and_keeps_getters_cached(self):
        self.b.do_GetAutoplayVolume = lambda args: {'CurrentVolume': '57'}
        self.b.do_GetUseAutoplayVolume = lambda args: {'UseVolume': '1'}
        self.b.do_GetAutoplayLinkedZones = lambda args: {'IncludeLinkedZones': '0'}
        self.b.do_GetAutoplayRoomUUID = lambda args: {'RoomUUID': ''}
        self.read()
        before = list(self.b.calls)
        with patch.object(self.provider, 'soap', side_effect=AssertionError('Getter did network I/O')):
            self.assertEqual(self.action('b', 'DeviceProperties', 'GetAutoplayVolume'), {'CurrentVolume': '57'})
            self.assertEqual(self.action('b', 'DeviceProperties', 'GetUseAutoplayVolume'), {'UseVolume': '1'})
        self.assertEqual(self.b.calls, before)
        self.assertEqual(self.b.volume, 20)


if __name__ == '__main__': unittest.main()
