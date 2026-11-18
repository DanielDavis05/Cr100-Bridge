"""Offline multi-controller pairing and safe direct-CLI state updates."""
import argparse
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import netstart_pair as pair
from test_bridge_service import CONTROLLER, STATE

SECOND = '02:00:00:00:00:03'


class Pairing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'

    def args(self, **changes):
        fields = dict(state=str(self.path), previous_state=None, mac=None, freq=None, household=None)
        fields.update(changes)
        return argparse.Namespace(**fields)

    def test_direct_pair_reuses_existing_credentials_and_rejects_replacement(self):
        self.path.write_text(json.dumps(STATE))
        initial = pair.initial_state(self.args())
        for field in ('bssid', 'freq', 'household', 'controllers'):
            self.assertEqual(initial[field], STATE[field])
        for change in ({'mac': SECOND}, {'freq': 2412}, {'household': 'Sonos_changed'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'cannot change'):
                pair.initial_state(self.args(**change))
        self.assertEqual(json.loads(self.path.read_text()), STATE)

    def test_first_pair_gets_identity_and_existing_state_can_use_separate_output(self):
        with self.assertRaisesRegex(ValueError, '--mac'):
            pair.initial_state(self.args())
        initial = pair.initial_state(self.args(mac=STATE['bssid']))
        self.assertEqual(initial['freq'], 2412)
        self.assertEqual(initial['controllers'], [])
        self.assertNotIn('network_key', initial)
        self.assertTrue(initial['household'].startswith('Sonos_'))
        self.assertFalse(self.path.exists())
        self.path.write_text(json.dumps(STATE))
        output = self.path.with_name('output.json')
        existing = pair.initial_state(self.args(state=str(output), previous_state=str(self.path)))
        self.assertEqual(existing['controllers'], STATE['controllers'])
        self.assertFalse(output.exists())

    def test_atomic_save_adds_controllers_and_repair_keeps_station_identity(self):
        self.path.write_text(json.dumps(STATE))
        initial = pair.initial_state(self.args())
        result = pair.save_pairing(self.path, initial, [{'mac': SECOND, 'txid': 6, 'time': 10, 'key': '06' * 16}])
        self.assertEqual([c['aid'] for c in result['controllers']], [1, 2])
        result = pair.save_pairing(self.path, result, [{'mac': SECOND, 'txid': 7, 'time': 11, 'key': '07' * 16}])
        self.assertEqual(len(result['controllers']), 2)
        self.assertEqual(result['controllers'][1], {'mac': SECOND, 'aid': 2, 'txid': 7, 'time': 11,
                                                    'key': '07' * 16, 'previous_key': '06' * 16})
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob('.pair-state-*')))

    def test_failed_atomic_save_retains_original_state(self):
        self.path.write_text(json.dumps(STATE))
        with patch.object(pair.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(OSError, 'disk error'):
                pair.save_pairing(self.path, STATE, [{'mac': SECOND, 'key': '06' * 16}])
        self.assertEqual(json.loads(self.path.read_text()), STATE)
        self.assertFalse(list(self.path.parent.glob('.pair-state-*')))

    def test_interleaved_queries_pair_both_and_cache_each_ciphertext(self):
        pairer = pair.Pairer(pair.player_uuid(STATE['bssid']), STATE['household'].encode(),
                            STATE['freq'], log=lambda _: None)
        txid = struct.pack('!I', 5)
        query = pair.encode(pair.QUERY, [(0, pairer.uuid), (1, txid), (3, pair.PLAYER_PUBLIC_KEY)])
        first = pairer.handle(CONTROLLER, query)
        second = pairer.handle(SECOND, query)
        self.assertEqual(pairer.handle(CONTROLLER, query), first)
        self.assertEqual(pairer.handle(SECOND, query), second)
        self.assertNotEqual(first, second)
        self.assertEqual(set(pairer.paired_controllers), {CONTROLLER, SECOND})
        self.assertEqual(len(pairer.responses), 2)

    def test_each_controller_gets_its_own_key_and_keeps_it_across_retries(self):
        keys = iter([bytes([n]) * 16 for n in range(1, 10)])
        encrypted = []
        def rsa(public_key, message):
            encrypted.append(message)
            return bytes(128)
        pairer = pair.Pairer(pair.player_uuid(STATE['bssid']), STATE['household'].encode(), STATE['freq'],
                             log=lambda _: None, new_key=lambda: next(keys))
        query = lambda txid: pair.encode(pair.QUERY, [(0, pairer.uuid), (1, struct.pack('!I', txid)),
                                                     (3, pair.PLAYER_PUBLIC_KEY)])
        with patch.object(pair, 'rsa_encrypt_key', side_effect=rsa):
            pairer.handle(CONTROLLER, query(1))
            pairer.handle(SECOND, query(1))
            pairer.handle(CONTROLLER, query(2))   # a new transaction from the same CR100
        self.assertEqual(encrypted, [bytes([1]) * 16, bytes([2]) * 16, bytes([1]) * 16])
        self.assertEqual(pairer.paired_controllers[CONTROLLER]['key'], '01' * 16)
        self.assertEqual(pairer.paired_controllers[SECOND]['key'], '02' * 16)
        self.assertEqual(pairer.paired_controllers[CONTROLLER]['mac'], CONTROLLER)

    def test_random_keys_differ_per_controller(self):
        pairer = pair.Pairer(pair.player_uuid(STATE['bssid']), STATE['household'].encode(), STATE['freq'],
                             log=lambda _: None)
        query = pair.encode(pair.QUERY, [(0, pairer.uuid), (1, struct.pack('!I', 1)), (3, pair.PLAYER_PUBLIC_KEY)])
        pairer.handle(CONTROLLER, query)
        pairer.handle(SECOND, query)
        keys = {record['key'] for record in pairer.paired_controllers.values()}
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(len(bytes.fromhex(key)) == 16 for key in keys))


if __name__ == '__main__': unittest.main()
