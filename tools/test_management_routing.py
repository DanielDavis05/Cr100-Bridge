"""System settings readback, controller visibility and capability advertisements."""
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from room_capabilities import RoomBinding
import test_capability_routing as routing
from test_capability_routing import DP
from test_ha_backend import field
from test_zoneplayers import soap

CR = '00:0e:58:01:02:03'


class ManagementRouting(unittest.TestCase):
    setUp = routing.CapabilityRouting.setUp
    rejected = routing.CapabilityRouting.rejected
    snapshot = routing.CapabilityRouting.snapshot

    def limit_to_den(self):
        self.house.controller_config = {'controllers': {CR: {
            'visible_components': [self.den.component_key]}}}
        self.house.controller_resolver = lambda _: CR

    def test_room_icon_agrees_in_topology_attributes_and_initial_event(self):
        self.den.icon = 'bedroom'
        status, body = soap(self.den, DP, 'DeviceProperties', 'GetZoneAttributes', '')
        self.assertEqual(status, 200, body)
        expected = 'x-rincon-roomicon:bedroom'
        self.assertEqual(field(body, 'CurrentIcon'), expected)
        self.assertEqual(self.den.state.event_values('DeviceProperties')['Icon'], expected)
        topology = ET.fromstring(self.house.topology())
        self.assertEqual(next(member.get('Icon') for member in topology.iter('ZoneGroupMember')
                              if member.get('UUID') == self.den.config.udn), expected)
        self.den.icon = 'office'
        self.assertEqual(self.den.state.event_values('DeviceProperties')['Icon'], 'x-rincon-roomicon:office')

    def test_native_autoplay_volume_is_not_master_volume_and_does_not_read_playback(self):
        self.room.settings_action = Mock(return_value={'CurrentVolume': '73'})
        self.room.position = Mock(side_effect=AssertionError('Device settings fetched playback position'))
        status, body = soap(self.den, DP, 'DeviceProperties', 'GetAutoplayVolume', '')
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'CurrentVolume'), '73')
        self.assertEqual(self.room.volume, 20)
        self.room.settings_action.assert_called_once_with('DeviceProperties', 'GetAutoplayVolume', {})

    def test_autoplay_room_is_virtualized_and_hidden_or_unknown_destination_is_empty(self):
        physical = 'RINCON_000E58BBBBBB01400'
        self.backend.binding = lambda key: RoomBinding(platform='sonos', native_id=physical if key == 'hall' else '')
        native = Mock(return_value={'RoomUUID': physical})
        self.room.settings_action = native
        status, body = soap(self.den, DP, 'DeviceProperties', 'GetAutoplayRoomUUID', '')
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'RoomUUID'), self.hall.config.udn)
        self.limit_to_den()
        status, body = soap(self.den, DP, 'DeviceProperties', 'GetAutoplayRoomUUID', '')
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(ET.fromstring(body).find('.//RoomUUID'))
        self.assertEqual(field(body, 'RoomUUID') or '', '')
        for target in ('RINCON_UNKNOWN01400', ''):
            native.return_value = {'RoomUUID': target}
            status, body = soap(self.den, DP, 'DeviceProperties', 'GetAutoplayRoomUUID', '')
            self.assertEqual(status, 200, body)
            self.assertIsNotNone(ET.fromstring(body).find('.//RoomUUID'))
            self.assertEqual(field(body, 'RoomUUID') or '', '')

    def test_group_volume_changeability_follows_visible_members_capabilities(self):
        self.house.coordinator[self.hall.config.udn] = self.den.config.udn
        self.assertEqual(self.den.state.event_values('GroupRenderingControl')['GroupVolumeChangeable'], '1')
        self.hall.state.backend.operations.discard('volume')
        self.assertEqual(self.den.state.event_values('GroupRenderingControl')['GroupVolumeChangeable'], '0')
        self.limit_to_den()
        with self.den.request_context('127.0.0.1'):
            self.assertEqual(self.den.state.event_values('GroupRenderingControl')['GroupVolumeChangeable'], '1')
        self.room.operations.discard('volume')
        with self.den.request_context('127.0.0.1'):
            self.assertEqual(self.den.state.event_values('GroupRenderingControl')['GroupVolumeChangeable'], '0')

    def test_unsupported_room_management_mutators_never_report_success_or_change_state(self):
        before = self.snapshot()
        for action in ('SetZoneAttributes', 'SetAutoplayVolume', 'SetUseAutoplayVolume',
                       'SetAutoplayRoomUUID', 'SetAutoplayLinkedZones', 'CreateStereoPair', 'AddBondedZones'):
            args = ''.join('<%s>%s</%s>' % (name, '0' if kind in ('ui4', 'ui2', 'boolean') else 'unchanged', name)
                           for name, direction, kind in self.den.state.actions['DeviceProperties'][action]
                           if direction == 'in')
            with self.subTest(action=action):
                self.rejected(self.den, DP, 'DeviceProperties', action, args)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.backend.calls, [])


if __name__ == '__main__': unittest.main()
