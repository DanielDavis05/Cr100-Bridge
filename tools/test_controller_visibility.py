"""Stable identities and explicit/inherited controller component policies."""
import copy
import unittest

from controller_visibility import (BRIDGE_COMPONENT, component_visible, configured_components,
                                   room_component_key, validate_selection, visible_components)

CR = '00:0e:58:01:02:03'
OTHER = '00:0e:58:04:05:06'


class Visibility(unittest.TestCase):
    def test_inherited_empty_and_independent_explicit_selections(self):
        config = {'controllers': {CR: {'name': 'Kitchen', 'visible_components': ['sonos:ONE']},
                                  OTHER: {'visible_components': []}}}
        self.assertEqual(visible_components(config, CR.upper()), {'sonos:ONE'})
        self.assertTrue(component_visible(config, CR, 'sonos:ONE'))
        self.assertFalse(component_visible(config, CR, BRIDGE_COMPONENT))
        self.assertFalse(component_visible(config, OTHER, 'sonos:ONE'))
        self.assertTrue(component_visible(config, None, 'sonos:ONE'))
        self.assertTrue(component_visible(config, '00:0e:58:07:08:09', 'sonos:ONE'))
        self.assertIsNone(visible_components({'controllers': None}, CR))
        for profile in (None, {}, {'visible_components': None}):
            self.assertIsNone(visible_components({'controllers': {CR: profile}}, CR))

    def test_component_keys_survive_names_order_and_ha_shorthand(self):
        self.assertEqual(room_component_key(('sonos', 'RINCON_ONE'), 3), 'sonos:RINCON_ONE')
        self.assertEqual(room_component_key(None, 8), 'sim:8')
        original = {'rooms': [{'entity': 'media_player.kitchen', 'name': 'Kitchen', 'slot': 2},
                              {'name': 'Spare', 'slot': 8}]}
        renamed = {'rooms': [{'name': 'Guest room', 'slot': 8},
                             {'backend': 'ha', 'id': 'media_player.kitchen', 'name': 'Cooking', 'slot': 2}]}
        self.assertEqual({item['key'] for item in configured_components(original)},
                         {item['key'] for item in configured_components(renamed)})

    def test_validation_rejects_duplicates_unknowns_and_bad_types_without_mutation(self):
        valid = ['sonos:ONE', BRIDGE_COMPONENT]
        before = copy.deepcopy(valid)
        self.assertEqual(validate_selection(valid, set(valid)), valid)
        self.assertEqual(valid, before)
        self.assertIsNone(validate_selection(None, set()))
        for value in ({}, 'sonos:ONE', True, ['sonos:ONE', 'sonos:ONE'], [None], ['bad\nkey'], ['unknown']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_selection(value, set(valid))


if __name__ == '__main__': unittest.main()
