"""Actual loopback TLS/HTTP/TCP contracts and household update serialization."""
import http.client
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import warnings
import xml.etree.ElementTree as ET

from update_transport import (BridgeUpdateCoordinator, UpdateTransport, UpdateTransportError,
                              MAX_BRIDGE_STATE_BYTES, ensure_certificate, message_xml, next_version)

A, B = '00:0e:58:01:02:03', '00:0e:58:04:05:06'


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'bridge.json'
        self.completed = Mock()
        self.now = [100.0]
        self.coordinator = BridgeUpdateCoordinator(self.completed, self.path, clock=lambda: self.now[0])
        self.addCleanup(self.coordinator.close)

    def test_pending_offer_is_stable_and_serializes_other_controller(self):
        c = self.coordinator
        first = c.offer(A, ['one', 'two'], '34.16-37101')
        self.assertTrue(first['stub'])
        self.assertEqual(first['target_version'], '34.16-37102')
        self.assertEqual(c.offer(A, ['one', 'two'], '34.16-37101')['token'], first['token'])
        second = c.offer(B, ['one'], '34.16-37101')
        c.begin(A, 'one', first['token'])
        with self.assertRaises(UpdateTransportError):
            c.begin(B, 'one', second['token'])
        with self.assertRaises(UpdateTransportError):
            c.begin(B, 'one', first['token'])
        c.cancel(B, first['token'])
        self.assertEqual(c.active, first['token'])
        c.cancel(A)
        c.begin(B, 'one', second['token'])
        self.assertEqual(c.active, second['token'])
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted['controllers'][A]['status'], 'cancelled')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_expiration_unblocks_household_without_installing(self):
        c = self.coordinator
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        self.now[0] += 901
        second = c.offer(B, ['one'], '34.16-37101')
        c.begin(B, 'one', second['token'])
        self.assertEqual(c.status(A)['status'], 'failed')
        self.completed.assert_not_called()

    def test_periodic_tick_expires_abandoned_job_and_persists_public_failure(self):
        c = self.coordinator
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        self.now[0] += 901
        self.assertTrue(c.tick())
        self.assertIsNone(c.active)
        self.assertEqual(c.status(A)['status'], 'failed')
        receipt = json.loads(self.path.read_text())
        self.assertIsNone(receipt['active'])
        self.assertEqual(receipt['controllers'][A]['status'], 'failed')
        self.assertFalse(c.tick())
        self.completed.assert_not_called()

    def test_corrupt_oversized_or_invalid_receipt_cannot_stop_discovery_startup(self):
        invalid = ('{broken', '[]', '{"controllers":[]}',
                   '{"controllers":{},"installed_version":"dev"}',
                   '{"controllers":{},"installed_version":"57.1-00000"}',
                   '{"controllers":{"bad":{}}}', ' ' * (MAX_BRIDGE_STATE_BYTES + 1))
        for index, raw in enumerate(invalid):
            with self.subTest(receipt=index):
                path = self.path.parent / ('invalid-%d.json' % index)
                path.write_text(raw)
                restored = BridgeUpdateCoordinator(Mock(), path, clock=lambda: self.now[0])
                self.addCleanup(restored.close)
                self.assertIsNone(restored.installed_version)
                self.assertEqual(restored.status()['controllers'], {})
                self.assertEqual(len(list(path.parent.glob(path.name + '.corrupt-*'))), 1)
                self.assertEqual(restored.offer(A, ['one'], '34.16-37101')['target_version'], '34.16-37102')

    def test_nonfinite_offer_timestamp_is_quarantined(self):
        self.coordinator.offer(A, ['one'], '34.16-37101')
        value = json.loads(self.path.read_text())
        value['controllers'][A]['expires_at'] = float('nan')
        self.path.write_text(json.dumps(value))
        restored = BridgeUpdateCoordinator(Mock(), self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.status()['controllers'], {})
        self.assertEqual(len(list(self.path.parent.glob('bridge.json.corrupt-*'))), 1)

    def test_unknown_player_cannot_join_offer_or_claim_other_controllers_job(self):
        c = self.coordinator
        offer = c.offer(A, ['one'], '34.16-37101')
        with self.assertRaises(UpdateTransportError):
            c.begin(A, 'missing', offer['token'])
        c.begin(A, 'one', offer['token'])
        with self.assertRaises(UpdateTransportError):
            c.claim(B, ['one'], timeout=0)
        with self.assertRaises(UpdateTransportError):
            c.claim(A, ['missing'], timeout=0)

    def finish(self, mac, token, player):
        c = self.coordinator
        claimed_token, claimed = c.claim(mac, [player], timeout=0)
        self.assertEqual((claimed_token, claimed), (token, player))
        for phase in ('DOWNLOAD', 'FLASHWRITE'):
            c.progress(mac, token, player, phase, 100)
            c.phase_complete(mac, token, player, phase)
        c.reboot(mac, token, player)

    def test_persisted_installed_version_and_completion_cooldown(self):
        c = self.coordinator
        event = threading.Event()
        c.on_complete = lambda offer: (self.completed(offer), event.set())
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        self.finish(A, offer['token'], 'one')
        self.assertTrue(event.wait(2))
        # The callback signals before the coordinator commits its state.
        with c.condition:
            self.assertEqual(c.status(A)['status'], 'completed')
        self.assertEqual(c.offer(A, ['one'], '34.16-37102')['token'], offer['token'])
        restored = BridgeUpdateCoordinator(Mock(), self.path, clock=lambda: self.now[0])
        self.addCleanup(restored.close)
        self.assertEqual(restored.installed_version, '34.16-37102')
        self.now[0] += 31
        self.assertEqual(restored.offer(A, ['one'], '34.16-37101')['target_version'], '34.16-37103')

    def test_restart_marks_active_job_failed_and_releases_serialization(self):
        c = self.coordinator
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        restored = BridgeUpdateCoordinator(Mock(), self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.status(A)['status'], 'failed')
        self.assertIsNone(restored.active)

    def test_compatibility_receipt_restart_preserves_persona_and_can_retry_with_new_session(self):
        c = self.coordinator
        selection = 'a' * 32
        c.compatibility_valid = lambda mac, token, player=None: mac == A and token == selection
        c.installed_version = '34.16-37101'
        offer = c.compatibility_offer(A, selection, ['one', 'two'], c.installed_version)
        self.assertEqual(offer['target_version'], c.installed_version)
        self.assertNotEqual(offer['token'], selection)
        c.begin(A, 'two', offer['token'])
        restored = BridgeUpdateCoordinator(Mock(), self.path, clock=lambda: self.now[0],
            compatibility_valid=c.compatibility_valid)
        self.addCleanup(restored.close)
        self.assertEqual(restored.status(A)['status'], 'failed')
        self.assertEqual(restored.installed_version, '34.16-37101')
        fresh = restored.compatibility_offer(A, selection, ['one', 'two'], restored.installed_version)
        self.assertNotEqual(fresh['token'], offer['token'])
        with self.assertRaises(UpdateTransportError):
            restored.begin(A, 'two', offer['token'])
        restored.begin(A, 'two', fresh['token'])

    def test_compatibility_cancelled_selection_during_completion_cannot_commit_success(self):
        c = self.coordinator
        active = [True]
        c.compatibility_valid = lambda *args: active[0]
        done = threading.Event()

        def complete(offer):
            self.assertEqual(offer['restart_players'], ['two'])
            active[0] = False
            done.set()

        c.on_complete = complete
        offer = c.compatibility_offer(A, 'a' * 32, ['one', 'two'], '34.16-37101')
        c.begin(A, 'two', offer['token'])
        self.finish(A, offer['token'], 'two')
        self.assertTrue(done.wait(2))
        with c.condition:
            c.condition.wait_for(lambda: c.status(A)['status'] == 'failed', timeout=2)
        self.assertEqual(c.status(A)['status'], 'failed')
        self.assertIsNone(c.installed_version)
        self.assertIsNone(c.active)

    def test_completion_callback_failure_releases_slot_and_preserves_installed_version(self):
        c = self.coordinator
        c.installed_version = '34.16-37101'
        event = threading.Event()

        def broken(_):
            event.set()
            raise RuntimeError('Cannot publish the resulting household')

        c.on_complete = broken
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        self.finish(A, offer['token'], 'one')
        self.assertTrue(event.wait(2))
        with c.condition:
            c.condition.wait_for(lambda: c.status(A)['status'] == 'failed', timeout=2)
        self.assertEqual(c.status(A)['status'], 'failed')
        self.assertIsNone(c.active)
        self.assertEqual(c.installed_version, '34.16-37101')
        second = c.offer(B, ['one'], '34.16-37101')
        c.begin(B, 'one', second['token'])

    def test_bad_version_and_out_of_order_phase_are_rejected(self):
        for value in ('dev', '35.0-00000', '34.16-123\n', ''):
            with self.subTest(version=value), self.assertRaises(UpdateTransportError):
                next_version(value)
        c = self.coordinator
        offer = c.offer(A, ['one'], '34.16-37101')
        c.begin(A, 'one', offer['token'])
        c.claim(A, ['one'], timeout=0)
        with self.assertRaises(UpdateTransportError):
            c.progress(A, offer['token'], 'one', 'FLASHWRITE', 0)
        with self.assertRaises(UpdateTransportError):
            c.reboot(A, offer['token'], 'one')
        self.completed.assert_not_called()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.completed = threading.Event()
        self.calls = []
        self.coordinator = BridgeUpdateCoordinator(lambda offer: (self.calls.append(offer), self.completed.set()))
        self.addCleanup(self.coordinator.close)

    def start(self, players=None, **options):
        transport = UpdateTransport('127.0.0.1', lambda ip: A,
            lambda mac: dict(MessageHeader='Controller firmware', MessageBody='Line 1\nLine 2 & <literal>',
                             ContinuePrompt='Install', CancelPrompt='Keep', AllowUpdate='false'),
            self.coordinator, self.root / 'tls', phase_delay=0, accept_timeout=.05,
            tls_port=None, http_port=None, tcp_port=0, **options)
        transport.start(players or {'127.0.0.1': ['one', 'two']})
        self.addCleanup(transport.close)
        return transport

    def stream(self, address):
        connection = socket.create_connection(address, timeout=2)
        self.addCleanup(connection.close)
        stream = connection.makefile('rwb', buffering=0)
        self.addCleanup(stream.close)
        return stream

    def run_phases(self, stream):
        stream.write(b'HELLO?version=25.2-50130\n')
        self.assertEqual(stream.readline(), b'HELLO?version=1\n')
        for phase in (b'DOWNLOAD', b'FLASHWRITE'):
            stream.write(phase + b'\n')
            for percent in (0, 25, 75, 100):
                self.assertEqual(stream.readline(), b'STATUS?phase=' + phase +
                    b';percent-complete=' + str(percent).encode() + b'\n')
            self.assertEqual(stream.readline(), b'COMPLETE?phase=' + phase + b'\n')

    def test_shared_address_udns_each_complete_before_common_reconnection(self):
        transport = self.start(players={'127.0.0.1': 'one'})
        original_address = transport.servers[0].server_address
        transport.set_players({'127.0.0.1': ['one', 'two']})
        self.assertEqual(transport.servers[0].server_address, original_address)
        offer = self.coordinator.offer(A, ['one', 'two'], '34.16-37101')
        for player in offer['players']:
            self.coordinator.begin(A, player, offer['token'])
        address = transport.servers[0].server_address
        first, second = self.stream(address), self.stream(address)
        self.run_phases(first)
        first.write(b'REBOOT?Delay=0\n')
        self.assertEqual(first.readline(), b'')
        self.assertFalse(self.completed.is_set())
        self.run_phases(second)
        second.write(b'REBOOT?Delay=0\n')
        self.assertTrue(self.completed.wait(2))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['players'], ['one', 'two'])

    def test_download_without_accepted_begin_fails_without_completion(self):
        transport = self.start(players={'127.0.0.1': 'one'})
        self.coordinator.offer(A, ['one'], '34.16-37101')
        stream = self.stream(transport.servers[0].server_address)
        stream.write(b'HELLO?version=1\nDOWNLOAD\n')
        self.assertEqual(stream.readline(), b'HELLO?version=1\n')
        self.assertEqual(stream.readline(), b'ERROR?phase=DOWNLOAD;error-code=13;internal-data=0\n')
        self.assertEqual(stream.readline(), b'')
        self.assertFalse(self.completed.is_set())

    def test_invalid_phase_fails_and_unblocks_household(self):
        transport = self.start(players={'127.0.0.1': 'one'})
        offer = self.coordinator.offer(A, ['one'], '34.16-37101')
        self.coordinator.begin(A, 'one', offer['token'])
        stream = self.stream(transport.servers[0].server_address)
        stream.write(b'HELLO?version=1\nFLASHWRITE\n')
        self.assertEqual(stream.readline(), b'HELLO?version=1\n')
        self.assertEqual(stream.readline(), b'ERROR?phase=FLASHWRITE;error-code=13;internal-data=0\n')
        self.assertIsNone(self.coordinator.active)
        self.assertEqual(self.coordinator.status(A)['status'], 'failed')

    def test_cancel_before_claim_is_scoped_and_leaves_no_install(self):
        transport = self.start(players={'127.0.0.1': 'one'})
        offer = self.coordinator.offer(A, ['one'], '34.16-37101')
        self.coordinator.begin(A, 'one', offer['token'])
        stream = self.stream(transport.servers[0].server_address)
        stream.write(b'CANCEL\n')
        self.assertEqual(stream.readline(), b'')
        self.assertEqual(self.coordinator.status(A)['status'], 'cancelled')
        self.assertFalse(self.completed.is_set())

    def test_http_streams_path_and_rejects_unknown_controller(self):
        package = self.root / 'candidate.upd'
        payload = b'authentic fixture bytes' * 10000
        package.write_bytes(payload)
        requests = []

        def request(*args):
            requests.append(args)
            return 200, package, 'application/octet-stream', {'X-Update': 'yes'}

        transport = UpdateTransport('127.0.0.1', lambda _: A, lambda _: None,
            self.coordinator, self.root, request=request, http_port=0, tls_port=None, tcp_port=0)
        transport.start({})
        self.addCleanup(transport.close)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('Must stream the package')):
            client = http.client.HTTPConnection(*transport.servers[0].server_address, timeout=2)
            client.request('GET', '/updates/package?token=opaque')
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Length'), str(len(payload)))
            self.assertEqual(response.read(), payload)
            client.close()
        self.assertEqual(requests[0][2], '127.0.0.1')
        transport.controller_resolver = lambda _: None
        client = http.client.HTTPConnection(*transport.servers[0].server_address, timeout=2)
        client.request('GET', '/updates/package')
        response = client.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        client.close()
        self.assertEqual(len(requests), 1)

    def test_original_tls10_message_fields_are_local_and_controller_scoped(self):
        seen = []
        transport = UpdateTransport('127.0.0.1', lambda _: A,
            lambda mac: (seen.append(mac) or dict(MessageHeader='Firmware', MessageBody='Test & <text>',
                                                  ContinuePrompt='Install', CancelPrompt='Keep', AllowUpdate='false')),
            self.coordinator, self.root / 'tls', http_port=None, tls_port=0, tcp_port=0)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            transport.start({})
            self.addCleanup(transport.close)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1
            context.set_ciphers('AES256-SHA:@SECLEVEL=0')
            address = transport.servers[0].server_address
            with context.wrap_socket(socket.create_connection(address, timeout=3),
                                     server_hostname='system-api.sonos.com') as tls:
                self.assertEqual(tls.version(), 'TLSv1')
                tls.sendall(b'POST /ws/pm/?wsdl HTTP/1.1\r\nHost: system-api.sonos.com\r\n'
                            b'SOAPAction: "http://cc.ws#GetOnlineUpdateMessage"\r\nContent-Length: 0\r\n\r\n')
                response = http.client.HTTPResponse(tls)
                response.begin()
                self.assertEqual(response.status, 200)
                result = ET.fromstring(response.read()).find('.//GetOnlineUpdateMessageReturn')
                self.assertEqual(result.findtext('MessageBody'), 'Test & <text>')
                self.assertEqual(result.findtext('AllowUpdate'), 'false')
                self.assertEqual(result.findtext('ContinuePrompt'), 'Install')
        self.assertEqual(seen, [A])
        self.assertEqual((self.root / 'tls/update-key.pem').stat().st_mode & 0o777, 0o600)

    def test_local_certificate_is_reused_and_no_embedded_private_key_is_needed(self):
        first_cert, first_key = ensure_certificate(self.root / 'tls')
        original = first_key.read_bytes()
        self.assertEqual(ensure_certificate(self.root / 'tls'), (first_cert, first_key))
        self.assertEqual(first_key.read_bytes(), original)
        self.assertEqual(first_key.stat().st_mode & 0o777, 0o600)

    def test_message_contract_escapes_plain_text_and_preserves_override(self):
        result = ET.fromstring(message_xml(dict(statusCode='1', AllowUpdate='false',
            MessageBody='Line 1\n<a>&', ContinuePrompt='Go', CancelPrompt='Stay')))
        message = result.find('.//GetOnlineUpdateMessageReturn')
        self.assertEqual(message.findtext('MessageBody'), 'Line 1\n<a>&')
        self.assertEqual(message.findtext('AllowUpdate'), 'false')
        with self.assertRaises(UpdateTransportError):
            message_xml({'Buttons': 'arbitrary'})

    @staticmethod
    def _legacy_tls_exchange(address, offered_ciphers, request_bytes):
        import hashlib
        import hmac
        import os
        import struct
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import padding
        import legacy_tls_rc4 as ltls

        cli = socket.create_connection(address, timeout=3)
        try:
            transcript = bytearray()
            client_random = os.urandom(32)
            cs_bytes = b''.join(struct.pack('>H', c) for c in offered_ciphers)
            ch = ltls._hs_msg(0x01, b'\x03\x01' + client_random + b'\x00'
                              + struct.pack('>H', len(cs_bytes)) + cs_bytes + b'\x01\x00')
            transcript.extend(ch)
            ltls._send_record(cli, 0x16, ch)

            _, _, sh = ltls._recv_record(cli)
            transcript.extend(sh)
            server_random = sh[6:38]
            chosen = struct.unpack('>H', sh[39:41])[0]
            _, _, cert_msg = ltls._recv_record(cli)
            transcript.extend(cert_msg)
            c_len = int.from_bytes(cert_msg[7:10], 'big')
            peer_cert = x509.load_der_x509_certificate(cert_msg[10:10 + c_len])
            _, _, shd = ltls._recv_record(cli)
            transcript.extend(shd)

            pms = b'\x03\x01' + os.urandom(46)
            enc_pms = peer_cert.public_key().encrypt(pms, padding.PKCS1v15())
            cke = ltls._hs_msg(0x10, struct.pack('>H', len(enc_pms)) + enc_pms)
            transcript.extend(cke)
            ltls._send_record(cli, 0x16, cke)

            mac_mod = hashlib.sha1 if chosen == ltls.TLS_RSA_WITH_RC4_128_SHA else hashlib.md5
            mac_len = 20 if chosen == ltls.TLS_RSA_WITH_RC4_128_SHA else 16
            master = ltls._tls10_prf(pms, b'master secret', client_random + server_random, 48)
            kb = ltls._tls10_prf(master, b'key expansion', server_random + client_random, 2 * mac_len + 32)
            c_mac = kb[0:mac_len]
            s_mac = kb[mac_len:2 * mac_len]
            c_rc4 = ltls._RC4(kb[2 * mac_len:2 * mac_len + 16])
            s_rc4 = ltls._RC4(kb[2 * mac_len + 16:2 * mac_len + 32])
            c_seq = s_seq = 0

            def send_enc(ct, data):
                nonlocal c_seq
                hdr = struct.pack('>QBBBH', c_seq, ct, 3, 1, len(data))
                m = hmac.new(c_mac, hdr + data, mac_mod).digest()
                c_seq += 1
                ltls._send_record(cli, ct, c_rc4.crypt(data + m))

            def recv_enc():
                nonlocal s_seq
                ct, _, enc = ltls._recv_record(cli)
                dec = s_rc4.crypt(enc)
                pl, got = dec[:-mac_len], dec[-mac_len:]
                hdr = struct.pack('>QBBBH', s_seq, ct, 3, 1, len(pl))
                exp = hmac.new(s_mac, hdr + pl, mac_mod).digest()
                s_seq += 1
                assert hmac.compare_digest(got, exp)
                return ct, pl

            ltls._send_record(cli, 0x14, b'\x01')
            d1 = hashlib.md5(transcript).digest() + hashlib.sha1(transcript).digest()
            c_fin = ltls._hs_msg(0x14, ltls._tls10_prf(master, b'client finished', d1, 12))
            transcript.extend(c_fin)
            send_enc(0x16, c_fin)

            ct, _, pl = ltls._recv_record(cli)
            assert ct == 0x14 and pl == b'\x01'
            ct, s_fin = recv_enc()
            d2 = hashlib.md5(transcript).digest() + hashlib.sha1(transcript).digest()
            assert ct == 0x16 and s_fin == ltls._hs_msg(0x14, ltls._tls10_prf(master, b'server finished', d2, 12))

            send_enc(0x17, request_bytes)
            chunks = bytearray()
            while True:
                try:
                    ct, data = recv_enc()
                except EOFError:
                    break
                if ct == 0x15:
                    break
                if ct == 0x17:
                    chunks.extend(data)
            return chosen, bytes(chunks)
        finally:
            cli.close()

    def test_legacy_rc4_tls10_auto_detected_for_pre_43_controllers(self):
        import legacy_tls_rc4 as ltls

        # Detection only triggers when OpenSSL shares zero ciphers and RC4-SHA/MD5 is offered.
        self.assertFalse(ltls.needs_legacy_rc4([0x0035, 0x0005], frozenset({0x0035})))
        self.assertTrue(ltls.needs_legacy_rc4([0x0005, 0x0004, 0x0064, 0x0060, 0x0003], frozenset({0x0035})))
        self.assertFalse(ltls.needs_legacy_rc4([0x0064, 0x0003], frozenset({0x0035})))

        events = []
        transport = UpdateTransport(
            '127.0.0.1', lambda _: A,
            lambda mac: dict(MessageHeader='Official firmware', MessageBody='Update to 4.3.2',
                             ContinuePrompt='Install', CancelPrompt='Cancel', AllowUpdate='true'),
            self.coordinator, self.root / 'tls',
            request=lambda method, path, source, headers, body: (503, b'No online registration service', 'text/plain', {}),
            log=events.append, http_port=None, tls_port=0, tcp_port=0,
        )
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            transport.start({})
            self.addCleanup(transport.close)
        address = transport.servers[0].server_address

        # Exact CR100 3.6 cipher list -> chooses RC4-SHA (0x0005) and answers GetOnlineUpdateMessage
        req = (b'POST /ws/pm/?wsdl HTTP/1.1\r\nHost: system-api.sonos.com\r\n'
               b'SOAPAction: "http://cc.ws#GetOnlineUpdateMessage"\r\nContent-Length: 4\r\n\r\ntest')
        chosen, raw = self._legacy_tls_exchange(address, [0x0005, 0x0004, 0x0064, 0x0060, 0x0003], req)
        self.assertEqual(chosen, ltls.TLS_RSA_WITH_RC4_128_SHA)
        head, _, body = raw.partition(b'\r\n\r\n')
        self.assertIn(b'200 OK', head)
        result = ET.fromstring(body).find('.//GetOnlineUpdateMessageReturn')
        self.assertEqual(result.findtext('statusCode'), '1')
        self.assertEqual(result.findtext('AllowUpdate'), 'true')
        self.assertEqual(result.findtext('MessageHeader'), 'Official firmware')

        # RC4-MD5-only client -> chooses RC4-MD5 (0x0004), non-update SOAP gets 503 via _HTTP
        optin = (b'POST /ws/sys/?wsdl HTTP/1.1\r\nHost: system-api.sonos.com\r\n'
                 b'SOAPAction: "http://system-api.sonos.com/ws/sys/1.0#usageTracking_optinGet"\r\n'
                 b'Content-Length: 0\r\n\r\n')
        chosen_md5, raw_md5 = self._legacy_tls_exchange(address, [0x0004], optin)
        self.assertEqual(chosen_md5, ltls.TLS_RSA_WITH_RC4_128_MD5)
        self.assertIn(b'503 ', raw_md5.split(b'\r\n', 1)[0])
        self.assertIn({'event': 'online-update-legacy-tls', 'controller': A, 'cipher': 'RC4-SHA'}, events)
        self.assertIn({'event': 'online-update-legacy-tls', 'controller': A, 'cipher': 'RC4-MD5'}, events)

        # Unpaired source IP is still dropped before any legacy TLS handshake
        transport.controller_resolver = lambda _: None
        with self.assertRaises((EOFError, ConnectionResetError, BrokenPipeError)):
            self._legacy_tls_exchange(address, [0x0005], req)


if __name__ == '__main__':
    unittest.main()
