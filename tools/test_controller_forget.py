"""Per-controller link keys, and forgetting or restoring one controller without touching the others."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import ccmp
import frames
import sonosnet_link as sl
import webui
from controller_state import set_forgotten
from test_multi_controller_link import (AP, FIRST, KEYS, SECOND, STATE, Radio, data, decode, ethernet,
                                        management, null)

FIRST_MAC, SECOND_MAC = (record['mac'] for record in STATE['controllers'])
ASSOCIATION = bytes.fromhex('1003f401dd06000e58020101')
NEW_KEY = bytes.fromhex('a5' * 16)


class Keys(unittest.TestCase):
    def setUp(self):
        tap, self.tap = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close)
        self.addCleanup(self.tap.close)
        self.tap.setblocking(False)
        self.radio, self.logs = Radio(), []
        self.link = sl.Link(STATE, self.radio, tap.fileno(), self.logs.append)
        pacer = patch.object(sl, 'PACE_WINDOW', 0)
        pacer.start()
        self.addCleanup(pacer.stop)
        for mac in (FIRST, SECOND):
            self.link.on_frame(management(mac, 0, ASSOCIATION))
            self.link.on_frame(null(mac, pm=False))
        self.radio.sent.clear()

    def test_each_controller_is_sent_its_own_key(self):
        self.link.from_tap(ethernet(FIRST, b'first'))
        self.link.from_tap(ethernet(SECOND, b'second'))
        to_first, to_second = self.radio.sent
        self.assertEqual(decode(to_first)[3], ethernet(FIRST, b'first'))
        self.assertEqual(decode(to_second)[3], ethernet(SECOND, b'second'))
        # Neither controller can read the other's frames.
        for frame, other in ((to_first, KEYS[SECOND]), (to_second, KEYS[FIRST])):
            with self.assertRaises(Exception): ccmp.decrypt(frame, other)

    def test_a_controller_cannot_forge_another_ones_frames(self):
        inner = ethernet(AP, b'forged', source=SECOND)
        # The first controller's key, with the second controller's address.
        self.link.on_frame(data(SECOND, inner, 1, key=KEYS[FIRST]))
        self.assertEqual(self.link.stations[SECOND].stats['rx_decrypt_fail'], 1)
        self.assertEqual(self.link.stations[SECOND].stats['rx_data'], 0)
        self.link.on_frame(data(SECOND, inner, 2))
        self.assertEqual(self.link.stations[SECOND].stats['rx_data'], 1)
        self.assertEqual(self.tap.recv(4096), inner)


class StateFile(unittest.TestCase):
    """The link applies forget, restore and re-pairing from the saved pairing state."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_path = Path(temp.name) / 'bridge-state.json'
        self.write(STATE)
        tap, self.tap = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close)
        self.addCleanup(self.tap.close)
        self.tap.setblocking(False)
        self.radio, self.logs = Radio(), []
        self.link = sl.Link(STATE, self.radio, tap.fileno(), self.logs.append, state_path=self.state_path)
        pacer = patch.object(sl, 'PACE_WINDOW', 0)
        pacer.start()
        self.addCleanup(pacer.stop)
        self.now = self.link.start
        for mac in (FIRST, SECOND):
            self.link.on_frame(management(mac, 0, ASSOCIATION))
            self.link.on_frame(null(mac, pm=True))
        self.radio.sent.clear()

    def write(self, state):
        self.state_path.write_text(json.dumps(state))
        self.state_path.chmod(0o600)

    def saved(self):
        return json.loads(self.state_path.read_text())

    def reload(self, seconds=1.1):
        self.now += seconds
        self.link.reload_controllers(self.now)

    def events(self, name):
        return [record for record in self.logs if record.get('event') == name]

    def test_forget_deauthenticates_and_ignores_only_that_controller(self):
        self.link.from_tap(ethernet(FIRST, b'waiting'))
        self.reload()
        self.write(set_forgotten(STATE, SECOND_MAC, True))
        self.link.reload_controllers(self.now + 0.5)   # checked at most once a second
        self.assertIn(SECOND, self.link.stations)
        self.reload()
        self.assertNotIn(SECOND, self.link.stations)
        deauth = self.radio.sent[-1]
        self.assertEqual((deauth[0], deauth[4:10], deauth[10:16]), (0xc0, SECOND, AP))
        self.assertEqual(deauth[24:26], bytes((sl.DEAUTH_REASON, 0)))
        self.assertEqual([record['mac'] for record in self.events('controller-forgotten')], [SECOND_MAC])
        # The other controller keeps its association and its queued frame.
        first = self.link.stations[FIRST]
        self.assertTrue(first.associated)
        self.assertEqual(len(first.queue), 1)
        # Anything from the forgotten controller is ignored, including association requests.
        sent = len(self.radio.sent)
        self.link.on_frame(management(SECOND, 0, ASSOCIATION))
        self.link.on_frame(data(SECOND, ethernet(AP, source=SECOND), 9))
        self.link.from_tap(ethernet(SECOND))
        self.assertEqual(len(self.radio.sent), sent)
        with self.assertRaises(BlockingIOError): self.tap.recv(4096)
        # Service passes still visit every remaining station.
        for _ in range(3): self.link.service_stations(self.now)

    def test_restore_adds_the_controller_back_with_its_aid_and_key(self):
        self.write(set_forgotten(STATE, SECOND_MAC, True))
        self.reload()
        self.write(set_forgotten(self.saved(), SECOND_MAC, False))
        self.reload()
        station = self.link.stations[SECOND]
        self.assertEqual((station.aid, station.key, station.associated), (65, KEYS[SECOND], False))
        self.assertTrue(station.auto_reconnect)
        self.assertEqual([record['aid'] for record in self.events('controller-restored')], [65])
        self.link.on_frame(management(SECOND, 0, ASSOCIATION))
        self.link.on_frame(data(SECOND, ethernet(AP, b'back', source=SECOND), 1))
        self.assertEqual(self.tap.recv(4096), ethernet(AP, b'back', source=SECOND))

    def test_state_for_another_household_or_without_active_controllers_is_ignored(self):
        for state in ({**STATE, 'household': 'Sonos_other'}, {**STATE, 'freq': 2412},
                      {**STATE, 'controllers': [{**record, 'forgotten': True} for record in STATE['controllers']]},
                      {'not': 'a state'}):
            with self.subTest(state=state):
                self.write(state)
                self.reload()
                self.assertEqual(set(self.link.stations), {FIRST, SECOND})
        # The same error twice in a row (household, then channel) is logged once.
        self.assertEqual(len(self.events('controller-state-invalid')), 3)
        self.write(STATE)
        self.reload()
        self.assertIsNone(self.link.state_error)

    def test_repair_accepts_the_old_key_until_the_new_one_is_heard(self):
        old = self.link.stations[FIRST]
        self.link.persist_controller({'mac': FIRST_MAC, 'txid': 3, 'time': 1.0, 'key': NEW_KEY.hex()})
        record = self.saved()['controllers'][0]
        self.assertEqual((record['key'], record['previous_key']), (NEW_KEY.hex(), KEYS[FIRST].hex()))
        station = self.link.stations[FIRST]
        self.assertIs(station, old)
        self.assertEqual((station.key, station.previous_key, station.tx_key), (NEW_KEY, KEYS[FIRST], NEW_KEY))
        # The pairing stopped halfway: the CR100 still uses its old key, and is answered with it.
        self.link.on_frame(data(FIRST, ethernet(AP, b'old', source=FIRST), 50))
        self.assertEqual(self.tap.recv(4096), ethernet(AP, b'old', source=FIRST))
        self.assertEqual(station.tx_key, KEYS[FIRST])
        self.link.on_frame(null(FIRST, pm=False))
        self.link.from_tap(ethernet(FIRST, b'reply'))
        self.assertEqual(decode(self.radio.sent[-1], KEYS[FIRST])[3], ethernet(FIRST, b'reply'))
        # Once it uses the new key, with PNs starting afresh, the old key is no longer accepted.
        self.link.on_frame(data(FIRST, ethernet(AP, b'new', source=FIRST), 1, key=NEW_KEY))
        self.assertEqual(self.tap.recv(4096), ethernet(AP, b'new', source=FIRST))
        self.assertEqual((station.previous_key, station.tx_key), (None, NEW_KEY))
        self.link.on_frame(data(FIRST, ethernet(AP, b'stale', source=FIRST), 60))
        self.assertEqual(station.stats['rx_decrypt_fail'], 1)
        self.link.confirm_keys()
        self.assertEqual(self.saved()['controllers'][0], {'mac': FIRST_MAC, 'aid': 1, 'key': NEW_KEY.hex(),
                                                          'txid': 3, 'time': 1.0})
        # The link's own write does not look like an outside change.
        self.reload()
        self.assertIs(self.link.stations[FIRST], station)

    def test_pairing_a_forgotten_controller_restores_it_with_only_the_new_key(self):
        self.write(set_forgotten(STATE, SECOND_MAC, True))
        self.reload()
        self.link.persist_controller({'mac': SECOND_MAC, 'txid': 4, 'time': 2.0, 'key': NEW_KEY.hex()})
        station = self.link.stations[SECOND]
        self.assertEqual((station.aid, station.key, station.previous_key), (65, NEW_KEY, None))
        self.assertNotIn('forgotten', self.saved()['controllers'][1])
        self.link.on_frame(management(SECOND, 0, ASSOCIATION))
        self.link.on_frame(data(SECOND, ethernet(AP, source=SECOND), 1))   # its forgotten key
        self.assertEqual(station.stats['rx_decrypt_fail'], 1)

    def test_a_snapshot_cannot_undo_a_concurrent_pairing(self):
        stale = json.loads(self.state_path.read_text())
        third = {'mac': '02:00:00:00:00:04', 'txid': 1, 'time': 3.0, 'key': NEW_KEY.hex()}
        self.link.persist_controller(third)
        self.reload()
        self.assertIn(frames.mac_bytes(third['mac']), self.link.stations)
        self.assertNotEqual(stale, self.saved())


class WebUI(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.config_path, self.state_path, self.run_dir = root / 'config.json', root / 'state.json', root / 'run'
        self.config_path.write_text(json.dumps({'backend': 'sonos', 'rooms': [], 'controllers': {
            FIRST_MAC: {'name': 'Kitchen'}, SECOND_MAC: {'name': 'Den', 'auto_reconnect': False}}}))
        self.state_path.write_text(json.dumps(STATE))
        self.bridge = webui.Bridge(self.config_path, self.state_path, self.run_dir, root / 'players.pid',
                                   webui.DataFile(root / 'webui.json'), log=lambda _: None)
        self.addCleanup(self.bridge.close)
        self.bridge.speaker_health = Mock(return_value={'healthy': True})
        self.bridge.leases = Mock(return_value={})
        self.bridge.link_status = Mock(return_value={'running': True, 'events': [], 'stations': {}})
        self.bridge.player_runtime = Mock(return_value={})
        self.updates = Mock()
        self.bridge.updates = Mock(return_value=self.updates)

    def saved(self):
        return json.loads(self.state_path.read_text())

    def test_forget_and_restore_one_controller(self):
        result = self.bridge.set_controller_forgotten(SECOND_MAC.upper(), True)
        self.assertEqual((result['changed'], result['forgotten']), (True, True))
        self.assertEqual(self.saved()['controllers'][1],
                         {'mac': SECOND_MAC, 'aid': 65, 'key': STATE['controllers'][1]['key'], 'forgotten': True})
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.updates.cancel_controller.assert_called_once_with(SECOND_MAC, reason='The controller was forgotten.')
        listing = self.bridge.controllers()
        self.assertEqual([c['mac'] for c in listing['controllers']], [FIRST_MAC])
        self.assertEqual(listing['forgotten'], [{'mac': SECOND_MAC, 'name': 'Den'}])
        # A forgotten controller is not served anywhere else in the web UI.
        for call in (lambda: self.bridge.rename_controller(SECOND_MAC, 'Other'),
                     lambda: self.bridge.spotify_controller(SECOND_MAC)):
            with self.assertRaises(webui.ApiError) as caught: call()
            self.assertEqual(caught.exception.status, 404)
        self.assertFalse(self.bridge.set_controller_forgotten(SECOND_MAC, True)['changed'])

        result = self.bridge.set_controller_forgotten(SECOND_MAC, False)
        self.assertTrue(result['changed'])
        self.assertEqual(self.saved()['controllers'], [{**record, 'aid': record['aid']} for record in STATE['controllers']])
        # Its settings were kept while it was forgotten.
        config = json.loads(self.config_path.read_text())
        self.assertEqual(config['controllers'][SECOND_MAC], {'name': 'Den', 'auto_reconnect': False})
        self.assertEqual(self.bridge.controllers()['forgotten'], [])

    def test_last_unknown_and_busy_controllers_are_refused_without_changes(self):
        self.bridge.set_controller_forgotten(FIRST_MAC, True)
        before = self.state_path.read_bytes()
        for mac, status, pairing in ((SECOND_MAC, 400, False), ('02:00:00:00:00:99', 404, False),
                                     ('not-a-mac', 400, False), (SECOND_MAC, 409, True)):
            with self.subTest(mac=mac, status=status), \
                    patch.object(self.bridge, 'pairing_status', return_value={'running': pairing}), \
                    self.assertRaises(webui.ApiError) as caught:
                self.bridge.set_controller_forgotten(mac, True)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_a_pairing_command_holding_the_lock_blocks_forget(self):
        import fcntl
        self.run_dir.mkdir()
        with (self.run_dir / 'pair.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(webui.ApiError) as caught:
                self.bridge.set_controller_forgotten(SECOND_MAC, True)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.saved(), STATE)


if __name__ == '__main__': unittest.main()
