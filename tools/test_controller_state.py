"""Pairing identities and link keys stay stable across repeated pairing, forget and restore."""
import unittest

from controller_state import (MAX_AID, active_controllers, confirm_key, controller_records, merge_controller,
                              normalize_state, set_forgotten)

FIRST, SECOND, THIRD = ('02:00:00:00:00:02', '02:00:00:00:00:03', '02:00:00:00:00:04')
K1, K2, K3, K4 = ('11' * 16, '22' * 16, '33' * 16, '44' * 16)


class ControllerState(unittest.TestCase):
    def test_empty_setup_and_obsolete_fields(self):
        self.assertEqual(controller_records({}), [])
        # Legacy single-controller and shared-key fields are not part of the state any more.
        state = normalize_state({'cr100_mac': FIRST, 'network_key': K1,
                                 'controllers': [{'mac': SECOND, 'key': K2.upper()}]})
        self.assertNotIn('cr100_mac', state)
        self.assertNotIn('network_key', state)
        self.assertEqual(state['controllers'], [{'mac': SECOND, 'key': K2, 'aid': 1}])

    def test_missing_aids_use_gaps_without_moving_explicit_aids(self):
        state = {'controllers': [{'mac': FIRST, 'key': K1}, {'mac': SECOND, 'aid': 1, 'key': K2},
                                 {'mac': THIRD, 'key': K3}]}
        self.assertEqual([c['aid'] for c in controller_records(state)], [2, 1, 3])

    def test_pair_adds_and_repair_preserves_aid_and_other_metadata(self):
        original = {'household': 'Sonos_test', 'custom': 'retained',
                    'controllers': [{'mac': FIRST, 'aid': 8, 'time': 1, 'custom': 'controller', 'key': K1}]}
        result = merge_controller(original, {'mac': SECOND, 'aid': 8, 'time': 2, 'key': K2})
        self.assertEqual(result['controllers'][1], {'mac': SECOND, 'aid': 1, 'time': 2, 'key': K2})
        result = merge_controller(result, {'mac': FIRST, 'aid': 1, 'time': 3, 'txid': 4, 'key': K1})
        self.assertEqual(result['controllers'][0],
                         {'mac': FIRST, 'aid': 8, 'time': 3, 'txid': 4, 'custom': 'controller', 'key': K1})
        self.assertEqual(result['custom'], 'retained')
        self.assertEqual(original['controllers'][0]['time'], 1)

    def test_repair_with_new_key_keeps_previous_until_confirmed(self):
        state = {'controllers': [{'mac': FIRST, 'key': K1}, {'mac': SECOND, 'key': K2}]}
        state = merge_controller(state, {'mac': FIRST, 'key': K3})
        self.assertEqual(state['controllers'][0], {'mac': FIRST, 'aid': 1, 'key': K3, 'previous_key': K1})
        # A second re-pair before confirmation supersedes the first new key, not the original.
        state = merge_controller(state, {'mac': FIRST, 'key': K4})
        self.assertEqual(state['controllers'][0]['previous_key'], K3)
        # Confirming a key that is not current changes nothing.
        self.assertEqual(confirm_key(state, FIRST, K3)['controllers'][0]['previous_key'], K3)
        state = confirm_key(state, FIRST, K4)
        self.assertEqual(state['controllers'][0], {'mac': FIRST, 'aid': 1, 'key': K4})
        self.assertEqual(state['controllers'][1], {'mac': SECOND, 'aid': 2, 'key': K2})

    def test_forget_and_restore_keep_aid_key_and_metadata(self):
        state = {'controllers': [{'mac': FIRST, 'aid': 5, 'key': K1, 'time': 1},
                                 {'mac': SECOND, 'aid': 9, 'key': K2}]}
        forgotten = set_forgotten(state, FIRST.upper(), True)
        self.assertEqual(forgotten['controllers'][0],
                         {'mac': FIRST, 'aid': 5, 'key': K1, 'time': 1, 'forgotten': True})
        self.assertEqual([c['mac'] for c in active_controllers(forgotten)], [SECOND])
        # A forgotten controller keeps its AID reserved: a new pairing does not take it.
        added = merge_controller(forgotten, {'mac': THIRD, 'key': K3})
        self.assertEqual(added['controllers'][2]['aid'], 1)
        restored = set_forgotten(added, FIRST, False)
        self.assertEqual(restored['controllers'][0], {'mac': FIRST, 'aid': 5, 'key': K1, 'time': 1})

    def test_forgetting_drops_a_superseded_key(self):
        state = {'controllers': [{'mac': FIRST, 'key': K1, 'previous_key': K2}, {'mac': SECOND, 'key': K3}]}
        self.assertNotIn('previous_key', set_forgotten(state, FIRST, True)['controllers'][0])

    def test_pairing_a_forgotten_controller_restores_it_with_only_the_new_key(self):
        state = set_forgotten({'controllers': [{'mac': FIRST, 'aid': 3, 'key': K1}, {'mac': SECOND, 'key': K2}]},
                              FIRST, True)
        state = merge_controller(state, {'mac': FIRST, 'key': K3, 'time': 7})
        # Its old key belonged to a forgotten device, so it is not accepted as a fallback.
        self.assertEqual(state['controllers'][0], {'mac': FIRST, 'aid': 3, 'key': K3, 'time': 7})

    def test_last_active_controller_cannot_be_forgotten(self):
        state = {'controllers': [{'mac': FIRST, 'key': K1}, {'mac': SECOND, 'key': K2}]}
        state = set_forgotten(state, FIRST, True)
        with self.assertRaisesRegex(ValueError, 'last active'):
            set_forgotten(state, SECOND, True)
        # Forgetting an already forgotten controller is not blocked by that rule.
        self.assertTrue(set_forgotten(state, FIRST, True)['controllers'][0]['forgotten'])
        with self.assertRaises(LookupError):
            set_forgotten(state, THIRD, True)

    def test_rejects_invalid_ambiguous_and_multicast_identities(self):
        invalid = [None, {}, ['not a record'], [{'mac': 'broken', 'key': K1}],
                   [{'mac': 'ff:ff:ff:ff:ff:ff', 'key': K1}], [{'mac': '00:00:00:00:00:00', 'key': K1}],
                   [{'mac': FIRST, 'key': K1}, {'mac': FIRST.upper(), 'key': K2}],
                   [{'mac': FIRST, 'aid': 1, 'key': K1}, {'mac': SECOND, 'aid': 1, 'key': K2}],
                   [{'mac': FIRST}], [{'mac': FIRST, 'key': K1[:-2]}], [{'mac': FIRST, 'key': 'zz' * 16}],
                   [{'mac': FIRST, 'key': K1, 'previous_key': 'short'}],
                   [{'mac': FIRST, 'key': K1, 'forgotten': 'yes'}]]
        invalid += [[{'mac': FIRST, 'aid': aid, 'key': K1}] for aid in (0, MAX_AID + 1, True, '1')]
        for records in invalid:
            with self.subTest(records=records), self.assertRaises(ValueError):
                controller_records({'controllers': records})

    def test_exhausted_aids_fail(self):
        records = [{'mac': '02:00:00:00:%02x:%02x' % (i >> 8, i & 255), 'aid': i, 'key': K1}
                   for i in range(1, MAX_AID + 1)]
        records.append({'mac': '02:00:00:ff:ff:ff', 'key': K1})
        with self.assertRaisesRegex(ValueError, 'Too many'):
            controller_records({'controllers': records})


if __name__ == '__main__': unittest.main()
