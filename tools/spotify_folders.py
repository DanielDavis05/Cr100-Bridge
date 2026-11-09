"""Read-only Spotify folder mirroring through the client rootlist interface.

Spotify's public Web API does not return folders. This module uses a separate
streaming-only client authorization, while the existing Web API client supplies
the canonical account ID. All library requests are GETs to a fixed Spotify host.
The complete last-good tree is cached privately and shared between processes.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urlencode

from spotify_client import (REFRESH_MARGIN, TOKEN_ENDPOINT, SpotifyError, _json_request,
                            read_token_file, save_token_response, write_token_file)

FOLDER_CLIENT_ID = '65b708073fc0480ea92a077233ca87bd'
FOLDER_REDIRECT_URI = 'http://127.0.0.1:8898/login'
FOLDER_SCOPES = ('streaming',)
ROOTLIST_ORIGIN = 'https://spclient.wg.spotify.com'
PAGE_SIZE = 500
MAX_PAGES = 20
MAX_ENTRIES = PAGE_SIZE * MAX_PAGES
MAX_DEPTH = 32
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 6 * 1024 * 1024
LOCK_TIMEOUT = 5
_FOLDER_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_URI = re.compile(r'spotify:(playlist|album|track):([A-Za-z0-9]{22})\Z')
_OLD_PLAYLIST = re.compile(r'spotify:user:[^:]+:playlist:([A-Za-z0-9]{22})\Z')
_BAD_PERCENT = re.compile(r'%(?![0-9a-fA-F]{2})')


class FolderError(SpotifyError):
    """A fixed, credential-free error suitable for the web interface."""


@contextlib.contextmanager
def _file_lock(path, timeout=LOCK_TIMEOUT):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FolderError(503, 'Spotify folder refresh is already in progress.') from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def folder_token_lock(token_file, timeout=LOCK_TIMEOUT):
    """Use around replacing a folder grant; refreshers use this same lock."""
    return _file_lock(str(token_file) + '.lock', timeout)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(value, target, ensure_ascii=False, separators=(',', ':'))
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
    return value or fallback


def _canonical_uri(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
    old = _OLD_PLAYLIST.fullmatch(value)
    if old:
        value = 'spotify:playlist:' + old[1]
    match = _URI.fullmatch(value)
    return (value, match[1]) if match else (None, None)


def parse_entries(entries):
    """Reconstruct nested folders from ordered start/end markers, rejecting tears."""
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise FolderError(502, 'Spotify returned an oversized folder hierarchy.')
    root, stack, seen = [], [], set()
    stack.append((None, root))
    for item, meta in entries:
        if not isinstance(item, dict) or not isinstance(meta, dict):
            raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
        uri = item.get('uri')
        if not isinstance(uri, str) or len(uri) > 4096:
            raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
        parts = uri.split(':', 3)
        kind = parts[1] if len(parts) > 1 and parts[0] == 'spotify' else ''
        if kind == 'start-group':
            if len(parts) != 4 or not _FOLDER_ID.fullmatch(parts[2]) or parts[2] in seen \
                    or len(stack) > MAX_DEPTH or _BAD_PERCENT.search(parts[3]):
                raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
            try:
                name = unquote_to_bytes(parts[3].replace('+', ' ')).decode('utf-8')
            except (UnicodeError, ValueError):
                raise FolderError(502, 'Spotify returned an invalid folder hierarchy.') from None
            node = {'kind': 'folder', 'id': parts[2], 'name': _name(name, 'Untitled folder'), 'children': []}
            stack[-1][1].append(node)
            stack.append((parts[2], node['children']))
            seen.add(parts[2])
        elif kind == 'end-group':
            if len(parts) != 3 or len(stack) <= 1 or parts[2] != stack[-1][0]:
                raise FolderError(502, 'Spotify returned an incomplete folder hierarchy.')
            stack.pop()
        else:
            canonical, item_kind = _canonical_uri(uri)
            if canonical is None:
                # The rootlist can also contain nonmusic shortcuts. They are not
                # supported by this service and do not change folder boundaries.
                if not kind:
                    raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
                continue
            attributes = meta.get('attributes') or {}
            if not isinstance(attributes, dict):
                raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
            stack[-1][1].append({'kind': item_kind, 'uri': canonical,
                                  'name': _name(attributes.get('name'), canonical)})
    if len(stack) != 1:
        raise FolderError(502, 'Spotify returned an incomplete folder hierarchy.')
    return root


def _validate_cached_tree(tree):
    """Cache files are bounded and validated before they reach browse handlers."""
    if not isinstance(tree, list):
        raise ValueError
    count, seen = 0, set()
    pending = [(tree, 0)]
    while pending:
        children, depth = pending.pop()
        if depth > MAX_DEPTH or not isinstance(children, list):
            raise ValueError
        for node in children:
            count += 1
            if count > MAX_ENTRIES or not isinstance(node, dict):
                raise ValueError
            if not isinstance(node.get('name'), str):
                raise ValueError
            _name(node['name'], '')
            if node.get('kind') == 'folder':
                ident = node.get('id')
                if not isinstance(ident, str) or not _FOLDER_ID.fullmatch(ident) or ident in seen:
                    raise ValueError
                seen.add(ident)
                pending.append((node.get('children'), depth + 1))
            elif node.get('kind') in ('playlist', 'album', 'track'):
                uri, kind = _canonical_uri(node.get('uri'))
                if not uri or kind != node['kind']:
                    raise ValueError
            else:
                raise ValueError


class SpotifyFolderLibrary:
    def __init__(self, token_file, cache_file, spotify_client, refresh_interval=300, log=print):
        if isinstance(refresh_interval, bool) or not isinstance(refresh_interval, (int, float)) \
                or not math.isfinite(refresh_interval) or not 30 <= refresh_interval <= 3600:
            raise ValueError('Spotify folder refresh interval must be 30–3600 seconds.')
        self.token_file, self.cache_file = Path(token_file), Path(cache_file)
        if self.token_file.resolve() == self.cache_file.resolve():
            raise ValueError('Spotify folder token and cache files must be different.')
        self.spotify_client, self.refresh_interval, self.log = spotify_client, refresh_interval, log
        self.lock, self.refresh_lock = threading.RLock(), threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self._tree, self._revision, self._account_id = [], '', None
        self._updated_at, self._attempted_at, self._retry_at, self._error = None, None, 0.0, None
        self._cache_signature = None
        self._user_id, self._identity_signature = None, None
        self._load_cache()

    def _connected(self):
        try:
            token = read_token_file(self.token_file, FOLDER_CLIENT_ID)
            return not token.get('authorization_required') and bool(token.get('access_token') or token.get('refresh_token'))
        except SpotifyError:
            return False

    def _load_cache(self):
        try:
            stat = self.cache_file.stat()
            if stat.st_size > MAX_CACHE_BYTES:
                raise ValueError
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            with self.lock:
                if signature == self._cache_signature:
                    return
            with self.cache_file.open(encoding='utf-8') as source:
                raw = source.read(MAX_CACHE_BYTES + 1)
            if len(raw) > MAX_CACHE_BYTES:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('schema') != 1:
                raise ValueError
            tree = value['tree']
            _validate_cached_tree(tree)
            revision = value.get('revision', '')
            if not isinstance(revision, str) or revision and not re.fullmatch('[a-f0-9]{64}', revision):
                raise ValueError
            dates = [value.get('updated_at'), value.get('attempted_at'), value.get('retry_at', 0)]
            if any(d is not None and (isinstance(d, bool) or not isinstance(d, (int, float))
                                      or not math.isfinite(d) or d < 0) for d in dates):
                raise ValueError
            expected_revision = hashlib.sha256(json.dumps(tree, ensure_ascii=False, sort_keys=True,
                                                          separators=(',', ':')).encode()).hexdigest()
            if dates[0] is not None and revision != expected_revision or dates[0] is None and (revision or tree):
                raise ValueError
            account = value.get('account_id')
            if account is not None and (not isinstance(account, str) or not 0 < len(account) <= 256):
                raise ValueError
            error = value.get('error')
            if error is not None and (not isinstance(error, str) or len(error) > 200):
                raise ValueError
            with self.lock:
                self._tree, self._revision, self._account_id = tree, revision, account
                self._updated_at, self._attempted_at, self._retry_at = dates
                self._error, self._cache_signature = error, signature
        except (OSError, ValueError, KeyError, TypeError, RecursionError, SpotifyError):
            # Corrupt/incomplete cache files never replace a valid in-memory tree.
            return

    def _save_cache(self):
        with self.lock:
            value = {'schema': 1, 'account_id': self._account_id, 'tree': self._tree,
                     'revision': self._revision, 'updated_at': self._updated_at,
                     'attempted_at': self._attempted_at, 'retry_at': self._retry_at, 'error': self._error}
            _atomic_json(self.cache_file, value)
            stat = self.cache_file.stat()
            self._cache_signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def get_tree(self):
        self._load_cache()
        with self.lock:
            return copy.deepcopy(self._tree)

    def find(self, folder_id):
        if not isinstance(folder_id, str) or not _FOLDER_ID.fullmatch(folder_id):
            return None
        pending = list(self.get_tree())
        while pending:
            node = pending.pop()
            if node.get('kind') == 'folder':
                if node['id'] == folder_id:
                    return node
                pending.extend(node['children'])
        return None

    @property
    def revision(self):
        self._load_cache()
        with self.lock:
            return self._revision

    def status(self):
        self._load_cache()
        with self.lock:
            return {'connected': self._connected(), 'updated_at': self._updated_at,
                    'error': self._error, 'revision': self._revision,
                    'refresh_interval': self.refresh_interval,
                    'stale': bool(self._error) or self._updated_at is None or
                             not 0 <= time.time() - self._updated_at < self.refresh_interval,
                    'refreshing': self.refresh_lock.locked()}

    def _access_token(self, rejected=None):
        with folder_token_lock(self.token_file):
            token = read_token_file(self.token_file, FOLDER_CLIENT_ID)
            if token.get('authorization_required'):
                raise FolderError(401, 'Connect Spotify folders in bridge setup.')
            if token.get('access_token') and float(token.get('expires_at', 0)) > time.time() + REFRESH_MARGIN \
                    and (rejected is None or token['access_token'] != rejected):
                return token['access_token']
            if not token.get('refresh_token'):
                raise FolderError(401, 'Connect Spotify folders in bridge setup.')
            try:
                response = _json_request('POST', TOKEN_ENDPOINT, fields={
                    'grant_type': 'refresh_token', 'refresh_token': token['refresh_token'],
                    'client_id': FOLDER_CLIENT_ID})
            except SpotifyError as error:
                if error.code == 'invalid_grant':
                    write_token_file(self.token_file, {'client_id': FOLDER_CLIENT_ID, 'authorization_required': True})
                raise
            token = save_token_response(self.token_file, FOLDER_CLIENT_ID, response, previous=token)
            self.log({'event': 'spotify-folder-token-refreshed'})
            return token['access_token']

    def _canonical_account(self):
        token_file = getattr(self.spotify_client, 'token_file', None)
        try:
            stat = Path(token_file).stat() if token_file is not None else None
            signature = (stat.st_mtime_ns, stat.st_size) if stat is not None else None
        except OSError:
            signature = None
        if self._user_id is not None and signature == self._identity_signature:
            return self._user_id
        profile = self.spotify_client.get('/me')
        user = profile.get('id') if isinstance(profile, dict) else None
        if not isinstance(user, str) or not 0 < len(user) <= 256 or any(ord(c) < 32 for c in user):
            raise FolderError(502, 'Spotify could not identify the signed-in account.')
        self._user_id, self._identity_signature = user, signature
        return user

    def _fetch_tree(self):
        token = self._access_token()
        user = self._canonical_account()
        endpoint = ROOTLIST_ORIGIN + '/playlist/v2/user/' + quote(user, safe='') + '/rootlist'
        entries, revision, total = [], None, None
        for page in range(MAX_PAGES):
            if self.stop.is_set():
                raise FolderError(503, 'Spotify folder refresh was stopped.')
            offset = len(entries)
            query = urlencode({'decorate': 'revision,attributes,length,owner,capabilities,status_code',
                               'from': offset, 'length': PAGE_SIZE})
            try:
                data = _json_request('GET', endpoint + '?' + query, headers={'Authorization': 'Bearer ' + token})
            except SpotifyError as error:
                if error.status != 401:
                    raise
                token = self._access_token(rejected=token)
                data = _json_request('GET', endpoint + '?' + query, headers={'Authorization': 'Bearer ' + token})
            contents = data.get('contents')
            length = data.get('length')
            if not isinstance(contents, dict) or type(length) is not int or not 0 <= length <= MAX_ENTRIES:
                raise FolderError(502, 'Spotify returned an invalid folder hierarchy.')
            key = json.dumps(data.get('revision'), sort_keys=True, separators=(',', ':'))
            if page == 0:
                revision, total = key, length
            elif key != revision or length != total or key == 'null':
                raise FolderError(502, 'Spotify folders changed during refresh. Try again.')
            batch = contents.get('items')
            metas = contents.get('metaItems', contents.get('meta_items', [])) or []
            truncated = contents.get('truncated', False)
            if not isinstance(batch, list) or len(batch) > PAGE_SIZE or not isinstance(metas, list) \
                    or len(metas) > len(batch) or type(contents.get('pos', offset)) is not int \
                    or contents.get('pos', offset) != offset or type(truncated) is not bool:
                raise FolderError(502, 'Spotify returned an invalid folder page.')
            entries.extend((item, metas[i] if i < len(metas) else {}) for i, item in enumerate(batch))
            if not truncated:
                if len(entries) != total:
                    raise FolderError(502, 'Spotify returned an incomplete folder hierarchy.')
                return user, parse_entries(entries)
            if not batch or len(entries) >= total:
                raise FolderError(502, 'Spotify returned an incomplete folder hierarchy.')
        raise FolderError(502, 'Spotify returned an oversized folder hierarchy.')

    @staticmethod
    def _safe_error(error):
        if error.status in (401, 403):
            return FolderError(error.status, 'Reconnect Spotify folders with the same Spotify account.')
        if error.status == 429:
            return FolderError(429, 'Spotify is busy. Try refreshing folders later.', retry_after=error.retry_after)
        if isinstance(error, FolderError):
            return error
        return FolderError(503, 'Spotify folders could not be refreshed. The last saved folders are still available.')

    def refresh(self, force=True):
        requested_at = time.time()
        if not self.refresh_lock.acquire(timeout=LOCK_TIMEOUT):
            raise FolderError(503, 'Spotify folder refresh is already in progress.')
        try:
            with _file_lock(str(self.cache_file) + '.lock'):
                self._load_cache()
                now = time.time()
                with self.lock:
                    if self._retry_at > now:
                        raise FolderError(429, 'Spotify is busy. Try refreshing folders later.',
                                          retry_after=self._retry_at - now)
                    if self._updated_at is not None and requested_at <= self._updated_at <= now:
                        return copy.deepcopy(self._tree)
                    if not force and self._attempted_at is not None and \
                            0 <= now - self._attempted_at < (min(30, self.refresh_interval) if self._error else self.refresh_interval):
                        return copy.deepcopy(self._tree)
                try:
                    account, tree = self._fetch_tree()
                    encoded = json.dumps(tree, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()
                    if len(encoded) > MAX_TREE_BYTES:
                        raise FolderError(502, 'Spotify returned an oversized folder hierarchy.')
                    revision = hashlib.sha256(encoded).hexdigest()
                    with self.lock:
                        self._account_id, self._tree, self._revision = account, tree, revision
                        self._updated_at = self._attempted_at = time.time()
                        self._retry_at, self._error = 0.0, None
                    self._save_cache()
                    self.log({'event': 'spotify-folders-refreshed', 'revision': revision})
                    return copy.deepcopy(tree)
                except (SpotifyError, OSError, ValueError, TypeError, RecursionError) as error:
                    safe = self._safe_error(error) if isinstance(error, SpotifyError) else FolderError(
                        503, 'Spotify folders could not be refreshed. The last saved folders are still available.')
                    with self.lock:
                        self._attempted_at, self._error = time.time(), safe.message
                        self._retry_at = self._attempted_at + max(1, safe.retry_after or 1) if safe.status == 429 else 0.0
                    try:
                        self._save_cache()
                    except OSError:
                        pass
                    self.log({'event': 'spotify-folders-refresh-failed', 'status': safe.status})
                    raise safe from None
        finally:
            self.refresh_lock.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        def run():
            while not self.stop.is_set():
                if self._connected():
                    try:
                        self.refresh(force=False)
                    except FolderError:
                        pass
                self.stop.wait(min(30, self.refresh_interval))
        self.thread = threading.Thread(target=run, name='spotify-folder-sync', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(0.25)


def make_folder_provider(config_path, settings, spotify_client, log=print):
    config_path = Path(config_path)
    folders = settings.get('folders') or {}
    if not isinstance(folders, dict):
        raise ValueError('Spotify folder settings must be an object.')
    def resolve(value, default):
        path = Path(value or default).expanduser()
        return path if path.is_absolute() else config_path.resolve().parent / path
    return SpotifyFolderLibrary(resolve(folders.get('token_file'), 'spotify-folders-token.json'),
                                resolve(folders.get('cache_file'), 'spotify-folders-cache.json'),
                                spotify_client, folders.get('refresh_interval', 300), log=log)
