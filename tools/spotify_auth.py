#!/usr/bin/env python3
"""Authorize the bridge's Spotify catalog through PKCE without a client secret.

Register http://127.0.0.1:8888/callback in the Spotify app's redirect allowlist.
Run on the Pi with --no-browser and forward port 8888 through SSH, or run on
the Mac with --client-id / --token-file and copy that token file to the Pi.
Only library, playlist, artist-follow and profile read scopes are requested;
native Sonos playback does not require Spotify player-control scopes.

References checked 2026-10-31:
https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
https://developer.spotify.com/documentation/web-api/concepts/scopes
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import math
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

from music_service_registry import provider_settings
from spotify_client import SpotifyError, _json_request, read_token_file, save_token_response, token_file_lock

AUTHORIZE_ENDPOINT = 'https://accounts.spotify.com/authorize'
TOKEN_ENDPOINT = 'https://accounts.spotify.com/api/token'
DEFAULT_REDIRECT_URI = 'http://127.0.0.1:8888/callback'
DEFAULT_SCOPES = ('user-library-read', 'playlist-read-private', 'playlist-read-collaborative',
                  'user-follow-read', 'user-read-private')


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    return verifier, challenge


def authorization_url(client_id, redirect_uri, state, challenge):
    return AUTHORIZE_ENDPOINT + '?' + urllib.parse.urlencode({
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri, 'state': state,
        'scope': ' '.join(DEFAULT_SCOPES), 'code_challenge_method': 'S256', 'code_challenge': challenge})


class CallbackServer:
    """One timed loopback callback. Invalid state never completes authorization."""
    def __init__(self, redirect_uri, state):
        try:
            parsed = urllib.parse.urlsplit(redirect_uri)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not parsed.path.startswith('/')):
                raise ValueError
            port = parsed.port
            if port is None:
                raise ValueError
        except (ValueError, TypeError):
            raise SpotifyError(400, 'Redirect URI must be http://127.0.0.1:PORT/callback') from None
        self.state, self.code, self.denied = state, None, False
        callback = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.urlsplit(self.path)
                values = urllib.parse.parse_qs(query.query, keep_blank_values=True)
                states = values.get('state', [])
                valid_state = len(states) == 1 and hmac.compare_digest(
                    states[0].encode('utf-8'), callback.state.encode('ascii'))
                codes, errors = values.get('code', []), values.get('error', [])
                if query.path != parsed.path:
                    status, message = 404, 'Unknown callback path.'
                elif not valid_state:
                    status, message = 400, 'Authorization state did not match. Return to the authorization link.'
                elif len(errors) == 1 and not codes:
                    callback.denied = True
                    status, message = 200, 'Spotify authorization was declined. You may close this window.'
                elif len(codes) == 1 and 0 < len(codes[0]) <= 8192 and not errors:
                    callback.code = codes[0]
                    status, message = 200, 'Spotify authorization received. You may close this window.'
                else:
                    status, message = 400, 'The callback did not contain an authorization code.'
                body = message.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # Callback URLs contain the authorization code.

            def setup(self):
                super().setup()
                self.connection.settimeout(2)

        try:
            self.server = http.server.HTTPServer(('127.0.0.1', port), Handler)
        except OSError:
            raise SpotifyError(503, 'Spotify callback port is unavailable; close the other listener or choose a port') from None
        actual_port = self.server.server_address[1]
        self.redirect_uri = urllib.parse.urlunsplit(('http', '127.0.0.1:%d' % actual_port, parsed.path, '', ''))

    def wait(self, timeout=180):
        if not math.isfinite(timeout) or timeout <= 0:
            raise SpotifyError(400, 'Authorization timeout must be positive')
        deadline = time.monotonic() + timeout
        while self.code is None and not self.denied:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SpotifyError(408, 'Spotify authorization timed out; run login again')
            self.server.timeout = min(1, remaining)
            self.server.handle_request()
        if self.denied:
            raise SpotifyError(401, 'Spotify authorization was declined')
        return self.code

    def close(self):
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def login(client_id, token_file, redirect_uri=DEFAULT_REDIRECT_URI, *, no_browser=False, timeout=180, output=print):
    if not isinstance(client_id, str) or not client_id.strip():
        raise SpotifyError(400, 'Set spotify_music_service.client_id before connecting Spotify')
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    with CallbackServer(redirect_uri, state) as callback:
        url = authorization_url(client_id, callback.redirect_uri, state, challenge)
        output('Open this Spotify authorization link in your browser:\n' + url)
        if not no_browser:
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                pass  # The printed link also works when no browser is installed.
        code = callback.wait(timeout)
        with token_file_lock(token_file):
            response = _json_request('POST', TOKEN_ENDPOINT, fields={
                'grant_type': 'authorization_code', 'client_id': client_id, 'code': code,
                'redirect_uri': callback.redirect_uri, 'code_verifier': verifier})
            save_token_response(token_file, client_id, response)
    output('Spotify connected. Credentials saved to ' + str(Path(token_file).expanduser()))


def status(client_id, token_file):
    try:
        token = read_token_file(token_file, client_id)
    except SpotifyError:
        return {'authorized': False, 'action': 'Run spotify_auth.py login'}
    authorized = bool(token.get('refresh_token'))
    return {'authorized': authorized, 'access_token_expired': float(token.get('expires_at', 0)) <= time.time(),
            'expires_at': token.get('expires_at', 0), 'scopes': token.get('scope', '').split()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('login', 'status'), nargs='?', default='login')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'bridge-config.json')
    parser.add_argument('--client-id')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--redirect-uri')
    parser.add_argument('--no-browser', action='store_true', help='Print the link; useful over an SSH port-8888 tunnel')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(argv)
    try:
        if args.config.exists():
            settings = provider_settings(json.loads(args.config.read_text()), 'spotify')
        else:
            settings = {}
        client_id = args.client_id or settings.get('client_id', '')
        token_file = args.token_file or Path(settings.get('token_file', 'spotify-token.json'))
        token_file = token_file.expanduser()
        if not token_file.is_absolute():
            token_file = args.config.resolve().parent / token_file
        redirect_uri = args.redirect_uri or settings.get('redirect_uri', DEFAULT_REDIRECT_URI)
        if args.command == 'status':
            print(json.dumps(status(client_id, token_file), indent=2))
        else:
            login(client_id, token_file, redirect_uri, no_browser=args.no_browser, timeout=args.timeout)
        return 0
    except SpotifyError as error:
        print(error.message, file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, AttributeError):
        print('Could not read Spotify configuration; check spotify_music_service settings', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Spotify login cancelled', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
