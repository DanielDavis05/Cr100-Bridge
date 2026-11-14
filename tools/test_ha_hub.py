"""Shared HA ownership and coherent registry reads, with no network access."""
import copy
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import ha_backend
import ha_hub
from ha_client import HomeAssistant
from ha_linein import SonosLineIn


def wait_for(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate(): return True
        time.sleep(.01)
    return False


class Client:
    def __init__(self, url='http://example.invalid', token='test', log=None):
        self.url, self.token = url, token
        self.connected = threading.Event()
        self.connection_generation = 0
        self.connection_listeners, self.listeners = [], []
        self.started = self.closed = 0
        self.calls = []
        self.registries = {command: [{'id': name}] for name, command in ha_hub.REGISTRIES.items()}

    def start(self): self.started += 1; self.connect()
    def close(self): self.closed += 1; self.connected.clear()
    def connect(self):
        self.connection_generation += 1
        self.connected.set()
        for listener in list(self.connection_listeners): listener()
    def call(self, payload, timeout):
        command = payload['type'].split('/')[1].removesuffix('_registry')
        self.calls.append(command)
        return copy.deepcopy(self.registries[command])


class Ownership(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        (self.path / 'token').write_text('one')
        self.config = {'home_assistant': {'url': 'http://example.invalid', 'token_file': 'token'},
                       'backend': 'ha', 'rooms': []}
        self.factory = patch.object(ha_hub, 'HomeAssistant', side_effect=Client).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(ha_hub.close)

    def test_source_switch_and_backend_reload_keep_one_connection(self):
        backends.load(self.config, self.path, [], lambda _: None)
        hub = ha_hub.current()
        self.assertTrue(hub.ready.wait(2))
        self.config['backend'] = 'sonos'
        backends.load(self.config, self.path, [], lambda _: None)
        self.assertIs(ha_hub.current(), hub)
        self.assertEqual((hub.client.started, hub.client.closed), (1, 0))
        self.assertEqual(self.factory.call_count, 1)
        # A backend borrows the hub client, and releases only its own workers.
        backend = ha_backend.HABackend.__new__(ha_backend.HABackend)
        backend.ha, backend.line_in, backend.settings = hub.client, Mock(), Mock()
        backend._ha_listener = lambda *_: None
        hub.client.listeners.append(backend._ha_listener)
        backend.close()
        self.assertEqual(hub.client.closed, 0)
        self.assertNotIn(backend._ha_listener, hub.client.listeners)

    def test_token_or_url_replacement_and_removal_close_previous_client(self):
        first = ha_hub.configure(self.config, self.path, lambda _: None)
        (self.path / 'token').write_text('two')
        second = ha_hub.configure(self.config, self.path, lambda _: None)
        self.assertIsNot(first, second)
        self.assertEqual(first.client.closed, 1)
        self.config['home_assistant']['url'] = 'http://another.invalid'
        third = ha_hub.configure(self.config, self.path, lambda _: None)
        self.assertEqual(second.client.closed, 1)
        self.assertIsNone(ha_hub.configure({}, self.path, lambda _: None))
        self.assertEqual(third.client.closed, 1)

    def test_missing_credentials_do_not_open_a_connection(self):
        for config in ({}, {'home_assistant': {'url': 'http://example.invalid'}},
                       {'home_assistant': {'url': 'http://example.invalid', 'token_file': 'missing'}}):
            self.assertIsNone(ha_hub.configure(config, self.path, lambda _: None))
        self.factory.assert_not_called()


class Registries(unittest.TestCase):
    def test_initial_periodic_and_reconnect_snapshots_are_coherent_and_owned(self):
        client = Client()
        hub = ha_hub.HAHub(client, lambda _: None, interval=.05)
        self.addCleanup(hub.close)
        notifications = []
        hub.listeners.append(lambda: notifications.append(hub.revision))
        hub.start()
        self.assertTrue(hub.ready.wait(2))
        self.assertEqual(client.calls[:4], ['entity', 'device', 'area', 'floor'])
        snapshot = hub.snapshot()
        snapshot['entities'][0]['id'] = 'caller edit'
        self.assertEqual(hub.snapshot()['entities'][0]['id'], 'entities')
        client.registries['area'] = [{'area_id': 'study', 'name': 'Study'}]
        self.assertTrue(wait_for(lambda: hub.snapshot()['areas'] == client.registries['area']))
        revision = hub.revision
        calls = len(client.calls)
        client.connect()
        self.assertTrue(wait_for(lambda: len(client.calls) >= calls + 4))
        self.assertEqual(hub.revision, revision)  # same snapshot is not structural change
        self.assertGreaterEqual(len(notifications), 3)

    def test_line_in_reads_shared_snapshot_without_a_second_registry_fetch(self):
        client = Client()
        hub = ha_hub.HAHub(client, lambda _: None)
        self.addCleanup(hub.close)
        hub.registries = {'entities': [], 'devices': [], 'areas': [], 'floors': []}
        hub.ready.set()
        line_in = SonosLineIn(client, lambda: None, lambda _: None, inventory=Mock())
        line_in.inventory.refresh.return_value = False
        self.addCleanup(line_in.close)
        self.assertTrue(line_in.refresh())
        self.assertEqual(client.calls, [])


class ServiceTargets(unittest.TestCase):
    def test_single_and_list_targets_preserve_the_existing_signature(self):
        ha = HomeAssistant('http://example.invalid', 'test', lambda _: None)
        self.addCleanup(ha.close)
        ha.call = Mock(return_value={'response': {'ok': True}})
        ha.call_service('light', 'turn_on', ['light.one', 'light.two'], {'brightness_pct': 40})
        payload = ha.call.call_args.args[0]
        self.assertEqual(payload['target'], {'entity_id': ['light.one', 'light.two']})
        self.assertEqual(payload['service_data'], {'brightness_pct': 40})
        result = ha.call_service('light', 'turn_off', 'light.one', response=True)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(ha.call.call_args.args[0]['target'], {'entity_id': 'light.one'})
        for invalid in ([], [None], 1):
            with self.assertRaises(ValueError): ha.call_service('light', 'turn_on', invalid)


if __name__ == '__main__': unittest.main()
