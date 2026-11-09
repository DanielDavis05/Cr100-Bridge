"""Ordinary startup command deadlines fail closed before radio/helper startup."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge_service as service
import test_bridge_service as fixtures


class StartupCommandBounds(unittest.TestCase):
    def test_route_query_has_a_timeout_and_failure_does_not_authorize_startup(self):
        with patch.object(service.subprocess, 'check_output', return_value='[]') as command:
            service.check_routes()
        self.assertEqual(command.call_args.kwargs['timeout'], service.STARTUP_COMMAND_TIMEOUT)
        with patch.object(service.subprocess, 'check_output',
                          side_effect=subprocess.TimeoutExpired('ip', service.STARTUP_COMMAND_TIMEOUT)):
            with self.assertRaises(subprocess.TimeoutExpired): service.check_routes()

    def test_firmware_banner_probes_each_have_a_timeout_and_continue_after_one_expires(self):
        timeout = subprocess.TimeoutExpired('ethtool', service.STARTUP_COMMAND_TIMEOUT)
        with patch.object(service.subprocess, 'check_output', side_effect=[timeout, 'Nexmon firmware']) as command:
            self.assertTrue(service.nexmon_loaded())
        self.assertEqual(command.call_count, 2)
        self.assertTrue(all(call.kwargs['timeout'] == service.STARTUP_COMMAND_TIMEOUT
                            for call in command.call_args_list))
        with patch.object(service.subprocess, 'check_output', side_effect=timeout) as command:
            self.assertFalse(service.nexmon_loaded())
        self.assertEqual(command.call_count, 3)

    def test_prepare_radio_commands_are_bounded_and_timeout_prevents_injection(self):
        monitor = Mock()
        with patch.object(service, 'nexmon_loaded', return_value=True), \
                patch.object(service, 'verify_injection_ownership'), \
                patch.object(service.subprocess, 'run') as commands, \
                patch('nexmon_power.disable_mpc', return_value=0), \
                patch('sonosnet_link.open_radio', return_value=monitor), patch.object(service, 'log'):
            service.prepare_radio()
        self.assertEqual(commands.call_count, 4)
        self.assertTrue(all(call.kwargs['timeout'] == service.STARTUP_COMMAND_TIMEOUT
                            for call in commands.call_args_list))
        monitor.close.assert_called_once()
        with patch.object(service, 'nexmon_loaded', return_value=True), \
                patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('rfkill', 3)), \
                patch.object(service, 'verify_injection_ownership') as verify, \
                patch('nexmon_power.disable_mpc') as power, patch('sonosnet_link.open_radio') as radio:
            with self.assertRaises(subprocess.TimeoutExpired): service.prepare_radio()
        verify.assert_not_called()
        power.assert_not_called()
        radio.assert_not_called()

    def test_tap_probe_timeout_respects_remaining_window_and_cleans_link(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            state = folder / 'state.json'
            state.write_text(json.dumps(fixtures.STATE))
            link = fixtures.Process(['synthetic link'])
            clock = iter((0., 14.75))
            with patch.object(service, 'check_routes'), patch.object(service, 'prepare_radio'), \
                    patch.object(service.subprocess, 'Popen', return_value=link) as spawn, \
                    patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ip', .25)) as command, \
                    patch.object(service.time, 'monotonic', side_effect=lambda: next(clock, 14.75)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    service.supervise(state, folder / 'config.json', folder / 'run')
            self.assertEqual(command.call_args.kwargs['timeout'], .25)
            self.assertEqual(spawn.call_count, 1)  # DHCP/players never started on an unknown TAP.
            self.assertTrue(link.terminated)

    def test_failed_radio_preparation_clears_previous_runtime_status_before_any_helper_starts(self):
        for error in (RuntimeError('Synthetic unsupported radio'), subprocess.TimeoutExpired('iw', 3)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                state, run = folder / 'state.json', folder / 'run'
                state.write_text(json.dumps(fixtures.STATE))
                run.mkdir()
                old = ('link.jsonl', 'link.jsonl.1', 'players.pid')
                for name in old:
                    (run / name).write_text('12345\n' if name == 'players.pid' else
                                           json.dumps({'event': 'stats', 'associated': True, 'clock': {'ready': True}}))
                (run / 'leases').write_text('preserved lease\n')
                with patch.object(service, 'check_routes'), \
                        patch.object(service, 'prepare_radio', side_effect=error), \
                        patch.object(service.subprocess, 'Popen') as spawn:
                    with self.assertRaises(type(error)):
                        service.supervise(state, folder / 'config.json', run)
                self.assertTrue(all(not (run / name).exists() for name in old))
                self.assertEqual((run / 'leases').read_text(), 'preserved lease\n')
                spawn.assert_not_called()

    def test_invalid_pairing_state_also_retires_old_runtime_status(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            run = folder / 'run'
            run.mkdir()
            for name in ('link.jsonl', 'link.jsonl.1', 'players.pid'):
                (run / name).write_text('old runtime state\n')
            with patch.object(service.subprocess, 'Popen') as spawn:
                with self.assertRaises(FileNotFoundError):
                    service.supervise(folder / 'missing-state.json', folder / 'config.json', run)
            self.assertEqual(list(run.iterdir()), [])
            spawn.assert_not_called()


if __name__ == '__main__': unittest.main()
