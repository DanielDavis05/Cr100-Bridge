"""Service lifecycle checks with fake processes, real state files, and no radio."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge_service as service

STATE = {'bssid': '02:00:00:00:00:01', 'cr100_mac': '02:00:00:00:00:02',
         'network_key': '01' * 16, 'household': 'Sonos_test', 'freq': 2437}


class Process:
    pid = 12345
    def __init__(self, args, code=None):
        self.args, self.returncode, self.terminated = args, code, False
    def poll(self): return self.returncode
    def terminate(self): self.terminated, self.returncode = True, -15
    def kill(self): self.returncode = -9
    def wait(self, timeout=None): return self.returncode or 0


class Services(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state = self.folder / 'bridge-state.json'
        self.state.write_text(json.dumps(STATE))
        self.run = self.folder / 'run'

    def test_routes_reject_upstream_collision_but_allow_previous_tap(self):
        with patch.object(service.subprocess, 'check_output', return_value=json.dumps([
                {'dst': 'default', 'dev': 'eth0'}, {'dst': '192.168.69.0/24', 'dev': 'sonos0'}])):
            service.check_routes()
        with patch.object(service.subprocess, 'check_output', return_value=json.dumps([
                {'dst': '192.168.0.0/16', 'dev': 'eth0'}])):
            with self.assertRaisesRegex(RuntimeError, 'overlaps'): service.check_routes()

    def test_radio_refuses_stock_firmware_without_reconfiguring_interfaces(self):
        with patch.object(service.subprocess, 'check_output', return_value='driver: brcmfmac\nfirmware-version: stock'), \
                patch.object(service.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Reboot'): service.prepare_radio()
            command.assert_not_called()

    def test_nexmon_is_recognised_from_the_kernel_log(self):
        # Kali's brcmfmac-nexmon 6.12.2 (Pi 4, 2026-11): ethtool shows only the version.
        outputs = {'ethtool': 'driver: brcmfmac\nversion: 7.45.206\nfirmware-version: 01-88ee44ea',
                   'journalctl': 'brcmfmac: brcmf_c_preinit_dcmds: Firmware: BCM4345/6 wl0: Jan 12 2025 20:03:13 '
                                 'version 7.45.206 (nexmon.org: 2.2.2-552-gb8c6-2) FWID 01-88ee44ea'}
        with patch.object(service.subprocess, 'check_output', side_effect=lambda command, **kw: outputs[command[0]]):
            self.assertTrue(service.nexmon_loaded())

    def test_child_failure_cleans_up_group_and_player_pid(self):
        started = []
        def spawn(args, **kwargs):
            process = Process(args, 7 if 'zoneplayers.py' in ' '.join(args) else None)
            started.append(process)
            return process
        with patch.object(service, 'check_routes'), patch.object(service, 'prepare_radio'), \
                patch.object(service.subprocess, 'Popen', side_effect=spawn), \
                patch.object(service.subprocess, 'run', return_value=Mock(stdout='inet 192.168.69.1/24')), \
                patch.object(service.threading, 'Event', return_value=Mock(is_set=lambda: False, wait=lambda _: False)):
            with self.assertRaisesRegex(RuntimeError, 'Essential bridge process exited'):
                service.supervise(self.state, self.folder / 'config.json', self.run)
        self.assertEqual(len(started), 3)
        self.assertTrue(all(p.terminated for p in started[:2]))
        self.assertFalse((self.run / 'players.pid').exists())
        dhcp = started[1].args
        self.assertIn('--interface=sonos0', dhcp)
        self.assertIn('--dhcp-option=3', dhcp)
        self.assertIn('--dhcp-option=6', dhcp)
        self.assertNotIn('--seconds', started[0].args)

    def test_tap_timeout_stops_radio_before_starting_dhcp(self):
        link = Process(['link'])
        with patch.object(service, 'check_routes'), patch.object(service, 'prepare_radio'), \
                patch.object(service.subprocess, 'Popen', return_value=link) as spawn, \
                patch.object(service.subprocess, 'run', return_value=Mock(stdout='')), \
                patch.object(service.time, 'monotonic', side_effect=[0, 20, 20, 20]):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                service.supervise(self.state, self.folder / 'config.json', self.run)
        spawn.assert_called_once()
        self.assertTrue(link.terminated)

    def pair_attempt(self, output):
        def spawn(args, **kwargs):
            if output is not None:
                Path(args[args.index('--state') + 1]).write_text(json.dumps(output))
            return Process(args, 0)
        with patch.object(service, 'prepare_radio'), patch.object(service, 'radio_mac', return_value=STATE['bssid']), \
                patch.object(service.subprocess, 'Popen', side_effect=spawn), \
                patch.object(service.subprocess, 'run') as command:
            self.commands = command
            service.pair(self.state, self.run)
        return command

    def test_successful_pair_atomically_replaces_state_with_private_permissions(self):
        changed = {**STATE, 'household': 'Sonos_new', 'network_key': '02' * 16}
        command = self.pair_attempt(changed)
        self.assertEqual(json.loads(self.state.read_text()), changed)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        command.assert_any_call(['systemctl', '--no-block', 'start', 'cr100-bridge.service'], check=True)
        self.assertFalse(list(self.folder.glob('.pair-*')))

    def test_pair_timeout_preserves_and_restarts_previous_pairing(self):
        with self.assertRaisesRegex(RuntimeError, 'No CR100 responded'): self.pair_attempt(None)
        self.assertEqual(json.loads(self.state.read_text()), STATE)
        self.commands.assert_any_call(['systemctl', '--no-block', 'start', 'cr100-bridge.service'], check=True)
        self.assertFalse(list(self.folder.glob('.pair-*')))

    def test_first_pair_timeout_leaves_bridge_unpaired(self):
        self.state.unlink()
        with self.assertRaisesRegex(RuntimeError, 'No CR100 responded'): self.pair_attempt(None)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(self.commands.call_args_list), 1)  # stopped, never restarted without state

    def test_invalid_pair_output_cannot_destroy_previous_pairing(self):
        with self.assertRaisesRegex(ValueError, 'network key'):
            self.pair_attempt({**STATE, 'network_key': '01'})
        self.assertEqual(json.loads(self.state.read_text()), STATE)


class PairingUI(unittest.TestCase):
    def test_managed_pairing_status_and_fixed_service_start(self):
        import webui
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            bridge = webui.Bridge(folder / 'config', folder / 'state', None, folder / 'pid',
                                  webui.DataFile(folder / 'webui.json'), managed=True)
            with patch.object(webui.subprocess, 'run', return_value=Mock(stdout='ActiveState=activating\nResult=success\n')) as command:
                self.assertEqual(bridge.pairing_status(), {'available': True, 'running': True, 'failed': False})
                bridge.pair_controller()
                command.assert_called_with(['systemctl', '--no-block', 'start', 'cr100-pair.service'],
                                           capture_output=True, text=True, timeout=5, check=True)
            bridge.managed = False
            with self.assertRaisesRegex(webui.ApiError, 'installed bridge services'): bridge.pair_controller()


if __name__ == '__main__': unittest.main()
