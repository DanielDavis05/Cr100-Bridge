"""Verify the portable tree works without the developer's checkout or secrets."""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import distribution


class Distribution(unittest.TestCase):
    def test_payload_excludes_runtime_data_and_has_standalone_player(self):
        selected = {str(p.relative_to(distribution.ROOT)) for p in distribution.files()}
        self.assertFalse(any('cr100-emulator' in p or '__pycache__' in p or p.endswith('.pcap') for p in selected))
        self.assertNotIn('bridge-state.json', selected)
        self.assertNotIn('bridge-config.json', selected)
        self.assertIn('tools/zp80/fixtures/synthetic_public_key.bin', selected)
        self.assertIn('tools/zp80/fixtures/xml/device_description.xml', selected)
        self.assertIn('tools/webui/app.js', selected)
        self.assertIn('tools/spotify_connect.py', selected)
        self.assertIn('tools/spotify_autoplay.py', selected)
        self.assertIn('tools/nexmon_power.py', selected)
        self.assertIn('tools/nexmon_clock.py', selected)
        self.assertIn('tools/radio_platform.py', selected)
        self.assertIn('tools/ha_hub.py', selected)
        self.assertTrue({'tools/smart_devices.py', 'tools/smart_device_service.py'} <= selected)
        self.assertIn('docs/features/SMART-DEVICES.md', selected)
        self.assertTrue({'tools/online_updates.py', 'tools/update_service.py',
                         'tools/update_transport.py', 'docs/features/ONLINE-UPDATES.md'} <= selected)
        self.assertIn('firmware/nexmon-ownership/apply.py', selected)
        self.assertIn('firmware/nexmon-ownership/status.py', selected)
        self.assertIn('firmware/nexmon-ownership/manifest.json', selected)
        self.assertTrue({'firmware/nexmon-ownership/tsf_query.c',
                         'firmware/nexmon-ownership/tsf_query.h'} <= selected)
        self.assertTrue({'firmware/nexmon-ownership/shm_query.c',
                         'firmware/nexmon-ownership/shm_query.h',
                         'firmware/nexmon-ownership/beacon_stamp.c',
                         'firmware/nexmon-ownership/beacon_stamp.h',
                         'firmware/nexmon-ownership/test_beacon_stamp.c',
                         'firmware/nexmon-ownership/test_shm_query.c'} <= selected)
        self.assertFalse(any(p.startswith('firmware/nexmon-ownership/build/') or
                             (p.startswith('firmware/') and p.endswith('.bin')) for p in selected))
        example = json.loads((distribution.ROOT / 'config/bridge-config.example.json').read_text())
        self.assertIs(example['spotify_music_service']['autoplay'], False)
        private_state = distribution.ROOT / 'bridge-state.json'
        if private_state.exists():
            private_key = json.loads(private_state.read_text())['network_key'].encode()
            for path in distribution.files():
                if private_key in path.read_bytes():
                    self.fail('Private pairing material in distribution: ' +
                              str(path.relative_to(distribution.ROOT)))

    def test_packaged_radio_power_helper_reads_the_bundled_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'app'
            distribution.copy(root)
            # Exercise the lazy status.py dependency from the packaged tree;
            # replace the radio socket so this test never accesses hardware.
            script = f'import sys; sys.path.insert(0, {str(root / "tools")!r})\n' + '''
import socket
import struct
from unittest.mock import Mock, patch
import nexmon_power
control = Mock()
control.__enter__ = Mock(return_value=control)
control.__exit__ = Mock(return_value=False)
control.getsockname.return_value = (1234, 0)
payload = b'NEX\\0' + struct.pack('<III', 0, 262, 0) + bytes(32)
control.sendto.side_effect = lambda packet, destination: len(packet)
control.recvfrom.return_value = (struct.pack('<IHHII', 16 + len(payload), 3, 0, 0, 0) + payload, (0, 0))
with patch.object(socket, 'socket', return_value=control) as factory:
    assert nexmon_power.disable_mpc(getter=nexmon_power._read_mpc) == 0
factory.assert_called_once_with(16, socket.SOCK_RAW, 31)
packet, destination = control.sendto.call_args.args
assert destination == (0, 0)
assert packet[16:] == b'NEX\\0' + struct.pack('<III', 0, 262, 0) + b'mpc\\0'.ljust(32, b'\\0')
'''
            subprocess.run([sys.executable, '-I', '-c', script], check=True)

    def test_isolated_first_boot_http_setup_and_empty_controller_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'app'
            distribution.copy(root)
            config = Path(temp) / 'bridge-config.json'
            config.write_bytes((root / 'config/bridge-config.example.json').read_bytes())
            # Import the shipped player, including its fixtures, in a clean interpreter.
            subprocess.run([sys.executable, '-I', '-c',
                            f'import sys; sys.path.insert(0, {str(root / "tools")!r}); '
                            'import zoneplayers, online_updates, update_transport, update_service, nexmon_clock; '
                            'zp=zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR); '
                            'state=zp.PeerState(zp.Config()); assert state.services'], check=True)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(root / 'tools/webui.py'), '--config', str(config),
                                        '--state', str(Path(temp) / 'missing-state.json'), '--port', str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        status, _, _ = self.request(port, 'GET', '/api/session')
                        if status == 200: break
                    except OSError: pass
                    if process.poll() is not None: self.fail(process.stderr.read().decode())
                    if time.monotonic() > deadline: self.fail('Web UI did not become ready')
                    time.sleep(0.05)
                self.assertEqual(self.request(port, 'GET', '/')[0], 200)
                self.assertEqual(self.request(port, 'POST', '/api/controllers/pair', {})[0], 401)
                status, headers, _ = self.request(port, 'POST', '/api/setup', {'password': 'test-password'})
                self.assertEqual(status, 200)
                cookie = dict(headers)['Set-Cookie'].split(';')[0]
                status, _, data = self.request(port, 'GET', '/api/controllers', cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(data['controllers'], [])
                self.assertIsNone(data['household'])
                self.assertFalse(data['radio_running'])
                self.assertEqual(self.request(port, 'GET', '/api/overview', cookie=cookie)[0], 200)
                self.assertEqual((Path(temp) / 'webui.json').stat().st_mode & 0o777, 0o600)
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stderr.close()

    @staticmethod
    def request(port, method, path, payload=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        headers = {'X-Bridge': '1'}
        if cookie: headers['Cookie'] = cookie
        body = None if payload is None else json.dumps(payload)
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        raw, result_headers, status = response.read(), response.getheaders(), response.status
        connection.close()
        return status, result_headers, json.loads(raw) if path.startswith('/api/') else raw


if __name__ == '__main__': unittest.main()
