"""Spotify page controller preview over real HTTP: status, built files, the SMAPI proxy and assets."""
import hashlib
import http.client
import http.server
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import online_updates
import webui
from smapi_service import PREVIEW_HEADER
from spotify_menu import default_items
from spotify_service import SpotifyService
from test_controller_preview import build_movie, build_rootfs, supported
from test_online_updates import build_test_package

CR1 = '00:0e:58:01:02:03'
CR2 = '00:0e:58:04:05:06'


def envelope(method, body='<id>root</id><index>0</index><count>100</count>'):
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<%s xmlns="http://www.sonos.com/Services/1.1">%s</%s></s:Body></s:Envelope>'
            % (method, body, method)).encode()


class Upstream(http.server.ThreadingHTTPServer):
    """A stand-in Spotify service that records what the bridge forwarded."""
    daemon_threads = True

    def __init__(self, status=200, body=b'<ok/>'):
        self.requests, self.status, self.body = [], status, body
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                server.requests.append((self.path, dict(self.headers), data))
                self.send_response(server.status)
                self.send_header('Content-Length', str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

            def log_message(self, *args): pass
        super().__init__(('127.0.0.1', 0), Handler)


class PreviewHttp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        menus = {CR1: {'items': [{'id': 'entry_k', 'kind': 'spotify', 'target': 'spotify:playlist:' + 'K' * 22,
                                  'label': 'Kitchen mix', 'locked': False}] + default_items()},
                 CR2: {'items': [{'id': 'entry_d', 'kind': 'spotify', 'target': 'spotify:playlist:' + 'D' * 22,
                                  'label': 'Den mix', 'locked': False}] + default_items()}}
        self.config.write_text(json.dumps({'backend': 'sonos', 'rooms': [],
            'controllers': {CR1: {'name': 'Kitchen'}, CR2: {'name': 'Den'}},
            'spotify_music_service': {'enabled': True, 'name': 'My Spotify', 'controller_menus': menus}}))
        state = self.root / 'state.json'
        state.write_text(json.dumps({'controllers': [{'mac': CR1}, {'mac': CR2}]}))
        self.logs = []
        self.bridge = webui.Bridge(self.config, state, None, self.root / 'pid',
            webui.DataFile(self.root / 'webui.json'), log=self.logs.append)
        self.addCleanup(self.bridge.close)
        self.runtime = {'running': True, 'ready': True, 'rooms': []}
        self.bridge.player_runtime = lambda: dict(self.runtime)
        self.auth = webui.Auth(self.bridge.data)
        self.session = self.auth.new_session()
        self.cookie = webui.SESSION_COOKIE + '=' + self.session
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, lambda _: None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.movie = build_movie()
        gate = supported(self.movie)
        gate.start()
        self.addCleanup(gate.stop)

    def request(self, method, path, body=None, *, cookie=True, headers=None):
        request_headers = {'Cookie': self.cookie} if cookie else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, path, **options):
        status, _, data = self.request('GET', path, **options)
        return status, json.loads(data)

    def upload(self, mac=CR1, rootfs=None):
        package, modulus = build_test_package(rootfs=rootfs or build_rootfs(self.movie))
        with patch.object(online_updates, 'VENDOR_MODULUS', modulus):
            self.bridge.updates().stage_controller(mac, package, arm=False)

    def smapi(self, url, body=None, headers=None, **options):
        status, headers, data = self.request('POST', url, body or envelope('getMetadata'),
                                             headers={'Content-Type': 'text/xml', **(headers or {})}, **options)
        return status, headers, data.decode()

    def upstream(self, **options):
        upstream = Upstream(**options)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        self.runtime['spotify_smapi'] = 'http://127.0.0.1:%d/smapi' % upstream.server_address[1]
        return upstream

    def test_without_firmware_the_page_gets_the_reason_and_still_a_smapi_route(self):
        status, preview = self.json('/api/spotify/preview/' + CR1.replace(':', '%3A'))
        self.assertEqual(status, 200)
        self.assertEqual((preview['available'], preview['controller']['mac'], preview['service_name']),
                         (False, CR1, 'My Spotify'))
        self.assertIn('Upload controller firmware 4.3.2', preview['reason'])
        self.assertNotIn('movie', preview)
        self.assertRegex(preview['smapi_url'], r'^/api/spotify/preview/%s/smapi\?t=[0-9a-f]{32}$' % CR1)
        self.assertEqual(self.json('/api/spotify/preview/' + CR1, cookie=False)[0], 401)
        self.assertEqual(self.json('/api/spotify/preview/00:0e:58:ff:ff:ff')[0], 404)

    def test_uploaded_firmware_gives_movie_and_fonts_served_only_when_signed_in(self):
        self.upload(CR2)
        status, preview = self.json('/api/spotify/preview/' + CR1)
        self.assertEqual((status, preview['available'], preview['firmware']), (200, True, 'test build'))
        self.assertRegex(preview['movie'], r'^/api/spotify/preview-files/[0-9a-f]{24}/MainUI\.swf$')
        self.assertEqual(len(preview['fonts']), 8)
        status, headers, movie = self.request('GET', preview['movie'])
        self.assertEqual((status, headers['Content-Type'], movie[:3]), (200, 'application/x-shockwave-flash', b'FWS'))
        self.assertIn('private', headers['Cache-Control'])
        status, headers, font = self.request('GET', preview['fonts'][0])
        self.assertEqual((status, headers['Content-Type']), (200, 'font/ttf'))
        self.assertEqual(self.request('GET', preview['movie'], cookie=False)[0], 401)
        key = preview['movie'].split('/')[3]
        for path in ('/api/spotify/preview-files/%s/receipt.json' % key, '/api/spotify/preview-files/%s/../index.json' % key,
                     '/api/spotify/preview-files/%s/MainUI.swf' % ('0' * 24)):
            self.assertEqual(self.request('GET', path)[0], 404, path)

    def test_smapi_route_needs_session_and_token_but_not_the_x_bridge_header(self):
        upstream = self.upstream(body=b'<reply/>')
        url = self.json('/api/spotify/preview/' + CR1)[1]['smapi_url']
        status, headers, body = self.smapi(url)
        self.assertEqual((status, body, headers['Content-Type']), (200, '<reply/>', 'text/xml; charset=utf-8'))
        path, forwarded, data = upstream.requests[-1]
        self.assertEqual((path, forwarded[PREVIEW_HEADER], data), ('/smapi', CR1, envelope('getMetadata')))
        self.assertEqual(self.smapi(url, cookie=False)[0], 401)
        for bad in (url.split('?')[0], url[:-1] + ('0' if url[-1] != '0' else '1'),
                    url.replace(CR1, CR2)):
            self.assertEqual(self.smapi(bad)[0], 403, bad)
        other = webui.SESSION_COOKIE + '=' + self.auth.new_session()
        self.assertEqual(self.smapi(url, cookie=False, headers={'Cookie': other})[0], 403)
        self.assertEqual(len(upstream.requests), 1)

    def test_only_browse_calls_are_forwarded_and_faults_travel_as_http_200(self):
        upstream = self.upstream(status=500, body=b'<s:Fault>locked</s:Fault>')
        url = self.json('/api/spotify/preview/' + CR1)[1]['smapi_url']
        for method in ('getMetadata', 'getMediaMetadata', 'getExtendedMetadata', 'search'):
            status, _, body = self.smapi(url, envelope(method))
            self.assertEqual((status, body), (200, '<s:Fault>locked</s:Fault>'), method)
        for raw in (envelope('getMediaURI'), envelope('getScrollIndices'), b'not xml',
                    b'<!DOCTYPE x [<!ENTITY a "b">]>' + envelope('getMetadata')):
            status, _, body = self.smapi(url, raw)
            self.assertEqual(status, 200)
            self.assertIn('Client.InvalidRequest', body)
        self.assertEqual(len(upstream.requests), 4)
        status, _, _ = self.smapi(url, b'x' * (webui.PREVIEW_SMAPI_LIMIT + 1))
        self.assertEqual(status, 413)

    def test_stopped_or_restarting_service_is_a_readable_fault(self):
        url = self.json('/api/spotify/preview/' + CR1)[1]['smapi_url']
        status, _, body = self.smapi(url)
        self.assertEqual(status, 200)
        self.assertIn('not running', body)
        upstream = self.upstream()
        upstream.shutdown(); upstream.server_close()
        status, _, body = self.smapi(url)
        self.assertEqual(status, 200)
        self.assertIn('restarting', body)

    def test_real_spotify_service_shows_the_named_controller_menu_not_the_callers(self):
        settings = json.loads(self.config.read_text())['spotify_music_service']
        service = SpotifyService('127.0.0.1', 0, object(), log=lambda _: None, menu_settings=settings,
                                 controller_resolver=lambda address: CR2)
        service.start()
        self.addCleanup(service.close)
        self.runtime['spotify_smapi'] = service.uri
        for mac, own, other in ((CR1, 'Kitchen mix', 'Den mix'), (CR2, 'Den mix', 'Kitchen mix')):
            url = self.json('/api/spotify/preview/' + mac)[1]['smapi_url']
            status, _, body = self.smapi(url)
            self.assertEqual(status, 200)
            self.assertIn(own, body)
            self.assertNotIn(other, body)

    def test_player_status_publishes_the_spotify_service_address(self):
        import zoneplayers
        service = SpotifyService('127.0.0.1', 0, object(), log=lambda _: None, menu_settings={})
        self.addCleanup(service.close)
        house = type('House', (), {'players': [], 'devices': lambda self: [], 'smart_devices': None,
                                   'music_service': service})()
        path = self.root / 'players-status.json'
        zoneplayers.publish_player_status(path, house, [])
        self.assertEqual(json.loads(path.read_text())['spotify_smapi'], service.uri)
        house.music_service = None
        zoneplayers.publish_player_status(path, house, [])
        self.assertNotIn('spotify_smapi', json.loads(path.read_text()))


class RuffleAssets(unittest.TestCase):
    VENDOR = webui.STATIC_DIR / 'vendor' / 'ruffle'

    def test_vendored_player_matches_its_pinned_hashes_and_ships_its_licenses(self):
        note = (self.VENDOR / 'VENDORED.md').read_text()
        pinned = dict(re.findall(r'\| `([^`]+)` \| `([0-9a-f]{64})` \|', note))
        self.assertEqual(set(pinned), {'ruffle.js', 'ruffle_web_bg.KIXQ5A6C.wasm', 'ruffle_web-wasm_mvp_bg.TOQYS3HB.wasm'})
        for name, digest in pinned.items():
            self.assertEqual(hashlib.sha256((self.VENDOR / name).read_bytes()).hexdigest(), digest, name)
        for name in ('LICENSE_MIT', 'LICENSE_APACHE'):
            self.assertTrue((self.VENDOR / name).is_file())
        self.assertIn('ruffle_web_bg.KIXQ5A6C.wasm', (self.VENDOR / 'ruffle.js').read_text())

    def test_wasm_is_served_with_its_type_cached_and_the_csp_allows_only_what_ruffle_needs(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'config.json').write_text(json.dumps({'backend': 'sonos', 'rooms': []}))
        (root / 'state.json').write_text(json.dumps({'controllers': []}))
        bridge = webui.Bridge(root / 'config.json', root / 'state.json', None, root / 'pid',
                              webui.DataFile(root / 'webui.json'), log=lambda _: None)
        self.addCleanup(bridge.close)
        server = webui.Server(('127.0.0.1', 0), webui.make_handler(bridge, webui.Auth(bridge.data), lambda _: None))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def head(path):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                response.read()
                return response.status, dict(response.getheaders())
            finally:
                connection.close()
        status, headers = head('/static/vendor/ruffle/ruffle_web_bg.KIXQ5A6C.wasm')
        self.assertEqual((status, headers['Content-Type'], headers['Cache-Control']),
                         (200, 'application/wasm', webui.VENDOR_CACHE))
        self.assertEqual(head('/static/vendor/ruffle/VENDORED.md')[0], 404)
        status, headers = head('/')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        csp = headers['Content-Security-Policy']
        self.assertIn("script-src 'self' 'wasm-unsafe-eval';", csp)
        self.assertNotIn('unsafe-inline', csp)
        self.assertNotIn("'unsafe-eval'", csp)
        for digest in webui.RUFFLE_STYLE_HASHES:
            self.assertIn(digest, csp)


if __name__ == '__main__':
    unittest.main()
