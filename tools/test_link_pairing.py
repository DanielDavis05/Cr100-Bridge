"""Pairing inside the running link: no outage on channel 1, channel-1 visits elsewhere."""
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import bridge_service as service
import frames
import link_pairing
import netstart_pair as netstart
import sonosnet_link as sl

KEY = '0123456789abcdef0123456789abcdef'
STATE = {'bssid': '02:00:00:00:00:01', 'household': 'Sonos_test', 'freq': 2437,
         'uuid': 'RINCON_02000000000101400', 'controllers': [{'mac': '02:00:00:00:00:02', 'aid': 1, 'key': KEY}]}
AP = frames.mac_bytes(STATE['bssid'])
OLD = frames.mac_bytes('02:00:00:00:00:02')
NEW = '02:00:00:00:00:07'


class Radio:
    def __init__(self): self.sent = []
    def send(self, packet, flags=0):
        assert flags & socket.MSG_DONTWAIT
        self.sent.append(packet[len(frames.TX_RADIOTAP):])


def query_frame(mac=NEW, txid=7):
    payload = netstart.encode(netstart.QUERY, [(0, netstart.player_uuid(STATE['bssid'])),
                                               (1, struct.pack('!I', txid)), (2, b''),
                                               (3, netstart.PLAYER_PUBLIC_KEY), (6, struct.pack('!I', 4))])
    return (b'\x40\x00\x00\x00' + frames.BROADCAST + frames.mac_bytes(mac) + frames.BROADCAST + b'\x10\x00'
            + netstart.frame_body(payload))


def settle(link, next_beacon=None, passes=6):
    """Main-loop passes far enough apart for the 1.5 ms transmit pacer."""
    for _ in range(passes):
        time.sleep(0.002)
        link.service_pairing(time.monotonic(), next_beacon)


def netstart_messages(sent):
    """(type, fields) of every Netstart frame the link sent."""
    found = []
    for frame in sent:
        if frame[0] == 0x40:
            payload = netstart.parse_body(frame[24:])
            if payload: found.append(netstart.decode(payload)[1:])
    return found


class LinkPairing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        tap, self.tap = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close); self.addCleanup(self.tap.close)
        self.tap_fd = tap.fileno()

    def link(self, freq=2437):
        state = {**STATE, 'freq': freq}
        self.state_path = self.folder / 'bridge-state.json'
        self.state_path.write_text(json.dumps(state))
        self.radio, self.logs, self.tunes = Radio(), [], []
        link = sl.Link(state, self.radio, self.tap_fd, self.logs.append, run_dir=self.folder,
                       state_path=self.state_path, set_channel=self.tunes.append)
        return link

    def request(self, seconds=180):
        request = {'id': 'abc123', 'until': time.time() + seconds}
        link_pairing.write_json_atomic(self.folder / link_pairing.PAIR_REQUEST, request)
        return request

    def status(self):
        return json.loads((self.folder / link_pairing.PAIR_STATUS).read_text())

    def test_channel_1_household_pairs_without_any_channel_change(self):
        link = self.link(freq=2412)
        self.request()
        link.service_pairing(time.monotonic(), None)
        self.assertIsNotNone(link.pairing)
        self.assertEqual(netstart_messages(self.radio.sent)[0][0], netstart.ALIVE)
        link.on_frame(query_frame())
        # Saved and accepted as a station before the RESPONSE is released.
        saved = json.loads(self.state_path.read_text())
        self.assertEqual([c['mac'] for c in saved['controllers']], ['02:00:00:00:00:02', NEW])
        self.assertEqual(saved['controllers'][1]['aid'], 2)
        self.assertEqual(saved['uuid'], STATE['uuid'])
        self.assertIn(frames.mac_bytes(NEW), link.stations)
        settle(link)
        responses = [fields for kind, fields in netstart_messages(self.radio.sent) if kind == netstart.RESPONSE]
        self.assertEqual(len(responses), link_pairing.RESPONSE_COPIES)
        self.assertEqual(struct.unpack('!I', responses[0][5])[0], 2412)
        self.assertEqual(self.tunes, [])
        self.assertEqual(self.status()['controllers'], [NEW])
        # The new controller's association is recognised at once.
        association = b'\x00\x01\x00\x00' + AP + frames.mac_bytes(NEW) + AP + b'\x20\x00' + b'\x31\x04\x0a\x00'
        link.on_frame(association)
        self.assertTrue(link.stations[frames.mac_bytes(NEW)].associated)

    def test_save_failure_withholds_the_response_until_a_retry_succeeds(self):
        link = self.link(freq=2412)
        self.request()
        link.service_pairing(time.monotonic(), None)
        self.state_path.write_text(json.dumps({**STATE, 'freq': 2412, 'household': 'Sonos_other'}))
        link.on_frame(query_frame())
        link.service_pairing(time.monotonic(), None)
        self.assertFalse([1 for kind, _ in netstart_messages(self.radio.sent) if kind == netstart.RESPONSE])
        self.assertNotIn(frames.mac_bytes(NEW), link.stations)
        self.state_path.write_text(json.dumps({**STATE, 'freq': 2412}))
        link.on_frame(query_frame())          # the CR100 resends every 250 ms
        settle(link)
        self.assertTrue([1 for kind, _ in netstart_messages(self.radio.sent) if kind == netstart.RESPONSE])
        self.assertIn(frames.mac_bytes(NEW), link.stations)

    def test_other_channels_visit_channel_1_only_between_beacons(self):
        link = self.link(freq=2437)
        self.request()
        start = time.monotonic()
        link.service_pairing(start, None)
        self.assertEqual(netstart_messages(self.radio.sent), [])     # no beacon grid yet: stay home
        link.last_beacon_at, next_beacon = start, start + 0.1024
        link.service_pairing(start + 0.010, next_beacon)
        self.assertEqual(self.tunes, [])                              # still serving PS-Polls after the beacon
        link.service_pairing(start + link_pairing.HOME_DWELL + 0.001, next_beacon)
        self.assertEqual(self.tunes, [1])
        self.assertEqual(netstart_messages(self.radio.sent)[0][0], netstart.ALIVE)
        # While away, home-channel traffic waits and keeps its nonce.
        station = link.stations[OLD]
        station.associated, station.dozing, station.last_pm_rx = True, False, time.monotonic()
        pn = link.tx_pn
        link.from_tap(OLD + AP + b'\x08\x06' + b'arp')
        self.assertEqual(len(station.queue), 1)
        self.assertEqual(link.tx_pn, pn)
        self.assertEqual(link.send(frames.mgmt_header(8, frames.BROADCAST, AP, 1) + b'beacon'), sl.BUSY)
        link.away_until = time.monotonic() - 0.001
        link.service_pairing(time.monotonic(), next_beacon)
        self.assertEqual(self.tunes, [1, 6])
        time.sleep(0.002)                                             # the transmit pacer
        station.last_pm_rx = time.monotonic()
        link.service_stations(time.monotonic())
        self.assertEqual(len(station.queue), 0)                       # delivered once home
        # One visit per beacon interval.
        link.service_pairing(start + 0.060, next_beacon)
        self.assertEqual(self.tunes, [1, 6])

    def test_slow_channel_changes_return_home_earlier(self):
        link = self.link(freq=2437)
        self.request()
        start = time.monotonic()
        link.service_pairing(start, None)
        link.set_channel = lambda channel: time.sleep(0.03)            # a slow board or a busy driver
        link.tune(1); link.tune(6)
        self.assertGreaterEqual(link.tune_cost, 0.03)
        link.set_channel = self.tunes.append
        start = time.monotonic()
        link.last_beacon_at, next_beacon = start, start + 0.1024
        link.service_pairing(start + link_pairing.HOME_DWELL + 0.001, next_beacon)
        self.assertEqual(link.tuned, 1)
        self.assertLessEqual(link.away_until, next_beacon - link.tune_cost - link_pairing.RETURN_SLACK + 1e-9)
        # Too slow to fit a useful visit: stay home.
        link.tune(6)
        link.tune_cost = 0.06
        link.last_beacon_at = start
        link.service_pairing(start + link_pairing.HOME_DWELL + 0.001, next_beacon)
        self.assertEqual(link.tuned, 6)

    def test_query_heard_on_a_visit_is_answered_on_that_visit(self):
        link = self.link(freq=2437)
        self.request()
        start = time.monotonic()
        link.service_pairing(start, None)
        link.last_beacon_at = start - 0.05
        link.service_pairing(start, start + 0.06)
        self.assertEqual(self.tunes, [1])
        link.on_frame(query_frame())
        settle(link, start + 0.06)
        self.assertEqual(self.tunes, [1])
        kinds = [kind for kind, _ in netstart_messages(self.radio.sent)]
        self.assertEqual(kinds.count(netstart.RESPONSE), link_pairing.RESPONSE_COPIES)
        responses = [fields for kind, fields in netstart_messages(self.radio.sent) if kind == netstart.RESPONSE]
        self.assertEqual(struct.unpack('!I', responses[0][5])[0], 2437)   # the household's own channel

    def test_window_ends_after_the_controller_stops_asking_and_returns_home(self):
        link = self.link(freq=2437)
        self.request()
        start = time.monotonic()
        link.service_pairing(start, None)
        link.last_beacon_at = start - 0.05
        link.service_pairing(start, start + 0.06)
        link.on_frame(query_frame())
        link.pairing.last_query -= link_pairing.LINGER
        link.service_pairing(time.monotonic(), start + 0.06)
        self.assertIsNone(link.pairing)
        self.assertEqual(self.tunes, [1, 6])
        self.assertEqual(self.status()['state'], 'done')
        link.service_pairing(time.monotonic() + 1, None)               # the same request is not reopened
        self.assertIsNone(link.pairing)

    def test_cancel_and_expiry(self):
        link = self.link(freq=2412)
        self.request()
        link.service_pairing(time.monotonic(), None)
        (self.folder / link_pairing.PAIR_REQUEST).unlink()
        link.pair_check_at = 0
        link.service_pairing(time.monotonic(), None)
        self.assertIsNone(link.pairing)
        self.assertEqual(self.status()['state'], 'failed')
        request = self.request()
        request['id'] = 'later'
        link_pairing.write_json_atomic(self.folder / link_pairing.PAIR_REQUEST, request)
        link.pair_check_at = 0
        link.service_pairing(time.monotonic(), None)
        link.pairing.until = time.time() - 1
        link.service_pairing(time.monotonic(), None)
        self.assertEqual(self.status()['state'], 'expired')

    def test_failed_channel_change_ends_pairing_and_a_failed_return_is_fatal(self):
        link = self.link(freq=2437)
        self.request()
        start = time.monotonic()
        link.service_pairing(start, None)
        calls = []
        def fail(channel):
            calls.append(channel)
            raise subprocess.CalledProcessError(1, 'iw')
        link.set_channel = fail
        link.last_beacon_at = start - 0.05
        with self.assertRaisesRegex(RuntimeError, 'return the radio to channel 6'):
            link.service_pairing(start, start + 0.06)
        self.assertEqual(calls, [1, 6])

    def test_netstart_probe_is_not_a_station_discovery_probe(self):
        link = self.link(freq=2412)
        frame = query_frame(mac='02:00:00:00:00:02')
        link.on_frame(frame)                                           # no window open: ignored
        self.assertEqual(self.radio.sent, [])
        self.assertEqual(link.stations[OLD].stats['probe_req'], 0)

    def test_read_request_rejects_stale_or_malformed_requests(self):
        path = self.folder / 'request.json'
        for value in ('nope', [], {'id': 'a'}, {'id': '', 'until': time.time() + 9},
                      {'id': 'a', 'until': time.time() - 1}, {'id': 'a', 'until': 'soon'}):
            path.write_text(json.dumps(value))
            self.assertIsNone(link_pairing.read_request(path), value)
        self.assertIsNone(link_pairing.read_request(self.folder / 'missing.json'))


class PairCommand(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = Path(temp.name)

    def drive(self, statuses):
        """Run pair_in_link with a fake link that writes ``statuses`` one poll at a time."""
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            request = json.loads((self.run / link_pairing.PAIR_REQUEST).read_text())
            if statuses:
                state = statuses.pop(0)
                if state is not None:
                    link_pairing.write_json_atomic(self.run / link_pairing.PAIR_STATUS,
                                                   {'id': request['id'], 'hopping': True, **state})
        with patch('builtins.print'):
            return service.pair_in_link(self.run, clock=lambda: clock[0], sleep=sleep)

    def test_done_returns_true_and_removes_the_request(self):
        self.assertTrue(self.drive([{'state': 'listening'}, {'state': 'paired', 'controllers': [NEW]},
                                    {'state': 'done', 'controllers': [NEW]}]))
        self.assertFalse((self.run / link_pairing.PAIR_REQUEST).exists())

    def test_no_answer_falls_back(self):
        self.assertFalse(self.drive([None] * 200))
        self.assertFalse((self.run / link_pairing.PAIR_REQUEST).exists())

    def test_expired_and_failed_raise(self):
        with self.assertRaisesRegex(RuntimeError, 'No CR100 responded'):
            self.drive([{'state': 'listening'}, {'state': 'expired'}])
        with self.assertRaisesRegex(RuntimeError, 'setup channel'):
            self.drive([{'state': 'failed', 'error': 'The radio could not change to the setup channel'}])

    def test_a_stale_status_from_another_request_is_ignored(self):
        link_pairing.write_json_atomic(self.run / link_pairing.PAIR_STATUS, {'id': 'old', 'state': 'done'})
        self.assertFalse(self.drive([None] * 200))

    def test_pair_uses_the_running_link_and_never_stops_the_bridge(self):
        state = self.run / 'bridge-state.json'
        state.write_text(json.dumps(STATE))
        with patch.object(service, 'bridge_active', return_value=True), \
                patch.object(service, 'pair_in_link', return_value=True) as in_link, \
                patch.object(service.subprocess, 'run') as command, patch('builtins.print'):
            service.pair(state, self.run)
        in_link.assert_called_once_with(self.run)
        command.assert_not_called()

    def test_unanswered_request_falls_back_to_pausing_the_bridge(self):
        state = self.run / 'bridge-state.json'
        state.write_text(json.dumps(STATE))
        with patch.object(service, 'bridge_active', return_value=True), \
                patch.object(service, 'pair_in_link', return_value=False), \
                patch.object(service, 'prepare_radio') as prepare, \
                patch.object(service.subprocess, 'Popen', return_value=Mock(wait=Mock(return_value=0))), \
                patch.object(service.subprocess, 'run') as command, patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'No CR100 responded'):
                service.pair(state, self.run)
        command.assert_any_call(['systemctl', 'stop', 'cr100-bridge.service'], check=True)
        prepare.assert_called_once_with(1)

    def test_first_household_defaults_to_the_setup_channel(self):
        state = self.run / 'bridge-state.json'
        commands = []
        def spawn(args, **kwargs):
            commands.append(args)
            return Mock(wait=Mock(return_value=0))
        with patch.object(service, 'prepare_radio'), patch.object(service, 'radio_mac', return_value=STATE['bssid']), \
                patch.object(service.subprocess, 'Popen', side_effect=spawn), \
                patch.object(service.subprocess, 'run'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'No CR100 responded'):
                service.pair(state, self.run)
        self.assertEqual(commands[0][commands[0].index('--freq') + 1], '2412')


class ChannelSetting(unittest.TestCase):
    def setUp(self):
        import webui
        self.webui = webui
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.state = folder / 'bridge-state.json'
        self.state.write_text(json.dumps(STATE))
        self.bridge = webui.Bridge(folder / 'config.json', self.state, folder / 'run', folder / 'pid',
                                   webui.DataFile(folder / 'webui.json'), log=lambda *_: None, managed=True)

    def systemctl(self, running=False):
        def run(command, **kwargs):
            self.commands.append(command)
            return Mock(stdout='ActiveState=%s\nResult=success\n' % ('active' if running else 'inactive'))
        self.commands = []
        return patch.object(self.webui.subprocess, 'run', side_effect=run)

    def test_change_writes_the_household_channel_and_restarts_the_link(self):
        with self.systemctl():
            result = self.bridge.set_channel({'channel': 1})
        self.assertTrue(result['changed'])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved['freq'], 2412)
        self.assertEqual({k: v for k, v in saved.items() if k != 'freq'}, {k: v for k, v in STATE.items() if k != 'freq'})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertIn(['systemctl', '--no-block', 'restart', 'cr100-bridge.service'], self.commands)

    def test_same_channel_is_a_no_op(self):
        with self.systemctl():
            self.assertFalse(self.bridge.set_channel({'channel': 6})['changed'])
        self.assertNotIn(['systemctl', '--no-block', 'restart', 'cr100-bridge.service'], self.commands)

    def test_rejections(self):
        for payload in ({'channel': 2}, {'channel': '1'}, {'channel': True}, {}, None):
            with self.subTest(payload=payload), self.systemctl(), self.assertRaises(self.webui.ApiError):
                self.bridge.set_channel(payload)
        with self.systemctl(running=True), self.assertRaisesRegex(self.webui.ApiError, 'pairing'):
            self.bridge.set_channel({'channel': 1})
        self.state.write_text(json.dumps({k: v for k, v in STATE.items() if k != 'controllers'}))
        with self.systemctl(), self.assertRaisesRegex(self.webui.ApiError, 'Pair a controller'):
            self.bridge.set_channel({'channel': 1})
        self.bridge.managed = False
        with self.assertRaisesRegex(self.webui.ApiError, 'installed bridge'):
            self.bridge.set_channel({'channel': 1})
        self.assertEqual(json.loads(self.state.read_text())['freq'], 2437)


if __name__ == '__main__':
    unittest.main()
