"""Startup regression: unavailable speakers must not take down virtual players."""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backends
import sonos_backend
import zoneplayers


class RoomConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'config.json'
        self.logs = []

    def load(self, rooms, connected):
        self.config.write_text(json.dumps({'rooms': rooms}))
        with patch.object(backends, 'load', return_value=connected):
            return zoneplayers.load_rooms(self.config, self.logs.append)

    def test_offline_sonos_starts_http_players_and_preserves_target_and_slot(self):
        backend = sonos_backend.SonosBackend(log=self.logs.append)
        self.addCleanup(backend.listener.server.server_close)
        target = 'RINCON_000E5820ACB601400'
        names, refs, slots, connected = self.load(
            [{'backend': 'sonos', 'id': target, 'slot': 10}], {'sonos': backend})
        self.assertEqual(refs, [('sonos', target)])
        self.assertEqual(slots, [10])
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0])
        zp = zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)
        house = zoneplayers.Household(zp, 'Sonos_offline_regression', names, ['127.0.0.1'],
                                     self.logs.append, port=0, refs=refs,
                                     backends=connected.values(), slots=slots)
        self.addCleanup(house.close)
        house.start('127.0.0.1', ssdp=False)
        player = house.players[0]
        self.assertEqual(player.state.backend.ref, ('sonos', target))
        self.assertFalse(player.state.backend.available)
        conn = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/xml/device_description.xml')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(names[0].encode(), response.read())
        status = Path(self.temp.name) / 'players-status.json'
        zoneplayers.publish_player_status(status, house, slots)
        self.assertEqual(json.loads(status.read_text()), {'pid': os.getpid(), 'rooms': [
            {'key': 'sonos:' + target, 'name': names[0], 'slot': 10}], 'components': [
            {'key': 'sonos:' + target, 'name': names[0], 'kind': 'room', 'parent_keys': []},
            {'key': 'cr100-bridge', 'name': 'CR100 Bridge', 'kind': 'bridge', 'parent_keys': []}]})
        self.assertTrue(any(record.get('event') == 'room-name-unavailable' for record in self.logs))

    def test_multiple_offline_backends_keep_distinct_rooms(self):
        sonos, ha = backends.Backend(self.logs.append), backends.Backend(self.logs.append)
        rooms = [{'backend': 'sonos', 'id': 'RINCON_ONE', 'slot': 3},
                 {'backend': 'sonos', 'id': 'RINCON_TWO', 'slot': 5},
                 {'entity': 'media_player.den', 'slot': 9}]
        names, refs, slots, _ = self.load(rooms, {'sonos': sonos, 'ha': ha})
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(refs, [('sonos', 'RINCON_ONE'), ('sonos', 'RINCON_TWO'),
                                ('ha', 'media_player.den')])
        self.assertEqual(slots, [3, 5, 9])

    def test_saved_and_discovered_names_take_precedence(self):
        backend = backends.Backend(self.logs.append)
        backend.display_name = lambda key: 'TechServ'
        names, _, _, _ = self.load([{'backend': 'sonos', 'id': 'one', 'name': 'Office'},
                                    {'backend': 'sonos', 'id': 'two'}], {'sonos': backend})
        self.assertEqual(names, ['Office', 'TechServ'])
        self.assertEqual(self.logs, [])

    def test_unnamed_simulated_rooms_and_duplicate_slots_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Room needs a name'):
            self.load([{}], {})
        with self.assertRaisesRegex(ValueError, 'Room slots must be unique'):
            self.load([{'backend': 'sonos', 'id': 'one', 'slot': 2},
                       {'backend': 'sonos', 'id': 'two', 'slot': 2}],
                      {'sonos': backends.Backend(self.logs.append)})


if __name__ == '__main__':
    unittest.main()
