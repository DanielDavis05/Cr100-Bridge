"""Authenticated Spotify menu editor and separate folder authorization contract."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import webui
from spotify_client import SpotifyError, write_token_file
from spotify_folders import FOLDER_CLIENT_ID, FOLDER_REDIRECT_URI
from spotify_menu import default_items

CR1 = '00:0e:58:12:34:56'
CR2 = '00:0e:58:02:6f:aa'
PLAYLIST = '0123456789ABCDEFGHIJKL'
ALBUM = 'ABCDEFGHIJKLMNOPQRSTUV'


class FakeClient:
    def __init__(self, client_id, token_file, log=print):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == '/me/playlists':
            return {'items': [{'id': PLAYLIST, 'name': 'Family playlist'}], 'next': None}
        if path == '/albums/' + ALBUM:
            return {'name': 'A Single', 'album_type': 'single'}
        raise SpotifyError(404, 'not found')


class FakeFolders:
    def __init__(self):
        self.starts = self.closes = self.refreshes = 0
        self.error = False
        self.tree = [{'kind': 'folder', 'id': 'root-folder', 'name': 'Family', 'children': [
            {'kind': 'folder', 'id': 'nested-folder', 'name': 'Nested', 'children': [
                {'kind': 'playlist', 'uri': 'spotify:playlist:' + PLAYLIST, 'name': 'Family playlist'}]}]}]

    def start(self): self.starts += 1
    def close(self): self.closes += 1
    def get_tree(self): return copy.deepcopy(self.tree)
    def status(self):
        return {'connected': True, 'updated_at': 123, 'revision': '1', 'stale': self.error,
                'error': 'Spotify is unavailable.' if self.error else None}

    def refresh(self, force=True):
        self.refreshes += 1
        if self.error: raise SpotifyError(503, 'private internal message')
        return self.get_tree()


class MenuFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = self.directory / 'config.json'
        self.state = self.directory / 'state.json'
        self.config.write_text(json.dumps({'backend': 'sonos', 'controllers': {CR1: {'name': 'Kitchen'}, CR2: {'name': 'Den'}},
            'other_setting': 'preserve', 'spotify_music_service': {'enabled': True, 'client_id': 'a' * 32,
                'token_file': 'spotify-token.json', 'name': 'Spotify'}}))
        self.state.write_text(json.dumps({'controllers': [{'mac': CR1}, {'mac': CR2}]}))
        self.logs, self.exchanges, self.providers = [], [], []
        def factory(*args, **kwargs):
            provider = FakeFolders(); self.providers.append(provider); return provider
        self.bridge = webui.Bridge(self.config, self.state, None, self.directory / 'missing.pid',
            webui.DataFile(self.directory / 'webui.json'), self.logs.append, token_exchange=self.exchange,
            spotify_factory=FakeClient, folder_factory=factory)

    def tearDown(self):
        self.bridge.close(); self.temp.cleanup()

    def exchange(self, pending, code, token_file):
        self.exchanges.append((pending['client_id'], code, token_file))
        write_token_file(token_file, {'client_id': pending['client_id'], 'access_token': 'folder-secret',
            'refresh_token': 'folder-refresh-secret', 'expires_at': time.time() + 3600, 'scope': 'streaming'})

    def saved(self): return json.loads(self.config.read_text())


class MenuApi(MenuFixture):
    def test_autoplay_defaults_off_and_live_toggle_preserves_setup_without_reload(self):
        self.assertFalse(self.bridge.spotify()['autoplay'])
        self.assertFalse(self.bridge.spotify()['autoplay_connected'])
        original = self.saved()
        with patch.object(self.bridge, 'reload_players') as reload_players:
            result = self.bridge.save_spotify({'autoplay': True})
            reload_players.assert_not_called()
        expected = copy.deepcopy(original)
        expected['spotify_music_service']['autoplay'] = True
        self.assertEqual(self.saved(), expected)
        self.assertTrue(result['applied'])
        self.assertTrue(self.bridge.spotify()['autoplay'])
        with patch.object(self.bridge, 'reload_players') as reload_players:
            self.bridge.save_spotify({'autoplay': False, 'enabled': True, 'name': 'Spotify', 'client_id': 'a' * 32})
            reload_players.assert_not_called()
        self.assertFalse(self.saved()['spotify_music_service']['autoplay'])
        # Settings reads/toggles do not create a Spotify client or folder worker.
        self.assertEqual(self.providers, [])

    def test_autoplay_save_uses_explicit_provider_without_legacy_shadow(self):
        config = self.saved()
        settings = config.pop('spotify_music_service')
        settings.update(provider='spotify', autoplay=False, folders={'token_file': 'separate.json'})
        config['music_services'] = [settings, {'provider': 'speaker_settings', 'enabled': True}]
        self.config.write_text(json.dumps(config))
        with patch.object(self.bridge, 'reload_players') as reload_players:
            self.bridge.save_spotify({'autoplay': True})
            reload_players.assert_not_called()
        saved = self.saved()
        self.assertNotIn('spotify_music_service', saved)
        self.assertEqual(saved['music_services'][0], {**settings, 'autoplay': True})
        self.assertEqual(saved['music_services'][1], config['music_services'][1])

    def test_autoplay_rejects_non_boolean_without_config_write(self):
        before = self.config.read_text()
        with patch.object(self.bridge, 'reload_players') as reload_players:
            for value in (None, 0, 1, 'false', 'true', [], {}):
                with self.subTest(value=value), self.assertRaises(webui.ApiError) as caught:
                    self.bridge.save_spotify({'autoplay': value})
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(self.config.read_text(), before)
            reload_players.assert_not_called()

    def test_legacy_settings_request_keeps_autoplay_and_original_reload(self):
        self.bridge.update_config(lambda config: config['spotify_music_service'].update(autoplay=True))
        with patch.object(self.bridge, 'reload_players', return_value={'applied': True}) as reload_players:
            self.bridge.save_spotify({'enabled': True, 'name': 'Tunes', 'client_id': 'a' * 32})
            reload_players.assert_called_once_with()
        self.assertTrue(self.saved()['spotify_music_service']['autoplay'])
        with patch.object(self.bridge, 'reload_players', return_value={'applied': True}) as reload_players:
            self.bridge.save_spotify({'enabled': True, 'name': 'Renamed', 'client_id': 'a' * 32, 'autoplay': False})
            reload_players.assert_called_once_with()

    def test_playback_connection_status_uses_streaming_grant_without_network(self):
        path = self.directory / 'spotify-folders-token.json'
        write_token_file(path, {'client_id': FOLDER_CLIENT_ID, 'refresh_token': 'private-refresh',
            'access_token': 'private-access', 'expires_at': time.time() + 3600, 'scope': 'streaming'})
        snapshot = self.bridge.spotify()
        self.assertTrue(snapshot['autoplay_connected'])
        self.assertNotIn('private-refresh', json.dumps(snapshot))
        self.assertNotIn('private-access', json.dumps(snapshot))
        self.assertEqual(self.providers, [])
        write_token_file(path, {'client_id': FOLDER_CLIENT_ID, 'refresh_token': 'private-refresh',
            'expires_at': time.time() + 3600, 'scope': 'user-library-read'})
        self.assertFalse(self.bridge.spotify()['autoplay_connected'])

    def test_explicit_service_settings_and_controller_menu_save_without_legacy_shadow(self):
        config = self.saved()
        settings = config.pop('spotify_music_service')
        settings.update(provider='spotify', enabled=False, token_file='explicit-token.json')
        config['music_services'] = [settings, {'provider': 'speaker_settings'}]
        self.config.write_text(json.dumps(config))

        self.assertFalse(self.bridge.spotify()['enabled'])
        self.assertEqual(self.bridge.spotify()['client_id'], 'a' * 32)
        self.assertEqual(self.bridge.spotify_settings()[1], self.directory.resolve() / 'explicit-token.json')
        menu = default_items()
        menu[2]['locked'] = True
        self.bridge.save_spotify_menu(CR1, {'items': menu})
        self.bridge.save_spotify({'client_id': 'b' * 32, 'name': 'Tunes', 'enabled': True})

        saved = self.saved()
        self.assertNotIn('spotify_music_service', saved)
        self.assertEqual(saved['music_services'][0], {
            **settings, 'client_id': 'b' * 32, 'name': 'Tunes', 'enabled': True,
            'controller_menus': {CR1: {'items': menu}}})
        self.assertEqual(saved['music_services'][1], {'provider': 'speaker_settings'})
        self.assertEqual(saved['other_setting'], 'preserve')
        self.assertEqual(self.bridge.spotify_menu(CR1)['items'], menu)
        self.assertEqual(self.bridge.spotify()['name'], 'Tunes')

    def test_defaults_and_independent_controller_menus(self):
        self.assertEqual(self.bridge.spotify_menu(CR1)['items'], default_items())
        menu = default_items()
        menu[3]['locked'] = True
        menu[3]['label'] = 'My favourites'
        menu.insert(0, {'id': 'entry_pin', 'kind': 'folder', 'target': 'root-folder', 'label': 'Family', 'locked': False})
        result = self.bridge.save_spotify_menu(CR1.upper(), {'items': menu})
        self.assertEqual(result['controller'], {'mac': CR1, 'name': 'Kitchen'})
        self.assertEqual(self.bridge.spotify_menu(CR1)['items'], menu)
        self.assertEqual(self.bridge.spotify_menu(CR2)['items'], default_items())
        config = self.saved()
        self.assertEqual(config['other_setting'], 'preserve')
        self.assertEqual(config['spotify_music_service']['client_id'], 'a' * 32)
        self.assertNotIn(CR2, config['spotify_music_service']['controller_menus'])

    def test_unknown_controller_and_invalid_menu_leave_settings_unchanged(self):
        before = self.config.read_text()
        with self.assertRaises(webui.ApiError) as error:
            self.bridge.save_spotify_menu('00:11:22:33:44:55', {'items': default_items()})
        self.assertEqual(error.exception.status, 404)
        for items in (None, [], default_items()[:-1], default_items() + [dict(default_items()[0])]):
            with self.assertRaises(webui.ApiError): self.bridge.save_spotify_menu(CR1, {'items': items})
        menu = default_items(); menu[0]['locked'] = 'false'
        with self.assertRaises(webui.ApiError): self.bridge.save_spotify_menu(CR1, {'items': menu})
        self.assertEqual(self.config.read_text(), before)

    def test_library_tree_and_pasted_single_metadata(self):
        data = self.bridge.spotify_library()
        self.assertEqual(data['folders'][0]['children'][0]['name'], 'Nested')
        self.assertEqual(data['playlists'], [{'uri': 'spotify:playlist:' + PLAYLIST, 'name': 'Family playlist'}])
        self.assertEqual(self.providers[0].starts, 1)
        single = self.bridge.spotify_resolve('https://open.spotify.com/album/' + ALBUM + '?si=test')
        self.assertEqual(single, {'uri': 'spotify:album:' + ALBUM, 'name': 'A Single', 'kind': 'single'})
        self.assertEqual(len(self.providers), 1)
        client, _ = self.bridge.spotify_resources(); before = len(client.calls)
        for value in ('http://127.0.0.1/private', 'https://evil.test/playlist/' + PLAYLIST,
                      'https://open.spotify.com@127.0.0.1/album/' + ALBUM):
            with self.assertRaises(webui.ApiError): self.bridge.spotify_resolve(value)
        self.assertEqual(len(client.calls), before)

    def test_folder_refresh_retains_last_good_tree_on_error(self):
        self.bridge.spotify_resources()
        provider = self.providers[0]
        provider.error = True
        result = self.bridge.spotify_folders_refresh()
        self.assertFalse(result['refreshed'])
        self.assertEqual(result['folders'], provider.tree)
        self.assertNotIn('private internal message', json.dumps(result))
        provider.error = False
        self.assertTrue(self.bridge.spotify_folders_refresh()['refreshed'])

    def test_folder_poll_snapshot_reads_only_cached_tree_and_status(self):
        client, provider = self.bridge.spotify_resources()
        snapshot = self.bridge.spotify_folder_snapshot()
        self.assertEqual(snapshot['folders'], provider.tree)
        self.assertTrue(snapshot['folder_status']['connected'])
        self.assertEqual(client.calls, [])
        self.assertEqual(provider.refreshes, 0)

    def test_config_change_replaces_worker(self):
        self.bridge.spotify_resources(); old = self.providers[0]
        self.bridge.update_config(lambda c: c['spotify_music_service'].update(folders={'cache_file': 'new-cache.json'}))
        self.bridge.spotify_resources()
        self.assertEqual(old.closes, 1)
        self.assertEqual(len(self.providers), 2)

    def test_folder_authorization_is_separate_streaming_only_and_one_use(self):
        existing = self.directory / 'spotify-token.json'; existing.write_text('existing-api-credentials')
        result = self.bridge.spotify_folders_begin(); query = parse_qs(urlsplit(result['url']).query)
        self.assertEqual(query['client_id'], [FOLDER_CLIENT_ID])
        self.assertEqual(query['scope'], ['streaming'])
        self.assertEqual(query['redirect_uri'], [FOLDER_REDIRECT_URI])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        state = self.bridge.spotify_folder_pending['state']
        callback = FOLDER_REDIRECT_URI + '?code=approved-code&state=' + state
        done = self.bridge.spotify_folders_finish(callback)
        self.assertTrue(done['refreshed'])
        self.assertEqual(self.exchanges[0][:2], (FOLDER_CLIENT_ID, 'approved-code'))
        token = self.directory / 'spotify-folders-token.json'
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(existing.read_text(), 'existing-api-credentials')
        self.assertIsNone(self.bridge.spotify_pending)
        self.assertIsNone(self.bridge.spotify_folder_pending)
        self.assertNotIn('folder-secret', json.dumps(done) + json.dumps(self.logs))
        with self.assertRaises(webui.ApiError): self.bridge.spotify_folders_finish(callback)

    def test_folder_auth_rejects_wrong_origin_state_duplicates_expiry_and_decline(self):
        self.bridge.spotify_folders_begin(); pending = self.bridge.spotify_folder_pending; state = pending['state']
        bad = ['https://evil.test/login?code=x&state=' + state,
               FOLDER_REDIRECT_URI + '?code=x&state=wrong',
               FOLDER_REDIRECT_URI + '?code=x&state=%F0%9F%98%80',
               FOLDER_REDIRECT_URI + '?code=x&code=y&state=' + state,
               FOLDER_REDIRECT_URI + '?code=x&state=' + state + '&state=' + state]
        for address in bad:
            with self.assertRaises(webui.ApiError): self.bridge.spotify_folders_finish(address)
        self.assertEqual(self.exchanges, [])
        pending['expires'] = time.time() - 1
        with self.assertRaises(webui.ApiError): self.bridge.spotify_folders_finish(FOLDER_REDIRECT_URI + '?code=x&state=' + state)
        self.bridge.spotify_folders_begin(); state = self.bridge.spotify_folder_pending['state']
        with self.assertRaises(webui.ApiError): self.bridge.spotify_folders_finish(FOLDER_REDIRECT_URI + '?error=access_denied&state=' + state)
        self.assertIsNone(self.bridge.spotify_folder_pending)

    def test_folder_exchange_error_does_not_expose_credentials(self):
        self.bridge.spotify_folders_begin(); state = self.bridge.spotify_folder_pending['state']
        with patch.object(self.bridge, 'token_exchange', side_effect=webui.ApiError(400, 'private-token-response')):
            with self.assertRaises(webui.ApiError) as error:
                self.bridge.spotify_folders_finish(FOLDER_REDIRECT_URI + '?code=x&state=' + state)
        self.assertNotIn('private-token-response', error.exception.message)
        self.assertIsNone(self.bridge.spotify_folder_pending)


class MenuHttp(MenuFixture):
    def setUp(self):
        super().setUp()
        self.auth = webui.Auth(self.bridge.data)
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, self.logs.append))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); super().tearDown()

    def call(self, method, path, value=None, header=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        headers = {'Content-Type': 'application/json'}
        if header: headers['X-Bridge'] = '1'
        if self.cookie: headers['Cookie'] = self.cookie
        conn.request(method, path, json.dumps(value) if value is not None else None, headers)
        r = conn.getresponse(); body = json.loads(r.read()); conn.close(); return r.status, body

    def test_authentication_and_csrf_guard_new_routes(self):
        self.assertEqual(self.call('GET', '/api/spotify/menu/' + CR1)[0], 401)
        self.assertEqual(self.call('GET', '/api/spotify/library')[0], 401)
        self.assertEqual(self.call('GET', '/api/spotify/folders')[0], 401)
        self.cookie = webui.SESSION_COOKIE + '=' + self.auth.new_session()
        self.assertEqual(self.call('GET', '/api/spotify/menu/' + CR1)[0], 200)
        self.assertEqual(self.call('GET', '/api/spotify/menu/' + CR1.replace(':', '%3A'))[0], 200)
        self.assertEqual(self.call('PUT', '/api/spotify/menu/' + CR1, {'items': default_items()}, header=False)[0], 403)
        self.assertEqual(self.call('POST', '/api/spotify/folders/sign-in', {}, header=False)[0], 403)
        self.assertEqual(self.call('POST', '/api/spotify/folders/refresh', {}, header=False)[0], 403)
        self.assertEqual(self.call('PUT', '/api/spotify/menu/' + CR2.replace(':', '%3A'), {'items': default_items()})[0], 200)
        self.assertEqual(self.call('GET', '/api/spotify/library')[0], 200)
        self.assertEqual(self.call('GET', '/api/spotify/folders')[0], 200)
        self.assertEqual(self.call('POST', '/api/spotify/folders/sign-in', {})[0], 200)
        self.assertEqual(self.call('GET', '/api/spotify/menu/00:11:22:33:44:55')[0], 404)

    def test_autoplay_uses_existing_settings_authentication_and_csrf(self):
        self.assertEqual(self.call('GET', '/api/spotify')[0], 401)
        self.assertEqual(self.call('PUT', '/api/spotify', {'autoplay': True})[0], 401)
        self.cookie = webui.SESSION_COOKIE + '=' + self.auth.new_session()
        self.assertFalse(self.call('GET', '/api/spotify')[1]['autoplay'])
        self.assertEqual(self.call('PUT', '/api/spotify', {'autoplay': True}, header=False)[0], 403)
        self.assertEqual(self.call('PUT', '/api/spotify', {'autoplay': 'true'})[0], 400)
        with patch.object(self.bridge, 'reload_players') as reload_players:
            code, body = self.call('PUT', '/api/spotify', {'autoplay': True})
            reload_players.assert_not_called()
        self.assertEqual(code, 200)
        self.assertTrue(body['applied'])
        self.assertTrue(self.call('GET', '/api/spotify')[1]['autoplay'])
        self.assertEqual(self.saved()['spotify_music_service']['client_id'], 'a' * 32)


if __name__ == '__main__': unittest.main()
