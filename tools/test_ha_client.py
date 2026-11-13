#!/usr/bin/env python3
"""Home Assistant picture fetching keeps credentials on their exact origin."""
import http.server
import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_client


class Fetch(unittest.TestCase):
    def setUp(self):
        self.ha = ha_client.HomeAssistant('http://ha:8123', 'private-token')
        self.addCleanup(self.ha.close)

    def request(self, path, expected_url, authorized):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'picture'
        with patch('ha_client.urllib.request.urlopen', return_value=response) as ordinary, \
                patch('ha_client.urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            self.assertEqual(self.ha.fetch(path), b'picture')
            chosen, unused = (build.return_value.open, ordinary) if authorized else (ordinary, build.return_value.open)
            chosen.assert_called_once()
            unused.assert_not_called()
            request = chosen.call_args.args[0]
            self.assertEqual(request.full_url, expected_url)
            self.assertEqual(request.get_header('Authorization'), 'Bearer private-token' if authorized else None)
            if authorized:
                self.assertIsNone(build.call_args.args[0].redirect_request(request, response, 302, 'Found', {}, 'http://evil.example/x'))

    def test_relative_paths_and_the_exact_origin_receive_the_token(self):
        for path in ('/api/image_proxy/picture', 'api/image_proxy/picture', 'http://ha:8123/api/image_proxy/picture'):
            with self.subTest(path=path):
                self.request(path, 'http://ha:8123/api/image_proxy/picture', True)

    def test_lookalike_origins_and_userinfo_do_not_receive_the_token(self):
        for path in ('http://ha:8123.evil.example/x', 'http://ha:81234/x', 'http://ha:8123@evil.example/x',
                     'http://user@ha:8123/x', 'http://user:pass@ha:8123/x', 'https://ha:8123/x', 'http://ha:0/x',
                     '//evil.example/x'):
            with self.subTest(path=path):
                self.request(path, 'http:' + path if path.startswith('//') else path, False)

    def test_default_ports_and_case_compare_as_the_same_origin(self):
        for base, path in (('http://HA', 'http://ha:80/x'), ('https://ha:443', 'https://HA/x')):
            with self.subTest(base=base):
                self.ha.url = base
                self.request(path, path, True)

    def test_non_http_schemes_and_urls_without_hosts_are_rejected_before_open(self):
        for path in ('file:///etc/hostname', 'ftp://ha/picture', 'data:image/png,test', 'http:///picture'):
            with self.subTest(path=path), patch('ha_client.urllib.request.urlopen') as ordinary, \
                    patch('ha_client.urllib.request.build_opener') as build:
                with self.assertRaises(ValueError): self.ha.fetch(path)
                ordinary.assert_not_called()
                build.assert_not_called()

    def test_authorized_fetch_does_not_follow_a_redirect(self):
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.send_header('Content-Length', '0')
                self.end_headers()
            def log_message(self, *args): pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.ha.url = 'http://127.0.0.1:%d' % server.server_address[1]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.ha.fetch('/picture')
        self.assertEqual(error.exception.code, 302)
        self.assertEqual(requests, [('/picture', 'Bearer private-token')])


if __name__ == '__main__':
    unittest.main()
