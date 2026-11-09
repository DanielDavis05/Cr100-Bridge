"""Per-controller component settings, runtime inventory and authenticated API."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

import webui

CR1 = '00:0e:58:01:02:03'
CR2 = '00:0e:58:04:05:06'
SPEAKER = 'sonos:RINCON_ONE'
SUB = 'sonos:RINCON_SUB'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'backend': 'sonos', 'rooms': [
            {'backend': 'sonos', 'id': 'RINCON_ONE', 'name': 'Living room', 'slot': 2},
            {'entity': 'media_player.kitchen', 'name': 'Kitchen', 'slot': 4},
            {'name': 'Spare', 'slot': 9}], 'controllers': {
                CR1: {'name': 'Living controller', 'other': 'preserved'}, CR2: {'name': 'Kitchen controller'}},
            'spotify_music_service': {'controller_menus': {CR1: {'items': ['unchanged']}}},
            'other': {'keep': True}}))
        self.state = self.root / 'state.json'
        self.state.write_text(json.dumps({'controllers': [{'mac': CR1}, {'mac': CR2}]}))
        self.bridge = webui.Bridge(self.config, self.state, None, self.root / 'pid',
                                   webui.DataFile(self.root / 'webui.json'), log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.runtime = {'running': True, 'ready': True, 'rooms': [], 'components': [
            {'key': SPEAKER, 'name': 'Living room', 'kind': 'room'},
            {'key': SUB, 'name': 'Living Sub', 'kind': 'about', 'parent_keys': [SPEAKER]},
            {'key': 'sonos:STALE_SUB', 'name': 'Other network Sub', 'kind': 'about', 'parent_keys': ['sonos:STALE']}]}
        self.bridge.player_runtime = lambda: self.runtime
        self.bridge.reload_players = Mock(return_value={'applied': True, 'message': 'Saved.'})

    def saved(self): return json.loads(self.config.read_text())


class ComponentApi(Fixture):
    def test_defaults_include_stable_rooms_bridge_and_bonded_components(self):
        result = self.bridge.controller_components(CR1.upper())
        self.assertIsNone(result['visible_components'])
        rows = {item['key']: item for item in result['components']}
        self.assertEqual(set(rows), {SPEAKER, SUB, 'ha:media_player.kitchen', 'sim:9', 'cr100-bridge'})
        self.assertTrue(all(item['selected'] and item['effective'] for item in rows.values()))
        self.assertEqual(rows[SUB]['parent_names'], ['Living room'])

    def test_separate_empty_explicit_and_inherited_selections_preserve_other_settings(self):
        before = self.saved()
        self.bridge.save_controller_components(CR1.upper(), {'visible_components': [SPEAKER, SUB]})
        self.assertEqual(self.bridge.controller_components(CR1)['visible_components'], [SPEAKER, SUB])
        self.assertIsNone(self.bridge.controller_components(CR2)['visible_components'])
        self.bridge.save_controller_components(CR2, {'visible_components': []})
        self.assertTrue(all(not item['effective'] for item in self.bridge.controller_components(CR2)['components']))
        self.bridge.save_controller_components(CR1, {'visible_components': None})
        after = self.saved()
        self.assertEqual(after['controllers'][CR1], before['controllers'][CR1])
        self.assertEqual(after['spotify_music_service'], before['spotify_music_service'])
        self.assertEqual(after['rooms'], before['rooms'])
        self.assertEqual(after['other'], before['other'])
        self.assertEqual(self.bridge.reload_players.call_count, 3)

    def test_bonded_about_entry_requires_visible_parent(self):
        self.bridge.save_controller_components(CR1, {'visible_components': [SUB]})
        rows = {item['key']: item for item in self.bridge.controller_components(CR1)['components']}
        self.assertTrue(rows[SUB]['selected'])
        self.assertFalse(rows[SUB]['effective'])

    def test_legacy_null_controller_settings_can_be_saved(self):
        for profiles in (None, {CR1: None, CR2: {'name': 'Kitchen controller'}}):
            config = self.saved()
            config['controllers'] = profiles
            self.config.write_text(json.dumps(config))
            self.assertIsNone(self.bridge.controller_components(CR1)['visible_components'])
            self.bridge.save_controller_components(CR1, {'visible_components': [SPEAKER]})
            self.assertEqual(self.saved()['controllers'][CR1]['visible_components'], [SPEAKER])
            self.assertEqual(self.saved()['other'], {'keep': True})

    def test_unknown_duplicates_and_bad_payloads_do_not_write_or_reload(self):
        before = self.config.read_text()
        for value in (None, [], {}, {'visible_components': 'all'}, {'visible_components': [SPEAKER, SPEAKER]},
                      {'visible_components': ['sonos:UNKNOWN']}, {'visible_components': [None]}):
            with self.subTest(value=value), self.assertRaises(webui.ApiError) as error:
                self.bridge.save_controller_components(CR1, value)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(webui.ApiError) as error:
            self.bridge.save_controller_components('00:11:22:33:44:55', {'visible_components': []})
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.config.read_text(), before)
        self.bridge.reload_players.assert_not_called()

    def test_unavailable_saved_selection_is_retained_but_cannot_enable_a_room(self):
        self.bridge.save_controller_components(CR1, {'visible_components': [SPEAKER, SUB]})
        config = self.saved()
        config['rooms'] = []
        config['stashed_sources'] = {'sonos': {'rooms': [{'backend': 'sonos', 'id': 'RINCON_ONE', 'name': 'Living room', 'slot': 2}]}}
        self.config.write_text(json.dumps(config))
        rows = {item['key']: item for item in self.bridge.controller_components(CR1)['components']}
        self.assertEqual(set(rows), {SPEAKER, SUB})
        self.assertTrue(all(not item['enabled'] and not item['effective'] for item in rows.values()))
        self.assertTrue(all(item['selected'] for item in rows.values()))
        self.bridge.save_controller_components(CR1, {'visible_components': [SPEAKER, SUB]})
        self.assertEqual(self.saved()['rooms'], [])


class ComponentHttp(Fixture):
    def test_authenticated_get_put_and_unknown_controller(self):
        auth = webui.Auth(self.bridge.data)
        server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, auth, lambda _: None))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cookie = auth.new_session()
        def call(method, path, value=None, authenticated=True):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'X-Bridge': '1', 'Content-Type': 'application/json'}
            if authenticated: headers['Cookie'] = webui.SESSION_COOKIE + '=' + cookie
            connection.request(method, path, None if value is None else json.dumps(value), headers)
            result = connection.getresponse()
            body = json.loads(result.read())
            connection.close()
            return result.status, body
        path = '/api/controllers/' + CR1 + '/components'
        self.assertEqual(call('GET', path, authenticated=False)[0], 401)
        self.assertEqual(call('GET', path)[0], 200)
        self.assertEqual(call('PUT', path, {'visible_components': []})[0], 200)
        self.assertEqual(call('GET', path)[1]['visible_components'], [])
        self.assertEqual(call('PUT', path, {'visible_components': ['unknown']})[0], 400)
        self.assertEqual(call('GET', '/api/controllers/00:11:22:33:44:55/components')[0], 404)


if __name__ == '__main__': unittest.main()
