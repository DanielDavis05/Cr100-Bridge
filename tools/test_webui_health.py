"""Regression checks for discovery/selection/runtime disagreement in the bridge UI."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backends
import webui
import zoneplayers

BEDROOM = 'RINCON_000E5800000A01400'
TECHSERV = 'RINCON_000E5800000B01400'


class SpeakerHealth(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.write_config(TECHSERV, 'TechServ')
        (self.root / 'state.json').write_text('{}')
        self.bridge = webui.Bridge(self.config, self.root / 'state.json', self.root,
                                   self.root / 'players.pid', None, log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.household = {'error': None, 'zones': [
            {'uuid': TECHSERV, 'name': 'TechServ', 'state': 'idle', 'playing': None,
             'base': 'http://172.24.0.151:1400', 'group': []}]}
        self.bridge.sonos_household = lambda: self.household
        self.bridge.sonos_known = lambda: self.household
        self.runtime = {'running': True, 'ready': True, 'rooms': [
            {'key': 'sonos:' + TECHSERV, 'name': 'TechServ', 'slot': 11}]}
        patcher = patch.object(self.bridge, 'player_runtime', side_effect=lambda: self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, target, name):
        self.config.write_text(json.dumps({'backend': 'sonos', 'rooms': [
            {'backend': 'sonos', 'id': target, 'last_known_name': name, 'slot': 11}]}))

    def test_discovering_techserv_does_not_make_a_missing_bedroom_selection_healthy(self):
        self.write_config(BEDROOM, 'Bedroom')
        self.runtime['rooms'] = [{'key': 'sonos:' + BEDROOM, 'name': 'Bedroom', 'slot': 11}]
        overview = self.bridge.overview()
        self.assertTrue(overview['source_connected'])
        health = overview['speaker_health']
        self.assertFalse(health['healthy'])
        self.assertEqual(health['selected'], ['Bedroom'])
        self.assertEqual(health['missing'], ['Bedroom'])
        self.assertIn('Selected speakers unavailable: Bedroom', health['message'])
        rows = {r['key']: r for r in self.bridge.speakers()['speakers']}
        self.assertTrue(rows['sonos:' + BEDROOM]['shown'])
        self.assertTrue(rows['sonos:' + BEDROOM]['missing'])
        self.assertEqual(rows['sonos:' + BEDROOM]['name'], 'Bedroom')
        self.assertFalse(rows['sonos:' + TECHSERV]['shown'])

    def test_connected_radio_and_discovery_do_not_hide_a_failed_player_process(self):
        self.runtime.update(running=False, ready=False, rooms=[])
        health = self.bridge.speaker_health()
        self.assertFalse(health['healthy'])
        self.assertFalse(health['players_running'])
        self.assertIn('speaker service is not running', health['message'])
        for result in (self.bridge.controllers(), self.bridge.source_status(), self.bridge.speakers()):
            self.assertFalse(result['speaker_health']['healthy'])

    def test_saved_selection_is_not_reported_applied_until_the_runtime_matches(self):
        self.runtime['rooms'][0]['key'] = 'sonos:' + BEDROOM
        health = self.bridge.speaker_health()
        self.assertFalse(health['healthy'])
        self.assertFalse(health['selection_applied'])
        self.assertIn('waiting to be applied', health['message'])

    def test_selected_available_and_serving_techserv_is_healthy(self):
        health = self.bridge.speaker_health()
        self.assertTrue(health['healthy'])
        self.assertTrue(health['selection_applied'])
        self.assertEqual(health['selected'], ['TechServ'])
        self.assertEqual(health['missing'], [])

    def test_no_selection_is_unhealthy_even_when_discovery_succeeds(self):
        self.config.write_text(json.dumps({'backend': 'sonos', 'rooms': []}))
        self.runtime['rooms'] = []
        health = self.bridge.speaker_health()
        self.assertFalse(health['healthy'])
        self.assertIn('No speakers selected', health['message'])

    def test_save_remembers_the_speaker_name_and_recovers_from_later_discovery_failure(self):
        self.bridge.save_speakers({'rooms': [{'key': 'sonos:' + TECHSERV}]})
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved['rooms'][0]['last_known_name'], 'TechServ')
        self.assertNotIn('name', saved['rooms'][0])  # follows future source renames
        self.household = {'error': 'offline', 'zones': []}
        rows = self.bridge.speakers()['speakers']
        self.assertEqual((rows[0]['name'], rows[0]['shown'], rows[0]['missing']), ('TechServ', True, True))
        self.bridge.update_config(lambda config: config['sonos'].update(follow_network=False))
        with patch.object(backends, 'load', return_value={'sonos': backends.Backend(lambda _: None)}):
            names, refs, slots, _ = zoneplayers.load_rooms(self.config, lambda _: None)
        self.assertEqual(names, ['TechServ'])
        self.assertEqual(refs, [('sonos', TECHSERV)])
        self.assertEqual(slots, [11])
        self.bridge.save_speakers({'rooms': [{'key': 'sonos:' + TECHSERV}]})
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved['rooms'][0]['last_known_name'], 'TechServ')
        self.assertNotIn('name', saved['rooms'][0])

    def test_readiness_from_a_dead_or_reused_pid_is_rejected(self):
        (self.root / 'players.pid').write_text('1234')
        path = self.root / 'players-status.json'
        path.write_text(json.dumps({'pid': 1234, 'rooms': self.runtime['rooms']}))
        command = os.fsencode(str(webui._HERE / 'zoneplayers.py')) + b'\0'
        with patch('webui.Path.read_bytes', return_value=command):
            self.assertTrue(webui.Bridge.player_runtime(self.bridge)['ready'])
            path.write_text(json.dumps({'pid': 9999, 'rooms': self.runtime['rooms']}))
            self.assertFalse(webui.Bridge.player_runtime(self.bridge)['ready'])
        with patch('webui.Path.read_bytes', return_value=b'/usr/bin/unrelated\0'):
            self.assertFalse(webui.Bridge.player_runtime(self.bridge)['running'])


if __name__ == '__main__':
    unittest.main()
