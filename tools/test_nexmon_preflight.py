"""Radio startup requires ownership, probe NoAck, clock and hardware beacon stamps."""
import copy
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

import bridge_service as service
import sonosnet_link as link

VALID = {'ok': True, 'ownership': {'magic': 0x4f4e5243, 'version': 1, 'size': 48, 'seen': 0}}
STAMP = dict.fromkeys(('sequence', 'calls', 'beacon_candidates', 'stamped', 'already_stamped',
                       'rejected_headers', 'ioctl_reads', 'last_valid', 'last_low', 'last_high_before',
                       'last_high_after', 'last_bss_index', 'last_prefix', 'last_length_before',
                       'last_length_after', 'reserved'), 0)
STAMP.update(magic=0x51545342, version=1, size=80, capabilities=1)
VALID_RADIO = {'ok': True,
               'noack': {'magic': 0x414e5243, 'version': 1, 'size': 40, 'capabilities': 1},
               'clock': {'magic': 0x51465354, 'version': 1, 'size': 24, 'status': 1,
                         'ready': True, 'tsf_us': 123456},
               'beacon_offsets': {'magic': 0x514d4853, 'version': 1, 'size': 48, 'status': 1,
                                  'ready': True, 'offsets_unsigned': [0, 0, 0, 0]},
               'beacon_stamp': STAMP}


def reply(record, returncode=0):
    return Mock(returncode=returncode, stdout=json.dumps(record))


class FirmwarePreflight(unittest.TestCase):
    def test_verified_running_firmware_allows_radio_without_version_query(self):
        with patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(VALID_RADIO)]) as run:
            service.verify_injection_ownership()
        self.assertEqual(run.call_count, 2)
        self.assertIn('--ownership-only', run.call_args_list[0].args[0])
        self.assertIn('--radio-only', run.call_args_list[1].args[0])
        for call in run.call_args_list:
            self.assertNotIn('--version-only', call.args[0])
            self.assertEqual(call.args[0][-2:], ['--timeout', '3'])
            self.assertEqual(call.kwargs['timeout'], 5)

    def test_missing_wrong_or_unreadable_running_patch_fails_with_install_reboot_remedy(self):
        records = [{'ok': False, 'ownership': {'error': 'not patched'}},
                   {'ok': True, 'ownership': {'magic': 0, 'version': 1, 'size': 48}},
                   {'ok': True, 'ownership': {'magic': 0x4f4e5243, 'version': 2, 'size': 48}},
                   {'ok': True, 'ownership': {'magic': 0x4f4e5243, 'version': 1, 'size': 44}}]
        for value in records:
            with self.subTest(value=value), patch.object(service.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(value))):
                with self.assertRaisesRegex(RuntimeError, 'installer.*reboot'): service.verify_injection_ownership()
        with patch.object(service.subprocess, 'run', side_effect=subprocess.TimeoutExpired('status.py', 5)):
            with self.assertRaisesRegex(RuntimeError, 'GET 430'): service.verify_injection_ownership()

    def test_ownership_only_firmware_and_unready_or_invalid_radio_features_are_rejected(self):
        records = [VALID, {'ok': False, 'noack': {'error': 'unsupported GET 432'},
                          'clock': {'error': 'unsupported GET 433'}},
                   {key: value for key, value in VALID_RADIO.items()
                    if key not in ('beacon_offsets', 'beacon_stamp')}]
        for field, key, values in (
                ('noack', 'magic', (0,)), ('noack', 'version', (2, True)),
                ('noack', 'size', (36,)), ('noack', 'capabilities', (0, 3, True)),
                ('clock', 'magic', (0,)), ('clock', 'version', (2,)), ('clock', 'size', (20,)),
                ('clock', 'status', (0, True)), ('clock', 'ready', (False, 1)),
                ('clock', 'tsf_us', (None, True, 1.0, -1, 1 << 64)),
                ('beacon_offsets', 'magic', (0,)), ('beacon_offsets', 'version', (2, True)),
                ('beacon_offsets', 'size', (16,)), ('beacon_offsets', 'status', (0, True)),
                ('beacon_offsets', 'ready', (False, 1)),
                ('beacon_offsets', 'offsets_unsigned', (None, [], [0, 0, 0], [0, 0, 0, 1],
                                                       [0, 0, False, 0], [0, 0, 0., 0])),
                ('beacon_stamp', 'magic', (0,)), ('beacon_stamp', 'version', (2, True)),
                ('beacon_stamp', 'size', (76,)), ('beacon_stamp', 'capabilities', (0, 3, True)),
                ('beacon_stamp', 'sequence', (1,)), ('beacon_stamp', 'reserved', (1,)),
                ('beacon_stamp', 'last_valid', (2, True)), ('beacon_stamp', 'calls', (-1, 1 << 32, True))):
            for value in values:
                record = copy.deepcopy(VALID_RADIO)
                record[field][key] = value
                records.append(record)
        for record in records:
            with self.subTest(record=record), \
                    patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(record)]):
                with self.assertRaisesRegex(RuntimeError, 'installer.*reboot.*432/433'):
                    service.verify_injection_ownership()

    def test_stamp_snapshot_relations_are_checked_only_for_a_valid_sample(self):
        initial = copy.deepcopy(VALID_RADIO)
        initial['beacon_stamp'].update(rejected_headers=1, last_prefix=7, last_length_before=10)
        with patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(initial)]):
            service.verify_injection_ownership()
        live = copy.deepcopy(VALID_RADIO)
        live['beacon_stamp'].update(last_valid=1, last_low=0x4a00, last_high_before=2,
                                    last_high_after=0x82, last_prefix=4, last_length_before=104,
                                    last_length_after=232)
        for prefix in (0, 4, 16):
            record = copy.deepcopy(live)
            record['beacon_stamp'].update(last_prefix=prefix, last_length_after=104 + 124 + prefix)
            with patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(record)]):
                service.verify_injection_ownership()
        for field, value in (('last_low', 0x4a01), ('last_low', 0x4a80), ('last_low', 0x10000),
                             ('last_high_before', 0x10002), ('last_high_after', 2),
                             ('last_high_after', 0x10082), ('last_bss_index', 4), ('last_prefix', 8),
                             ('last_length_before', 37), ('last_length_before', 513),
                             ('last_length_after', 231)):
            record = copy.deepcopy(live)
            record['beacon_stamp'][field] = value
            with self.subTest(field=field, value=value), \
                    patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(record)]):
                with self.assertRaises(RuntimeError): service.verify_injection_ownership()

    def test_each_diagnostic_must_succeed_with_a_strict_boolean_success_record(self):
        for ownership, radio in (({**VALID, 'ok': 1}, VALID_RADIO),
                                 (VALID, {**VALID_RADIO, 'ok': 'true'})):
            with self.subTest(ownership=ownership['ok'], radio=radio['ok']), \
                    patch.object(service.subprocess, 'run', side_effect=[reply(ownership), reply(radio)]):
                with self.assertRaises(RuntimeError): service.verify_injection_ownership()
        with patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(VALID_RADIO, returncode=1)]):
            with self.assertRaises(RuntimeError): service.verify_injection_ownership()
        with patch.object(service.subprocess, 'run', side_effect=[reply(VALID), subprocess.TimeoutExpired('status.py', 5)]):
            with self.assertRaisesRegex(RuntimeError, 'GET 432/433'): service.verify_injection_ownership()

    def test_unpatched_radio_never_opens_monitor_injection_socket(self):
        with patch.object(service, 'nexmon_loaded', return_value=True), \
                patch.object(service.subprocess, 'run'), \
                patch.object(service, 'verify_injection_ownership', side_effect=RuntimeError('not loaded')), \
                patch('sonosnet_link.open_radio') as radio:
            with self.assertRaisesRegex(RuntimeError, 'not loaded'): service.prepare_radio()
        radio.assert_not_called()

    def test_startup_refuses_old_ownership_only_firmware_before_opening_radio(self):
        def run(command, **_):
            if '--ownership-only' in command or '--radio-only' in command: return reply(VALID)
            return Mock(returncode=0)
        with patch.object(service, 'nexmon_loaded', return_value=True), \
                patch.object(service.subprocess, 'run', side_effect=run), \
                patch('sonosnet_link.open_radio') as radio, \
                patch('nexmon_power.disable_mpc') as power:
            with self.assertRaisesRegex(RuntimeError, '432/433'): service.prepare_radio()
        radio.assert_not_called()
        power.assert_not_called()

    def test_each_reopened_socket_verifies_loaded_firmware_and_closes_on_failure(self):
        radio = Mock()
        with patch.object(link, 'open_radio', return_value=radio), \
                patch.object(service, 'verify_injection_ownership') as verify, \
                patch('nexmon_power.disable_mpc') as power:
            self.assertIs(link.open_verified_radio('mon0', 6), radio)
            self.assertIs(link.open_verified_radio('mon0', 6), radio)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(power.call_count, 2)
        radio.close.assert_not_called()
        with patch.object(link, 'open_radio', return_value=radio), \
                patch.object(service, 'verify_injection_ownership', side_effect=RuntimeError('not patched')):
            with self.assertRaisesRegex(RuntimeError, 'not patched'): link.open_verified_radio('mon0', 6)
        radio.close.assert_called_once_with()

    def test_power_setting_failure_closes_the_socket_before_injection(self):
        radio = Mock()
        with patch.object(link, 'open_radio', return_value=radio), \
                patch.object(service, 'verify_injection_ownership'), \
                patch('nexmon_power.disable_mpc', side_effect=OSError('readback failed')):
            with self.assertRaisesRegex(OSError, 'readback failed'):
                link.open_verified_radio('mon0', 6)
        radio.close.assert_called_once_with()

    def test_radio_feature_failure_closes_a_reopened_socket_before_mpc_or_injection(self):
        radio = Mock()
        with patch.object(link, 'open_radio', return_value=radio), \
                patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(VALID)]), \
                patch('nexmon_power.disable_mpc') as power:
            with self.assertRaisesRegex(RuntimeError, '432/433'):
                link.open_verified_radio('mon0', 6)
        radio.close.assert_called_once_with()
        power.assert_not_called()

    def test_clock_only_firmware_cannot_inject_and_fresh_socket_is_closed(self):
        legacy = {key: value for key, value in VALID_RADIO.items()
                  if key not in ('beacon_offsets', 'beacon_stamp')}
        radio = Mock()
        with patch.object(link, 'open_radio', return_value=radio), \
                patch.object(service.subprocess, 'run', side_effect=[reply(VALID), reply(legacy)]), \
                patch('nexmon_power.disable_mpc') as power:
            with self.assertRaisesRegex(RuntimeError, '435/436'):
                link.open_verified_radio('mon0', 6)
        radio.close.assert_called_once_with()
        radio.send.assert_not_called()
        power.assert_not_called()


if __name__ == '__main__': unittest.main()
