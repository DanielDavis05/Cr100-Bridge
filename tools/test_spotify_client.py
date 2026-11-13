"""Local HTTP checks for Spotify OAuth refresh, retry bounds and token storage."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spotify_client as spotify


class FakeSpotify:
    def __init__(self):
        self.requests, self.routes = [], {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                request = {'method': self.command, 'url': self.path, 'body': body,
                           'authorization': self.headers.get('Authorization')}
                fake.requests.append(request)
                route = fake.routes.get((self.command, urllib.parse.urlsplit(self.path).path))
                status, payload, headers = route(request) if route else (404, {}, {})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_request

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def reply(self, method, path, status=200, payload=None, headers=None):
        self.routes[(method, path)] = lambda request: (status, payload or {}, headers or {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class SpotifyClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'spotify-token.json'
        self.fake = FakeSpotify()
        self.api_patch = patch.object(spotify, 'API_ROOT', self.fake.url + '/v1')
        self.token_patch = patch.object(spotify, 'TOKEN_ENDPOINT', self.fake.url + '/token')
        self.api_patch.start(); self.token_patch.start()
        self.logs = []
        self.client = spotify.SpotifyClient('test-client', self.path, log=self.logs.append)

    def tearDown(self):
        self.api_patch.stop(); self.token_patch.stop()
        self.fake.close(); self.temp.cleanup()

    def token(self, expired=False):
        spotify.write_token_file(self.path, {
            'client_id': 'test-client', 'access_token': 'ACCESS_SECRET', 'refresh_token': 'REFRESH_SECRET',
            'expires_at': time.time() + (-10 if expired else 3600), 'scope': 'user-library-read'})

    def token_reply(self, **extra):
        self.fake.reply('POST', '/token', payload={
            'access_token': 'NEW_ACCESS_SECRET', 'expires_in': 3600, 'token_type': 'Bearer', **extra})

    def test_lazy_missing_setup_and_hotload_after_login(self):
        unconfigured = spotify.SpotifyClient('', self.path, log=self.logs.append)
        with self.assertRaises(spotify.SpotifyError) as failure:
            unconfigured.get('/me')
        self.assertEqual(failure.exception.status, 401)
        with self.assertRaises(spotify.SpotifyError):
            self.client.get('/me')
        self.assertEqual(self.fake.requests, [])
        self.token()
        self.fake.reply('GET', '/v1/me/playlists', payload={'items': []})
        self.assertEqual(self.client.get('/me/playlists', {'limit': 10, 'offset': 20}), {'items': []})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.fake.requests[0]['url']).query)
        self.assertEqual(query, {'limit': ['10'], 'offset': ['20']})

    def test_refresh_preserves_token_and_atomically_replaces_with_private_mode(self):
        self.token(expired=True)
        os.chmod(self.path, 0o644)
        self.token_reply()
        self.fake.reply('GET', '/v1/me', payload={'id': 'synthetic-user'})
        with patch.object(spotify.os, 'replace', wraps=os.replace) as replace:
            self.assertEqual(self.client.get('/me')['id'], 'synthetic-user')
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['refresh_token'], 'REFRESH_SECRET')
        self.assertEqual(saved['access_token'], 'NEW_ACCESS_SECRET')
        request = self.fake.requests[0]
        self.assertIsNone(request['authorization'])
        fields = urllib.parse.parse_qs(request['body'].decode())
        self.assertEqual(fields, {'grant_type': ['refresh_token'], 'client_id': ['test-client'],
                                  'refresh_token': ['REFRESH_SECRET']})
        self.assertNotIn('SECRET', repr(self.logs))

    def test_401_refreshes_once_then_stops(self):
        self.token(); self.token_reply(refresh_token='ROTATED_REFRESH_SECRET')
        calls = []
        def api(request):
            calls.append(request['authorization'])
            return (401, {'error': {'message': 'echo ACCESS_SECRET'}}, {}) if len(calls) == 1 else (200, {'ok': True}, {})
        self.fake.routes[('GET', '/v1/me')] = api
        self.assertEqual(self.client.get('/me'), {'ok': True})
        self.assertEqual(calls, ['Bearer ACCESS_SECRET', 'Bearer NEW_ACCESS_SECRET'])
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], 'ROTATED_REFRESH_SECRET')
        self.fake.requests.clear()
        self.fake.reply('GET', '/v1/me', status=401)
        with self.assertRaises(spotify.SpotifyError) as failure:
            self.client.get('/me')
        self.assertEqual(failure.exception.status, 401)
        self.assertEqual([r['method'] for r in self.fake.requests], ['GET', 'POST', 'GET'])

    def test_rate_limit_retry_is_bounded_and_long_delay_is_respected(self):
        self.token()
        self.fake.reply('GET', '/v1/me', status=429, headers={'Retry-After': '0'})
        with patch.object(spotify.time, 'sleep') as sleep, self.assertRaises(spotify.SpotifyError) as failure:
            self.client.get('/me')
        self.assertEqual(failure.exception.status, 429)
        self.assertEqual(len(self.fake.requests), 2)
        sleep.assert_called_once_with(0)
        self.fake.reply('GET', '/v1/me', status=429, headers={'Retry-After': '60'})
        self.fake.requests.clear()
        with patch.object(spotify.time, 'sleep') as sleep:
            for _ in range(2):
                with self.assertRaises(spotify.SpotifyError) as failure:
                    self.client.get('/me')
                self.assertEqual(failure.exception.status, 429)
            sleep.assert_not_called()
        self.assertEqual(len(self.fake.requests), 1)

    def test_invalid_grant_discards_revoked_refresh_and_does_not_echo_body(self):
        self.token(expired=True)
        self.fake.reply('POST', '/token', status=400,
                        payload={'error': 'invalid_grant', 'error_description': 'REFRESH_SECRET ACCESS_SECRET'})
        with self.assertRaises(spotify.SpotifyError) as failure:
            self.client.get('/me')
        self.assertEqual(failure.exception.status, 401)
        self.assertNotIn('SECRET', str(failure.exception) + repr(self.logs) + self.path.read_text())
        with self.assertRaises(spotify.SpotifyError):
            self.client.get('/me')
        self.assertEqual(len(self.fake.requests), 1)

    def test_two_clients_share_one_rotating_refresh(self):
        self.token(expired=True)
        original = spotify.read_token_file
        barrier, endpoint_lock = threading.Barrier(2), threading.Lock()
        initial_reads, current_refresh, refreshes = set(), ['REFRESH_SECRET'], []
        def read_token(path, client_id):
            token = original(path, client_id)
            thread = threading.get_ident()
            if thread not in initial_reads:
                initial_reads.add(thread)
                barrier.wait(3)
            return token
        def token_endpoint(request):
            supplied = urllib.parse.parse_qs(request['body'].decode())['refresh_token'][0]
            with endpoint_lock:
                refreshes.append(supplied)
                if supplied != current_refresh[0]: return 400, {'error': 'invalid_grant'}, {}
                current_refresh[0] = 'ROTATED_REFRESH_SECRET'
            time.sleep(.05)
            return 200, {'access_token': 'NEW_ACCESS_SECRET', 'refresh_token': current_refresh[0],
                         'expires_in': 3600}, {}
        self.fake.routes[('POST', '/token')] = token_endpoint
        self.fake.reply('GET', '/v1/me', payload={'ok': True})
        clients = [self.client, spotify.SpotifyClient('test-client', self.path, log=self.logs.append)]
        results, failures = [], []
        def get(client):
            try: results.append(client.get('/me'))
            except Exception as error: failures.append(error)
        with patch.object(spotify, 'read_token_file', side_effect=read_token):
            threads = [threading.Thread(target=get, args=(client,)) for client in clients]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(results, [{'ok': True}, {'ok': True}])
        self.assertEqual(refreshes, ['REFRESH_SECRET'])
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], current_refresh[0])

    def test_invalid_grant_retries_a_newer_refresh_token_once(self):
        self.token(expired=True)
        refreshes = []
        def token_endpoint(request):
            supplied = urllib.parse.parse_qs(request['body'].decode())['refresh_token'][0]
            refreshes.append(supplied)
            if len(refreshes) == 1:
                spotify.write_token_file(self.path, {'client_id': 'test-client', 'access_token': 'OTHER_ACCESS',
                    'refresh_token': 'OTHER_REFRESH', 'expires_at': time.time() - 10})
                return 400, {'error': 'invalid_grant'}, {}
            return 200, {'access_token': 'NEW_ACCESS_SECRET', 'refresh_token': 'ROTATED_REFRESH_SECRET',
                         'expires_in': 3600}, {}
        self.fake.routes[('POST', '/token')] = token_endpoint
        self.fake.reply('GET', '/v1/me', payload={'ok': True})
        self.assertEqual(self.client.get('/me'), {'ok': True})
        self.assertEqual(refreshes, ['REFRESH_SECRET', 'OTHER_REFRESH'])
        self.assertEqual(json.loads(self.path.read_text())['refresh_token'], 'ROTATED_REFRESH_SECRET')

    def test_invalid_grant_with_changing_tokens_only_retries_once(self):
        self.token(expired=True)
        refreshes = []
        def token_endpoint(request):
            refreshes.append(urllib.parse.parse_qs(request['body'].decode())['refresh_token'][0])
            spotify.write_token_file(self.path, {'client_id': 'test-client', 'access_token': 'OTHER_ACCESS',
                'refresh_token': 'OTHER_REFRESH_%d' % len(refreshes), 'expires_at': time.time() - 10})
            return 400, {'error': 'invalid_grant'}, {}
        self.fake.routes[('POST', '/token')] = token_endpoint
        with self.assertRaises(spotify.SpotifyError) as failure: self.client.get('/me')
        self.assertEqual(failure.exception.status, 401)
        self.assertEqual(refreshes, ['REFRESH_SECRET', 'OTHER_REFRESH_1'])
        self.assertTrue(json.loads(self.path.read_text())['authorization_required'])

    def test_ordinary_api_call_does_not_take_credential_lock(self):
        self.token()
        self.fake.reply('GET', '/v1/me', payload={'ok': True})
        with patch.object(spotify, 'token_file_lock', side_effect=AssertionError('API call took token lock')):
            self.assertEqual(self.client.get('/me'), {'ok': True})

    def test_token_lock_excludes_other_processes_and_allows_nesting(self):
        code = '''
import sys
import spotify_client as spotify
try:
    with spotify.token_file_lock(sys.argv[1], timeout=.1): print('ACQUIRED')
except spotify.SpotifyError:
    print('LOCKED')
'''
        args = [sys.executable, '-c', code, str(self.path)]
        with spotify.token_file_lock(self.path), spotify.token_file_lock(self.path):
            result = subprocess.run(args, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stdout.strip(), 'LOCKED')
        result = subprocess.run(args, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stdout.strip(), 'ACQUIRED')

    def test_token_lock_survives_atomic_replacement_of_a_symlink(self):
        self.token()
        target = self.path.with_name('original-token.json')
        self.path.rename(target); self.path.symlink_to(target.name)
        directory_alias = self.path.parent / 'directory-alias'
        directory_alias.symlink_to(self.path.parent, target_is_directory=True)
        paths = (self.path, directory_alias / self.path.name)
        code = '''
import sys
import spotify_client as spotify
try:
    with spotify.token_file_lock(sys.argv[1], timeout=.1): print('ACQUIRED')
except spotify.SpotifyError:
    print('LOCKED')
'''
        def attempt(path):
            return subprocess.run([sys.executable, '-c', code, str(path)], cwd=Path(__file__).parent,
                                  capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        with spotify.token_file_lock(self.path):
            self.token()
            self.assertFalse(self.path.is_symlink())
            for path in paths:
                with self.subTest(path=path): self.assertEqual(attempt(path), 'LOCKED')
        for path in paths:
            with self.subTest(path=path): self.assertEqual(attempt(path), 'ACQUIRED')

    def test_untrusted_paths_redirects_and_bad_responses_are_bounded(self):
        self.token()
        for path in ('https://example.com/v1/me', '//example.com/me', '/me?token=bad', None):
            with self.assertRaises(spotify.SpotifyError) as failure:
                self.client.get(path)
            self.assertEqual(failure.exception.status, 400)
        self.assertEqual(self.fake.requests, [])
        self.fake.reply('GET', '/v1/me', status=302, headers={'Location': self.fake.url + '/stolen'})
        with self.assertRaises(spotify.SpotifyError) as failure:
            self.client.get('/me')
        self.assertEqual(failure.exception.status, 302)
        self.assertEqual(len(self.fake.requests), 1)
        for status, body in ((500, {'error': 'ACCESS_SECRET' * 1000}), (200, b'ACCESS_SECRET not JSON')):
            self.fake.reply('GET', '/v1/me', status=status, payload=body)
            with self.assertRaises(spotify.SpotifyError) as failure:
                self.client.get('/me')
            self.assertLess(len(failure.exception.message), 160)
            self.assertNotIn('SECRET', failure.exception.message + repr(self.logs))


if __name__ == '__main__':
    unittest.main()
