"""Pairing identities remain stable across migration and repeated pairing."""
import unittest

from controller_state import MAX_AID, controller_records, merge_controller, normalize_state

FIRST, SECOND, THIRD = ('02:00:00:00:00:02', '02:00:00:00:00:03', '02:00:00:00:00:04')


class ControllerState(unittest.TestCase):
    def test_legacy_state_and_empty_setup(self):
        self.assertEqual(controller_records({}), [])
        legacy = {'cr100_mac': FIRST.upper(), 'txid': 5, 'time': 10}
        self.assertEqual(controller_records(legacy), [{'mac': FIRST, 'aid': 1, 'txid': 5, 'time': 10}])
        self.assertNotIn('controllers', legacy)

    def test_missing_aids_use_gaps_without_moving_explicit_aids(self):
        state = {'controllers': [{'mac': FIRST}, {'mac': SECOND, 'aid': 1}, {'mac': THIRD}]}
        self.assertEqual([c['aid'] for c in controller_records(state)], [2, 1, 3])
        self.assertEqual(normalize_state(state)['cr100_mac'], FIRST)

    def test_pair_adds_and_repair_preserves_aid_and_other_metadata(self):
        original = {'cr100_mac': FIRST, 'household': 'Sonos_test', 'custom': 'retained',
                    'controllers': [{'mac': FIRST, 'aid': 8, 'time': 1, 'custom': 'controller'}]}
        result = merge_controller(original, {'cr100_mac': SECOND, 'aid': 8, 'time': 2})
        self.assertEqual(result['controllers'][1], {'mac': SECOND, 'aid': 1, 'time': 2})
        result = merge_controller(result, {'cr100_mac': FIRST, 'aid': 1, 'time': 3, 'txid': 4})
        self.assertEqual(result['controllers'][0],
                         {'mac': FIRST, 'aid': 8, 'time': 3, 'txid': 4, 'custom': 'controller'})
        self.assertEqual((result['cr100_mac'], result['custom']), (FIRST, 'retained'))
        self.assertEqual(original['controllers'][0]['time'], 1)

    def test_rejects_invalid_ambiguous_and_multicast_identities(self):
        invalid = [None, {}, ['not a record'], [{'mac': 'broken'}],
                   [{'mac': 'ff:ff:ff:ff:ff:ff'}], [{'mac': '00:00:00:00:00:00'}],
                   [{'mac': FIRST}, {'mac': FIRST.upper()}],
                   [{'mac': FIRST, 'aid': 1}, {'mac': SECOND, 'aid': 1}]]
        invalid += [[{'mac': FIRST, 'aid': aid}] for aid in (0, MAX_AID + 1, True, '1')]
        for records in invalid:
            with self.subTest(records=records), self.assertRaises(ValueError):
                controller_records({'controllers': records})

    def test_exhausted_aids_fail(self):
        records = [{'mac': '02:00:00:00:%02x:%02x' % (i >> 8, i & 255), 'aid': i}
                   for i in range(1, MAX_AID + 1)]
        records.append({'mac': '02:00:00:ff:ff:ff'})
        with self.assertRaisesRegex(ValueError, 'Too many'):
            controller_records({'controllers': records})


if __name__ == '__main__': unittest.main()
