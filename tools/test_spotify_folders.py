"""Folder synchronization, authentication isolation, cache and protocol checks."""
import copy
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

import spotify_folders as folders
from spotify_client import SpotifyError, write_token_file

PLAYLIST = 'P' * 22
ALBUM = 'A' * 22
TRACK = 'T' * 22


class Client:
    def __init__(self, ident='synthetic-user'):
        self.ident, self.calls = ident, []

    def get(self, path):
        self.calls.append(path)
        return {'id': self.ident}


def page(uris=None, names=None, *, pos=0, total=None, truncated=False, revision='revision-1'):
    uris = uris if uris is not None else ['spotify:start-group:root:Test+Root+Folder',
                                         'spotify:playlist:' + PLAYLIST, 'spotify:end-group:root']
    names = names or {}
    return {'revision': revision, 'length': len(uris) if total is None else total,
            'contents': {'pos': pos, 'truncated': truncated,
                         'items': [{'uri': uri} for uri in uris],
                         'metaItems': [{'attributes': {'name': names[uri]}} if uri in names else {} for uri in uris]}}


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.token = self.base / 'folders-token.json'
        self.cache = self.base / 'folders-cache.json'
        self.logs, self.requests, self.client = [], [], Client()
        self.response = page(names={'spotify:playlist:' + PLAYLIST: 'Test Playlist'})
        self.token_value()
        self.provider = self.make()
        self.patch = patch.object(folders, '_json_request', side_effect=self.request)
        self.patch.start()

    def tearDown(self):
        self.provider.close()
        self.patch.stop()
        self.temp.cleanup()

    def make(self, **kwargs):
        return folders.SpotifyFolderLibrary(kwargs.pop('token_file', self.token),
                                             kwargs.pop('cache_file', self.cache),
                                             kwargs.pop('client', self.client), log=self.logs.append, **kwargs)

    def token_value(self, expired=False):
        write_token_file(self.token, {'client_id': folders.FOLDER_CLIENT_ID,
                                     'access_token': 'ACCESS_SECRET', 'refresh_token': 'REFRESH_SECRET',
                                     'expires_at': time.time() + (-100 if expired else 3600), 'scope': 'streaming'})

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == 'POST':
            return {'access_token': 'NEW_ACCESS_SECRET', 'token_type': 'Bearer', 'expires_in': 3600}
        if isinstance(self.response, Exception):
            raise self.response
        if callable(self.response):
            return self.response(method, url, **kwargs)
        return copy.deepcopy(self.response)

    def get_count(self):
        return len([r for r in self.requests if r[0] == 'GET'])

    def test_real_shape_nested_names_order_and_old_playlist_uri(self):
        uris = ['spotify:start-group:root:Test+Root+Folder', 'spotify:playlist:' + PLAYLIST,
                'spotify:start-group:nested:%F0%9F%8E%B5+Jazz', 'spotify:album:' + ALBUM,
                'spotify:track:' + TRACK, 'spotify:end-group:nested',
                'spotify:user:old-user:playlist:' + PLAYLIST, 'spotify:end-group:root']
        self.response = page(uris, {'spotify:playlist:' + PLAYLIST: 'Original title'})
        tree = self.provider.refresh()
        self.assertEqual(tree[0]['name'], 'Test Root Folder')
        self.assertEqual([n['kind'] for n in tree[0]['children']], ['playlist', 'folder', 'playlist'])
        self.assertEqual(tree[0]['children'][1]['name'], '🎵 Jazz')
        self.assertEqual([n['kind'] for n in tree[0]['children'][1]['children']], ['album', 'track'])
        self.assertEqual(tree[0]['children'][2]['uri'], 'spotify:playlist:' + PLAYLIST)
        self.assertEqual(self.client.calls, ['/me'])
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.provider.status()['connected'])
        self.assertFalse(self.provider.status()['stale'])
        self.assertEqual(len(self.provider.revision), 64)
        tree[0]['children'].clear()
        self.assertEqual(len(self.provider.find('root')['children']), 3)
        self.assertIsNone(self.provider.find('../token'))

    def test_only_fixed_get_endpoint_uses_separate_token_and_canonical_id(self):
        self.client.ident = 'user /with?characters'
        self.provider.refresh()
        method, url, args = self.requests[0]
        self.assertEqual(method, 'GET')
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.hostname, 'spclient.wg.spotify.com')
        self.assertIn('/user/user%20%2Fwith%3Fcharacters/rootlist', parsed.path)
        self.assertEqual(args['headers']['Authorization'], 'Bearer ACCESS_SECRET')
        self.assertEqual(urllib.parse.parse_qs(parsed.query)['length'], ['500'])
        self.assertNotIn('SECRET', self.cache.read_text())
        self.assertNotIn('SECRET', repr(self.logs))

    def test_revision_only_changes_for_visible_tree_mutations(self):
        self.provider.refresh(); first = self.provider.revision
        self.response['revision'] = 'other-source-revision'
        self.provider.refresh()
        self.assertEqual(self.provider.revision, first)
        self.response['contents']['metaItems'][1]['attributes']['name'] = 'Renamed'
        self.provider.refresh()
        self.assertNotEqual(self.provider.revision, first)
        self.assertEqual(self.client.calls, ['/me'])

    def test_folders_can_span_pages_and_meta_items_alternative_key(self):
        uris = ['spotify:start-group:root:Root', 'spotify:playlist:' + PLAYLIST,
                'spotify:track:' + TRACK, 'spotify:end-group:root']
        def response(method, url, **kwargs):
            offset = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['from'][0])
            d = page(uris[offset:offset + 2], pos=offset, total=4, truncated=offset == 0)
            d['contents']['meta_items'] = d['contents'].pop('metaItems')
            return d
        self.response = response
        with patch.object(folders, 'PAGE_SIZE', 2):
            tree = self.provider.refresh()
        self.assertEqual(self.get_count(), 2)
        self.assertEqual([x['kind'] for x in tree[0]['children']], ['playlist', 'track'])

    def test_mutation_between_pages_preserves_last_good_snapshot(self):
        old = self.provider.refresh(); revision = self.provider.revision
        def response(method, url, **kwargs):
            offset = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['from'][0])
            return page(['spotify:playlist:' + PLAYLIST], pos=offset, total=2,
                        truncated=offset == 0, revision='rev-' + str(offset))
        self.response = response
        with patch.object(folders, 'PAGE_SIZE', 1):
            with self.assertRaisesRegex(folders.FolderError, 'changed during refresh'):
                self.provider.refresh()
        self.assertEqual(self.provider.get_tree(), old)
        self.assertEqual(self.provider.revision, revision)
        other = self.make()
        self.assertEqual(other.get_tree(), old)
        self.assertIn('changed during refresh', other.status()['error'])

    def test_malformed_trees_never_replace_snapshot(self):
        old = self.provider.refresh(); revision = self.provider.revision
        malformed = [
            ['spotify:start-group:root:Unclosed'],
            ['spotify:end-group:root'],
            ['spotify:start-group:root:A', 'spotify:end-group:wrong'],
            ['spotify:start-group:root:A', 'spotify:end-group:root',
             'spotify:start-group:root:B', 'spotify:end-group:root'],
            ['spotify:start-group:root:%GG', 'spotify:end-group:root'],
            ['spotify:start-group:root:%FF', 'spotify:end-group:root'],
            ['not-a-spotify-uri'],
        ]
        for uris in malformed:
            with self.subTest(uris=uris):
                self.response = page(uris)
                with self.assertRaises(folders.FolderError): self.provider.refresh()
                self.assertEqual(self.provider.get_tree(), old)
                self.assertEqual(self.provider.revision, revision)
        deep = ['spotify:start-group:g%d:Folder' % n for n in range(folders.MAX_DEPTH + 1)]
        deep += ['spotify:end-group:g%d' % n for n in reversed(range(folders.MAX_DEPTH + 1))]
        self.response = page(deep)
        with self.assertRaises(folders.FolderError): self.provider.refresh()

    def test_invalid_page_lengths_offsets_and_truncation(self):
        old = self.provider.refresh()
        cases = [page(total=99), page(pos=1), page([], total=1, truncated=True), page(total=True)]
        invalid = page(); invalid['contents']['truncated'] = 'false'; cases.append(invalid)
        for doc in cases:
            with self.subTest(doc=doc):
                self.response = doc
                with self.assertRaises(folders.FolderError): self.provider.refresh()
                self.assertEqual(self.provider.get_tree(), old)

    def test_missing_or_wrong_grant_never_fetches_library(self):
        self.token.unlink()
        self.assertFalse(self.provider.status()['connected'])
        with self.assertRaises(folders.FolderError): self.provider.refresh()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.client.calls, [])
        write_token_file(self.token, {'client_id': 'wrong', 'access_token': 'ACCESS_SECRET',
                                     'refresh_token': 'REFRESH_SECRET', 'expires_at': time.time() + 1000})
        with self.assertRaises(folders.FolderError): self.provider.refresh()
        self.assertEqual(self.requests, [])

    def test_refresh_preserves_grant_and_private_mode(self):
        self.token_value(expired=True); os.chmod(self.token, 0o644)
        self.provider.refresh()
        self.assertEqual([r[0] for r in self.requests], ['POST', 'GET'])
        token_request = self.requests[0]
        self.assertEqual(token_request[1], folders.TOKEN_ENDPOINT)
        self.assertEqual(token_request[2]['fields'], {'grant_type': 'refresh_token',
                         'refresh_token': 'REFRESH_SECRET', 'client_id': folders.FOLDER_CLIENT_ID})
        self.assertEqual(self.requests[1][2]['headers']['Authorization'], 'Bearer NEW_ACCESS_SECRET')
        stored = json.loads(self.token.read_text())
        self.assertEqual(stored['refresh_token'], 'REFRESH_SECRET')
        self.assertEqual(stored['scope'], 'streaming')
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('SECRET', repr(self.logs))

    def test_rejected_access_token_refreshes_once(self):
        calls = []
        def response(method, url, **kwargs):
            calls.append(kwargs['headers']['Authorization'])
            if len(calls) == 1: raise SpotifyError(401, 'token rejected')
            return page()
        self.response = response
        self.provider.refresh()
        self.assertEqual(calls, ['Bearer ACCESS_SECRET', 'Bearer NEW_ACCESS_SECRET'])
        self.assertEqual(len([r for r in self.requests if r[0] == 'POST']), 1)

    def test_server_errors_are_sanitized_and_rate_limit_is_respected(self):
        old = self.provider.refresh()
        self.response = SpotifyError(503, 'echo ACCESS_SECRET REFRESH_SECRET')
        with self.assertRaises(folders.FolderError): self.provider.refresh()
        self.assertEqual(self.provider.get_tree(), old)
        self.assertNotIn('SECRET', self.cache.read_text())
        self.assertNotIn('SECRET', repr(self.provider.status()))
        self.response = SpotifyError(429, 'echo ACCESS_SECRET', retry_after=120)
        with self.assertRaises(folders.FolderError): self.provider.refresh()
        count = self.get_count()
        with self.assertRaises(folders.FolderError) as failure: self.provider.refresh(force=True)
        self.assertEqual(failure.exception.status, 429)
        self.assertEqual(self.get_count(), count)

    def test_shared_cache_concurrent_refresh_is_coalesced(self):
        other = self.make(client=Client())
        entered, release = threading.Event(), threading.Event()
        def response(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(3)); return page()
        self.response = response
        results, failures = [], []
        def refresh(provider):
            try: results.append(provider.refresh())
            except BaseException as error: failures.append(error)
        first = threading.Thread(target=refresh, args=(self.provider,))
        second = threading.Thread(target=refresh, args=(other,))
        first.start(); self.assertTrue(entered.wait(2)); second.start()
        time.sleep(.1); release.set(); first.join(3); second.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.get_count(), 1)

    def test_shared_token_refresh_is_serialized_even_with_different_caches(self):
        self.token_value(expired=True)
        other = self.make(cache_file=self.base / 'another-cache.json', client=Client())
        original = self.request
        def request(method, url, **kwargs):
            if method == 'POST': time.sleep(.1)
            return original(method, url, **kwargs)
        results, failures = [], []
        def run(provider):
            try: results.append(provider.refresh())
            except BaseException as error: failures.append(error)
        with patch.object(folders, '_json_request', side_effect=request):
            threads = [threading.Thread(target=run, args=(p,)) for p in (self.provider, other)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len([r for r in self.requests if r[0] == 'POST']), 1)

    def test_cache_reload_corruption_and_auto_refresh_interval(self):
        old = self.provider.refresh(); other = self.make()
        self.response['contents']['metaItems'][1]['attributes']['name'] = 'Changed elsewhere'
        self.provider.refresh()
        self.assertEqual(other.get_tree()[0]['children'][0]['name'], 'Changed elsewhere')
        good = other.get_tree()
        self.cache.write_text('{bad json')
        self.assertEqual(other.get_tree(), good)
        self.assertEqual(self.make().get_tree(), [])
        # A fresh auto cycle does not make redundant network calls.
        self.provider._save_cache()
        count = self.get_count()
        self.provider.refresh(force=False)
        self.assertEqual(self.get_count(), count)
        future = self.provider._updated_at + self.provider.refresh_interval + 1
        with patch.object(folders.time, 'time', return_value=future): self.provider.refresh(force=False)
        self.assertEqual(self.get_count(), count + 1)

    def test_background_worker_starts_and_stops(self):
        called = threading.Event()
        def refresh(force):
            self.assertFalse(force); called.set(); self.provider.stop.set()
        with patch.object(self.provider, 'refresh', side_effect=refresh):
            self.provider.start(); self.assertTrue(called.wait(2)); self.provider.close()
        self.assertFalse(self.provider.thread.is_alive())

    def test_factory_paths_limits_and_private_cache_validation(self):
        provider = folders.make_folder_provider(self.base / 'config.json',
                    {'folders': {'token_file': 'token.json', 'cache_file': 'cache.json', 'refresh_interval': 60}}, Client())
        self.assertEqual(provider.token_file, self.base.resolve() / 'token.json')
        self.assertEqual(provider.cache_file, self.base.resolve() / 'cache.json')
        self.assertEqual(provider.refresh_interval, 60)
        for interval in (0, 29, 3601, True, float('nan')):
            with self.subTest(interval=interval), self.assertRaises(ValueError): self.make(refresh_interval=interval)
        with self.assertRaises(ValueError): self.make(cache_file=self.token)

    def test_credential_lock_excludes_other_processes(self):
        code = '''
import sys
import spotify_folders as f
try:
    with f.folder_token_lock(sys.argv[1], timeout=.1):
        print('ACQUIRED')
except f.FolderError:
    print('LOCKED')
'''
        args = [sys.executable, '-c', code, str(self.token)]
        with folders.folder_token_lock(self.token):
            result = subprocess.run(args, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stdout.strip(), 'LOCKED')
        result = subprocess.run(args, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stdout.strip(), 'ACQUIRED')

    def test_cache_rejects_inconsistent_revision_and_missing_names(self):
        self.provider.refresh()
        snapshot = json.loads(self.cache.read_text())
        snapshot['tree'][0]['name'] = 'Unhashed mutation'
        self.cache.write_text(json.dumps(snapshot))
        self.assertEqual(self.make().get_tree(), [])
        snapshot['tree'][0].pop('name')
        self.cache.write_text(json.dumps(snapshot))
        self.assertEqual(self.make().get_tree(), [])

    def test_normalized_tree_size_limit_preserves_last_good_snapshot(self):
        old = self.provider.refresh(); revision = self.provider.revision
        with patch.object(folders, 'MAX_TREE_BYTES', 1):
            with self.assertRaisesRegex(folders.FolderError, 'oversized'):
                self.provider.refresh()
        self.assertEqual(self.provider.get_tree(), old)
        self.assertEqual(self.provider.revision, revision)

    def test_clock_moving_back_does_not_freeze_refresh(self):
        self.provider.refresh()
        calls = self.get_count()
        earlier = self.provider._updated_at - 3600
        with patch.object(folders.time, 'time', return_value=earlier):
            self.assertTrue(self.provider.status()['stale'])
            self.provider.refresh(force=False)
        self.assertEqual(self.get_count(), calls + 1)


if __name__ == '__main__': unittest.main()
