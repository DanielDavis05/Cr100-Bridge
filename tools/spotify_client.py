"""Small read-only Spotify Web API client with secret-free PKCE token refresh.

Official protocol references (checked 2026-10-31):
https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens
https://developer.spotify.com/documentation/web-api/concepts/rate-limits

Only explicit API paths are accepted; pagination URLs and HTTP redirects cannot
send a Bearer token to another host. Errors and logs never include response
bodies, authorization codes, tokens, or request query strings.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = 'https://api.spotify.com/v1'
TOKEN_ENDPOINT = 'https://accounts.spotify.com/api/token'
REQUEST_TIMEOUT = 10
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RATE_WAIT = 5
REFRESH_MARGIN = 30
TOKEN_LOCK_TIMEOUT = REQUEST_TIMEOUT + 5  # let another process finish its HTTP refresh
_TOKEN_LOCKS = threading.local()


class SpotifyError(Exception):
    """A bounded, token-free error suitable for bridge logs and SMAPI faults."""
    def __init__(self, status: int, message: str, *, retry_after: float | None = None, code: str = ''):
        self.status, self.message = int(status), message
        self.retry_after, self.code = retry_after, code
        super().__init__(message)


@contextlib.contextmanager
def token_file_lock(token_file, timeout=TOKEN_LOCK_TIMEOUT):
    """Serialize credential changes across processes, allowing nested callers."""
    token_path = Path(token_file).expanduser()
    # Atomic writes replace this entry, including a symlink, rather than its target.
    path = token_path.parent.resolve() / (token_path.name + '.lock')
    held = getattr(_TOKEN_LOCKS, 'held', None)
    if held is None: held = _TOKEN_LOCKS.held = set()
    key = (os.getpid(), str(path))
    if key in held:
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SpotifyError(503, 'Spotify credentials are busy; try again shortly.') from None
                time.sleep(0.05)
        held.add(key)
        try: yield
        finally: held.remove(key)
    finally:
        os.close(fd)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(headers) -> float:
    try:
        value = float(headers.get('Retry-After', '1'))
        return value if math.isfinite(value) and value >= 0 else 1
    except (TypeError, ValueError):
        return 1


def _json_request(method, url, *, fields=None, headers=None):
    data = urllib.parse.urlencode(fields).encode('ascii') if fields is not None else None
    request_headers = {'Accept': 'application/json', **(headers or {})}
    if data is not None:
        request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SpotifyError(502, 'Spotify returned an oversized response')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise SpotifyError(502, 'Spotify returned invalid JSON') from None
            if not isinstance(result, dict):
                raise SpotifyError(502, 'Spotify returned an invalid response')
            return result
    except urllib.error.HTTPError as error:
        # Spotify's body may echo credentials or arbitrary text. Only inspect a
        # fixed OAuth error identifier, and never propagate or log the body.
        status, code = error.code, ''
        try:
            if fields is not None:
                raw = error.read(8192)
                body = json.loads(raw)
                if isinstance(body, dict) and body.get('error') == 'invalid_grant':
                    status, code = 401, 'invalid_grant'
        except (ValueError, UnicodeError, OSError):
            pass
        finally:
            error.close()
        messages = {
            400: 'Spotify rejected the request',
            401: 'Spotify authorization is required; run spotify_auth.py login',
            403: 'Spotify denied access; check app access and granted scopes',
            404: 'Spotify item is unavailable',
            429: 'Spotify rate limit reached; try again later',
        }
        message = messages.get(status, 'Spotify request failed (HTTP %d)' % status)
        raise SpotifyError(status, message, retry_after=_retry_after(error.headers) if status == 429 else None,
                           code=code) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise SpotifyError(503, 'Spotify could not be reached') from None


def read_token_file(path, client_id):
    """Read credentials without including file content in any exception."""
    try:
        with Path(path).open(encoding='utf-8') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError
        token = json.loads(raw)
        if not isinstance(token, dict) or token.get('client_id') != client_id:
            raise ValueError
        for key in ('access_token', 'refresh_token'):
            value = token.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 16384
                                      or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)):
                raise ValueError
        expiry = float(token.get('expires_at', 0))
        if not math.isfinite(expiry) or not isinstance(token.get('scope', ''), str):
            raise ValueError
        return token
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SpotifyError(401, 'Spotify authorization is required; run spotify_auth.py login') from None


def write_token_file(path, token):
    """Atomically replace credentials with a mode-0600 file in the same directory."""
    path = Path(path).expanduser()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(token, target, separators=(',', ':'))
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SpotifyError(500, 'Spotify credentials could not be saved') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def save_token_response(path, client_id, response, previous=None):
    """Validate an OAuth reply and retain the old refresh token if omitted."""
    previous = previous or {}
    access, refresh = response.get('access_token'), response.get('refresh_token', previous.get('refresh_token'))
    try:
        seconds = float(response.get('expires_in', 0))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        for value in (access, refresh):
            if not isinstance(value, str) or not value or len(value) > 16384 or any(
                    ord(ch) < 33 or ord(ch) > 126 for ch in value):
                raise ValueError
        if response.get('token_type', 'Bearer').lower() != 'bearer':
            raise ValueError
        scope = response.get('scope', previous.get('scope', ''))
        if not isinstance(scope, str):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SpotifyError(502, 'Spotify returned invalid credentials') from None
    token = {'client_id': client_id, 'access_token': access, 'refresh_token': refresh,
             'expires_at': time.time() + seconds, 'scope': scope}
    write_token_file(path, token)
    return token


class SpotifyClient:
    def __init__(self, client_id: str, token_file: str | Path, log=print):
        self.client_id, self.token_file, self.log = client_id, Path(token_file).expanduser(), log
        self.lock = threading.RLock()
        self.rate_limit_until = 0.0

    def _refresh(self, token):
        with token_file_lock(self.token_file):
            current = read_token_file(self.token_file, self.client_id)
            # Another process may have refreshed while this caller waited.
            if current != token and current.get('access_token') and float(current.get('expires_at', 0)) > time.time() + REFRESH_MARGIN:
                return current
            token = current
            for attempt in range(2):
                refresh = token.get('refresh_token')
                if not refresh:
                    raise SpotifyError(401, 'Spotify authorization is required; run spotify_auth.py login')
                try:
                    response = _json_request('POST', TOKEN_ENDPOINT,
                                             fields={'grant_type': 'refresh_token', 'refresh_token': refresh,
                                                     'client_id': self.client_id})
                except SpotifyError as error:
                    if error.code == 'invalid_grant':
                        current = read_token_file(self.token_file, self.client_id)
                        if not attempt and current.get('refresh_token') and current['refresh_token'] != refresh:
                            token = current
                            continue
                        write_token_file(self.token_file, {'client_id': self.client_id, 'authorization_required': True})
                    if error.status == 429:
                        self.rate_limit_until = time.monotonic() + (error.retry_after or 1)
                    self.log({'event': 'spotify-token-refresh-failed', 'status': error.status})
                    raise
                updated = save_token_response(self.token_file, self.client_id, response, previous=token)
                self.log({'event': 'spotify-token-refreshed'})
                return updated

    def get(self, path: str, params: dict | None = None) -> dict:
        """Read one API resource; retry one expired token and one short rate limit.

        Paths are relative to /v1 (for example '/me/playlists'). A Retry-After
        above five seconds is returned immediately as SpotifyError(429), with a
        local cooldown preventing premature repeated requests.
        """
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise SpotifyError(401, 'Set spotify_music_service.client_id, then run spotify_auth.py login')
        if not isinstance(path, str):
            raise SpotifyError(400, 'Expected a Spotify API path without a query string')
        parsed = urllib.parse.urlsplit(path)
        if (not path.startswith('/') or path.startswith('//') or '\\' in path
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in path)
                or parsed.scheme or parsed.netloc or parsed.fragment or parsed.query):
            raise SpotifyError(400, 'Expected a Spotify API path without a query string')
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = API_ROOT.rstrip('/') + path + ('?' + query if query else '')
        with self.lock:
            if self.rate_limit_until > time.monotonic():
                raise SpotifyError(429, 'Spotify rate limit reached; try again later',
                                   retry_after=self.rate_limit_until - time.monotonic())
            token = read_token_file(self.token_file, self.client_id)
            if not token.get('access_token') or float(token.get('expires_at', 0)) <= time.time() + REFRESH_MARGIN:
                token = self._refresh(token)
            refreshed, rate_retried = False, False
            while True:
                try:
                    return _json_request('GET', url, headers={'Authorization': 'Bearer ' + token['access_token']})
                except SpotifyError as error:
                    if error.status == 401 and not refreshed:
                        token = self._refresh(token)
                        refreshed = True
                        continue
                    if error.status == 429:
                        delay = error.retry_after if error.retry_after is not None else 1
                        self.rate_limit_until = time.monotonic() + delay
                        if not rate_retried and delay <= MAX_RATE_WAIT:
                            rate_retried = True
                            time.sleep(delay)
                            continue
                    self.log({'event': 'spotify-api-error', 'status': error.status})
                    raise
