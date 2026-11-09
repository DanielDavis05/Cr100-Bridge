"""Reconnect controls preserve pairing and component settings without restarting radio."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import webui


FIRST = '00:0e:58:01:02:03'
SECOND = '00:0e:58:04:05:06'


class ReconnectControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps({'backend': 'sonos', 'rooms': [],
            'controllers': {FIRST: {'name': 'First', 'visible_components': ['cr100-bridge']},
                            SECOND: {'name': 'Second', 'visible_components': []}},
            'other': {'preserved': True}}))
        self.state_path = self.root / 'state.json'
        self.state_path.write_text(json.dumps({'household': 'Sonos_test', 'freq': 2437,
            'network_key': '0123456789abcdef0123456789abcdef',
            'controllers': [{'mac': FIRST, 'aid': 1}, {'mac': SECOND, 'aid': 65}]}))
        self.state_path.chmod(0o600)
        self.pairing_before = self.state_path.read_bytes()
        self.bridge = webui.Bridge(self.config_path, self.state_path, None,
            self.root / 'players.pid', webui.DataFile(self.root / 'webui.json'), log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.bridge.reload_players = Mock()
        self.bridge.speaker_health = Mock(return_value={'healthy': True})
        self.bridge.leases = Mock(return_value={})
        self.bridge.link_status = Mock(return_value={'running': True, 'events': [], 'stations': {
            FIRST: {'associated': True, 'searching': False, 'last_heard_s': 1.0},
            SECOND: {'associated': False, 'searching': True, 'last_heard_s': 121.0}}})

    def saved(self):
        return json.loads(self.config_path.read_text())

    def assert_pairing_unchanged(self):
        self.assertEqual(self.state_path.read_bytes(), self.pairing_before)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    def test_pause_resume_preserves_other_settings_and_does_not_restart_services(self):
        before = self.saved()
        with patch.object(webui.subprocess, 'run') as run, patch.object(webui.os, 'kill') as kill:
            result = self.bridge.save_controller_reconnect(SECOND.upper(), {'enabled': False})
            self.assertIs(result['auto_reconnect'], False)
            paused = self.saved()
            expected = json.loads(json.dumps(before))
            expected['controllers'][SECOND]['auto_reconnect'] = False
            self.assertEqual(paused, expected)
            result = self.bridge.save_controller_reconnect(SECOND, {'enabled': True})
            self.assertIs(result['auto_reconnect'], True)
            expected['controllers'][SECOND]['auto_reconnect'] = True
            self.assertEqual(self.saved(), expected)
        run.assert_not_called()
        kill.assert_not_called()
        self.bridge.reload_players.assert_not_called()
        self.assert_pairing_unchanged()

    def test_list_keeps_connection_status_separate_from_reconnect_preference(self):
        rows = {c['mac']: c for c in self.bridge.controllers()['controllers']}
        self.assertTrue(rows[FIRST]['auto_reconnect'])
        self.assertTrue(rows[SECOND]['auto_reconnect'])
        self.bridge.save_controller_reconnect(FIRST, {'enabled': False})
        paused = {c['mac']: c for c in self.bridge.controllers()['controllers']}
        self.assertFalse(paused[FIRST]['auto_reconnect'])
        self.assertEqual(paused[FIRST]['status'], rows[FIRST]['status'])
        self.assertEqual(paused[FIRST]['status_label'], rows[FIRST]['status_label'])
        self.assertTrue(paused[SECOND]['auto_reconnect'])

    def test_legacy_null_profiles_can_be_updated(self):
        for profiles in (None, {SECOND: None}):
            self.config_path.write_text(json.dumps({'rooms': [], 'controllers': profiles}))
            self.assertTrue(self.bridge.controllers()['controllers'][1]['auto_reconnect'])
            self.bridge.save_controller_reconnect(SECOND, {'enabled': False})
            self.assertIs(self.saved()['controllers'][SECOND]['auto_reconnect'], False)
        self.assert_pairing_unchanged()

    def test_bad_payload_or_unknown_controller_never_changes_configuration(self):
        before = self.config_path.read_bytes()
        for payload in (None, [], {}, {'enabled': None}, {'enabled': 0}, {'enabled': 1},
                        {'enabled': 'false'}, {'enabled': 'true'}):
            with self.subTest(payload=payload), self.assertRaises(webui.ApiError) as error:
                self.bridge.save_controller_reconnect(FIRST, payload)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(webui.ApiError) as error:
            self.bridge.save_controller_reconnect('00:11:22:33:44:55', {'enabled': False})
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assert_pairing_unchanged()

    def test_route_requires_authentication_and_accepts_only_a_paired_controller(self):
        auth = webui.Auth(self.bridge.data)
        server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, auth, lambda _: None))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cookie = auth.new_session()
        def call(mac, payload, authenticated=True):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'X-Bridge': '1', 'Content-Type': 'application/json'}
            if authenticated: headers['Cookie'] = webui.SESSION_COOKIE + '=' + cookie
            connection.request('PUT', '/api/controllers/' + mac + '/reconnect', json.dumps(payload), headers)
            result = connection.getresponse()
            status, body = result.status, json.loads(result.read())
            connection.close()
            return status, body
        self.assertEqual(call(SECOND, {'enabled': False}, authenticated=False)[0], 401)
        self.assertNotIn('auto_reconnect', self.saved()['controllers'][SECOND])
        status, result = call(SECOND.upper(), {'enabled': False})
        self.assertEqual(status, 200)
        self.assertIs(result['auto_reconnect'], False)
        self.assertEqual(call(SECOND, {'enabled': 'false'})[0], 400)
        self.assertEqual(call('00:11:22:33:44:55', {'enabled': False})[0], 404)
        self.assert_pairing_unchanged()
        self.bridge.reload_players.assert_not_called()


if __name__ == '__main__':
    unittest.main()
