"""Pausing proactive reconnect traffic preserves pairing and received traffic."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge_service as service
import sonosnet_link as sl
from test_multi_controller_link import STATE, FIRST, SECOND, Radio, ethernet, management, null, poll, decode
import test_bridge_service

FIRST_MAC, SECOND_MAC = (':'.join('%02x' % byte for byte in mac) for mac in (FIRST, SECOND))


class ReconnectPolicy(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.config = self.folder / 'config.json'
        self.state = self.folder / 'state.json'
        self.state.write_text(json.dumps(STATE))
        self.radio, self.logs = Radio(), []
        pacer = patch.object(sl, 'PACE_WINDOW', 0)
        pacer.start()
        self.addCleanup(pacer.stop)

    def make_link(self, config):
        self.config.write_text(json.dumps(config))
        return sl.Link(STATE, self.radio, -1, self.logs.append, config_path=self.config)

    def paused(self):
        return self.make_link({'controllers': {SECOND_MAC: {'auto_reconnect': False}}})

    def test_valid_missing_or_null_legacy_settings_default_to_enabled(self):
        for config in ({}, {'controllers': None}, {'controllers': {SECOND_MAC: None}}):
            with self.subTest(config=config):
                link = self.make_link(config)
                self.assertTrue(all(station.auto_reconnect for station in link.stations.values()))
        link = sl.Link(STATE, self.radio, -1, self.logs.append)
        self.assertTrue(all(station.auto_reconnect for station in link.stations.values()))

    def test_pause_suppresses_only_targeted_unsolicited_probes(self):
        link = self.paused()
        with link.lock: link.service_stations(sl.time.monotonic())
        self.assertEqual([frame[4:10] for frame in self.radio.sent], [FIRST])
        station = link.stations[SECOND]
        self.assertFalse(link.station_status(station, sl.time.monotonic())['auto_reconnect'])
        self.radio.sent.clear()
        link.on_frame(management(SECOND, 4))
        link.on_frame(management(SECOND, 0, bytes.fromhex('1003f401dd06000e58020101')))
        self.assertEqual([frame[0] for frame in self.radio.sent], [0x50, 0x10])
        self.assertTrue(station.associated)
        link.on_frame(null(SECOND))
        link.from_tap(ethernet(SECOND))
        link.on_frame(poll(SECOND, station.aid))
        self.assertEqual(decode(self.radio.sent[-1])[0], SECOND)
        self.assertTrue(station.dozing)
        self.assertFalse(station.auto_reconnect)
        self.assertEqual(station.aid, 65)
        self.assertEqual(station.key.hex(), STATE['controllers'][1]['key'])

    def test_live_reload_changes_only_policy_and_obeys_one_second_interval(self):
        link = self.paused()
        station, start, key = link.stations[SECOND], link.start, link.stations[SECOND].key
        self.config.write_text(json.dumps({'controllers': {SECOND_MAC: {'auto_reconnect': True}},
                                          'private_setting': 'never-log-this'}))
        link.reload_reconnect_policy(start + .5)
        self.assertFalse(station.auto_reconnect)
        link.reload_reconnect_policy(start + 1.01)
        self.assertTrue(station.auto_reconnect)
        self.assertIs(link.stations[SECOND], station)
        self.assertEqual((link.start, station.key), (start, key))
        self.assertNotIn('never-log-this', json.dumps(self.logs))

    def test_malformed_or_unreadable_updates_keep_last_good_policy(self):
        link = self.paused()
        for text in ('private-not-json', 'null', '{"controllers": []}',
                     json.dumps({'controllers': {SECOND_MAC: {'auto_reconnect': 'false'}}})):
            self.config.write_text(text)
            link.reload_reconnect_policy(sl.time.monotonic(), force=True)
            self.assertFalse(link.stations[SECOND].auto_reconnect)
            self.assertTrue(link.stations[FIRST].auto_reconnect)
        self.config.unlink()
        link.reload_reconnect_policy(sl.time.monotonic(), force=True)
        self.assertFalse(link.stations[SECOND].auto_reconnect)
        self.assertNotIn('private-not-json', json.dumps(self.logs))

    def test_initial_invalid_config_fails_closed_until_first_valid_policy(self):
        self.config.write_text('broken')
        link = sl.Link(STATE, self.radio, -1, self.logs.append, config_path=self.config)
        self.assertFalse(any(station.auto_reconnect for station in link.stations.values()))
        link.on_frame(management(SECOND, 0, bytes.fromhex('1003f401dd06000e58020101')))
        self.assertEqual(self.radio.sent[-1][0], 0x10)
        self.config.write_text('{}')
        link.reload_reconnect_policy(sl.time.monotonic(), force=True)
        self.assertTrue(all(station.auto_reconnect for station in link.stations.values()))

    def test_transient_read_error_retries_the_same_file_on_next_check(self):
        link = self.paused()
        self.config.write_text('{}')
        with patch.object(Path, 'read_text', side_effect=OSError('temporary read failure')):
            link.reload_reconnect_policy(link.start + 1.01)
        self.assertFalse(link.stations[SECOND].auto_reconnect)
        link.reload_reconnect_policy(link.start + 2.02)
        self.assertTrue(link.stations[SECOND].auto_reconnect)

    def test_cli_pause_resume_preserves_pairing_and_other_configuration(self):
        original = self.state.read_bytes()
        config = {'rooms': [{'name': 'Keep me'}], 'controllers': {
            SECOND_MAC: {'name': 'Controller 2', 'visible_components': []}}}
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)
        with patch.object(service, 'log'):
            result = service.controller_search(self.state, self.config, SECOND_MAC.upper(), 'pause')
            self.assertFalse(result['auto_reconnect'])
            saved = json.loads(self.config.read_text())
            self.assertFalse(saved['controllers'][SECOND_MAC]['auto_reconnect'])
            self.assertEqual(saved['rooms'], config['rooms'])
            self.assertEqual(saved['controllers'][SECOND_MAC]['visible_components'], [])
            service.controller_search(self.state, self.config, SECOND_MAC, 'resume')
        self.assertTrue(json.loads(self.config.read_text())['controllers'][SECOND_MAC]['auto_reconnect'])
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_cli_rejects_unknown_controller_without_changing_any_file(self):
        self.config.write_text('{}')
        before = self.config.read_bytes(), self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'No such paired'):
            service.controller_search(self.state, self.config, '02:00:00:00:00:99', 'pause')
        self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)

    def test_supervisor_passes_its_actual_config_path_to_radio_link(self):
        fixture = test_bridge_service.Services(methodName='runTest')
        fixture.setUp()
        try:
            started = fixture.run_bridge(lambda name, n, age: None, 1)
            args = started['link'][0].args
            self.assertEqual(args[args.index('--config') + 1], str(fixture.folder / 'config.json'))
        finally:
            fixture.doCleanups()


if __name__ == '__main__': unittest.main()
