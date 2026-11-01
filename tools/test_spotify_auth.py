"""PKCE and loopback state checks with local HTTP and invented credentials."""
import base64
import hashlib
import http.client
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spotify_auth as auth
from spotify_client import SpotifyError
from test_spotify_client import FakeSpotify


def callback_get(callback, query):
    parsed = urllib.parse.urlsplit(callback.redirect_uri)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.request('GET', parsed.path + '?' + urllib.parse.urlencode(query))
    response = connection.getresponse()
    status, body = response.status, response.read()
    connection.close()
    return status, body


class SpotifyAuthTests(unittest.TestCase):
    def test_pkce_matches_rfc7636_vector(self):
        verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
        with patch.object(auth.secrets, 'token_urlsafe', return_value=verifier):
            actual_verifier, challenge = auth.pkce_pair()
        self.assertEqual(actual_verifier, verifier)
        self.assertEqual(challenge, 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(
            auth.authorization_url('public-client', auth.DEFAULT_REDIRECT_URI, 'state', challenge)).query)
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertNotIn('client_secret', params)
        self.assertNotIn('code_verifier', params)
        self.assertFalse(any(scope in params['scope'][0] for scope in ('streaming', 'user-modify-playback-state')))

    def test_bad_state_is_rejected_without_consuming_valid_callback(self):
        with auth.CallbackServer('http://127.0.0.1:0/callback', 'expected-state') as callback:
            result = queue.Queue()
            def waiter():
                try:
                    result.put(callback.wait(2))
                except Exception as error:
                    result.put(error)
            thread = threading.Thread(target=waiter)
            thread.start()
            status, body = callback_get(callback, {'state': 'wrong', 'code': 'LEAK_CANDIDATE'})
            self.assertEqual(status, 400)
            self.assertNotIn(b'LEAK_CANDIDATE', body)
            self.assertIsNone(callback.code)
            self.assertEqual(callback_get(callback, {'state': '日本', 'code': 'LEAK_CANDIDATE'})[0], 400)
            self.assertEqual(callback_get(callback, {'state': 'expected-state', 'code': 'invented-code'})[0], 200)
            thread.join(3)
            self.assertEqual(result.get(timeout=1), 'invented-code')

    def test_timeout_denial_and_non_loopback_uri(self):
        with auth.CallbackServer('http://127.0.0.1:0/callback', 'state') as callback:
            with self.assertRaises(SpotifyError) as failure:
                callback.wait(0.01)
            self.assertEqual(failure.exception.status, 408)
            with self.assertRaises(SpotifyError):
                callback.wait(float('nan'))
        with auth.CallbackServer('http://127.0.0.1:0/callback', 'state') as callback:
            denied = []
            def wait_for_denial():
                try:
                    callback.wait(2)
                except SpotifyError as error:
                    denied.append(error.status)
            thread = threading.Thread(target=wait_for_denial)
            thread.start()
            self.assertEqual(callback_get(callback, {'state': 'state', 'error': 'access_denied'})[0], 200)
            thread.join(3)
            self.assertEqual(denied, [401])
        for redirect in ('http://0.0.0.0:8888/callback', 'http://localhost:8888/callback',
                         'https://example.com/callback', 'http://127.0.0.1:8888/callback?state=bad'):
            with self.assertRaises(SpotifyError):
                auth.CallbackServer(redirect, 'state')
        with self.assertRaises(SpotifyError):
            auth.login('', '/tmp/unused-spotify-token')

    def test_login_exchanges_code_with_pkce_and_status_never_returns_tokens(self):
        fake = FakeSpotify()
        try:
            fake.reply('POST', '/token', payload={'access_token': 'ACCESS_SECRET', 'refresh_token': 'REFRESH_SECRET',
                                                  'expires_in': 3600, 'scope': 'user-library-read'})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'token.json'
                outputs, browser_queries = [], []
                def open_browser(url):
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    browser_queries.append(params)
                    def send_callback():
                        parsed = urllib.parse.urlsplit(params['redirect_uri'][0])
                        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                        connection.request('GET', parsed.path + '?' + urllib.parse.urlencode(
                            {'state': params['state'][0], 'code': 'AUTH_CODE_SECRET'}))
                        response = connection.getresponse(); response.read(); connection.close()
                    threading.Thread(target=send_callback).start()
                with patch.object(auth.webbrowser, 'open', side_effect=open_browser), \
                        patch.object(auth, 'TOKEN_ENDPOINT', fake.url + '/token'):
                    auth.login('public-client', path, 'http://127.0.0.1:0/callback', output=outputs.append, timeout=2)
                fields = urllib.parse.parse_qs(fake.requests[0]['body'].decode())
                verifier = fields['code_verifier'][0]
                expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                self.assertEqual(browser_queries[0]['code_challenge'], [expected_challenge])
                self.assertEqual(fields['code'], ['AUTH_CODE_SECRET'])
                self.assertEqual(fields['grant_type'], ['authorization_code'])
                self.assertNotIn('client_secret', fields)
                self.assertTrue(auth.status('public-client', path)['authorized'])
                self.assertNotIn('SECRET', json.dumps(auth.status('public-client', path)) + repr(outputs))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            fake.close()

    def test_config_and_cli_token_paths_expand_home_before_config_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / 'config' / 'bridge-config.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'spotify_music_service': {
                'client_id': 'public-client', 'token_file': '~/spotify-token.json'}}))
            with patch.dict(os.environ, {'HOME': str(folder / 'home')}), patch.object(auth, 'login') as login:
                self.assertEqual(auth.main(['--config', str(config), 'login']), 0)
                self.assertEqual(login.call_args.args[1], folder / 'home' / 'spotify-token.json')
                self.assertEqual(auth.main(['--config', str(config), '--token-file', '~/explicit.json', 'login']), 0)
                self.assertEqual(login.call_args.args[1], folder / 'home' / 'explicit.json')


if __name__ == '__main__':
    unittest.main()
