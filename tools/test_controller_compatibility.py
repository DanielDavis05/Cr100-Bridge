"""Controllers on any firmware accept bridge discovery without changing its 7.1 persona."""
import copy
import itertools
import re
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import zoneplayers

zp = zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)
NS = {'d': 'urn:schemas-upnp-org:device-1-0'}
FIRMWARE_43 = '24.1-75101'
OLDEST = '1.0-00001'
FLOOR = '1.0-00000'


def version(value):
    return tuple(int(part) for part in re.split(r'[.-]', value))


class ControllerCompatibility(unittest.TestCase):
    def setUp(self):
        original = zp.PROFILE
        self.addCleanup(setattr, zp, 'PROFILE', original)
        self.addCleanup(setattr, zp.PeerHandler, 'server_version', zp.PeerHandler.server_version)
        zp.PROFILE = dict(zp.PROFILES['7.1'])

    def household(self):
        # Rendering the real descriptions/topology needs no listener threads or
        # network traffic; replace only construction of the unused HTTP servers.
        ports = itertools.count(1400)
        class UnstartedHTTPServer:
            def __init__(self, address, handler): self.server_port = next(ports)
            def server_close(self): pass
        with patch.object(zp, 'PeerHTTPServer', UnstartedHTTPServer):
            house = zoneplayers.Household(zp, 'Sonos_compatibility', ['Living Room'], ['127.0.0.1'],
                lambda _: None, port=0, build={'software': 'test-build', 'display': 'test'},
                bridge_ip='192.0.2.1')
        self.addCleanup(house.close)
        return house

    def description(self, player):
        return ET.fromstring(player.state.description()).find('d:device', NS)

    def test_open_floor_is_consistent_in_room_and_about_discovery(self):
        zoneplayers.configure_profile(zp)
        house = self.household()
        self.assertEqual(zoneplayers.MIN_CONTROLLER_VERSION, FLOOR)
        self.assertEqual(len(house.devices()), 2)
        self.assertIn(zoneplayers.BRIDGE_KEY, house.about_players)
        for player in house.devices():
            description = self.description(player)
            for tag in ('minCompatibleVersion', 'legacyCompatibleVersion'):
                advertised = description.findtext('d:' + tag, namespaces=NS)
                self.assertEqual(advertised, FLOOR)
                self.assertGreaterEqual(version(OLDEST), version(advertised))
            self.assertEqual(description.findtext('d:softwareVersion', namespaces=NS), '34.16-37101')
        members = ET.fromstring(house.topology()).findall('.//ZoneGroupMember')
        self.assertEqual(len(members), len(house.devices()))
        for member in members:
            self.assertEqual(member.get('SoftwareVersion'), '34.16-37101')
            for field in ('MinCompatibleVersion', 'LegacyCompatibleVersion'):
                self.assertEqual(member.get(field), FLOOR)
                self.assertGreaterEqual(version(OLDEST), version(member.get(field)))

    def test_lowering_floor_preserves_captured_profiles_and_existing_persona(self):
        captured = copy.deepcopy(zp.PROFILES)
        house = self.household()
        before = {player.config.udn: self.description(player) for player in house.devices()}
        discovery = {player.config.udn: player.ssdp_packet(zp.ZONE_PLAYER) for player in house.devices()}
        zoneplayers.configure_profile(zp)
        self.assertEqual(zp.PROFILES, captured, 'Captured physical profiles must remain unchanged')
        self.assertIsNot(zp.PROFILE, zp.PROFILES['7.1'])
        for key in ('sw', 'display', 'hw', 'server'):
            self.assertEqual(zp.PROFILE[key], captured['7.1'][key])
        for player in house.devices():
            after = self.description(player)
            for tag in ('feature1', 'feature2', 'feature3'):
                previous = before[player.config.udn].findtext('d:' + tag, namespaces=NS)
                self.assertIsNotNone(previous)
                self.assertEqual(after.findtext('d:' + tag, namespaces=NS), previous)
            self.assertEqual(player.ssdp_packet(zp.ZONE_PLAYER), discovery[player.config.udn])
            self.assertIn(captured['7.1']['server'].encode(), discovery[player.config.udn])


if __name__ == '__main__': unittest.main()
