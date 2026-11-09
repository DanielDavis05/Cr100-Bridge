"""A source outage must leave a discoverable, read-only diagnostic zone."""
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import zoneplayers
from test_backends import MemoryBackend, MemoryRoom
from test_zoneplayers import HOUSEHOLD, groups, zp


class ReachableRoom(MemoryRoom):
    online = True

    @property
    def available(self):
        return self.online


class ReachableBackend(MemoryBackend):
    def room(self, key):
        if key not in self.rooms:
            self.rooms[key] = ReachableRoom(self, key)
        return self.rooms[key]


class BridgeFallback(unittest.TestCase):
    def open_house(self, config=None):
        backend = ReachableBackend('memory')
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall'], ['127.0.0.1'] * 2,
            log=lambda _: None, port=0, backends=[backend], refs=[('memory', 'den'), ('memory', 'hall')],
            controller_config=config, controller_resolver=lambda ip: {'192.0.2.1': '00:00:00:00:00:01',
                '192.0.2.2': '00:00:00:00:00:02'}.get(ip), listen_ip='127.0.0.1')
        self.addCleanup(house.close)
        return house, backend, house.about_players[zoneplayers.BRIDGE_KEY]

    def test_outage_and_recovery_update_one_stable_bridge_endpoint_and_generation(self):
        house, backend, bridge = self.open_house()
        endpoint = (bridge.config.udn, bridge.state.location)
        self.assertEqual(groups(house.topology()), [['Den'], ['Hall']])
        first_generation, first_boot = house.discovery_generation, bridge.config.boot_sequence
        for room in backend.rooms.values(): room.online = False
        self.assertTrue(house.refresh_identities())
        self.assertIn(['CR100 Bridge'], groups(house.topology()))
        self.assertGreater(house.discovery_generation, first_generation)
        self.assertGreater(bridge.config.boot_sequence, first_boot)
        self.assertEqual((bridge.config.udn, bridge.state.location), endpoint)
        self.assertFalse(house.refresh_identities())
        offline_boot = bridge.config.boot_sequence
        backend.rooms['hall'].online = True
        house.refresh_identities()
        self.assertNotIn(['CR100 Bridge'], groups(house.topology()))
        self.assertGreater(bridge.config.boot_sequence, offline_boot)
        self.assertEqual((bridge.config.udn, bridge.state.location), endpoint)

    def test_fallback_uses_each_controllers_reachable_selection_and_respects_hidden_bridge(self):
        config = {'controllers': {'00:00:00:00:00:01': {'visible_components': ['memory:den', 'cr100-bridge']},
                                  '00:00:00:00:00:02': {'visible_components': ['memory:hall']}}}
        house, backend, bridge = self.open_house(config)
        backend.rooms['den'].online = False
        house.refresh_identities()
        self.assertIn(['CR100 Bridge'], groups(house.topology('192.0.2.1')))
        self.assertNotIn(['CR100 Bridge'], groups(house.topology('192.0.2.2')))
        with bridge.request_context('192.0.2.1'):
            description = bridge.state.description()
            self.assertEqual(bridge.state.event_values('DeviceProperties')['IsZoneBridge'], '0')
        self.assertIn(b'<modelNumber>ZP80</modelNumber>', description)
        self.assertIn(b'<modelNumber>ZoneBridge</modelNumber>', bridge.state.description())
        config['controllers']['00:00:00:00:00:01']['visible_components'] = []
        self.assertEqual(house.devices('192.0.2.1'), [])
        self.assertEqual(groups(house.topology('192.0.2.1')), [])

    def test_no_room_runtime_reports_bridge_component_without_changing_saved_selection(self):
        house = zoneplayers.Household(zp, HOUSEHOLD, [], [], lambda _: None, port=0,
                                      listen_ip='127.0.0.1')
        self.addCleanup(house.close)
        bridge = house.about_players[zoneplayers.BRIDGE_KEY]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            zoneplayers.publish_player_status(path, house, [])
            status = json.loads(path.read_text())
        self.assertEqual(status['rooms'], [])
        self.assertEqual(status['components'], [{'key': 'cr100-bridge', 'name': 'CR100 Bridge',
                                                'kind': 'bridge', 'parent_keys': []}])
        query = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
                 'MX: 1\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n').encode()
        self.assertTrue(bridge.search_responses(query))
        member = ET.fromstring(house.topology()).find('.//ZoneGroupMember')
        self.assertEqual((member.get('Invisible'), member.get('IsZoneBridge')), ('0', '0'))


if __name__ == '__main__':
    unittest.main()
