"""Native Spotify metadata and finite playback for bridge-owned Autoplay.

This module never changes Spotify account preferences. The optional librespot
dependency runs in an isolated, bounded helper process; importing or constructing
the adapter performs no authentication or network requests. Credentials stay in
memory/private pipes and are never included in errors or logs.
"""
from __future__ import annotations

import hashlib
from collections import deque
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from backends import BackendError
from spotify_client import _NoRedirect
from spotify_folders import FOLDER_CLIENT_ID
from sonos_upnp import soap as sonos_soap

TRACK = re.compile(r'spotify:track:[A-Za-z0-9]{22}\Z')
DEVICE = re.compile(r'[A-Fa-f0-9]{40}\Z')
CLIENT = re.compile(r'[A-Fa-f0-9]{32}\Z')
HTTP_TIMEOUT = 12
SESSION_TIMEOUT = 30
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HISTORY = 100
MAX_RECOMMENDATIONS = 200
MAX_EMPTY_PAGES = 8
MAX_STREAM_HISTORY = 100000
DEFAULT_DEVICE_ID = hashlib.sha1(b'cr100-bridge-spotify-continuation').hexdigest()


class SpotifyConnectError(BackendError):
    """A fixed, credential-free playback error suitable for controller notices."""
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def _track(uri):
    if not isinstance(uri, str) or not TRACK.fullmatch(uri):
        raise SpotifyConnectError('Autoplay requires a Spotify track.')
    return uri


def _base(value):
    """Accept only a literal local address supplied by the Sonos room adapter."""
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        if (parsed.scheme != 'http' or parsed.port != 1400 or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or not address.is_private or address.is_loopback or address.is_multicast
                or address.is_unspecified):
            raise ValueError
        return 'http://' + parsed.netloc
    except (ValueError, TypeError, AttributeError):
        raise SpotifyConnectError('Spotify playback requires a verified local Sonos speaker.') from None


def _proto_string(field, value):
    raw = value.encode('utf-8')
    size, prefix = len(raw), bytearray()
    while size >= 128:
        prefix.append((size & 127) | 128)
        size >>= 7
    prefix.append(size)
    return bytes([field * 8 + 2]) + bytes(prefix) + raw


def _history(values):
    history = []
    for index, uri in enumerate(values):
        if index >= MAX_STREAM_HISTORY:
            raise SpotifyConnectError('Spotify playback history is too large.')
        history.append(_track(uri))
    return history


def _page_target(origin, value):
    """Map native page links onto this stream's pinned Spotify origin only.

    Spotify returns radio-apollo and radio-router v3 ContextPage links, including
    a router link after fetching an apollo page. Context-resolve paths and same-
    origin HTTPS forms are accepted too. No token
    is ever sent to a hostname supplied by a recommendation response.
    """
    try:
        if not isinstance(value, str) or not 0 < len(value) <= 16384 \
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        if parsed.scheme == 'hm':
            if parsed.netloc:
                if parsed.netloc not in ('radio-apollo', 'radio-router', 'context-resolve'):
                    raise ValueError
                path = '/' + parsed.netloc + parsed.path
            else:
                path = parsed.path
        elif parsed.scheme == 'https':
            pinned = urllib.parse.urlsplit(origin)
            if parsed.hostname != pinned.hostname or (parsed.port or 443) != (pinned.port or 443):
                raise ValueError
            path = parsed.path
        elif not parsed.scheme and not parsed.netloc:
            path = parsed.path if parsed.path.startswith('/') else '/' + parsed.path
        else:
            raise ValueError
        decoded = urllib.parse.unquote(path)
        if (not path.startswith(('/radio-apollo/v3/', '/radio-router/v3/', '/context-resolve/v1/'))
                or '\\' in decoded or '//' in decoded
                or any(part in ('.', '..') for part in decoded.split('/'))):
            raise ValueError
        return origin + path + ('?' + parsed.query if parsed.query else '')
    except (TypeError, ValueError, AttributeError):
        raise SpotifyConnectError('Spotify returned an unsafe recommendation page.') from None


class RecommendationStream:
    """One station's incremental pages; [] means exhausted, exceptions mean failed.

    Native station pagination is retained for the entire listening run. Returned
    tracks are deduplicated across pages even if the server's recent-history
    window is shorter. Creating the stream does not authenticate or fetch data.
    """
    def __init__(self, adapter, seed, recent=()):
        self.adapter, self.seed = adapter, _track(seed)
        history = _history(recent)
        self._history = deque(history, maxlen=MAX_HISTORY)
        self._seen = set(history) | {self.seed}
        self._pages = deque()
        self._visited = set()
        self._next = self._origin = None
        self._started, self.exhausted = False, False
        self._failed = False
        self._lock = threading.Lock()

    def _accept(self, response, requested_url=None):
        if 'pages' in response:
            pages = response['pages']
            if not isinstance(pages, list) or len(pages) > 20:
                raise SpotifyConnectError('Spotify returned invalid recommendation pages.')
        elif 'tracks' in response or 'page_url' in response or 'next_page_url' in response:
            pages = [response]
        else:
            raise SpotifyConnectError('Spotify returned invalid recommendation pages.')
        if any(not isinstance(page, dict) for page in pages):
            raise SpotifyConnectError('Spotify returned invalid recommendation pages.')
        # Contexts may inline several ordered pages; next_page_url on their
        # intermediate pages points to a page that is already included.
        for page in reversed(pages):
            self._pages.appendleft((page, requested_url))

    def _fetch(self, url):
        if url in self._visited:
            raise SpotifyConnectError('Spotify repeated a recommendation page.')
        self._visited.add(url)
        self.adapter.prepare()
        try:
            response = self.adapter._request('GET', url, token=self.adapter._token())
        except SpotifyConnectError as error:
            # Native pages sometimes switch to the logical Mercury radio-router
            # service, whose HTTP route returns 404. The same station cursor is
            # served by radio-apollo. Retry that one known alias once, retaining
            # the pinned origin, complete path suffix and opaque query unchanged.
            prefix = self._origin + '/radio-router/v3/'
            if error.http_status != 404 or not url.startswith(prefix):
                raise
            fallback = self._origin + '/radio-apollo/v3/' + url[len(prefix):]
            if fallback in self._visited:
                raise SpotifyConnectError('Spotify repeated a recommendation page.') from None
            self._visited.add(fallback)
            response = self.adapter._request('GET', fallback, token=self.adapter._token())
            url = fallback
        self._accept(response, requested_url=url)

    def next_batch(self, recent=()):
        with self._lock:
            if self._failed:
                raise SpotifyConnectError('The Spotify recommendation stream stopped. Start a new selection.')
            try:
                return self._next_batch(recent)
            except SpotifyConnectError:
                self._failed = True
                raise

    def _next_batch(self, recent):
        history = _history(recent)
        with self.adapter._lock:
            if self.exhausted:
                return []
            self._seen.update(history)
            if history:
                self._history = deque(history, maxlen=MAX_HISTORY)
            if not self._started:
                self.adapter.prepare()
                self._origin = self.adapter._spclient()
                body = _proto_string(1, self.seed) + b''.join(_proto_string(2, uri) for uri in self._history)
                response = self.adapter._request('POST', self._origin + '/context-resolve/v1/autoplay',
                    body=body, token=self.adapter._token(), protobuf=True)
                self._accept(response)
                self._started = True
            empty_pages = 0
            while empty_pages < MAX_EMPTY_PAGES:
                if not self._pages:
                    if self._next is None:
                        self.exhausted = True
                        return []
                    url, self._next = self._next, None
                    self._fetch(url)
                    continue
                page, requested_url = self._pages.popleft()
                empty_pages += 1
                tracks = page.get('tracks')
                if tracks is not None and (not isinstance(tracks, list) or len(tracks) > 1000):
                    raise SpotifyConnectError('Spotify returned invalid recommendation tracks.')
                page_url = page.get('page_url')
                next_url = page.get('next_page_url')
                # Validate response links even before choosing whether to fetch them.
                page_target = _page_target(self._origin, page_url) if page_url not in (None, '') else None
                next_target = _page_target(self._origin, next_url) if next_url not in (None, '') else None
                if not tracks and page_target and page_target != requested_url:
                    self._fetch(page_target)
                    continue
                if tracks is None:
                    raise SpotifyConnectError('Spotify returned invalid recommendation tracks.')
                if tracks and page_target:
                    self._visited.add(page_target)
                if not self._pages:
                    self._next = next_target
                result = []
                for index, track in enumerate(tracks):
                    uri = track.get('uri') if isinstance(track, dict) else None
                    if isinstance(uri, str) and TRACK.fullmatch(uri) and uri not in self._seen:
                        self._seen.add(uri)
                        result.append(uri)
                        if len(result) == MAX_RECOMMENDATIONS:
                            remainder = tracks[index + 1:]
                            if remainder:
                                self._pages.appendleft((dict(page, tracks=remainder), requested_url))
                            break
                if result:
                    self.exhausted = not self._pages and self._next is None
                    return result
            raise SpotifyConnectError('Spotify returned too many empty or repeated recommendation pages.')


class _SessionProcess:
    """Bound SDK login/token work without changing bridge socket/logging globals."""
    def __init__(self, account, oauth_token, device_id):
        self.lock, self.closed = threading.Lock(), False
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--session-worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, close_fds=True)
        try:
            self._rpc({'op': 'login', 'account': account, 'oauth_token': oauth_token, 'device_id': device_id})
        except Exception:
            self.close()
            raise

    def _rpc(self, message):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise SpotifyConnectError('Spotify playback connection is unavailable. Try again.')
            try:
                self.process.stdin.write((json.dumps(message, separators=(',', ':')) + '\n').encode())
                self.process.stdin.flush()
                deadline, raw = time.monotonic() + SESSION_TIMEOUT, bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdout, selectors.EVENT_READ)
                    while b'\n' not in raw:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(max(0, remaining)):
                            raise TimeoutError
                        part = os.read(self.process.stdout.fileno(), 4096)
                        if not part:
                            raise EOFError
                        raw.extend(part)
                        if len(raw) > 65536:
                            raise ValueError
                response = json.loads(raw)
                if not isinstance(response, dict) or response.get('ok') is not True:
                    if isinstance(response, dict) and response.get('error') == 'account':
                        raise SpotifyConnectError('Connect Spotify playback using the same account as the music service.')
                    raise SpotifyConnectError('Spotify playback sign-in failed. Reconnect Spotify playback in bridge setup.')
                return response
            except SpotifyConnectError:
                raise
            except Exception:
                self.closed = True
                if self.process.poll() is None:
                    self.process.terminate()
                raise SpotifyConnectError('Spotify playback connection timed out or closed. Try again.') from None

    def token(self):
        token = self._rpc({'op': 'token'}).get('access_token')
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise SpotifyConnectError('Spotify playback authorization is unavailable. Reconnect Spotify playback.')
        return token

    def close(self):
        # Do not wait for a concurrent 30-second SDK call before shutting down.
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=.5)
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass


class SpotifyConnect:
    def __init__(self, client, folder_provider, *, log=print, device_id=None,
                 session_factory=None, opener=None, soap=None, clock=time.monotonic):
        self.client, self.folders, self.log = client, folder_provider, log
        self.device_id = device_id or DEFAULT_DEVICE_ID
        if not DEVICE.fullmatch(self.device_id):
            raise ValueError('Spotify controller device ID must be 40 hexadecimal characters')
        self._session_factory = session_factory or _SessionProcess
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._soap, self._clock = soap or sonos_soap, clock
        self._lock = threading.RLock()
        self._session = self._signature = None
        self._oauth_token = self._account = None
        self._origin, self._origin_at = None, 0.0
        self._closed = False

    def available(self):
        """Local preflight only; it never imports the SDK or refreshes credentials."""
        try:
            return (self.folders is not None and self.folders._connected()
                    and (self._session_factory is not _SessionProcess
                         or importlib.util.find_spec('librespot') is not None))
        except (AttributeError, ImportError, ValueError):
            return False

    def check_config(self):
        if self._closed:
            raise SpotifyConnectError('Spotify playback has stopped.')
        if self.folders is None or not self.folders._connected():
            raise SpotifyConnectError('Connect Spotify playback in bridge setup to use Autoplay.')
        if self._session_factory is _SessionProcess and importlib.util.find_spec('librespot') is None:
            raise SpotifyConnectError('Install bridge Spotify playback support to use Autoplay.')

    def prepare(self):
        """Authenticate and verify the account before the caller changes a queue."""
        with self._lock:
            self.check_config()
            try:
                # Both methods retain the folder provider's identity cache and refresh lock.
                account = self.folders._canonical_account()
                oauth_token = self.folders._access_token()
                if not isinstance(account, str) or not 0 < len(account) <= 256:
                    raise ValueError
                signature = (account, hashlib.sha256(oauth_token.encode()).digest())
                if self._session is None or signature != self._signature:
                    old, self._session = self._session, None
                    if old is not None:
                        old.close()
                    session = self._session_factory(account, oauth_token, self.device_id)
                    if self._closed:
                        session.close()
                        raise SpotifyConnectError('Spotify playback has stopped.')
                    try:
                        # Login5 may fail independently of AP login. Verify it before
                        # the caller clears a queue or activates a receiver.
                        session.token()
                    except Exception:
                        session.close()
                        raise
                    if self._closed:
                        session.close()
                        raise SpotifyConnectError('Spotify playback has stopped.')
                    self._session, self._signature = session, signature
                    self.log({'event': 'spotify-playback-connected'})
                self._account, self._oauth_token = account, oauth_token
            except SpotifyConnectError:
                raise
            except Exception:
                raise SpotifyConnectError('Spotify playback sign-in failed. Reconnect Spotify playback in bridge setup.') from None

    def _token(self):
        try:
            return self._session.token()
        except Exception:
            # A crashed/timed-out helper must not poison all later Play attempts.
            old, self._session = self._session, None
            self._signature = None
            if old is not None:
                old.close()
            raise SpotifyConnectError('Spotify playback connection is unavailable. Try again.') from None

    def _request(self, method, url, *, fields=None, body=None, token=None, protobuf=False):
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if fields is not None:
            body = urllib.parse.urlencode(fields).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            headers['Content-Type'] = 'application/x-protobuf' if protobuf else 'application/json'
            if not protobuf:
                body = json.dumps(body, separators=(',', ':')).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError
                result = json.loads(raw) if raw else {}
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise SpotifyConnectError('Spotify playback access was denied. Reconnect Spotify playback.',
                                          http_status=status) from None
            if status == 429:
                raise SpotifyConnectError('Spotify playback is busy. Try again later.', http_status=status) from None
            raise SpotifyConnectError('Spotify playback request failed (HTTP %d).' % status,
                                      http_status=status) from None
        except SpotifyConnectError:
            raise
        except Exception:
            raise SpotifyConnectError('Spotify playback could not be reached or returned an invalid response.') from None

    def _spclient(self):
        if self._origin is not None and self._clock() - self._origin_at < 600:
            return self._origin
        response = self._request('GET', 'https://apresolve.spotify.com/?type=spclient')
        try:
            endpoint = response['spclient'][0]
            parsed = urllib.parse.urlsplit('https://' + endpoint)
            if (not parsed.hostname or not parsed.hostname.endswith('.spotify.com')
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError
            self._origin, self._origin_at = 'https://' + parsed.netloc, self._clock()
            return self._origin
        except (KeyError, IndexError, TypeError, ValueError):
            raise SpotifyConnectError('Spotify playback could not find a valid connection server.') from None

    def recommendation_stream(self, seed, recent=()):
        return RecommendationStream(self, seed, recent)

    def recommendations(self, seed, recent=()):
        """Compatibility convenience for callers that only need the first batch."""
        result = self.recommendation_stream(seed, recent).next_batch()
        if not result:
            raise SpotifyConnectError('Spotify did not provide related tracks.')
        return result

    def play_track(self, base, uri):
        base, uri = _base(base), _track(uri)
        with self._lock:
            self.prepare()
            native_token = self._token()
            info = self._request('GET', base + '/spotifyzc?action=getInfo')
            receiver, client_id = info.get('deviceID'), info.get('clientID')
            if (not isinstance(receiver, str) or not DEVICE.fullmatch(receiver)
                    or not isinstance(client_id, str) or not CLIENT.fullmatch(client_id)
                    or str(info.get('brandDisplayName', '')).lower() != 'sonos'):
                raise SpotifyConnectError('This speaker does not provide compatible Spotify playback.')
            try:
                media = self._soap(base, 'AVTransport', 'GetMediaInfo', [('InstanceID', 0)], timeout=HTTP_TIMEOUT)
            except Exception:
                raise SpotifyConnectError('The Sonos speaker could not be reached.') from None
            activated = not str(media.get('CurrentURI', '')).startswith('x-sonos-vli:')
            if activated:
                if self._closed:
                    raise SpotifyConnectError('Spotify playback has stopped.')
                version = info.get('version')
                if info.get('tokenType') != 'authorization_code' or not isinstance(version, str) \
                        or not re.fullmatch(r'[0-9.]{1,24}', version):
                    raise SpotifyConnectError('This speaker does not provide compatible Spotify playback.')
                exchange = self._request('POST', 'https://accounts.spotify.com/api/token', fields={
                    'audience': client_id, 'client_id': FOLDER_CLIENT_ID,
                    'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                    'requested_token_type': 'urn:spotify:params:oauth:authorization_code',
                    'subject_token_type': 'urn:ietf:params:oauth:token-type:access_token',
                    'subject_token': self._oauth_token, 'resource': 'urn:spotify:resources:connect', 'scope': 'streaming'})
                code = exchange.get('access_token')
                if not isinstance(code, str) or not code or len(code) > 16384:
                    raise SpotifyConnectError('Spotify could not activate playback on this speaker.')
                if self._closed:
                    raise SpotifyConnectError('Spotify playback has stopped.')
                result = self._request('POST', base + '/spotifyzc', fields={
                    'action': 'addUser', 'version': version, 'tokenType': 'authorization_code', 'clientKey': '',
                    'loginId': self._account, 'userName': self._account, 'blob': code,
                    'deviceName': 'CR100 bridge', 'deviceId': self.device_id})
                if result.get('status') != 101 or result.get('spotifyError', 0) != 0:
                    raise SpotifyConnectError('Spotify could not activate playback on this speaker.')
            body = {'command': {'endpoint': 'play',
                'context': {'uri': '', 'url': '', 'pages': [{'tracks': [{'uri': uri}]}], 'metadata': {}},
                'play_origin': {'feature_identifier': 'cr100-bridge'},
                'options': {'license': 'on-demand', 'skip_to': {},
                    'player_options_override': {'repeating_context': False, 'repeating_track': False,
                                                'shuffling_context': False}}}}
            if self._closed:
                raise SpotifyConnectError('Spotify playback has stopped.')
            self._request('POST', self._spclient() + '/connect-state/v1/player/command/from/'
                + self.device_id + '/to/' + receiver, body=body, token=native_token)
            return {'device_id': receiver, 'activated': activated}

    def close(self):
        self._closed = True
        session, self._session = self._session, None
        self._oauth_token = self._account = self._signature = None
        if session is not None:
            session.close()


def _session_worker():
    # These process-wide SDK accommodations are deliberately isolated from the bridge.
    import contextlib
    import logging
    import socket
    logging.disable(logging.CRITICAL)
    socket.setdefaulttimeout(HTTP_TIMEOUT)
    session = None
    sink = open(os.devnull, 'w')
    try:
        for raw in sys.stdin.buffer:
            if len(raw) > 65536:
                return
            try:
                request = json.loads(raw)
                operation = request.get('op')
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    if operation == 'login':
                        from librespot.core import Session
                        from librespot.proto import Authentication_pb2 as Authentication
                        configuration = Session.Configuration.Builder().set_store_credentials(False).set_cache_enabled(False).build()
                        builder = Session.Builder(configuration).set_device_id(request['device_id']).set_device_name('CR100 bridge')
                        builder.login_credentials = Authentication.LoginCredentials(username=request['account'],
                            typ=Authentication.AuthenticationType.AUTHENTICATION_SPOTIFY_TOKEN,
                            auth_data=request['oauth_token'].encode())
                        session = builder.create()
                        matches = session.username() == request['account']
                        response = {'ok': True} if matches else {'ok': False, 'error': 'account'}
                    elif operation == 'token' and session is not None:
                        response = {'ok': True, 'access_token': session.tokens().get('app-remote-control')}
                    else:
                        response = {'ok': False, 'error': 'operation'}
            except Exception:
                response = {'ok': False, 'error': 'authentication'}
            print(json.dumps(response, separators=(',', ':')), flush=True)
    finally:
        # Parent enforces a short terminate/kill deadline even if SDK shutdown blocks.
        if session is not None:
            with contextlib.suppress(Exception), contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                session.close()
        sink.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--session-worker']:
        _session_worker()
    else:
        raise SystemExit('This module is used by the bridge.')
