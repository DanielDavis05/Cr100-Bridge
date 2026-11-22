"""Device fixture and HA WebSocket lifecycle; all requests stay on loopback."""
import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_ha
from ha_client import HAError, HomeAssistant
from ha_hub import HAHub
from ha_linein import SonosLineIn


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.01)
    return False


class SmartFixture(unittest.TestCase):
    def setUp(self):
        self.fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture())
        self.addCleanup(self.fake.close)
        self.ha = HomeAssistant(self.fake.url, fake_ha.TOKEN, lambda _: None)
        self.addCleanup(self.ha.close)
        self.ha.start()
        self.assertTrue(self.ha.connected.wait(5))

    def test_registries_have_real_shapes_hue_groups_and_config_entities(self):
        snapshot = {name: self.ha.call({'type': 'config/%s_registry/list' % command})
                    for name, command in (('entities', 'entity'), ('devices', 'device'),
                                          ('areas', 'area'), ('floors', 'floor'))}
        self.assertEqual(snapshot, self.fake.registry_snapshot())
        entities = {entry['entity_id']: entry for entry in snapshot['entities']}
        self.assertEqual(entities['light.lantern_counter']['platform'], 'hue')
        self.assertEqual(entities['switch.lantern_loudness']['entity_category'], 'config')
        self.assertEqual(entities['switch.lantern_crossfade']['entity_category'], 'config')
        self.assertIn('light.registry_only', entities)
        self.assertNotIn('light.registry_only', self.ha.states)
        self.assertEqual(len(snapshot['areas']), 3)
        self.assertEqual(len(snapshot['floors']), 2)
        self.assertTrue(all(not device.get('configuration_url') for device in snapshot['devices']))
        self.assertEqual(self.fake.calls, [])

    def test_settling_light_walks_to_its_target_in_several_events_and_groups_follow(self):
        self.fake.settle = {'steps': 4, 'span': .2}
        seen = []
        self.ha.listeners.append(lambda entity, state: seen.append(
            (entity, state['attributes'].get('brightness'))) if entity.startswith('light.') else None)
        self.ha.call_service('light', 'turn_on', ['light.lantern_ceiling'], {'brightness_pct': 100})
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_ceiling')['attributes']['brightness'] == 255))
        ceiling = [value for entity, value in seen if entity == 'light.lantern_ceiling']
        self.assertEqual(len(ceiling), 4)
        self.assertEqual(ceiling, sorted(ceiling))
        self.assertGreater(ceiling[0], 153)
        self.assertEqual(ceiling[-1], 255)
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_all')['attributes']['brightness'] == round((255 + 102) / 2)))
        self.assertGreater(len([1 for entity, _ in seen if entity == 'light.lantern_all']), 1)
        # A newer command replaces the walk in progress and ends on its own target.
        self.ha.call_service('light', 'turn_on', ['light.lantern_ceiling'], {'brightness_pct': 10})
        self.ha.call_service('light', 'turn_on', ['light.lantern_ceiling'], {'brightness_pct': 50})
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_ceiling')['attributes']['brightness'] == 128))
        time.sleep(.4)
        self.assertEqual(self.ha.state('light.lantern_ceiling')['attributes']['brightness'], 128)
        self.assertEqual(self.fake.settling, {})

    def test_default_light_levels_arrive_in_one_event_per_light(self):
        seen = []
        self.ha.listeners.append(lambda entity, state: seen.append(entity) if entity == 'light.lantern_ceiling' else None)
        self.ha.call_service('light', 'turn_on', ['light.lantern_ceiling'], {'brightness_pct': 100})
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_ceiling')['attributes']['brightness'] == 255))
        time.sleep(.2)
        self.assertEqual(seen, ['light.lantern_ceiling'])

    def test_absolute_power_list_targets_and_light_levels_emit_state_events(self):
        events = []
        self.ha.listeners.append(lambda entity, state: events.append(entity))
        members = ['light.lantern_ceiling', 'light.lantern_counter_left', 'light.lantern_counter_right']
        self.ha.call_service('light', 'turn_off', members)
        self.assertTrue(wait_for(lambda: all(self.ha.state(entity)['state'] == 'off' for entity in members)))
        self.assertEqual(self.ha.state('light.lantern_counter')['state'], 'off')
        self.assertEqual(self.fake.calls[-1], ('turn_off', members, {}))
        self.ha.call_service('light', 'turn_on', members, {'brightness_pct': 70})
        self.assertTrue(wait_for(lambda: all(self.ha.state(entity)['attributes']['brightness'] == 178 for entity in members)))
        self.assertTrue(set(members).issubset(events))
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_all')['state'] == 'on'))

    def test_hue_group_service_changes_only_its_members(self):
        self.ha.call_service('light', 'turn_off', 'light.lantern_counter')
        self.assertTrue(wait_for(lambda: self.ha.state('light.lantern_counter_left')['state'] == 'off'))
        self.assertEqual(self.ha.state('light.lantern_counter_right')['state'], 'off')
        self.assertEqual(self.ha.state('light.lantern_ceiling')['state'], 'on')
        self.assertEqual(self.ha.state('light.lantern_all')['state'], 'on')

    def test_switch_boolean_fan_scene_and_script_services(self):
        for domain, entity in (('switch', 'switch.lantern_plug'),
                               ('input_boolean', 'input_boolean.lantern_guest_mode')):
            self.ha.call_service(domain, 'turn_off', entity)
            self.assertTrue(wait_for(lambda: self.ha.state(entity)['state'] == 'off'))
            self.ha.call_service(domain, 'turn_on', entity)
            self.assertTrue(wait_for(lambda: self.ha.state(entity)['state'] == 'on'))
        fan = 'fan.lantern_extractor'
        self.ha.call_service('fan', 'set_percentage', fan, {'percentage': 65})
        self.assertTrue(wait_for(lambda: self.ha.state(fan)['attributes']['percentage'] == 65))
        self.ha.call_service('fan', 'set_percentage', fan, {'percentage': 0})
        self.assertTrue(wait_for(lambda: self.ha.state(fan)['state'] == 'off'))
        for domain, entity in (('scene', 'scene.lantern_evening'), ('script', 'script.lantern_welcome')):
            for count in (1, 2):
                self.ha.call_service(domain, 'turn_on', entity)
                self.assertTrue(wait_for(lambda: self.ha.state(entity)['attributes'].get('activation_count') == count))

    def test_service_failure_changes_no_state(self):
        before = copy.deepcopy(self.fake.states)
        self.fake.fail_services = True
        with self.assertRaises(HAError): self.ha.call_service('light', 'turn_off', 'light.lantern_ceiling')
        self.assertEqual(self.fake.states, before)

    def test_climate_mode_temperature_and_fan_services(self):
        entity = 'climate.lantern_thermostat'
        self.ha.call_service('climate', 'set_hvac_mode', entity, {'hvac_mode': 'cool'})
        self.assertTrue(wait_for(lambda: self.ha.state(entity)['state'] == 'cool'))
        self.assertEqual(self.ha.state(entity)['attributes']['hvac_action'], 'idle')
        self.ha.call_service('climate', 'set_temperature', entity, {'temperature': 69})
        self.assertTrue(wait_for(lambda: self.ha.state(entity)['attributes']['temperature'] == 69))
        self.assertEqual(self.ha.state(entity)['attributes']['hvac_action'], 'cooling')
        self.ha.call_service('climate', 'set_hvac_mode', entity, {'hvac_mode': 'heat_cool'})
        self.assertTrue(wait_for(lambda: self.ha.state(entity)['state'] == 'heat_cool'))
        self.ha.call_service('climate', 'set_temperature', entity,
                             {'target_temp_low': 68, 'target_temp_high': 74})
        self.assertTrue(wait_for(lambda: self.ha.state(entity)['attributes']['target_temp_high'] == 74))
        self.ha.call_service('climate', 'set_fan_mode', entity, {'fan_mode': 'high'})
        self.assertTrue(wait_for(lambda: self.ha.state(entity)['attributes']['fan_mode'] == 'high'))
        rad = 'climate.lantern_radiator'
        self.ha.call_service('climate', 'set_temperature', rad, {'temperature': 22.5})
        self.assertTrue(wait_for(lambda: self.ha.state(rad)['attributes']['temperature'] == 22.5))
        self.assertEqual(self.ha.state(rad)['attributes']['hvac_action'], 'heating')


class Lifecycle(unittest.TestCase):
    def test_shared_hub_fetches_each_registry_once_and_line_in_uses_snapshot(self):
        fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture())
        self.addCleanup(fake.close)
        hub = HAHub(HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None), lambda _: None)
        self.addCleanup(hub.close)
        hub.start()
        self.assertTrue(hub.ready.wait(5))
        self.assertEqual(len(fake.registry_requests), 4)
        line_in = SonosLineIn(hub.client, lambda: None, lambda _: None)
        self.addCleanup(line_in.close)
        self.assertTrue(line_in.refresh())
        self.assertEqual(len(fake.registry_requests), 4)
        self.assertEqual(line_in.binding('media_player.lantern').native_id, 'RINCON_00000000001101400')

    def test_drop_and_return_use_the_real_connection_lifecycle(self):
        with patch('ha_client.RECONNECT_DELAY', .03):
            fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture())
            ha = HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None)
            try:
                ha.start()
                self.assertTrue(ha.connected.wait(5))
                initial_generation = ha.connection_generation
                fake.set_available(False)
                self.assertTrue(wait_for(lambda: not ha.connected.is_set()))
                time.sleep(.1)
                self.assertFalse(ha.connected.is_set())
                fake.set_available(True)
                self.assertTrue(wait_for(lambda: ha.connected.is_set() and ha.connection_generation > initial_generation))
                self.assertIn('light.lantern_ceiling', ha.states)
                self.assertEqual(fake.calls, [])
            finally:
                ha.close(); fake.close()


if __name__ == '__main__': unittest.main()
