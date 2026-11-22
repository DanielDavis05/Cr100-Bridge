"""Pure catalogue rules. All identities and state are invented local fixtures."""
import copy
import unittest
import unicodedata

from smart_devices import SmartCatalogue, area_container, room_container, unavailable_row


NATIVE = 'RINCON_00000000000101400'


def fixture():
    snapshot = {'areas': [
        {'area_id': 'galley', 'name': 'Galley', 'floor_id': 'lower'},
        {'area_id': 'study', 'name': 'Study', 'floor_id': 'upper'},
        {'area_id': 'deck', 'name': 'Deck', 'floor_id': None}],
        'floors': [{'floor_id': 'lower', 'name': 'Lower Floor', 'level': 0},
                   {'floor_id': 'upper', 'name': 'Upper Floor', 'level': 1}],
        'devices': [{'id': 'speaker', 'area_id': 'galley', 'identifiers': [['sonos', NATIVE]],
                     'configuration_url': 'http://127.0.0.1:1400/support/review'},
                    {'id': 'lamp-device', 'area_id': 'galley'},
                    {'id': 'child', 'parent_device_id': 'lamp-device'}],
        'entities': []}
    states = {}
    def add(entity, name, area='galley', state='off', **attrs):
        snapshot['entities'].append({'entity_id': entity, 'area_id': area, 'platform': 'fixture'})
        states[entity] = {'entity_id': entity, 'state': state,
                          'attributes': {'friendly_name': name, **attrs}}
    add('media_player.lab', 'Galley Player', area=None)
    snapshot['entities'][-1].update(device_id='speaker', platform='sonos', unique_id=NATIVE)
    add('light.ceiling', 'Galley Ceiling', state='on', brightness=128, supported_color_modes=['brightness'])
    add('light.counter_a', 'Galley Counter A', state='on', brightness=204)
    add('light.counter_b', 'Galley Counter B', brightness=64)
    add('light.counter', 'Galley Counter', state='on', brightness=204,
        entity_id=['light.counter_a', 'light.counter_b'])
    snapshot['entities'][-1]['platform'] = 'hue'
    add('switch.plug', 'Galley Coffee Plug', state='on')
    add('input_boolean.helper', 'Galley Helper')
    add('fan.extractor', 'Galley Extractor Fan', percentage=40, supported_features=1)
    add('scene.evening', 'Galley Evening', state='2026-11-01T00:00:00Z')
    add('script.tidy', 'Galley Tidy', state='off')
    add('light.study', 'Study Desk', area='study', brightness=25)
    add('switch.deck', 'Deck String Lights', area='deck')
    return snapshot, states


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.states = fixture()
        self.room = {'backend': 'ha', 'key': 'media_player.lab', 'name': 'Lab'}

    def model(self, previous=None):
        return SmartCatalogue(self.snapshot, self.states, previous=previous)

    def entry(self, entity):
        return next(entry for entry in self.snapshot['entities'] if entry['entity_id'] == entity)

    def row(self, entity, container='SD:'):
        return next(row for row in self.model().rows(container, [self.room]) if row.row_id == entity)

    def test_supported_domains_and_required_state_only(self):
        for domain in ('cover', 'climate', 'lock', 'alarm_control_panel', 'button', 'siren', 'valve', 'sensor'):
            entity = domain + '.excluded'
            self.snapshot['entities'].append({'entity_id': entity, 'area_id': 'galley'})
            self.states[entity] = {'state': 'on', 'attributes': {}}
        self.states.pop('switch.plug')
        model = self.model()
        self.assertEqual({entry['domain'] for entry in model.entities.values()},
                         {'light', 'switch', 'input_boolean', 'fan', 'scene', 'script'})
        self.assertNotIn('switch.plug', model.entities)
        self.assertNotIn('media_player.lab', model.entities)
        self.assertFalse(any(entity.endswith('.excluded') for entity in model.entities))

    def test_entity_ids_remain_ascii_ha_identifiers(self):
        for entity in ('light.café', 'switch.bad:row', 'light.space name', 'light.Bad', 'light.'):
            self.snapshot['entities'].append({'entity_id': entity, 'area_id': 'galley'})
            self.states[entity] = {'state': 'on', 'attributes': {'friendly_name': 'Invalid'}}
        model = self.model()
        self.assertFalse(any(row.name == 'Invalid' for row in model.rows('SD:', [self.room])))
        self.assertTrue(all(entity.isascii() for entity in model.entities))

    def test_hidden_disabled_category_and_missing_area_filtered(self):
        for entity, field, value in [('light.ceiling', 'hidden_by', 'user'),
                                     ('light.counter_a', 'disabled_by', 'integration'),
                                     ('switch.plug', 'entity_category', 'config'),
                                     ('input_boolean.helper', 'entity_category', 'diagnostic')]:
            self.entry(entity)[field] = value
        self.entry('light.counter_b')['area_id'] = None
        model = self.model()
        for entity in ('light.ceiling', 'light.counter_a', 'switch.plug', 'input_boolean.helper', 'light.counter_b'):
            self.assertNotIn(entity, model.entities)

    def test_sonos_configuration_switches_never_appear(self):
        self.entry('switch.plug').update(platform='sonos', entity_category='config')
        self.assertNotIn('switch.plug', self.model().entities)
        self.assertFalse(any('Coffee' in row.title for row in self.model().rows('SD:', [self.room])))

    def test_entity_area_wins_over_device_then_parent_inheritance(self):
        self.entry('light.ceiling').update(device_id='child', area_id='study')
        self.assertEqual(self.model().entities['light.ceiling']['area_id'], 'study')
        self.entry('light.ceiling')['area_id'] = None
        self.assertEqual(self.model().entities['light.ceiling']['area_id'], 'galley')
        self.snapshot['devices'][1]['area_id'] = None
        self.snapshot['devices'][1]['parent_device_id'] = 'child'
        self.assertNotIn('light.ceiling', self.model().entities)  # cycle terminates

    def test_room_entity_device_and_explicit_override(self):
        model = self.model()
        self.assertEqual(model.resolve_area(self.room), 'galley')
        self.entry('media_player.lab')['area_id'] = 'study'
        self.assertEqual(self.model().resolve_area(self.room), 'study')
        self.assertEqual(model.resolve_area({**self.room, 'devices_area': 'deck'}), 'deck')
        self.assertIsNone(model.resolve_area({**self.room, 'devices_area': ''}))
        self.assertIsNone(model.resolve_area({**self.room, 'devices_area': 'missing'}))

    def test_native_room_exact_identity_join_and_no_url_guess(self):
        model = self.model()
        self.assertEqual(model.resolve_area({'backend': 'sonos', 'key': NATIVE}), 'galley')
        self.assertEqual(model.resolve_area({'backend': 'sonos', 'key': 'opaque',
                                            'identity': {'id': 'uuid:' + NATIVE}}), 'galley')
        self.assertIsNone(model.resolve_area({'backend': 'sonos', 'key': 'RINCON_other',
            'identity': {'ip': '127.0.0.1', 'url': 'http://127.0.0.1:1400'}}))
        self.snapshot['devices'][0]['identifiers'] = []
        self.assertEqual(self.model().resolve_area({'backend': 'sonos', 'key': NATIVE}), 'galley')
        self.entry('media_player.lab')['platform'] = 'other'
        self.assertIsNone(self.model().resolve_area({'backend': 'sonos', 'key': NATIVE}))

    def test_unassigned_room_has_only_other_rooms(self):
        rows = self.model().rows('SD:', [{**self.room, 'devices_area': ''}])
        self.assertEqual([row.name for row in rows], ['Other Rooms'])
        self.assertEqual(len(self.model().rows('SD:other', [{**self.room, 'devices_area': ''}])), 3)

    def test_hue_group_and_individual_member_folder(self):
        rows = self.model().rows('SD:', [self.room])
        self.assertEqual([row.row_id for row in rows], ['SD:all:galley', 'light.counter',
            'light.ceiling', 'switch.plug', 'fan.extractor', 'input_boolean.helper',
            'SD:a:galley:scenes', 'SD:a:galley:lights', 'SD:other'])
        self.assertEqual([row.row_id for row in self.model().rows('SD:a:galley:lights', [self.room])],
                         ['light.counter_a', 'light.counter_b'])
        self.assertEqual(self.row('light.counter').targets, ('light.counter',))

    def test_full_area_group_collapses_but_single_lamp_group_remains(self):
        self.states['light.counter']['attributes']['entity_id'] = [
            'light.ceiling', 'light.counter_a', 'light.counter_b']
        self.assertNotIn('light.counter', [row.row_id for row in self.model().rows('SD:', [self.room])])
        self.states.pop('light.counter_a')
        self.states.pop('light.counter_b')
        self.states['light.counter']['attributes']['entity_id'] = ['light.ceiling']
        rows = self.model().rows('SD:', [self.room])
        self.assertIn('light.counter', [row.row_id for row in rows])
        self.assertNotIn('SD:all:galley', [row.row_id for row in rows])

    def test_redundant_only_group_keeps_every_individual_lamp_in_main_list(self):
        self.states['light.counter']['attributes']['entity_id'] = [
            'light.ceiling', 'light.counter_a', 'light.counter_b']
        model = self.model()
        rows = model.rows('SD:', [self.room])
        self.assertEqual([row.row_id for row in rows], [
            'SD:all:galley', 'light.ceiling', 'light.counter_a', 'light.counter_b',
            'switch.plug', 'fan.extractor', 'input_boolean.helper', 'SD:a:galley:scenes', 'SD:other'])
        self.assertEqual(rows[0].targets, ('light.ceiling', 'light.counter_a', 'light.counter_b'))
        self.assertEqual(model.grouped_lights, frozenset())
        self.assertNotIn('SD:a:galley:lights', model.containers([self.room]))

    def test_redundant_plus_partial_group_hides_only_retained_group_members(self):
        self.snapshot['entities'].append({'entity_id': 'light.everything', 'area_id': 'galley', 'platform': 'hue'})
        self.states['light.everything'] = {'state': 'on', 'attributes': {
            'friendly_name': 'Galley Everything', 'brightness': 128,
            'entity_id': ['light.ceiling', 'light.counter_a', 'light.counter_b']}}
        model = self.model()
        rows = model.rows('SD:', [self.room])
        self.assertEqual([row.row_id for row in rows], ['SD:all:galley', 'light.counter',
            'light.ceiling', 'switch.plug', 'fan.extractor', 'input_boolean.helper',
            'SD:a:galley:scenes', 'SD:a:galley:lights', 'SD:other'])
        self.assertEqual(rows[0].targets, ('light.ceiling', 'light.counter_a', 'light.counter_b'))
        self.assertEqual(model.grouped_lights, frozenset(('light.counter_a', 'light.counter_b')))
        self.assertEqual([row.row_id for row in model.rows('SD:a:galley:lights', [self.room])],
                         ['light.counter_a', 'light.counter_b'])
        self.assertIn('light.everything', model.dependencies('SD:', [self.room]))

    def test_groups_only_live_in_their_own_area_cross_area_members_keep_theirs(self):
        self.states['light.counter']['attributes']['entity_id'].append('light.study')
        model = self.model()
        study = model.rows('SD:a:study', [self.room])
        self.assertNotIn('light.counter', [row.row_id for row in study])
        self.assertEqual([row.row_id for row in study], ['SD:a:study:lights'])
        self.assertEqual([row.row_id for row in model.rows('SD:a:study:lights', [self.room])], ['light.study'])
        self.assertEqual(model.rows('SD:', [self.room])[0].targets,
                         ('light.ceiling', 'light.counter_a', 'light.counter_b'))

    def test_all_lights_explicit_filtered_individual_targets_and_any_on(self):
        row = self.model().rows('SD:', [self.room])[0]
        self.assertEqual(row.targets, ('light.ceiling', 'light.counter_a', 'light.counter_b'))
        self.assertTrue(row.on)
        self.assertEqual(row.level, 65)  # mean 128 and 204; off lamp excluded
        self.assertEqual(row.title, 'All Lights: On · 65%')
        self.entry('light.counter_a')['hidden_by'] = 'user'
        self.assertEqual(self.model().rows('SD:', [self.room])[0].targets,
                         ('light.ceiling', 'light.counter_b'))

    def test_all_lights_unavailable_only_when_all_targets_unavailable(self):
        for entity in ('light.ceiling', 'light.counter_a', 'light.counter_b'):
            self.states[entity]['state'] = 'unavailable'
        row = self.model().rows('SD:', [self.room])[0]
        self.assertFalse(row.available)
        self.assertEqual(row.upnp_class, 'object.item.sdUnavailable.inert.none')
        self.states['light.counter_b']['state'] = 'off'
        row = self.model().rows('SD:', [self.room])[0]
        self.assertTrue(row.available)
        self.assertFalse(row.on)
        self.assertEqual(row.level, 0)

    def test_on_brightness_never_zero_and_half_up_rounding(self):
        self.states['light.ceiling']['attributes']['brightness'] = 0
        self.assertEqual(self.row('light.ceiling').title, 'Ceiling: On · 1%')
        self.states['light.ceiling']['attributes']['brightness'] = 1.275  # half percent
        self.assertEqual(self.row('light.ceiling').level, 1)
        self.states['light.ceiling']['attributes']['brightness'] = float('nan')
        self.assertEqual(self.row('light.ceiling').title, 'Ceiling: On')

    def test_fan_state_percentage_and_percentage_capability(self):
        self.states['fan.extractor']['state'] = 'on'
        row = self.row('fan.extractor')
        self.assertEqual(row.level_kind, 'speed')
        self.assertEqual(row.title, 'Extractor Fan: On · 40%')
        self.assertEqual(row.upnp_class, 'object.item.sdOn.fan.40')
        self.states['fan.extractor']['attributes'] = {'friendly_name': 'Galley Basic Fan'}
        self.assertEqual(self.row('fan.extractor').upnp_class, 'object.item.sdOn.power.none')

    def test_dimming_modes_legacy_feature_and_non_dimmable_light(self):
        attrs = self.states['light.ceiling']['attributes']
        attrs.pop('brightness')
        self.assertEqual(self.row('light.ceiling').level_kind, 'brightness')
        attrs['supported_color_modes'] = ['onoff']
        self.assertEqual(self.row('light.ceiling').level_kind, '')
        attrs['supported_features'] = 1
        self.assertEqual(self.row('light.ceiling').level_kind, 'brightness')
        self.assertEqual(self.row('light.ceiling').upnp_class, 'object.item.sdOn.light.0')

    def test_names_registry_override_prefix_boundary_and_keep_nonempty(self):
        self.entry('light.ceiling')['name'] = 'Galley Registry name'
        self.assertEqual(self.row('light.ceiling').name, 'Registry name')
        for name, expected in [('Galley', 'Galley'), ('galley lamp', 'lamp'),
                               ('Galley - Task', 'Task'), ('Galleycat', 'Galleycat')]:
            self.entry('light.ceiling')['name'] = name
            self.assertEqual(self.row('light.ceiling').name, expected)

    def test_titles_preserve_unicode_are_one_line_and_keep_state_visible(self):
        self.entry('light.ceiling')['name'] = 'Galley    Çok uzun\n bir lamba ismi burada'
        row = self.row('light.ceiling')
        self.assertIn('Çok', row.name)
        self.assertNotIn('\n', row.title)
        self.assertLessEqual(len(row.title), 24)
        self.assertTrue(row.title.endswith(': On · 50%'))
        self.states['light.ceiling']['state'] = 'unknown'
        self.assertTrue(self.row('light.ceiling').title.endswith(': Unavailable'))

    def test_wide_unicode_title_budget_and_combining_marks(self):
        self.entry('light.ceiling')['name'] = 'Galley ' + '電灯' * 20
        row = self.row('light.ceiling')
        width = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in row.title)
        self.assertLessEqual(width, 24)
        self.assertTrue(row.title.endswith(': On · 50%'))
        self.entry('light.ceiling')['name'] = 'Galley ' + 'e\u0301' * 20
        row = self.row('light.ceiling')
        self.assertIn('e\u0301', row.title)
        visible_width = sum(not unicodedata.combining(char) for char in row.title)
        self.assertLessEqual(visible_width, 24)

    def test_names_sort_case_insensitive_state_does_not_order_rows(self):
        before = [row.row_id for row in self.model().rows('SD:', [self.room])]
        for state in self.states.values(): state['state'] = 'on'
        self.assertEqual([row.row_id for row in self.model().rows('SD:', [self.room])], before)
        self.entry('input_boolean.helper')['name'] = 'Galley aaa helper'
        ids = [row.row_id for row in self.model().rows('SD:', [self.room])]
        self.assertLess(ids.index('input_boolean.helper'), ids.index('switch.plug'))
        self.assertLess(ids.index('light.ceiling'), ids.index('input_boolean.helper'))

    def test_scenes_scripts_are_one_shot_without_state_or_slider(self):
        rows = self.model().rows('SD:a:galley:scenes', [self.room])
        self.assertEqual([(row.row_id, row.title, row.upnp_class) for row in rows], [
            ('scene.evening', 'Evening', 'object.item.sdActivate.activate.none'),
            ('script.tidy', 'Tidy', 'object.item.sdActivate.activate.none')])
        self.assertTrue(all(row.level_kind == '' and not row.folder for row in rows))

    def test_group_root_union_dedup_and_one_folder_per_member(self):
        rooms = [self.room, {**self.room, 'name': 'Second'},
                 {'backend': 'ha', 'key': 'media_player.study', 'name': 'Study', 'devices_area': 'study'},
                 {'name': 'Unassigned', 'devices_area': ''}]
        model = self.model()
        rows = model.rows('SD:', rooms)
        self.assertEqual(rows[0].targets, ('light.ceiling', 'light.counter_a', 'light.counter_b', 'light.study'))
        self.assertEqual([row.name for row in rows], ['All Lights', 'Lab', 'Second', 'Study', 'Unassigned', 'Other Rooms'])
        self.assertEqual(len({row.row_id for row in rows}), len(rows))
        for room in rooms[:2]:
            self.assertEqual(model.rows(room_container(room), rooms)[0].targets,
                             ('light.ceiling', 'light.counter_a', 'light.counter_b'))
        self.assertEqual([row.name for row in model.rows(room_container(rooms[-1]), rooms)], ['Other Rooms'])
        self.assertNotIn('SD:a:galley', [row.row_id for row in model.rows('SD:floor:lower', rooms)])

    def test_group_all_lights_can_aggregate_a_single_remaining_lamp(self):
        rooms = [{'name': 'A', 'devices_area': 'study'}, {'name': 'B', 'devices_area': ''}]
        row = self.model().rows('SD:', rooms)[0]
        self.assertEqual(row.row_id, 'SD:all:group')
        self.assertEqual(row.targets, ('light.study',))

    def test_other_rooms_floors_sorted_level_then_name_and_exclude_members(self):
        model = self.model()
        rows = model.rows('SD:other', [self.room])
        self.assertEqual([row.name for row in rows], ['Upper Floor', 'No Floor'])
        self.assertEqual([row.name for row in model.rows('SD:floor:upper', [self.room])], ['Study'])
        self.assertEqual([row.name for row in model.rows('SD:floor:', [self.room])], ['Deck'])
        self.assertTrue(all(row.folder and row.upnp_class == 'object.container' for row in rows))
        rows = model.rows('SD:other', [])
        self.assertEqual([row.name for row in rows], ['Lower Floor', 'Upper Floor', 'No Floor'])

    def test_other_rooms_flat_when_at_most_one_floor_used(self):
        self.snapshot['areas'][1]['floor_id'] = 'lower'
        rows = self.model().rows('SD:other', [self.room])
        self.assertEqual([row.row_id for row in rows], ['SD:a:study', 'SD:a:deck'])
        with self.assertRaises(KeyError): self.model().rows('SD:floor:lower', [self.room])

    def test_floor_name_and_area_name_stable_tie_breaks(self):
        self.snapshot['floors'][0]['level'] = 1
        self.snapshot['floors'][0]['name'] = 'Zulu'
        self.snapshot['floors'][1]['name'] = 'Alpha'
        rows = self.model().rows('SD:other', [])
        self.assertEqual([row.name for row in rows], ['Alpha', 'Zulu', 'No Floor'])

    def test_stale_floor_reference_uses_readable_no_floor_folder(self):
        self.snapshot['areas'][2]['floor_id'] = 'removed-floor'
        model = self.model()
        self.assertEqual([row.name for row in model.rows('SD:floor:', [self.room])], ['Deck'])
        for container in model.containers([self.room]): model.rows(container, [self.room])

    def test_signature_ignores_power_brightness_and_availability(self):
        model = self.model()
        before = model.structural_signature(model.rows('SD:', [self.room]))
        self.states['light.ceiling'].update(state='off')
        self.states['light.ceiling']['attributes']['brightness'] = 5
        self.states['switch.plug']['state'] = 'unknown'
        after = self.model().structural_signature(self.model().rows('SD:', [self.room]))
        self.assertEqual(before, after)

    def test_signature_changes_for_names_membership_order_and_capability(self):
        before = self.model().structural_signature(self.model().rows('SD:', [self.room]))
        for change in ('name', 'members', 'capability'):
            snapshot, states = copy.deepcopy(self.snapshot), copy.deepcopy(self.states)
            if change == 'name':
                next(entry for entry in snapshot['entities'] if entry['entity_id'] == 'switch.plug')['name'] = 'New name'
            elif change == 'members': states['light.counter']['attributes']['entity_id'] = ['light.counter_a']
            else: states['switch.plug']['attributes']['entity_id'] = []  # switch unaffected; change light capability
            if change == 'capability':
                states['light.ceiling']['attributes'].pop('brightness')
                states['light.ceiling']['attributes']['supported_color_modes'] = ['onoff']
            model = SmartCatalogue(snapshot, states)
            self.assertNotEqual(before, model.structural_signature(model.rows('SD:', [self.room])), change)

    def test_unavailable_missing_attributes_retains_structure_until_explicit_change(self):
        model = self.model()
        before = model.structural_signature(model.rows('SD:', [self.room]))
        self.states['light.counter'] = {'state': 'unavailable', 'attributes': {}}
        refreshed = self.model(previous=model)
        self.assertEqual(before, refreshed.structural_signature(refreshed.rows('SD:', [self.room])))
        row = next(row for row in refreshed.rows('SD:', [self.room]) if row.row_id == 'light.counter')
        self.assertEqual(row.upnp_class, 'object.item.sdUnavailable.inert.none')
        self.states['light.counter']['attributes']['entity_id'] = []
        explicit = self.model(previous=refreshed)
        self.assertNotEqual(before, explicit.structural_signature(explicit.rows('SD:', [self.room])))

    def test_signature_uses_full_name_beyond_clipped_title(self):
        self.entry('switch.plug')['name'] = 'Galley A very very very long name 1'
        first = self.row('switch.plug')
        self.entry('switch.plug')['name'] = 'Galley A very very very long name 2'
        second = self.row('switch.plug')
        self.assertEqual(first.title, second.title)
        self.assertNotEqual(first.signature, second.signature)

    def nested_groups(self):
        for entity, area, name, members in (
                ('light.study_second', 'study', 'Study Second', []),
                ('light.inner', 'study', 'Study Inner', ['light.study']),
                ('light.outer', 'galley', 'Galley Outer', ['light.inner'])):
            self.snapshot['entities'].append({'entity_id': entity, 'area_id': area, 'platform': 'hue'})
            self.states[entity] = {'entity_id': entity, 'state': 'on', 'attributes': {
                'friendly_name': name, 'brightness': 128, 'supported_color_modes': ['brightness'],
                **({'entity_id': members} if members else {})}}

    def test_nested_group_leaf_change_revokes_outer_and_container_signature(self):
        self.nested_groups()
        before = self.model()
        first = next(row for row in before.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.states['light.inner']['attributes']['entity_id'] = ['light.study_second']
        after = self.model()
        second = next(row for row in after.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.assertEqual(first.members, second.members)
        self.assertEqual(first.targets, second.targets)
        self.assertEqual(first.member_closure, ('light.inner', 'light.study'))
        self.assertEqual(second.member_closure, ('light.inner', 'light.study_second'))
        self.assertNotEqual(first.signature, second.signature)
        self.assertNotEqual(before.structural_signature(before.rows('SD:', [self.room])),
                            after.structural_signature(after.rows('SD:', [self.room])))

    def test_nested_group_state_and_availability_keep_membership_signature(self):
        self.nested_groups()
        before = self.model()
        first = next(row for row in before.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.states['light.inner']['state'] = 'off'
        self.states['light.inner']['attributes']['brightness'] = 230
        state_only = self.model(previous=before)
        self.assertEqual(first.signature, next(row for row in state_only.rows('SD:', [self.room])
                                              if row.row_id == 'light.outer').signature)
        self.states['light.inner'] = {'entity_id': 'light.inner', 'state': 'unavailable', 'attributes': {}}
        unavailable = self.model(previous=state_only)
        self.assertEqual(first.signature, next(row for row in unavailable.rows('SD:', [self.room])
                                              if row.row_id == 'light.outer').signature)
        self.states['light.outer'] = {'entity_id': 'light.outer', 'state': 'unknown', 'attributes': {}}
        both_unavailable = self.model(previous=unavailable)
        self.assertEqual(first.signature, next(row for row in both_unavailable.rows('SD:', [self.room])
                                              if row.row_id == 'light.outer').signature)

    def test_member_closure_is_cycle_safe_and_retains_intermediate_and_missing_ids(self):
        self.nested_groups()
        self.states['light.inner']['attributes']['entity_id'] = ['light.outer', 'light.study', 'light.missing']
        row = self.row('light.outer')
        self.assertEqual(row.members, ('light.inner',))
        self.assertEqual(row.member_closure, ('light.inner', 'light.missing', 'light.study'))
        self.assertNotIn('light.outer', row.member_closure)

    def test_filtered_and_missing_registry_inner_groups_still_change_outer_signature(self):
        self.nested_groups()
        for mode in ('hidden', 'no-area', 'missing-registry'):
            with self.subTest(mode=mode):
                snapshot, states = copy.deepcopy(self.snapshot), copy.deepcopy(self.states)
                entry = next(e for e in snapshot['entities'] if e['entity_id'] == 'light.inner')
                if mode == 'hidden': entry['hidden_by'] = 'integration'
                elif mode == 'no-area': entry['area_id'] = None
                else: snapshot['entities'].remove(entry)
                states['light.unrelated_group'] = {'state': 'on', 'attributes': {'entity_id': ['light.unrelated_leaf']}}
                first = SmartCatalogue(snapshot, copy.deepcopy(states))
                before = next(row for row in first.rows('SD:', [self.room]) if row.row_id == 'light.outer')
                self.assertNotIn('light.inner', first.entities)
                self.assertEqual(before.member_closure, ('light.inner', 'light.study'))
                self.assertIn('light.inner', first.referenced_entities)
                self.assertNotIn('light.unrelated_group', first.referenced_entities)
                self.assertNotIn('light.unrelated_leaf', first.referenced_entities)
                states['light.inner']['attributes']['entity_id'] = ['light.study_second']
                second = SmartCatalogue(snapshot, states, previous=first)
                after = next(row for row in second.rows('SD:', [self.room]) if row.row_id == 'light.outer')
                self.assertNotEqual(before.signature, after.signature)
                self.assertIn('light.study_second', second.dependencies('SD:', [self.room]))

    def test_unavailable_raw_inner_keeps_known_members_until_explicit_membership_change(self):
        self.nested_groups()
        self.entry('light.inner')['hidden_by'] = 'integration'
        first = self.model()
        before = next(row for row in first.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.states['light.inner'] = {'state': 'unavailable', 'attributes': {}}
        unavailable = self.model(previous=first)
        after = next(row for row in unavailable.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.assertEqual(before.signature, after.signature)
        self.assertEqual(after.member_closure, ('light.inner', 'light.study'))
        self.states['light.inner']['attributes']['entity_id'] = []
        cleared = self.model(previous=unavailable)
        after_clear = next(row for row in cleared.rows('SD:', [self.room]) if row.row_id == 'light.outer')
        self.assertNotEqual(after.signature, after_clear.signature)
        self.assertEqual(after_clear.member_closure, ('light.inner',))

    def test_dependencies_skip_navigation_and_include_actual_targets(self):
        model = self.model()
        self.assertEqual(model.dependencies('SD:other', [self.room]), frozenset())
        self.assertEqual(model.dependencies('SD:floor:upper', [self.room]), frozenset())
        self.assertEqual(model.dependencies('SD:a:galley:lights', [self.room]),
                         frozenset(('light.counter_a', 'light.counter_b')))
        self.assertIn('light.counter', model.dependencies('SD:', [self.room]))
        self.assertNotIn('light.study', model.dependencies('SD:', [self.room]))
        self.assertEqual(model.dependencies('SD:', [self.room, {'name': 'B', 'devices_area': 'study'}]),
                         frozenset(('light.ceiling', 'light.counter_a', 'light.counter_b', 'light.study')))

    def test_ascii_ids_unicode_area_and_floor_names_never_enter_ids(self):
        self.snapshot['areas'][0]['area_id'] = 'café:west'
        self.snapshot['areas'][0]['name'] = 'Café'
        for entry in self.snapshot['entities']:
            if entry.get('area_id') == 'galley': entry['area_id'] = 'café:west'
        self.snapshot['devices'][0]['area_id'] = 'café:west'
        self.snapshot['devices'][1]['area_id'] = 'café:west'
        model = self.model()
        ids = model.containers([self.room])
        self.assertIn('SD:a:caf%C3%A9%3Awest', ids)
        self.assertTrue(all(identity.isascii() for identity in ids))
        self.assertTrue(model.rows(area_container('café:west'), [self.room]))

    def test_unknown_container_rejected_and_all_known_containers_readable(self):
        model = self.model()
        rooms = [self.room, {'name': 'B', 'devices_area': ''}]
        for identity in model.containers(rooms): model.rows(identity, rooms)
        for identity in ('x', 'SD:nope', 'SD:a:missing', 'SD:a:galley:bad', 'SD:r:foreign', 'SD:floor:missing'):
            with self.assertRaises(KeyError, msg=identity): model.rows(identity, rooms)

    def test_input_snapshots_remain_unchanged_and_repeated_browse_is_pure(self):
        snapshot, states = copy.deepcopy(self.snapshot), copy.deepcopy(self.states)
        model = self.model()
        for _ in range(4):
            for container in model.containers([self.room]):
                model.rows(container, [self.room])
                model.dependencies(container, [self.room])
        self.assertEqual(snapshot, self.snapshot)
        self.assertEqual(states, self.states)

    def test_registry_snapshots_can_be_mappings_and_empty_catalogue_safe(self):
        for key, field in [('entities', 'entity_id'), ('devices', 'id'), ('areas', 'area_id'), ('floors', 'floor_id')]:
            self.snapshot[key] = {entry[field]: entry for entry in self.snapshot[key]}
        self.assertEqual(self.model().resolve_area(self.room), 'galley')
        empty = SmartCatalogue({}, {})
        self.assertEqual(empty.catalogue_areas, ())
        self.assertEqual([row.row_id for row in empty.rows('SD:', [])], ['SD:other'])

    def test_global_unavailable_row_is_inert(self):
        row = unavailable_row()
        self.assertFalse(row.available)
        self.assertFalse(row.folder)
        self.assertEqual(row.targets, ())
        self.assertEqual(row.upnp_class, 'object.item.sdUnavailable.inert.none')
        self.assertEqual(row.title, 'Home Assistant unavailable')

    def add_climate(self, entity_id='climate.hallway', name='Galley Hallway', area='galley',
                    state='heat', **attrs):
        defaults = {
            'hvac_modes': ['off', 'heat', 'cool', 'heat_cool', 'fan_only', 'dry'],
            'hvac_action': 'heating',
            'current_temperature': 71,
            'current_humidity': 45,
            'temperature': 68,
            'target_temp_low': 68,
            'target_temp_high': 74,
            'min_temp': 50,
            'max_temp': 90,
            'target_temp_step': 1,
            'temperature_unit': '°F',
            'supported_features': 1 | 2 | 128 | 256,
        }
        defaults.update(attrs)
        self.snapshot['entities'].append({'entity_id': entity_id, 'area_id': area, 'platform': 'fixture'})
        self.states[entity_id] = {'entity_id': entity_id, 'state': state,
                                  'attributes': {'friendly_name': name, **defaults}}

    def test_climate_thermostat_area_row_icons_and_never_joins_all_lights(self):
        self.add_climate()
        model = self.model()
        all_lights = self.row('SD:all:galley')
        self.assertNotIn('climate.hallway', all_lights.targets)
        row = self.row('SD:t:climate.hallway')
        self.assertEqual(row.name, 'Hallway')
        self.assertEqual(row.title, 'Hallway: 71° -> 68°')
        self.assertEqual(row.upnp_class, 'object.item.sdHeat.climate.none')
        self.assertIn('SD:t:climate.hallway', model.containers([self.room]))
        # Icon follows hvac_action first, falling back to hvac_mode when absent.
        for action, expected in [('cooling', 'sdCool'), ('idle', 'sdIdle'), ('off', 'sdClimateOff'), ('fan', 'sdFan')]:
            self.states['climate.hallway']['attributes']['hvac_action'] = action
            self.assertEqual(self.row('SD:t:climate.hallway').upnp_class, f'object.item.{expected}.climate.none')
        self.states['climate.hallway']['attributes'].pop('hvac_action')
        for mode, expected in [('heat', 'sdHeat'), ('cool', 'sdCool'), ('heat_cool', 'sdHeatCool'),
                               ('auto', 'sdHeatCool'), ('fan_only', 'sdFan'), ('dry', 'sdIdle'), ('off', 'sdClimateOff')]:
            self.states['climate.hallway']['state'] = mode
            self.assertEqual(self.row('SD:t:climate.hallway').upnp_class, f'object.item.{expected}.climate.none')

    def test_climate_modes_heat_only_cool_only_heat_cool_auto_and_fan_mode(self):
        # Heat-only radiator valve in Celsius with 0.5 steps.
        self.add_climate('climate.radiator', 'Galley Radiator', state='heat',
                         hvac_modes=['off', 'heat'], hvac_action='idle',
                         current_temperature=21.0, current_humidity=None, temperature=20.5,
                         min_temp=10, max_temp=32, target_temp_step=0.5, temperature_unit='°C',
                         supported_features=1 | 128 | 256)
        # Cool-only unit with fan modes.
        self.add_climate('climate.cooler', 'Galley Cooler', state='cool',
                         hvac_modes=['off', 'cool', 'fan_only'], hvac_action='cooling',
                         current_temperature=75, current_humidity=50, temperature=72,
                         min_temp=60, max_temp=85, target_temp_step=1, temperature_unit='°F',
                         fan_modes=['auto', 'low', 'high'], fan_mode='low',
                         supported_features=1 | 8 | 128 | 256)
        # Device offering both heat_cool ("Heat/Cool") and auto ("Auto").
        self.add_climate('climate.dual', 'Galley Dual', state='heat_cool',
                         hvac_modes=['off', 'heat', 'cool', 'heat_cool', 'auto'], hvac_action='idle',
                         current_temperature=70, current_humidity=40,
                         target_temp_low=68, target_temp_high=74,
                         supported_features=1 | 2 | 128 | 256)
        model = self.model()
        rad_rows = model.rows('SD:t:climate.radiator', [self.room])
        self.assertEqual([r.name for r in rad_rows], ['Mode', 'Heat to', 'Now'])
        self.assertEqual(rad_rows[0].upnp_class, 'object.item.sdHeat.hvacMode.heat:off,heat')
        self.assertEqual(rad_rows[1].title, 'Heat to: 20.5°')
        self.assertEqual(rad_rows[1].upnp_class, 'object.item.sdHeat.setpoint.205:100:320:5:C')
        self.assertEqual(rad_rows[2].title, 'Now: 21° · Idle')

        cool_rows = model.rows('SD:t:climate.cooler', [self.room])
        self.assertEqual([r.name for r in cool_rows], ['Mode', 'Cool to', 'Fan', 'Now'])
        self.assertEqual(cool_rows[1].title, 'Cool to: 72°')
        self.assertEqual(cool_rows[1].upnp_class, 'object.item.sdCool.setpoint.720:600:850:10:F')
        self.assertEqual(cool_rows[2].title, 'Fan: Low')
        self.assertEqual(cool_rows[2].upnp_class, 'object.item.sdFan.fanMode.low:auto,low,high')
        self.assertEqual(cool_rows[3].title, 'Now: 75° · Cooling · 50%')

        dual_rows = model.rows('SD:t:climate.dual', [self.room])
        self.assertEqual([r.name for r in dual_rows], ['Mode', 'Heat to', 'Cool to', 'Now'])
        self.assertEqual(dual_rows[0].title, 'Mode: Heat/Cool')
        self.assertEqual(dual_rows[0].upnp_class, 'object.item.sdHeatCool.hvacMode.heat_cool:off,heat,cool,heat_cool,auto')
        self.assertEqual(dual_rows[1].row_id, 'SD:t:climate.dual:low')
        self.assertEqual(dual_rows[2].row_id, 'SD:t:climate.dual:high')
        self.assertEqual(self.row('SD:t:climate.dual').title, 'Dual: 70° -> 68°-74°')

        self.states['climate.dual']['state'] = 'auto'
        auto_rows = self.model().rows('SD:t:climate.dual', [self.room])
        self.assertEqual(auto_rows[0].title, 'Mode: Auto')
        self.states['climate.dual']['state'] = 'off'
        off_rows = self.model().rows('SD:t:climate.dual', [self.room])
        self.assertEqual([r.name for r in off_rows], ['Mode', 'Now'])

    def test_climate_mode_change_changes_folder_signature_while_setpoint_and_unavailable_retain_it(self):
        self.add_climate()
        first = self.model()
        folder = 'SD:t:climate.hallway'
        sig_heat = first.structural_signature(first.rows(folder, [self.room]))
        area_sig = first.structural_signature(first.rows('SD:', [self.room]))
        # Setpoint and sensor updates retain the folder signature.
        self.states['climate.hallway']['attributes']['temperature'] = 70
        self.states['climate.hallway']['attributes']['current_temperature'] = 72
        second = self.model(previous=first)
        self.assertEqual(second.structural_signature(second.rows(folder, [self.room])), sig_heat)
        saved_state = copy.deepcopy(self.states['climate.hallway'])
        # Unavailable with cleared attributes retains structure from previous catalogue.
        self.states['climate.hallway'] = {'entity_id': 'climate.hallway', 'state': 'unavailable', 'attributes': {}}
        unavail = self.model(previous=second)
        self.assertEqual(unavail.structural_signature(unavail.rows(folder, [self.room])), sig_heat)
        self.assertFalse(next(r for r in unavail.rows('SD:', [self.room]) if r.row_id == folder).available)
        # Mode change produces a new structural signature for the folder, but not the area list.
        self.states['climate.hallway'] = saved_state
        self.states['climate.hallway']['state'] = 'cool'
        cool_model = self.model(previous=second)
        self.assertNotEqual(cool_model.structural_signature(cool_model.rows(folder, [self.room])), sig_heat)
        self.assertEqual(cool_model.structural_signature(cool_model.rows('SD:', [self.room])), area_sig)
        # Non-climate controller view hides climate rows and rejects SD:t:<entity>.
        self.assertNotIn('SD:t:climate.hallway', [r.row_id for r in cool_model.rows('SD:', [self.room], climate=False)])
        self.assertNotIn(folder, cool_model.containers([self.room], climate=False))
        with self.assertRaises(KeyError):
            cool_model.rows(folder, [self.room], climate=False)


if __name__ == '__main__': unittest.main()
