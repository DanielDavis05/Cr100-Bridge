"""Authenticated firmware upload, review, arm and cancellation over real HTTP."""
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import online_updates
import webui
from test_online_updates import build_test_cramfs, build_test_descriptor, build_test_movie, build_test_package


CR1 = '00:0e:58:01:02:03'
CR2 = '00:0e:58:04:05:06'


class FirmwareHttp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'backend': 'sonos', 'rooms': [],
            'controllers': {CR1: {'name': 'Living controller'}, CR2: {'name': 'Kitchen controller'}}}))
        state = self.root / 'state.json'
        state.write_text(json.dumps({'controllers': [{'mac': CR1, 'key': '01' * 16}, {'mac': CR2, 'key': '02' * 16}]}))
        self.bridge = webui.Bridge(self.config, state, None, self.root / 'pid',
            webui.DataFile(self.root / 'webui.json'), log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.bridge.details = {CR1: {'firmware': '24.1-75101'}, CR2: {'firmware': '25.2-50130'}}
        self.auth = webui.Auth(self.bridge.data)
        self.cookie = webui.SESSION_COOKIE + '=' + self.auth.new_session()
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, lambda _: None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _, modulus = build_test_package()
        signer = patch.object(online_updates, 'VENDOR_MODULUS', modulus)
        signer.start()
        self.addCleanup(signer.stop)
        self.path = '/api/controllers/' + CR1 + '/firmware'

    def firmware(self, version=(25, 2, 50130), model=2):
        return build_test_package(version=version, model=model)[0]

    def call(self, method, path, body=None, *, authenticated=True, csrf=True, headers=None):
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
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def upload(self, data=None, path=None, **options):
        return self.call('POST', path or self.path, self.firmware() if data is None else data,
            headers={'Content-Type': 'application/octet-stream', 'X-Firmware-Name': 'misleading-99.9.upd'}, **options)

    def test_web_panel_distinguishes_uploaded_release_from_installed_release(self):
        marker = 'cfw3:' + 'b2' * 32
        descriptor, modulus = build_test_descriptor(marker, 'R117')
        movie = build_test_movie(marker + '\x00' + descriptor)
        package, _ = build_test_package(rootfs=build_test_cramfs(movie))
        # The custom fixture does not have a vendor signature.
        package = package[:-1] + bytes([package[-1] ^ 1])
        with patch.object(online_updates, '_CFW_MODULUS', modulus):
            code, upload = self.upload(package)
            self.assertEqual(code, 200, upload)
            self.assertEqual(upload['firmware']['cfw_release'], 'R117')
            self.assertEqual(upload['firmware_identity']['classification'], 'stock')
            self.bridge.updates().report_release(CR2, descriptor)
            code, response = self.call('GET', '/api/controllers')
            self.assertEqual(code, 200, response)
            identities = {row['mac']: row['firmware_identity'] for row in response['controllers']}
            self.assertIsNone(identities[CR1]['release'])
            self.assertEqual(identities[CR2]['release'], 'R117')

    def test_bridge_package_uses_normal_review_and_requires_authenticated_paired_controller(self):
        path = self.path + '/bridge-package'
        self.assertEqual(self.call('POST', path, {})[0], 404)
        firmware = self.root / 'firmware' / 'controller.upd'
        firmware.parent.mkdir()
        firmware.write_bytes(self.firmware())
        self.assertEqual(self.call('POST', path, {}, authenticated=False)[0], 401)
        self.assertEqual(self.call('POST', path, {}, csrf=False)[0], 403)
        self.assertEqual(self.call('POST', path.replace(CR1, '00:11:22:33:44:55'), {})[0], 404)
        self.assertTrue(self.call('GET', '/api/controllers')[1]['controllers'][0]['bridge_firmware_available'])
        code, reviewed = self.call('POST', path, {})
        self.assertEqual(code, 200, reviewed)
        self.assertEqual(reviewed['firmware']['status'], 'staged')
        self.assertFalse(reviewed['firmware']['armed'])
        self.assertIsNone(self.bridge.updates().selection(CR1, '24.1-75101'))
        firmware.write_bytes(b'invalid')
        self.assertEqual(self.call('POST', path, {})[0], 400)
        self.assertEqual(self.bridge.updates().status(CR1)['sha256'], reviewed['firmware']['sha256'])

    def test_upload_reviews_embedded_version_and_install_defaults_to_manual_prompt(self):
        before = self.config.read_bytes()
        code, result = self.upload()
        self.assertEqual(code, 200, result)
        self.assertEqual(result['current_version'], '24.1-75101')
        self.assertEqual(result['firmware']['version'], '25.2-50130')
        self.assertEqual(result['firmware']['status'], 'staged')
        self.assertFalse(result['firmware']['armed'])
        self.assertFalse(result['firmware']['force_prompt'])
        manager = self.bridge.updates()
        self.assertIsNone(manager.selection(CR1, '24.1-75101'))
        code, result = self.call('POST', self.path + '/install', {})
        self.assertEqual(code, 200, result)
        self.assertTrue(result['firmware']['armed'])
        self.assertFalse(result['firmware']['force_prompt'])
        self.assertIsNotNone(manager.selection(CR1, '24.1-75101'))
        self.assertEqual(manager.status(CR2)['status'], 'none')
        self.assertEqual(self.config.read_bytes(), before)

    def test_forced_prompt_and_cancel_clear_stage_selection_and_temporary_floor(self):
        self.assertEqual(self.upload()[0], 200)
        code, result = self.call('POST', self.path + '/install', {'force': True})
        self.assertEqual(code, 200, result)
        self.assertTrue(result['firmware']['force_prompt'])
        manager = self.bridge.updates()
        self.assertNotEqual(manager.compatibility_floor(CR1), '1.0-00000')
        code, result = self.call('POST', self.path + '/cancel', {})
        self.assertEqual(code, 200, result)
        self.assertEqual(result['firmware']['status'], 'cancelled')
        self.assertFalse(result['firmware']['armed'])
        self.assertFalse(result['firmware']['force_prompt'])
        self.assertIsNone(manager.selection(CR1, '24.1-75101'))
        self.assertEqual(manager.compatibility_floor(CR1), '1.0-00000')

    def test_authentication_csrf_and_controller_identity_checked_before_staging(self):
        self.assertEqual(self.upload(authenticated=False)[0], 401)
        self.assertEqual(self.upload(csrf=False)[0], 403)
        self.assertEqual(self.upload(path='/api/controllers/00:11:22:33:44:55/firmware')[0], 404)
        self.assertIsNone(self.bridge._online_update_management)
        for method, path, payload in [('GET', self.path, None), ('GET', '/api/updates', None),
                ('POST', self.path + '/install', {}), ('POST', self.path + '/cancel', {})]:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, payload, authenticated=False)[0], 401)
        self.assertFalse((self.root / 'controller-updates').exists())

    def test_wrong_model_and_malformed_upload_preserve_existing_staged_package(self):
        self.assertEqual(self.upload()[0], 200)
        before = self.bridge.updates().status(CR1)
        for payload in (self.firmware(model=1), b'not firmware'):
            with self.subTest(size=len(payload)):
                code, result = self.upload(payload)
                self.assertEqual(code, 400, result)
                self.assertTrue(result['error'])
                self.assertEqual(self.bridge.updates().status(CR1), before)

    def test_unverified_firmware_requires_consent_and_is_deliverable_after_consent(self):
        data = self.firmware()
        modified = bytearray(data)
        modified[-1] ^= 1
        for payload in (bytes(modified), data[:-144]):
            with self.subTest(unsigned=len(payload) < len(data)):
                code, uploaded = self.upload(payload)
                self.assertEqual(code, 200, uploaded)
                firmware = uploaded['firmware']
                self.assertFalse(firmware['signature_valid'])
                self.assertTrue(firmware['signature_warning'])
                self.assertFalse(firmware['armed'])
                for consent, identity, expected in [(False, firmware['sha256'], 409),
                        ('yes', firmware['sha256'], 400), (True, None, 409),
                        (True, '0' * 64, 409), (True, firmware['sha256'], 200)]:
                    code, result = self.call('POST', self.path + '/install',
                        {'allow_unverified': consent, 'sha256': identity})
                    self.assertEqual(code, expected, result)
                manager = self.bridge.updates()
                selection = manager.selection(CR1)
                self.assertEqual(manager.authorize_download(selection['token'], CR1).read_bytes(), payload)
                # The persisted consent survives a manager restart.
                restarted = online_updates.UpdateManager(self.config)
                self.assertEqual(restarted.authorize_download(selection['token'], CR1).read_bytes(), payload)
                stored = restarted.authorize_download(selection['token'], CR1)
                changed = bytearray(payload)
                changed[100] ^= 1
                stored.write_bytes(changed)
                with self.assertRaises(online_updates.UpdateError):
                    restarted.authorize_download(selection['token'], CR1)
                manager.cancel_controller(CR1)

    def test_stream_metadata_limits_and_content_type_rejected_before_read(self):
        for headers, expected in [({'Content-Length': str(webui.FIRMWARE_BODY_LIMIT + 1)}, 413),
                ({'Content-Length': '-1'}, 400), ({'Content-Length': 'x'}, 400),
                ({'Content-Length': '1' * 5000}, 400), ({'Content-Length': '0'}, 400),
                ({'Content-Length': '1', 'Content-Type': 'application/json'}, 415),
                ({'Content-Length': '1', 'Transfer-Encoding': 'chunked'}, 400)]:
            with self.subTest(headers=headers):
                value = {'Content-Type': 'application/octet-stream', 'X-Firmware-Name': 'firmware.upd', **headers}
                code, result = self.call('POST', self.path, b'', headers=value)
                self.assertEqual(code, expected, result)
        self.assertIsNone(self.bridge._online_update_management)

    def test_missing_length_and_truncated_upload_return_explicit_errors(self):
        for length, payload, expected in [(None, b'', 411), (100, b'short', 400)]:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            try:
                connection.putrequest('POST', self.path)
                for key, value in [('Cookie', self.cookie), ('X-Bridge', '1'),
                                   ('Content-Type', 'application/octet-stream')]:
                    connection.putheader(key, value)
                if length is not None:
                    connection.putheader('Content-Length', str(length))
                connection.endheaders(payload)
                if length is not None:
                    connection.sock.shutdown(socket.SHUT_WR)
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, expected, result)
            finally:
                connection.close()
        self.assertIsNone(self.bridge._online_update_management)

    def test_install_json_framing_and_body_limit_rejected_before_arming(self):
        self.assertEqual(self.upload()[0], 200)
        for headers, expected in [({'Content-Length': '-1'}, 400),
                ({'Content-Length': 'invalid'}, 400), ({'Content-Length': '1' * 5000}, 400),
                ({'Content-Length': str(webui.BODY_LIMIT + 1)}, 413),
                ({'Content-Length': '0', 'Transfer-Encoding': 'chunked'}, 400)]:
            with self.subTest(headers=headers):
                code, result = self.call('POST', self.path + '/install', b'', headers=headers)
                self.assertEqual(code, expected, result)
                self.assertFalse(self.bridge.updates().status(CR1)['armed'])
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.putrequest('POST', self.path + '/install')
            connection.putheader('Cookie', self.cookie)
            connection.putheader('X-Bridge', '1')
            connection.putheader('Content-Length', '0')
            connection.putheader('Content-Length', '0')
            connection.endheaders()
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 400, result)
            self.assertFalse(self.bridge.updates().status(CR1)['armed'])
        finally:
            connection.close()

    def test_install_rejects_nonboolean_force_but_allows_same_version(self):
        self.assertEqual(self.upload()[0], 200)
        code, result = self.call('POST', self.path + '/install', {'force': 'yes'})
        self.assertEqual(code, 400)
        self.assertIn('true or false', result['error'])
        self.bridge.details[CR1]['firmware'] = '25.2-50130'
        code, result = self.call('POST', self.path + '/install', {})
        self.assertEqual(code, 200, result)
        self.assertTrue(result['firmware']['armed'])
        self.assertIsNotNone(self.bridge.updates().selection(CR1, '25.2-50130'))

    def test_global_status_is_read_only_and_does_not_expose_package_paths_or_tokens(self):
        self.assertEqual(self.upload()[0], 200)
        code, result = self.call('GET', '/api/updates')
        self.assertEqual(code, 200)
        self.assertEqual(result['bridge']['mode'], 'stub')
        self.assertFalse(result['bridge']['automatic_installation'])
        self.assertEqual(result['controllers'][CR1]['version'], '25.2-50130')
        self.assertNotIn('token', result['controllers'][CR1])
        self.assertNotIn('stored_name', result['controllers'][CR1])
        self.assertFalse(result['controllers'][CR1]['armed'])

    def test_stale_review_cannot_arm_a_different_administrators_replacement(self):
        _, first = self.upload()
        _, replacement = self.upload(self.firmware(version=(25, 2, 50131)))
        code, result = self.call('POST', self.path + '/install',
            {'force': True, 'sha256': first['firmware']['sha256']})
        self.assertEqual(code, 409, result)
        current = self.bridge.updates().status(CR1)
        self.assertEqual(current['sha256'], replacement['firmware']['sha256'])
        self.assertFalse(current['armed'])
        self.assertFalse(current['force_active'])
        code, result = self.call('POST', self.path + '/install',
            {'force': False, 'sha256': replacement['firmware']['sha256']})
        self.assertEqual(code, 200, result)
        self.assertTrue(result['firmware']['armed'])

    def test_status_records_acknowledged_prompt_without_losing_manual_retry(self):
        self.assertEqual(self.upload()[0], 200)
        self.assertEqual(self.call('POST', self.path + '/install', {'force': True})[0], 200)
        manager = self.bridge.updates()
        manager.acknowledge_prompt(CR1)
        code, result = self.call('GET', self.path)
        self.assertEqual(code, 200)
        self.assertTrue(result['firmware']['armed'])
        self.assertFalse(result['firmware']['force_active'])
        self.assertIsNotNone(manager.selection(CR1, '24.1-75101'))
        self.assertEqual(manager.compatibility_floor(CR1), '1.0-00000')

    def test_force_prompt_rejects_432_later_and_unknown_while_manual_install_works(self):
        for current in ('25.2-50130', '4.3.2', '25.3-60000', None):
            with self.subTest(current=current):
                self.bridge.details[CR1] = {'firmware': current} if current else {}
                target = (24, 1, 75101) if current else (25, 2, 50130)
                code, uploaded = self.upload(self.firmware(version=target))
                self.assertEqual(code, 200, uploaded)
                code, result = self.call('POST', self.path + '/install', {'force': True})
                self.assertEqual(code, 400, result)
                self.assertIn('Online Updates', result['error'])
                manager = self.bridge.updates()
                status = manager.status(CR1)
                self.assertFalse(status['armed'])
                self.assertFalse(status['force_active'])
                self.assertEqual(manager.compatibility_floor(CR1), '1.0-00000')
                code, result = self.call('POST', self.path + '/install',
                    {'force': False, 'sha256': uploaded['firmware']['sha256']})
                self.assertEqual(code, 200, result)
                self.assertTrue(result['firmware']['armed'])
                self.assertFalse(result['firmware']['force_active'])


if __name__ == '__main__':
    unittest.main()
