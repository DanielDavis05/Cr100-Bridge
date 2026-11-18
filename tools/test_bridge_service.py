"""Service lifecycle checks with fake processes, real state files, and no radio."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge_service as service

CONTROLLER, CONTROLLER_KEY = '02:00:00:00:00:02', '01' * 16
STATE = {'bssid': '02:00:00:00:00:01', 'household': 'Sonos_test', 'freq': 2437,
         'controllers': [{'mac': CONTROLLER, 'aid': 1, 'key': CONTROLLER_KEY}]}


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

    def run_bridge(self, script, seconds):
        """supervise() on a fake clock for `seconds`. script(name, n, now) gives the exit
        status, or None while running, for the n-th start (0-based) of 'link', 'dhcp' or
        'players' at fake time now. Returns the started processes by name."""
        clock = {'now': 0.0}
        started = {'link': [], 'dhcp': [], 'players': []}
        class Stop:
            def is_set(self): return clock['now'] >= seconds
            def wait(self, delay):
                clock['now'] += delay
                return self.is_set()
        def spawn(args, **kwargs):
            joined = ' '.join(args)
            name = 'link' if 'sonosnet_link' in joined else 'dhcp' if args[0] == 'dnsmasq' else 'players'
            process = Process(args)
            process.pid = 1000 + sum(len(v) for v in started.values())
            process.name, process.number, process.born = name, len(started[name]), clock['now']
            process.poll = lambda p=process: p.returncode if p.terminated else script(p.name, p.number, clock['now'] - p.born)
            started[name].append(process)
            return process
        with patch.object(service, 'check_routes'), patch.object(service, 'prepare_radio'), \
                patch.object(service.subprocess, 'Popen', side_effect=spawn), \
                patch.object(service.subprocess, 'run', return_value=Mock(stdout='inet 192.168.69.1/24')), \
                patch.object(service.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(service, 'log', side_effect=lambda **record: self.logs.append(record)):
            self.logs, self.pids = [], []
            original = Path.write_text
            def record_pid(path, text, *args, **kwargs):
                if path.name == 'players.pid': self.pids.append(int(text))
                return original(path, text, *args, **kwargs)
            with patch.object(Path, 'write_text', record_pid):
                service.supervise(self.state, self.folder / 'config.json', self.run, stop=Stop())
        return started

    def test_players_crash_restarts_them_alone_with_backoff(self):
        # A bad config: the players exit at once, every time.
        started = self.run_bridge(lambda name, n, age: 2 if name == 'players' else None, 20)
        self.assertEqual((len(started['link']), len(started['dhcp'])), (1, 1))   # the CR100's link stays up
        delays = [r['restart_in'] for r in self.logs if r['event'] == 'helper-exited']
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0])
        self.assertEqual(len(started['players']), 5)          # at 0, ~1, ~3, ~7, ~15 s; the next waits 16 s
        self.assertEqual(self.pids, [p.pid for p in started['players']])   # each live player's PID
        self.assertFalse((self.run / 'players.pid').exists())

    def test_a_helper_that_ran_a_while_starts_its_backoff_again(self):
        # DHCP dies at once twice, then runs 90 s and dies again: the wait is back to 1 s.
        lives = {0: 0, 1: 0, 2: 90}
        started = self.run_bridge(lambda name, n, age: 1 if name == 'dhcp' and age >= lives.get(n, 1e9) else None, 100)
        delays = [r['restart_in'] for r in self.logs if r['event'] == 'helper-exited' and r['helper'] == 'dhcp']
        self.assertEqual(delays, [1.0, 2.0, 1.0])
        self.assertEqual(len(started['dhcp']), 4)
        self.assertEqual(len(started['players']), 1)

    def test_radio_link_exit_stops_the_group(self):
        with self.assertRaisesRegex(RuntimeError, 'Radio link exited'):
            self.run_bridge(lambda name, n, age: 3 if name == 'link' and age >= 5 else None, 60)
        self.assertFalse((self.run / 'players.pid').exists())

    def test_child_processes_have_the_expected_arguments(self):
        started = self.run_bridge(lambda name, n, age: None, 1)
        dhcp = started['dhcp'][0].args
        self.assertIn('--interface=sonos0', dhcp)
        self.assertIn('--dhcp-option=3', dhcp)
        self.assertIn('--dhcp-option=6,192.168.69.1', dhcp)
        self.assertIn('--listen-address=192.168.69.1', dhcp)
        self.assertIn('--except-interface=lo', dhcp)
        self.assertIn('--no-resolv', dhcp)
        self.assertIn('--no-hosts', dhcp)
        self.assertIn('--address=/update.sonos.com/update-firmware.sonos.com/system-api.sonos.com/192.168.69.1', dhcp)
        self.assertIn('--local=/update.sonos.com/update-firmware.sonos.com/system-api.sonos.com/', dhcp)
        self.assertNotIn('--port=0', dhcp)
        self.assertNotIn('--seconds', started['link'][0].args)
        self.assertTrue(all(p.terminated for group in started.values() for p in group))   # stopped on the way out

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
            self.pair_command = args
            if output is not None:
                Path(args[args.index('--state') + 1]).write_text(json.dumps(output))
            return Process(args, 0)
        with patch.object(service, 'prepare_radio'), patch.object(service, 'radio_mac', return_value=STATE['bssid']), \
                patch.object(service.subprocess, 'Popen', side_effect=spawn), \
                patch.object(service.subprocess, 'run') as command:
            self.commands = command
            service.pair(self.state, self.run)
        return command

    def test_successful_pair_atomically_adds_controller_with_private_permissions(self):
        added = {'mac': '02:00:00:00:00:03', 'txid': 5, 'time': 10, 'key': '03' * 16}
        command = self.pair_attempt({**STATE, 'controllers': STATE['controllers'] + [added]})
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['controllers'], [STATE['controllers'][0], {**added, 'aid': 2}])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pair_command[self.pair_command.index('--previous-state') + 1], str(self.state))
        command.assert_any_call(['systemctl', '--no-block', 'start', 'cr100-bridge.service'], check=True)
        self.assertFalse(list(self.folder.glob('.pair-*')))

    def test_repair_keeps_existing_controller_and_household_metadata(self):
        previous = {**STATE, 'custom': 'household', 'controllers': [
            {'mac': CONTROLLER, 'aid': 8, 'time': 1, 'custom': 'controller', 'key': CONTROLLER_KEY},
            {'mac': '02:00:00:00:00:03', 'aid': 2, 'key': '03' * 16}]}
        self.state.write_text(json.dumps(previous))
        self.pair_attempt({**STATE, 'controllers': [{'mac': CONTROLLER, 'txid': 9, 'time': 20, 'key': '09' * 16}]})
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['custom'], 'household')
        # The re-paired controller keeps its AID; its old key is accepted until the link hears the new one.
        self.assertEqual(saved['controllers'], [
            {'mac': CONTROLLER, 'aid': 8, 'time': 20, 'txid': 9, 'custom': 'controller', 'key': '09' * 16,
             'previous_key': CONTROLLER_KEY},
            {'mac': '02:00:00:00:00:03', 'aid': 2, 'key': '03' * 16}])

    def test_pairing_cannot_replace_existing_household_credentials(self):
        for field, value in (('household', 'Sonos_changed'), ('bssid', '02:00:00:00:00:03'), ('freq', 2412)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'cannot change'):
                self.pair_attempt({**STATE, field: value})
            self.assertEqual(json.loads(self.state.read_text()), STATE)

    def test_first_pair_creates_canonical_controller_state(self):
        self.state.unlink()
        self.pair_attempt(STATE)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['controllers'], STATE['controllers'])

    def test_validate_state_accepts_multiple_and_needs_an_active_keyed_controller(self):
        state = {**STATE, 'controllers': [{'mac': CONTROLLER, 'aid': 5, 'key': CONTROLLER_KEY},
                                          {'mac': '02:00:00:00:00:03', 'key': '03' * 16}]}
        self.state.write_text(json.dumps(state))
        self.assertEqual([c['aid'] for c in service.validate_state(self.state)['controllers']], [5, 1])
        for controllers, error in (([{'mac': CONTROLLER}], 'link key'),
                                   ([{'mac': CONTROLLER, 'key': CONTROLLER_KEY, 'forgotten': True}], 'No paired')):
            with self.subTest(error=error):
                self.state.write_text(json.dumps({**STATE, 'controllers': controllers}))
                with self.assertRaisesRegex(ValueError, error): service.validate_state(self.state)

    def test_pairing_retains_channel_and_bssid_from_the_current_household(self):
        previous = {**STATE, 'freq': 2462}
        self.state.write_text(json.dumps(previous))
        self.pair_attempt(previous)
        self.assertEqual(self.pair_command[self.pair_command.index('--freq') + 1], '2462')
        self.assertEqual(self.pair_command[self.pair_command.index('--mac') + 1], STATE['bssid'])

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
        with self.assertRaisesRegex(ValueError, 'link key'):
            self.pair_attempt({**STATE, 'controllers': [{'mac': CONTROLLER, 'key': '01'}]})
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
