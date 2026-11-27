import hmac
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock

from bridge_config import ConfigError
from controller_remote import ACTIONS, Command, ControllerRemote, EMPTY, MAGIC, WIRE, RemoteCommandError
from webui import Auth, Bridge, DataFile, SESSION_COOKIE, listen, make_handler


class RemoteCommands(unittest.TestCase):
    mac = '02:00:00:00:00:01'
    other = '02:00:00:00:00:02'
    key = b'a' * 16
    other_key = b'b' * 16
    room = 'RINCON_02690000000101400'
    session = b's' * 12

    def setUp(self):
        self.pairing = {'controllers': [{'mac': self.mac, 'key': self.key.hex()},
                                        {'mac': self.other, 'key': self.other_key.hex()}]}
        self.service = ControllerRemote(lambda: self.pairing, threading.Event(), Mock(), timeout=0)
        self.command = Command(b'command!', ACTIONS['now_playing'], self.room.encode(), self.key,
                               time.monotonic() + 10)
        self.service.pending[self.mac] = self.command

    def poll(self, key, receipt=EMPTY, status=0):
        packet = WIRE.pack(MAGIC, b'P', self.session, 11, receipt, status, b'')
        return packet + hmac.digest(key, packet, 'sha256')

    def test_command_and_receipt_are_bound_to_one_paired_controller(self):
        other = self.service.response(self.poll(self.other_key, self.command.id, 1))
        self.assertEqual(WIRE.unpack(other[:-32])[4:6], (EMPTY, 0))
        self.assertFalse(self.command.done.is_set())
        reply = self.service.response(self.poll(self.key))
        self.assertEqual(WIRE.unpack(reply[:-32]), (MAGIC, b'C', self.session, 11,
                         self.command.id, ACTIONS['now_playing'], self.room.encode()))
        self.assertEqual(reply[-32:], hmac.digest(self.key, reply[:-32], 'sha256'))
        self.assertTrue(self.service.supported(self.mac))
        self.assertIsNotNone(self.service.response(self.poll(self.key, self.command.id, 2)))
        self.assertTrue(self.command.done.is_set())
        self.assertEqual(self.command.status, 2)

    def test_invalid_authentication_or_expiry_cannot_release_a_command(self):
        self.assertIsNone(self.service.response(self.poll(b'invalid')))
        self.assertIsNone(self.service.response(self.poll(self.key) + b'overflow'))
        self.assertFalse(self.service.supported(self.mac))
        self.command.expires = time.monotonic() - 1
        reply = self.service.response(self.poll(self.key, self.command.id, 1))
        self.assertEqual(WIRE.unpack(reply[:-32])[4:6], (EMPTY, 0))
        self.assertFalse(self.command.done.is_set())

    def test_timeout_discards_the_pending_command(self):
        self.service.pending.clear()
        with self.assertRaises(ConfigError) as timed_out:
            self.service.execute(self.mac, 'wake')
        self.assertEqual(timed_out.exception.status, 504)
        self.assertNotIn(self.mac, self.service.pending)

    def test_hidden_rooms_are_filtered_before_dispatch(self):
        bridge = Mock()
        bridge.spotify_controller.return_value = {'mac': self.mac, 'name': 'Controller'}
        bridge.player_runtime.return_value = {'rooms': [
            {'key': 'media_player.first', 'name': 'First', 'slot': 0},
            {'key': 'media_player.second', 'name': 'Second', 'slot': 1}]}
        bridge.config.return_value = {'controllers': {
            self.mac: {'visible_components': ['ha:media_player.first']}}}
        result = Bridge.controller_remote(bridge, self.mac)
        self.assertEqual(result['rooms'], [{'id': 'ha:media_player.first', 'name': 'First'}])
        with self.assertRaises(ConfigError):
            Bridge.controller_remote(bridge, self.mac, {'action': 'now_playing',
                                     'room': 'ha:media_player.second'}, execute=True)
        Bridge.controller_remote(bridge, self.mac, {'action': 'now_playing',
                                 'room': 'ha:media_player.first'}, execute=True)
        bridge.remote.execute.assert_called_once_with(self.mac, 'now_playing', self.room)


class ControllerAPI(unittest.TestCase):
    first = '02:00:00:00:00:01'
    second = '02:00:00:00:00:02'
    collection = '/api/v1/controllers'
    tokens = '/api/controller-api-tokens'

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        root = Path(directory)
        self.config, self.state = root / 'config.json', root / 'state.json'
        self.config.write_text(json.dumps({'controllers': {self.first: {'name': 'Kitchen'}}}))
        self.state.write_text(json.dumps({'controllers': [{'mac': mac, 'key': key * 16}
                                                         for mac, key in [(self.first, '0a'), (self.second, '0b')]]}))
        self.data_path = root / 'webui.json'
        data = DataFile(self.data_path)
        self.bridge = Bridge(self.config, self.state, None, root / 'players.pid', data)
        self.bridge.player_runtime = Mock(return_value={'rooms': [
            {'key': 'media_player.kitchen', 'name': 'Kitchen', 'slot': 0}]})
        self.bridge.remote.execute = Mock(return_value={'message': 'Command completed.'})
        self.auth = Auth(data)
        self.session = {'Cookie': f'{SESSION_COOKIE}={self.auth.new_session()}', 'X-Bridge': '1'}
        self.server = listen(0, make_handler(self.bridge, self.auth, Mock()))
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, *, headers=None, raw=False):
        headers = dict(headers or {})
        if body is not None and not isinstance(body, str):
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data if raw else json.loads(data), dict(response.getheaders())
        finally:
            connection.close()

    def create_token(self):
        status, data, headers = self.request('POST', self.tokens, {'name': 'Test integration'},
                                             headers=self.session)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return data, {'Authorization': 'Bearer ' + data['token']}

    def test_token_persists_as_a_digest_and_is_revocable(self):
        data, bearer = self.create_token()
        self.assertNotIn(data['token'], self.data_path.read_text())
        self.assertTrue(Auth(DataFile(self.data_path)).valid_controller_token(data['token']))
        status, listing, _ = self.request('GET', self.tokens, headers=self.session)
        self.assertEqual(status, 200)
        self.assertEqual(listing['tokens'], [{key: data[key] for key in ('id', 'name', 'created_at')}])
        self.assertEqual(self.request('POST', self.tokens + '/' + data['id'] + '/revoke', {},
                                      headers=self.session)[0], 200)
        status, denied, headers = self.request('GET', self.collection, headers=bearer)
        self.assertEqual((status, denied['code']), (401, 'invalid_token'))
        self.assertTrue(headers['WWW-Authenticate'].startswith('Bearer '))

    def test_token_is_scoped_and_a_mac_targets_only_that_controller(self):
        _, bearer = self.create_token()
        self.assertEqual(self.request('GET', self.collection, headers=self.session)[0], 401)
        for path in (self.tokens, '/api/controllers', '/api/source'):
            self.assertEqual(self.request('GET', path, headers=bearer)[0], 401)
        self.assertEqual(self.request('POST', self.tokens, {'name': 'Other'},
                                     headers={**bearer, 'X-Bridge': '1'})[0], 401)
        status, data, _ = self.request('GET', self.collection, headers=bearer)
        self.assertEqual(status, 200)
        self.assertEqual([row['mac'] for row in data['controllers']], [self.first, self.second])
        path = self.collection + '/' + self.second.replace(':', '%3A') + '/remote'
        status, options, _ = self.request('GET', path, headers=bearer)
        self.assertEqual(status, 200)
        self.assertEqual(set(options['actions']), set(ACTIONS))
        status, receipt, _ = self.request('POST', path, {'action': 'now_playing', 'room': 'ha:media_player.kitchen'},
                                          headers=bearer)
        self.assertEqual((status, receipt['controller'], receipt['ok']), (200, self.second, True))
        self.bridge.remote.execute.assert_called_once_with(self.second, 'now_playing', RemoteCommands.room)

    def test_all_returns_independent_results_and_dispatches_concurrently(self):
        _, bearer = self.create_token()
        both = threading.Barrier(2, timeout=2)

        def execute(mac, action, argument):
            both.wait()
            if mac == self.second:
                raise RemoteCommandError(504, 'controller_timeout', 'No response')
            return {'message': 'Command completed.'}

        self.bridge.remote.execute.side_effect = execute
        status, result, _ = self.request('POST', self.collection + '/all/remote', {'action': 'wake'}, headers=bearer)
        self.assertEqual((status, result['ok']), (200, False))
        self.assertEqual([(row['controller'], row['status']) for row in result['results']],
                         [(self.first, 200), (self.second, 504)])
        self.assertEqual(result['results'][1]['code'], 'controller_timeout')

    def test_bad_requests_and_empty_all_do_not_dispatch(self):
        _, bearer = self.create_token()
        path = self.collection + '/all/remote'
        self.assertEqual(self.request('POST', path, {'action': 'now_playing'}, headers=bearer)[0], 400)
        self.assertEqual(self.request('POST', path, '{"action":"find"}', headers=bearer)[0], 415)
        self.state.write_text('{}')
        status, result, _ = self.request('POST', path, {'action': 'find'}, headers=bearer)
        self.assertEqual((status, result['code']), (404, 'no_controllers'))
        self.bridge.remote.execute.assert_not_called()

    def test_swagger_schema_and_assets_are_available(self):
        status, spec, _ = self.request('GET', '/api/v1/openapi.json')
        self.assertEqual(status, 200)
        self.assertEqual(spec['components']['securitySchemes']['controllerToken']['scheme'], 'bearer')
        self.assertEqual(set(spec['components']['schemas']['Action']['enum']), set(ACTIONS))
        self.assertIn('post', spec['paths']['/controllers/{target}/remote'])
        status, page, _ = self.request('GET', '/swagger/', raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b'/swagger/swagger-ui-bundle.js', page)
