"""Area overrides and live-process device status, with loopback HA only."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import backends
import bridge_config
import fake_ha
from ha_client import HomeAssistant
from ha_hub import HAHub
import webui
import zoneplayers


class Overrides(unittest.TestCase):
    def test_speaker_form_save_preserves_explicit_area_and_empty_override(self):
        config = {'rooms': [{'entity': 'media_player.lantern', 'devices_area': ''},
                            {'entity': 'media_player.workshop', 'devices_area': 'landing'}]}
        listing = {'media_player.lantern': 'Lantern', 'media_player.workshop': 'Workshop'}
        bridge_config.apply_rooms(config, [{'key': 'media_player.lantern', 'name': 'New Lantern'},
            {'key': 'media_player.workshop'}], None, 'ha', listing, {})
        self.assertEqual([room['devices_area'] for room in config['rooms']], ['', 'landing'])
        self.assertEqual(config['rooms'][0]['name'], 'New Lantern')

    def test_hidden_and_rediscovered_sonos_room_restores_area_preference(self):
        config = {'backend': 'sonos', 'rooms': [{'backend': 'sonos', 'id': 'RINCON_TEST',
                   'last_known_name': 'Lantern', 'devices_area': ''}],
                  'sonos': {'hidden': []}}
        bridge_config.apply_rooms(config, [], None, 'sonos', {'sonos:RINCON_TEST': 'Lantern'}, {})
        self.assertEqual(config['rooms'], [])
        bridge_config.apply_rooms(config, [{'key': 'sonos:RINCON_TEST'}], None, 'sonos',
                                  {'sonos:RINCON_TEST': 'Lantern'}, {})
        self.assertEqual(config['rooms'][0]['devices_area'], '')

    def test_network_reconciliation_preserves_area_through_absence_and_return(self):
        config = {'backend': 'sonos', 'rooms': [
            {'backend': 'sonos', 'id': 'RINCON_ONE', 'last_known_name': 'Lantern', 'devices_area': ''},
            {'backend': 'sonos', 'id': 'RINCON_TWO', 'last_known_name': 'Workshop', 'devices_area': 'landing'}],
            'sonos': {'hidden': []}}
        listing = {'sonos:RINCON_ONE': 'Lantern', 'sonos:RINCON_TWO': 'Workshop'}
        bridge_config.reconcile_sonos_rooms(config, listing)
        self.assertEqual([room['devices_area'] for room in config['rooms']], ['', 'landing'])
        bridge_config.reconcile_sonos_rooms(config, {'sonos:RINCON_ONE': 'Lantern'})
        bridge_config.reconcile_sonos_rooms(config, listing)
        self.assertEqual([room['devices_area'] for room in config['rooms']], ['', 'landing'])


class PublishedStatus(unittest.TestCase):
    def test_actual_hub_summary_has_counts_resolved_areas_and_connection_state(self):
        class FixtureRoom(backends.Room):
            def group_members(self): return getattr(self.backend, 'members', None) or [self.key]
        class FixtureBackend(backends.Backend):
            name = 'ha'
            def room(self, key): return FixtureRoom(self, key)
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'status.json'
        fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture());self.addCleanup(fake.close)
        hub = HAHub(HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None), lambda _: None)
        self.addCleanup(hub.close);hub.start();self.assertTrue(hub.ready.wait(5))
        zp = zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)
        house = zoneplayers.Household(zp, 'Sonos_synthetic_status', ['Lantern', 'Workshop'], ['127.0.0.1'] * 2,
            lambda _: None, port=0, backends=[FixtureBackend(lambda _: None)],
            refs=[('ha', 'media_player.lantern'), ('ha', 'media_player.workshop')], devices_hub=hub,
            devices_config={'rooms': [{'entity': 'media_player.lantern', 'devices_area': ''},
                                       {'entity': 'media_player.workshop'}]})
        self.addCleanup(house.close)
        zoneplayers.publish_player_status(path, house, [0, 1])
        data = json.loads(path.read_text());status = data['smart_devices']
        self.assertTrue(status['connected']);self.assertEqual(status['areas'], 3)
        self.assertGreater(status['devices'], 5)
        self.assertEqual(status['rooms'], [{'room': 'Lantern', 'area': None}, {'room': 'Workshop', 'area': 'Workshop'}])
        self.assertEqual(fake.service_requests, [])
        # Names need not be unique. A grouped status must retain each stable
        # backend key's own area, including the explicit blank first override.
        for player in house.players: player.config.room = 'Shared Name'
        house.backends['ha'].members = ['media_player.lantern', 'media_player.workshop']
        house.sync_groups()
        self.assertEqual(len(house.members(house.players[0].config.udn)), 2)
        zoneplayers.publish_player_status(path, house, [0, 1])
        self.assertEqual(json.loads(path.read_text())['smart_devices']['rooms'],
                         [{'room': 'Shared Name', 'area': None}, {'room': 'Shared Name', 'area': 'Workshop'}])
        self.assertEqual(fake.service_requests, [])
        fake.set_available(False)
        deadline = time.monotonic() + 5
        while hub.client.connected.is_set() and time.monotonic() < deadline: time.sleep(.01)
        zoneplayers.publish_player_status(path, house, [0, 1])
        self.assertFalse(json.loads(path.read_text())['smart_devices']['connected'])
        self.assertEqual(fake.service_requests, [])

    def test_status_revision_tracks_device_refresh_and_missing_hub(self):
        house = SimpleNamespace(discovery_generation=2, smart_devices=SimpleNamespace(revision=3))
        self.assertEqual(zoneplayers.player_status_revision(house), (2, 3))
        house.smart_devices.revision = 4
        self.assertEqual(zoneplayers.player_status_revision(house), (2, 4))
        house.smart_devices = None
        self.assertEqual(zoneplayers.player_status_revision(house), (2, None))


    def test_publisher_without_a_hub_keeps_legacy_payload_shape(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'status.json'
        zp = zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)
        house = zoneplayers.Household(zp, 'Sonos_synthetic_no_ha', ['Synthetic Room'], ['127.0.0.1'],
                                     lambda _: None, port=0)
        self.addCleanup(house.close)
        zoneplayers.publish_player_status(path, house, [0])
        payload = json.loads(path.read_text())
        self.assertEqual(set(payload), {'pid', 'rooms', 'components'})
        self.assertNotIn('smart_devices', payload)


class WebStatus(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config, state = self.root / 'config.json', self.root / 'state.json'
        config.write_text(json.dumps({'backend': 'sonos', 'rooms': []}));state.write_text('{}')
        self.bridge = webui.Bridge(config, state, self.root, self.root / 'players.pid', None, log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.summary = {'configured': True, 'connected': True, 'enabled': True, 'areas': 3, 'devices': 13,
                        'rooms': [{'room': 'Lantern', 'area': 'Lantern'}]}


    def test_source_switch_roundtrip_preserves_empty_and_explicit_area_overrides(self):
        ha_rooms = [{'entity': 'media_player.lantern', 'devices_area': ''},
                    {'entity': 'media_player.workshop', 'devices_area': 'landing'}]
        self.bridge.config_path.write_text(json.dumps({'backend': 'ha', 'rooms': ha_rooms}))
        listings = {'ha': {'media_player.lantern': 'Lantern', 'media_player.workshop': 'Workshop'},
                    'sonos': {'sonos:RINCON_ONE': 'Lantern', 'sonos:RINCON_TWO': 'Workshop'}}
        self.bridge.listing = Mock(side_effect=lambda source: listings[source])
        self.bridge.platforms = Mock(return_value={entity: 'sonos' for entity in listings['ha']})
        self.bridge.connect_ha = Mock()
        self.bridge.reload_players = Mock(return_value={'applied': True})
        self.bridge.switch_source('sonos')
        def set_overrides(config):
            config['rooms'][0]['devices_area'] = ''
            config['rooms'][1]['devices_area'] = 'workshop'
        self.bridge.update_config(set_overrides)
        self.bridge.switch_source('ha')
        self.assertEqual([room['devices_area'] for room in self.bridge.config()['rooms']], ['', 'landing'])
        self.bridge.switch_source('sonos')
        self.assertEqual([room['devices_area'] for room in self.bridge.config()['rooms']], ['', 'workshop'])
        self.bridge.connect_ha.assert_called_once()

    def test_only_current_player_pid_can_supply_the_device_summary(self):
        (self.root / 'players.pid').write_text('1234')
        status = self.root / 'players-status.json'
        status.write_text(json.dumps({'pid': 1234, 'rooms': [], 'smart_devices': self.summary}))
        command = os.fsencode(str(webui._HERE / 'zoneplayers.py')) + b'\0'
        with patch('webui.Path.read_bytes', return_value=command):
            self.assertEqual(self.bridge.player_runtime()['smart_devices'], self.summary)
            status.write_text(json.dumps({'pid': 9999, 'rooms': [], 'smart_devices': self.summary}))
            self.assertNotIn('smart_devices', self.bridge.player_runtime())
        with patch('webui.Path.read_bytes', return_value=b'/usr/bin/unrelated\0'):
            self.assertNotIn('smart_devices', self.bridge.player_runtime())

    def test_controller_status_includes_live_summary_only_and_old_no_ha_payload_is_unchanged(self):
        self.bridge.link_status = Mock(return_value={'running': False, 'stations': {}, 'events': []})
        self.bridge.leases = Mock(return_value={})
        self.bridge.speaker_health = Mock(return_value={'healthy': True})
        self.bridge.pairing_status = Mock(return_value={})
        self.bridge.player_runtime = Mock(return_value={'ready': True, 'rooms': [], 'smart_devices': self.summary})
        self.assertEqual(self.bridge.controllers()['smart_devices'], self.summary)
        self.bridge.player_runtime.return_value = {'ready': False, 'rooms': [], 'smart_devices': self.summary}
        self.assertNotIn('smart_devices', self.bridge.controllers())
        self.bridge.player_runtime.return_value = {'ready': True, 'rooms': []}
        self.assertNotIn('smart_devices', self.bridge.controllers())


if __name__ == '__main__': unittest.main()
