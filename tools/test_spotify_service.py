"""Offline Spotify catalogue/SMAPI checks; no Spotify account or speaker is used."""
import http.client
import io
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spotify_client import SpotifyError
from spotify_service import ART_BYTES, CatalogueError, SpotifyService, _NoRedirect
from smapi_stub import SMAPI_NS

NS = {'m': SMAPI_NS}
TRACK = '4uLU6hMCjMI75M1A2tKUQC'
ALBUM = '3RGL98v78bWQ0geqt9TRjg'
ARTIST = '7oPftvlwr6VrsViSDV7fJY'
PLAYLIST = '37i9dQZF1DXcBWIGoYBM5M'
IMAGE = 'https://i.scdn.co/image/abc'


def item(kind='track', ident=TRACK, name='Song & Title'):
    result = {'id': ident, 'type': kind, 'uri': 'spotify:%s:%s' % (kind, ident), 'name': name,
              'artists': [{'name': 'Artist'}], 'images': [{'url': IMAGE, 'width': 64}]}
    if kind == 'track':
        result.update(duration_ms=201999, album={'id': ALBUM, 'name': 'Album', 'images': [{'url': IMAGE}]})
    return result


class Client:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def get(self, path, params=None):
        self.calls.append((path, params))
        response = self.responses[path]
        if isinstance(response, Exception): raise response
        return response(params) if callable(response) else response


def call(service, method, args=None):
    root = ET.Element('{http://schemas.xmlsoap.org/soap/envelope/}Envelope')
    header = ET.SubElement(root, '{http://schemas.xmlsoap.org/soap/envelope/}Header')
    ET.SubElement(header, 'credentials').text = 'must-not-appear-in-logs'
    body = ET.SubElement(root, '{http://schemas.xmlsoap.org/soap/envelope/}Body')
    request = ET.SubElement(body, '{%s}%s' % (SMAPI_NS, method))
    for key, value in (args or {}).items(): ET.SubElement(request, '{%s}%s' % (SMAPI_NS, key)).text = str(value)
    connection = http.client.HTTPConnection('127.0.0.1', service.port, timeout=5)
    connection.request('POST', '/smapi', ET.tostring(root), {'Content-Type': 'text/xml'})
    response = connection.getresponse()
    status, data = response.status, response.read()
    connection.close()
    return status, ET.fromstring(data)


def result(service, method, args):
    return ET.fromstring('<root xmlns="%s">%s</root>' % (SMAPI_NS, service.dispatch(method, args)))


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.client, self.logs = Client(), []
        self.service = SpotifyService('127.0.0.1', 0, self.client, log=self.logs.append)
        self.service.start()

    def tearDown(self):
        self.service.close()

    def test_discovery_root_and_search_categories_without_api_calls(self):
        descriptor = self.service.descriptor()
        self.assertEqual(descriptor['AvailableServiceListVersion'], '3')
        self.assertIn('Name="Spotify"', descriptor['AvailableServiceDescriptorList'])
        self.assertIn('Auth="Anonymous"', descriptor['AvailableServiceDescriptorList'])
        policy = ET.fromstring(descriptor['AvailableServiceDescriptorList']).find('./Service/Policy')
        self.assertEqual(policy.get('PollInterval'), '300')
        self.assertIn('SA_RINCON61447_bridge', self.service.media_servers())
        doc = result(self.service, 'getMetadata', {'id': 'root', 'index': '0', 'count': '499'})
        self.assertEqual([e.text for e in doc.findall('.//m:title', NS)], ['My Playlists', 'Saved Albums', 'Liked Songs'])
        doc = result(self.service, 'getMetadata', {'id': 'search'})
        self.assertEqual([e.text for e in doc.findall('.//m:id', NS)], ['tracks', 'albums', 'artists', 'playlists'])
        self.assertEqual(self.client.calls, [])

    def test_search_exposes_truthful_snapshot_totals_and_no_fake_results(self):
        tracks = [item(ident=TRACK if n == 0 else '%022d' % n) for n in range(10)]
        self.client.responses['/search'] = {'tracks': {'items': tracks, 'total': 103, 'next': None}}
        doc = result(self.service, 'search', {'id': 'tracks', 'term': 'a & b', 'index': '0', 'count': '499'})
        self.assertEqual(self.client.calls, [('/search', {'q': 'a & b', 'type': 'track', 'offset': 0, 'limit': 10})])
        self.assertEqual([doc.find('.//m:' + key, NS).text for key in ('index', 'count', 'total')], ['0', '10', '10'])
        self.assertEqual(doc.find('.//m:id', NS).text, 'spotify:track:' + TRACK)
        self.assertEqual(doc.find('.//m:mimeType', NS).text, 'audio/mpeg')
        self.assertEqual(doc.find('.//m:duration', NS).text, '201')
        self.assertEqual(doc.find('.//m:title', NS).text, 'Song & Title')
        self.assertIn('http://127.0.0.1:', doc.find('.//m:albumArtURI', NS).text)
        # Back navigation reuses the bounded short-lived cache.
        self.service.dispatch('search', {'id': 'tracks', 'term': 'a & b', 'index': 0, 'count': 499})
        self.assertEqual(len(self.client.calls), 1)
        self.client.responses['/search'] = {'tracks': {'items': [], 'total': 0}}
        doc = result(self.service, 'search', {'id': 'tracks', 'term': 'no results'})
        self.assertEqual(doc.find('.//m:total', NS).text, '0')
        self.assertEqual(doc.findall('.//m:mediaMetadata', NS), [])

    def test_moving_provider_totals_have_a_fixed_50_result_window(self):
        def moving_page(params):
            offset = params['offset']
            return {'artists': {'items': [item('artist', '%022d' % n, 'Artist %d' % n)
                                          for n in range(offset, offset + 10)],
                                'total': offset + 19, 'next': 'https://api.spotify.com/ignored'}}
        self.client.responses['/search'] = moving_page
        doc = result(self.service, 'search', {'id': 'artists', 'term': 'artist', 'index': 0, 'count': 500})
        self.assertEqual([doc.find('.//m:' + key, NS).text for key in ('index', 'count', 'total')], ['0', '50', '50'])
        self.assertEqual([params['offset'] for _, params in self.client.calls], [0, 10, 20, 30, 40])
        # Native cache priming may ask overlapping or trailing ranges. Every one
        # is served from exactly the same finite result set, with no API flood.
        for index, expected in ((3, 47), (10, 40), (13, 37), (49, 1), (50, 0), (500, 0)):
            doc = result(self.service, 'search', {'id': 'artists', 'term': 'artist', 'index': index, 'count': 500})
            self.assertEqual(doc.find('.//m:count', NS).text, str(expected))
            self.assertEqual(doc.find('.//m:total', NS).text, '50')
        self.assertEqual(len(self.client.calls), 5)

    def test_search_filters_null_unsupported_local_and_duplicate_items(self):
        first = item('playlist', PLAYLIST)
        second = item('playlist', '%022d' % 2, 'Second playlist')
        self.client.responses['/search'] = {
            'playlists': {'items': [None, first, None, first, second,
                                    {'type': 'episode', 'id': TRACK, 'name': 'Episode'},
                                    {**first, 'is_local': True}],
                          'total': 1_000_000, 'next': None}}
        doc = result(self.service, 'search', {'id': 'playlists', 'term': 'playlist', 'count': 500})
        self.assertEqual(doc.find('.//m:count', NS).text, '2')
        self.assertEqual(doc.find('.//m:total', NS).text, '2')
        self.assertEqual([e.text for e in doc.findall('.//m:id', NS)],
                         ['spotify:playlist:' + PLAYLIST, 'spotify:playlist:' + '%022d' % 2])
        self.client.responses['/search'] = {'playlists': {'items': [None] * 10, 'total': 999, 'next': None}}
        doc = result(self.service, 'search', {'id': 'playlists', 'term': 'all unavailable', 'count': 500})
        self.assertEqual(doc.find('.//m:total', NS).text, '0')
        self.assertEqual(doc.findall('.//m:mediaCollection', NS), [])

    def test_same_query_concurrency_shares_one_snapshot_and_cache(self):
        barrier = threading.Barrier(7)
        entered, release = threading.Event(), threading.Event()

        def page(params):
            if params['offset'] == 0:
                entered.set()
                self.assertTrue(release.wait(3))
            offset = params['offset']
            return {'artists': {'items': [item('artist', '%022d' % n) for n in range(offset, offset + 10)],
                                'total': offset + 19, 'next': 'still more'}}

        def search():
            barrier.wait(3)
            return self.service.dispatch('search', {'id': 'artists', 'term': 'shared', 'count': 500})

        self.client.responses['/search'] = page
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = [pool.submit(search) for _ in range(6)]
            barrier.wait(3)
            self.assertTrue(entered.wait(3))
            release.set()
            bodies = [f.result(5) for f in results]
        self.assertEqual(len(set(bodies)), 1)
        self.assertEqual(len(self.client.calls), 5)
        with patch('spotify_service.time.monotonic', return_value=10 ** 12):
            # A late continuation preserves the old ordering even after TTL.
            self.service.dispatch('search', {'id': 'artists', 'term': 'shared', 'index': 10, 'count': 10})
        self.assertEqual(len(self.client.calls), 5)

    def test_all_search_categories_and_album_artist_browse(self):
        for category, kind, ident in (('albums', 'album', ALBUM), ('artists', 'artist', ARTIST),
                                       ('playlists', 'playlist', PLAYLIST)):
            self.client.responses['/search'] = {category: {'items': [item(kind, ident)], 'total': 1}}
            doc = result(self.service, 'search', {'id': category, 'term': category})
            self.assertEqual(doc.find('.//m:id', NS).text, 'spotify:%s:%s' % (kind, ident))
            self.assertEqual(doc.find('.//m:canPlay', NS).text, 'false' if kind == 'artist' else 'true')
        album_track = item()
        album_track.pop('album')
        self.client.responses['/albums/%s/tracks' % ALBUM] = {'items': [album_track], 'total': 62}
        doc = result(self.service, 'getMetadata', {'id': 'spotify:album:' + ALBUM, 'index': 50, 'count': 499})
        self.assertEqual(self.client.calls[-1], ('/albums/%s/tracks' % ALBUM, {'offset': 50, 'limit': 50}))
        self.assertEqual(doc.find('.//m:albumId', NS).text, 'spotify:album:' + ALBUM)
        self.assertNotIn('/albums/' + ALBUM, [p for p, _ in self.client.calls])
        self.client.responses['/artists/%s/albums' % ARTIST] = {'items': [item('album', ALBUM)], 'total': 1}
        result(self.service, 'getMetadata', {'id': 'spotify:artist:' + ARTIST})
        self.assertEqual(self.client.calls[-1][1], {'include_groups': 'album,single', 'offset': 0, 'limit': 50})

    def test_library_and_both_playlist_item_shapes(self):
        cases = [('library:playlists', '/me/playlists', item('playlist', PLAYLIST)),
                 ('library:albums', '/me/albums', {'album': item('album', ALBUM)}),
                 ('library:tracks', '/me/tracks', {'track': item()}),
                 ('spotify:playlist:' + PLAYLIST, '/playlists/%s/items' % PLAYLIST, {'item': item()})]
        for ident, path, raw in cases:
            self.client.responses[path] = {'items': [raw], 'total': 1}
            doc = result(self.service, 'getMetadata', {'id': ident})
            self.assertEqual(doc.find('.//m:count', NS).text, '1')
        path = '/playlists/%s/items' % PLAYLIST
        self.client.responses[path] = {'items': [{'track': item()}], 'total': 2}
        doc = result(self.service, 'getMetadata', {'id': 'spotify:playlist:' + PLAYLIST, 'index': 1})
        self.assertEqual(doc.find('.//m:count', NS).text, '1')
        self.assertEqual(doc.find('.//m:index', NS).text, '1')

    def test_unknown_direct_item_and_unavailable_entries_are_not_invented(self):
        self.client.responses['/tracks/' + TRACK] = item()
        doc = result(self.service, 'getMediaMetadata', {'id': 'spotify:track:' + TRACK})
        self.assertEqual(doc.find('.//m:id', NS).text, 'spotify:track:' + TRACK)
        self.client.responses['/me/tracks'] = {'items': [{'track': None}], 'total': 1}
        with self.assertRaises(CatalogueError): self.service.dispatch('getMetadata', {'id': 'library:tracks'})
        with self.assertRaises(CatalogueError): self.service.dispatch('getMediaURI', {'id': 'spotify:track:' + TRACK})

    def test_friendly_faults_malformed_paging_and_no_credentials_in_logs(self):
        for status in (401, 403, 404, 429, 502):
            self.client.responses['/me/playlists'] = SpotifyError(status, 'secret-access-token')
            code, doc = call(self.service, 'getMetadata', {'id': 'library:playlists'})
            self.assertEqual(code, 500)
            self.assertNotIn('secret-access-token', ET.tostring(doc).decode())
            if status in (401, 403):
                self.assertEqual(doc.find('.//faultcode').text, 's:Server.ServiceUnavailable')
                self.assertIn('bridge setup', doc.find('.//faultstring').text)
            if status == 429: self.assertIn('Wait a little', doc.find('.//faultstring').text)
        code, _ = call(self.service, 'getMetadata', {'id': 'root', 'count': 'not an integer'})
        self.assertEqual(code, 500)
        code, _ = call(self.service, 'getMetadata', {'id': 'root', 'index': '-1'})
        self.assertEqual(code, 500)
        self.assertNotIn('must-not-appear', repr(self.logs))
        self.assertNotIn('secret-access-token', repr(self.logs))
        self.assertTrue(all('credentials' not in entry for entry in self.logs))

    def test_last_update_is_stable_until_an_explicit_catalogue_change(self):
        versions = []
        for clock in (259, 269, 270):
            with patch('spotify_service.time.time', return_value=clock):
                doc = result(self.service, 'getLastUpdate', {})
            catalog, favorites = (doc.find('.//m:' + key, NS).text for key in ('catalog', 'favorites'))
            self.assertEqual(catalog, favorites)
            self.assertEqual(doc.find('.//m:pollInterval', NS).text, '300')
            versions.append(catalog)
        self.assertEqual(len(set(versions)), 1)
        self.service.invalidate_catalog()
        doc = result(self.service, 'getLastUpdate', {})
        self.assertNotEqual(doc.find('.//m:catalog', NS).text, versions[0])
        self.assertEqual(self.client.calls, [])

    def test_missing_or_malformed_soap_body_returns_a_fault(self):
        for payload in (b'<root/>',
                        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"/>',
                        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>',
                        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><wrong/></s:Body></s:Envelope>'):
            with self.subTest(payload=payload):
                connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=5)
                connection.request('POST', '/smapi', payload, {'Content-Type': 'text/xml'})
                response = connection.getresponse()
                self.assertEqual(response.status, 500)
                doc = ET.fromstring(response.read())
                self.assertIsNotNone(doc.find('.//faultcode'))
                connection.close()

    def test_art_is_registered_not_an_open_url_proxy(self):
        self.assertEqual(self.service._image_url([{'url': 'http://127.0.0.1/admin'}]), '')
        self.assertEqual(self.service._image_url([{'url': 'https://i.scdn.co.evil.test/a'}]), '')
        self.assertEqual(self.service._image_url([{'url': 'https://user:pass@i.scdn.co/a'}]), '')
        with patch('spotify_service.urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError): self.service._fetch_art('http://192.168.1.1/')
            opener.assert_not_called()
        with self.assertRaises(ValueError): _NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=5)
        connection.request('GET', '/art/000000000000000000000000.jpg?url=http://127.0.0.1/')
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read(); connection.close()

    def test_art_download_bounds_timeout_and_baseline_conversion(self):
        from PIL import Image
        source = io.BytesIO()
        Image.new('RGB', (600, 300), (20, 30, 40)).save(source, 'PNG')
        response = Mock()
        response.headers = {'Content-Type': 'image/png'}
        response.read.return_value = source.getvalue()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch('spotify_service.urllib.request.build_opener', return_value=opener):
            data = self.service._fetch_art(IMAGE)
            self.assertEqual(opener.open.call_args.kwargs, {'timeout': 5})
            response.read.assert_called_once_with(ART_BYTES + 1)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (200, 100))
                self.assertEqual(image.format, 'JPEG')
                self.assertFalse(image.info.get('progressive'))
            response.headers['Content-Length'] = str(ART_BYTES + 1)
            with self.assertRaises(ValueError): self.service._fetch_art(IMAGE)


if __name__ == '__main__':
    unittest.main()
