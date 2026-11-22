"""Authenticated stock-bootstrap API gates over local HTTP, with no live controller."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webui


CR1 = '00:0e:58:01:02:03'


class FakeBootstrap:
    def __init__(self):
        self.snapshot = {'status': 'none', 'message': 'No package staged.', 'can_cancel': False}
        self.package = None
        self.public_key = None
        self.calls = []
        self.flash_launches = 0

    def status(self, mac):
        self.calls.append(('status', mac))
        return dict(self.snapshot)

    def stage(self, mac, package_path, filename, key_path=None):
        self.calls.append(('stage', mac))
        self.package = Path(package_path).read_bytes()
        self.public_key = Path(key_path).read_bytes() if key_path else None
        self.snapshot = {'status': 'staged', 'message': 'Package uploaded.',
                         'filename': filename, 'sha256': hashlib.sha256(self.package).hexdigest(),
                         'can_cancel': True}
        return dict(self.snapshot)

    def start(self, mac):
        self.calls.append(('start', mac))
        if self.snapshot['status'] != 'staged':
            raise ValueError('Stage a package first.')
        self.snapshot.update(status='await_now_playing', message='Open Now Playing.')
        return dict(self.snapshot)

    def confirm_screen(self, mac):
        self.calls.append(('confirm_screen', mac))
        if self.snapshot['status'] != 'await_now_playing':
            raise ValueError('Confirm the screen after preparation.')
        self.snapshot.update(status='await_flash_confirmation', message='Confirm flash.',
                             destination_bank=1, destination_generation=7, controller_ip='192.168.69.100')
        return dict(self.snapshot)

    def confirm_flash(self, mac, sha256):
        self.calls.append(('confirm_flash', mac))
        if self.snapshot['status'] != 'await_flash_confirmation' or sha256 != self.snapshot['sha256']:
            raise ValueError('Package identity or state changed.')
        self.flash_launches += 1
        self.snapshot.update(status='installing', message='Upgrade started.', can_cancel=False)
        return dict(self.snapshot)

    def cancel(self, mac):
        self.calls.append(('cancel', mac))
        if not self.snapshot['can_cancel']:
            raise ValueError('Cancellation is not safe now.')
        self.snapshot.update(status='cancelled', message='Cancelled.', can_cancel=False)
        return dict(self.snapshot)


class BootstrapHttp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'backend': 'sonos', 'rooms': [],
                                      'controllers': {CR1: {'name': 'Stock controller'}}}))
        state = self.root / 'state.json'
        state.write_text(json.dumps({'controllers': [{'mac': CR1, 'key': '01' * 16}]}))
        self.bridge = webui.Bridge(config, state, None, self.root / 'pid',
                                   webui.DataFile(self.root / 'webui.json'), log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.bridge.details = {CR1: {'firmware': '25.2-50130'}}
        self.bridge.default_cfw_path = self.root / 'default-cfw.upd'
        self.fake = FakeBootstrap()
        self.bridge._stock_bootstrap_management = self.fake
        self.auth = webui.Auth(self.bridge.data)
        self.cookie = webui.SESSION_COOKIE + '=' + self.auth.new_session()
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, lambda _: None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.path = '/api/controllers/' + CR1 + '/bootstrap'

    def call(self, method, path=None, body=None, *, authenticated=True, csrf=True, headers=None):
        request_headers = {}
        if authenticated:
            request_headers['Cookie'] = self.cookie
        if csrf:
            request_headers['X-Bridge'] = '1'
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path or self.path, body, request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def upload(self, data=b'opaque custom firmware', key=None, **options):
        headers = {'Content-Type': 'application/octet-stream', 'X-Firmware-Name': 'candidate.upd'}
        if key is not None:
            headers['X-Public-Key'] = base64.b64encode(key).decode('ascii')
        return self.call('POST', body=data, headers=headers, **options)

    def test_auth_csrf_and_controller_identity_guard_every_route(self):
        routes = [('GET', self.path, None), ('POST', self.path, b'package'),
                  ('POST', self.path + '/stage-default', {}),
                  ('POST', self.path + '/start', {}), ('POST', self.path + '/confirm-screen', {}),
                  ('POST', self.path + '/confirm-flash', {'sha256': '0' * 64}),
                  ('POST', self.path + '/cancel', {})]
        for method, path, body in routes:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, body, authenticated=False)[0], 401)
                if method != 'GET':
                    self.assertEqual(self.call(method, path, body, csrf=False)[0], 403)
        self.assertEqual(self.upload(path='/api/controllers/00:11:22:33:44:55/bootstrap')[0], 404)
        self.assertFalse([call for call in self.fake.calls if call[0] != 'status'])

    def test_upload_is_separate_from_online_updates_and_never_launches_flash(self):
        data = b'not a release-manifested vendor package'
        key = b'custom public key bytes'
        code, result = self.upload(data, key)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['bootstrap']['status'], 'staged')
        self.assertEqual(result['bootstrap']['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.fake.package, data)
        self.assertEqual(self.fake.public_key, key)
        self.assertEqual(result['firmware_identity']['classification'], 'stock')
        self.assertEqual(self.fake.flash_launches, 0)
        self.assertEqual(self.call('GET')[1]['bootstrap']['status'], 'staged')
        self.assertEqual(self.fake.flash_launches, 0)

    def test_screen_and_exact_package_confirmation_are_both_required(self):
        self.assertEqual(self.upload()[0], 200)
        sha256 = self.fake.snapshot['sha256']
        self.assertEqual(self.call('POST', self.path + '/confirm-screen', {})[0], 400)
        self.assertEqual(self.call('POST', self.path + '/confirm-flash', {'sha256': sha256})[0], 400)
        self.assertEqual(self.fake.flash_launches, 0)
        self.assertEqual(self.call('POST', self.path + '/start', {})[1]['bootstrap']['status'], 'await_now_playing')
        self.assertEqual(self.fake.flash_launches, 0)
        self.assertEqual(self.call('POST', self.path + '/confirm-screen', {})[1]['bootstrap']['status'], 'await_flash_confirmation')
        self.assertEqual(self.fake.flash_launches, 0)
        for payload, expected in [({}, 400), ({'sha256': '0' * 64}, 400), ({'sha256': sha256.upper()}, 400)]:
            with self.subTest(payload=payload):
                self.assertEqual(self.call('POST', self.path + '/confirm-flash', payload)[0], expected)
                self.assertEqual(self.fake.flash_launches, 0)
        self.assertEqual(self.call('POST', self.path + '/confirm-flash', {'sha256': sha256})[1]['bootstrap']['status'], 'installing')
        self.assertEqual(self.fake.flash_launches, 1)

    def test_cancel_before_flash_and_upload_framing(self):
        self.assertEqual(self.upload()[0], 200)
        self.assertEqual(self.call('POST', self.path + '/cancel', {})[1]['bootstrap']['status'], 'cancelled')
        self.assertEqual(self.fake.flash_launches, 0)
        for headers, expected in [({'X-Public-Key': '???'}, 400),
                                  ({'X-Public-Key': base64.b64encode(b'k' * 4097).decode()}, 400),
                                  ({'X-Firmware-Name': 'wrong.bin'}, 400),
                                  ({'Content-Type': 'text/plain'}, 415)]:
            with self.subTest(headers=headers):
                value = {'Content-Type': 'application/octet-stream', 'X-Firmware-Name': 'candidate.upd', **headers}
                self.assertEqual(self.call('POST', body=b'package', headers=value)[0], expected)
        self.assertEqual(self.fake.flash_launches, 0)

    def test_real_manager_can_stage_without_release_manifest_or_run_directory(self):
        self.bridge._stock_bootstrap_management = None
        code, status = self.call('GET')
        self.assertEqual(code, 200, status)
        self.assertEqual(status['bootstrap']['status'], 'none')
        data = b'opaque, user-selected custom firmware bytes'
        code, staged = self.upload(data)
        self.assertEqual(code, 200, staged)
        self.assertEqual(staged['bootstrap']['status'], 'staged')
        self.assertEqual(staged['bootstrap']['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(staged['bootstrap']['can_cancel'])
        code, cancelled = self.call('POST', self.path + '/cancel', {})
        self.assertEqual(code, 200, cancelled)
        self.assertEqual(cancelled['bootstrap']['status'], 'cancelled')
        self.assertEqual(cancelled['firmware_identity']['classification'], 'stock')

    def test_default_candidate_stages_through_same_gates(self):
        self.assertEqual(self.call('GET')[1]['default_cfw'], {'available': False})
        data = b'locally bundled R15 package'
        self.bridge.default_cfw_path.write_bytes(data)
        code, result = self.call('GET')
        self.assertEqual(code, 200, result)
        self.assertEqual(result['default_cfw']['bytes'], len(data))
        code, staged = self.call('POST', self.path + '/stage-default', {})
        self.assertEqual(code, 200, staged)
        self.assertEqual(self.fake.package, data)
        self.assertEqual(staged['bootstrap']['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.fake.flash_launches, 0)

    def test_first_install_shows_r16_release_for_uploaded_and_cached_package(self):
        from test_online_updates import build_test_cramfs, build_test_movie, build_test_package
        build_id = 'cfw3:7e6879933c4a4ecee735e5f3f6d83201a2403c82b86dbffaa4c7a18a4ece016a'
        data, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(build_id)))
        self.bridge._stock_bootstrap_management = None
        self.bridge.default_cfw_path.write_bytes(data)
        code, staged = self.upload(data)
        self.assertEqual(code, 200, staged)
        code, reopened = self.call('GET')
        self.assertEqual(code, 200, reopened)
        for candidate in (staged['bootstrap'], reopened['bootstrap'], reopened['default_cfw']):
            self.assertEqual(candidate['version'], '25.2-50130')
            self.assertEqual(candidate['cfw_release'], 'R16')
            self.assertEqual(candidate['firmware_kind'], 'custom')
            self.assertEqual(candidate['cfw_build_id'], build_id)
        self.assertEqual(reopened['firmware_identity']['classification'], 'stock')

        path = self.root / 'stock-bootstrap' / CR1.replace(':', '') / 'candidate.upd'
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        code, changed = self.call('GET')
        self.assertEqual(code, 200, changed)
        self.assertNotIn('cfw_release', changed['bootstrap'])

    def test_first_install_shows_a_signed_release_unknown_to_the_bridge_catalog(self):
        import online_updates
        from test_online_updates import (build_test_cramfs, build_test_descriptor,
                                         build_test_movie, build_test_package)
        build_id = 'cfw3:' + 'ab' * 32
        release = 'R123.4-rc2'
        self.assertNotIn(build_id, online_updates.KNOWN_CFW_BUILDS)
        descriptor, modulus = build_test_descriptor(build_id, release)
        data, _ = build_test_package(rootfs=build_test_cramfs(
            build_test_movie(build_id + '\0' + descriptor)))
        self.bridge._stock_bootstrap_management = None
        with patch.object(online_updates, '_CFW_MODULUS', modulus):
            code, staged = self.upload(data)
            self.assertEqual(code, 200, staged)
            self.assertEqual(staged['bootstrap']['cfw_release'], release)
            self.bridge._stock_bootstrap_management = None
            webui._candidate_metadata.cache_clear()
            code, reopened = self.call('GET')
            self.assertEqual(code, 200, reopened)
            self.assertEqual(reopened['bootstrap']['cfw_release'], release)
            self.assertEqual(reopened['firmware_identity']['classification'], 'stock')

    def test_live_custom_identity_blocks_stock_bootstrap_start(self):
        from online_updates import KNOWN_CFW_BUILDS
        self.assertEqual(self.upload()[0], 200)
        build_id = next(iter(KNOWN_CFW_BUILDS))
        self.bridge.updates().report_build(CR1, build_id)
        code, result = self.call('GET')
        self.assertEqual(code, 200, result)
        self.assertEqual(result['firmware_identity']['classification'], 'custom')
        code, error = self.call('POST', self.path + '/start', {})
        self.assertEqual(code, 409, error)
        self.assertEqual(self.fake.flash_launches, 0)


if __name__ == '__main__':
    unittest.main()
