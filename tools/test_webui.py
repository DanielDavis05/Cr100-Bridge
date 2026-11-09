#!/usr/bin/env python3
"""Offline tests for the web interface (webui.py) and the player side it drives
(zoneplayers room slots and the Music menu's library speaker). Home Assistant is
fake_ha.py; Spotify's token exchange and profile are stand-ins."""
import http.client
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_ha
import webui
import zoneplayers
from ha_client import HomeAssistant
from test_zoneplayers import HOUSEHOLD, zp

CR = '00:0e:58:02:6f:a9'


def write(path, value):
    Path(path).write_text(json.dumps(value) if not isinstance(value, str) else value)


class Fixture(unittest.TestCase):
    """A config with three legacy rooms (no slots), a link log and leases in a temp dir."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.fake = fake_ha.FakeHA([fake_ha.sonos_state('media_player.kitchen', 'Kitchen'),
                                    fake_ha.sonos_state('media_player.patio', 'Patio'),
                                    fake_ha.sonos_state('media_player.den', 'Den'),
                                    fake_ha.sonos_state('media_player.tv', 'TV')])
        self.fake.platforms['media_player.tv'] = 'webostv'
        write(self.dir / 'ha-token', fake_ha.TOKEN)
        self.config = self.dir / 'config.json'
        write(self.config, {'home_assistant': {'url': self.fake.url, 'token_file': 'ha-token'},
                            'rooms': [{'entity': 'media_player.kitchen'}, {'entity': 'media_player.patio'},
                                      {'name': 'Spare'}],
                            'spotify_music_service': {'enabled': True, 'client_id': 'a' * 32,
                                                      'token_file': 'spotify-token.json'},
                            'other': {'kept': True}})
        write(self.dir / 'state.json', {'household': HOUSEHOLD, 'freq': 2437, 'cr100_mac': CR, 'bssid': 'dc:a6:32:e7:3b:0e'})
        self.run_dir = self.dir / 'run'
        self.run_dir.mkdir()
        self.logs = []
        self.data = webui.DataFile(self.dir / 'webui.json')
        self.exchanges = []
        self.bridge = webui.Bridge(self.config, self.dir / 'state.json', self.run_dir, self.dir / 'players.pid',
                                   self.data, self.logs.append, token_exchange=self.exchange,
                                   profile_fetch=lambda client_id, token_file: {'name': 'Dee', 'plan': 'premium'})
        self.bridge.connect_ha()
        self.assertTrue(self.bridge.ha.connected.wait(5))

    def tearDown(self):
        self.bridge.close()
        self.fake.close()

    def exchange(self, pending, code, token_file):
        self.exchanges.append(code)
        write(token_file, {'client_id': pending['client_id'], 'access_token': 'x', 'refresh_token': 'y',
                           'expires_at': time.time() + 3600, 'scope': ''})

    def saved(self):
        return json.loads(self.config.read_text())


class Speakers(Fixture):
    def test_list_marks_shown_hidden_and_non_sonos(self):
        rows = {r['key']: r for r in self.bridge.speakers()['speakers']}
        self.assertEqual([k for k, r in rows.items() if r['shown']], ['media_player.kitchen', 'media_player.patio', 'sim:Spare'])
        self.assertFalse(rows['media_player.den']['shown'])
        self.assertTrue(rows['media_player.kitchen']['sonos'])
        self.assertEqual((rows['media_player.tv']['platform'], rows['media_player.tv']['sonos']), ('webostv', False))
        self.assertTrue(rows['sim:Spare']['simulated'])

    def test_save_keeps_player_identities_and_reloads_players(self):
        write(self.dir / 'players.pid', str(os.getpid()))
        with patch('webui.os.kill') as kill:
            result = self.bridge.save_speakers({'rooms': [
                {'key': 'media_player.patio', 'name': 'Garden'}, {'key': 'media_player.den', 'name': 'Den'},
                {'key': 'sim:Spare'}], 'library_entity': 'media_player.patio'})
        kill.assert_called_once_with(os.getpid(), signal.SIGHUP)
        self.assertTrue(result['applied'])
        config = self.saved()
        # Patio keeps its old position's slot (1), Spare its (2); Kitchen, now hidden, keeps 0
        # reserved, so the newly shown Den takes the first never-used slot.
        self.assertEqual(config['rooms'], [{'entity': 'media_player.patio', 'name': 'Garden', 'slot': 1},
                                           {'entity': 'media_player.den', 'slot': 3},
                                           {'name': 'Spare', 'slot': 2}])
        self.assertEqual(config['player_slots'], {'media_player.kitchen': 0, 'media_player.patio': 1,
                                                  'media_player.den': 3})
        self.assertEqual(config['music_menu'], {'library_entity': 'media_player.patio'})
        self.assertEqual(config['other'], {'kept': True})
        # Shown again later, Kitchen gets its old player back.
        with patch('webui.os.kill'):
            self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen'}], 'library_entity': None})
        self.assertEqual(self.saved()['rooms'], [{'entity': 'media_player.kitchen', 'slot': 0}])
        self.assertNotIn('library_entity', self.saved()['music_menu'])

    def test_save_rejects_bad_input_and_leaves_config_alone(self):
        before = self.config.read_text()
        for payload, message in [
                ({'rooms': [{'key': 'media_player.kitchen', 'name': 'Patio'}, {'key': 'media_player.patio'}]}, 'both named'),
                ({'rooms': [{'key': 'media_player.nope'}]}, 'not a Home Assistant'),
                ({'rooms': [{'key': 'media_player.kitchen'}, {'key': 'media_player.kitchen'}]}, 'once'),
                ({'rooms': [{'key': 'media_player.tv'}], 'library_entity': 'media_player.tv'}, 'Sonos speaker'),
                ({'rooms': [{'key': 'media_player.kitchen', 'name': 'x' * 31}]}, 'characters')]:
            with self.assertRaises(webui.ApiError) as caught:
                self.bridge.save_speakers(payload)
            self.assertIn(message, caught.exception.message)
        self.assertEqual(self.config.read_text(), before)

    def test_room_icons(self):
        rows = {r['key']: r for r in self.bridge.speakers()['speakers']}
        self.assertEqual((rows['media_player.kitchen']['icon'], rows['media_player.kitchen']['custom_icon']), ('kitchen', ''))
        self.assertEqual(rows['media_player.tv']['icon'], 'tvroom')       # guessed from the name
        icons = self.bridge.speakers()['icons']
        self.assertEqual(len(icons), 15)   # one per picture: same-picture rooms are merged
        self.assertIn({'name': 'bedroom', 'label': 'Bedroom'}, icons)
        with self.assertRaises(webui.ApiError):
            self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen', 'icon': 'spaceship'}]})
        self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen', 'icon': 'kitchen'},
                                             {'key': 'media_player.patio', 'icon': 'pool'}]})
        self.assertEqual(self.saved()['rooms'], [{'entity': 'media_player.kitchen', 'slot': 0},   # = guess: not stored
                                                 {'entity': 'media_player.patio', 'icon': 'pool', 'slot': 1}])
        # A same-picture name from an older config reads as its picture.
        self.bridge.update_config(lambda c: c['rooms'][0].update(icon='masterbedroom'))
        kitchen = next(r for r in self.bridge.speakers()['speakers'] if r['key'] == 'media_player.kitchen')
        self.assertEqual((kitchen['icon'], kitchen['custom_icon']), ('bedroom', 'bedroom'))
        self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen', 'icon': 'guestroom'},
                                             {'key': 'media_player.patio', 'icon': 'pool'}]})
        self.assertEqual(self.saved()['rooms'][0], {'entity': 'media_player.kitchen', 'icon': 'bedroom', 'slot': 0})
        # Hidden, Patio keeps its icon; shown again, it comes back.
        self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen'}]})
        self.assertEqual(self.saved()['room_icons'], {'media_player.patio': 'pool'})
        patio = next(r for r in self.bridge.speakers()['speakers'] if r['key'] == 'media_player.patio')
        self.assertEqual((patio['shown'], patio['icon'], patio['custom_icon']), (False, 'pool', 'pool'))
        self.bridge.save_speakers({'rooms': [{'key': 'media_player.patio', 'icon': patio['custom_icon']}]})
        self.assertEqual(self.saved()['rooms'], [{'entity': 'media_player.patio', 'icon': 'pool', 'slot': 1}])
        self.assertNotIn('room_icons', self.saved())

    def test_no_running_bridge_saves_for_next_start(self):
        result = self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen'}]})
        self.assertFalse(result['applied'])
        self.assertIn('next starts', result['message'])


class Controllers(Fixture):
    def link_log(self, wall, **station):
        lines = [{'t': 0.1, 'event': 'start'}, {'t': 50.0, 'event': 'associated'},
                 {'t': 80.0, 'event': 'cr100-searching', 'reason': 'check-unanswered'},
                 {'t': 81.0, 'event': 'link-heard'},
                 {'t': 100.0, 'event': 'stats', 'wall': wall,
                  'stations': [{'mac': CR, 'associated': True, 'searching': False, 'dozing': False, **station}]}]
        write(self.run_dir / 'link.jsonl', '\n'.join(json.dumps(l) for l in lines) + '\n')
        write(self.run_dir / 'leases', '1793500000 %s 192.168.69.52 * 01:%s\n' % (CR, CR))

    def test_status_from_link_log_and_leases(self):
        self.link_log(time.time(), last_heard_s=2.0)
        result = self.bridge.controllers()
        [c] = result['controllers']
        self.assertEqual((c['status'], c['ip'], c['name']), ('connected', '192.168.69.52', 'Controller'))
        self.assertEqual((result['channel'], result['household']), (6, HOUSEHOLD))
        self.assertTrue(result['multi_controller'])
        self.assertEqual([e['event'] for e in c['events']], ['associated', 'cr100-searching', 'link-heard'])
        self.assertAlmostEqual(c['events'][0]['time'], time.time() - 50, delta=2)

    def test_idle_away_and_radio_off(self):
        self.link_log(time.time() - 5, last_heard_s=40.0)
        [c] = self.bridge.controllers()['controllers']
        self.assertEqual(c['status'], 'dozing')
        self.assertGreaterEqual(c['last_heard_s'], 45)   # counted to now, not to the log line
        self.link_log(time.time(), last_heard_s=300.0, associated=False)
        self.assertEqual(self.bridge.controllers()['controllers'][0]['status'], 'away')
        self.link_log(time.time() - 120, last_heard_s=1.0)
        self.assertEqual(self.bridge.controllers()['controllers'][0]['status'], 'radio-off')

    def test_several_controllers_and_rename(self):
        write(self.dir / 'state.json', {'household': HOUSEHOLD, 'freq': 2437,
                                        'controllers': [{'mac': CR}, {'mac': '00:0E:58:11:22:33'}]})
        self.bridge.rename_controller(CR.upper(), 'Kitchen remote')
        names = [c['name'] for c in self.bridge.controllers()['controllers']]
        self.assertEqual(names, ['Kitchen remote', 'Controller 2'])
        with self.assertRaises(webui.ApiError):
            self.bridge.rename_controller('00:0e:58:99:99:99', 'Nobody')

    def test_controller_status_and_events_are_independent(self):
        other = '00:0e:58:11:22:33'
        wall = time.time()
        write(self.dir / 'state.json', {'household': HOUSEHOLD, 'freq': 2437,
                                        'controllers': [{'mac': CR}, {'mac': other}]})
        # An old unaddressed station event cannot be attributed with several
        # controllers; a radio-wide outage belongs to both controllers.
        lines = [{'t': 1, 'event': 'associated'},
                 {'t': 2, 'event': 'resumed', 'mac': other.upper()},
                 {'t': 3, 'event': 'radio-reopened'}]
        lines += [{'t': 10 + n, 'event': 'associated', 'mac': CR} for n in range(15)]
        lines.append({'t': 100, 'event': 'stats', 'wall': wall, 'stations': [
            {'mac': CR, 'associated': True, 'searching': False, 'dozing': False, 'last_heard_s': 1},
            {'mac': other, 'associated': False, 'searching': False, 'dozing': False, 'last_heard_s': 300}]})
        write(self.run_dir / 'link.jsonl', '\n'.join(json.dumps(line) for line in lines) + '\n')
        write(self.run_dir / 'leases', '0 %s 192.168.69.50 * *\n0 %s 192.168.69.51 * *\n' % (CR, other))
        result = self.bridge.controllers()
        first, second = result['controllers']
        self.assertTrue(result['multi_controller'])
        self.assertEqual((first['status'], first['ip']), ('connected', '192.168.69.50'))
        self.assertEqual((second['status'], second['ip']), ('away', '192.168.69.51'))
        self.assertEqual(len(first['events']), 12)
        self.assertTrue(all(event.get('mac') == CR for event in first['events']))
        self.assertEqual([event['event'] for event in second['events']], ['resumed', 'radio-reopened'])
        self.assertEqual(second['events'][0]['mac'], other)

    def test_single_controller_list_keeps_legacy_unaddressed_events(self):
        write(self.dir / 'state.json', {'household': HOUSEHOLD, 'freq': 2437,
                                        'controllers': [{'mac': CR}]})
        self.link_log(time.time(), last_heard_s=1)
        [controller] = self.bridge.controllers()['controllers']
        self.assertEqual([event['event'] for event in controller['events']],
                         ['associated', 'cr100-searching', 'link-heard'])

    def test_cr100_status_parsing(self):
        self.assertEqual(webui.parse_uptime('  3:44pm  up  2:12, load average: 0.07'), 2 * 3600 + 12 * 60)
        self.assertEqual(webui.parse_uptime(' 4:40pm  up 32 min, load average'), 32 * 60)
        self.assertEqual(webui.parse_uptime(' up 3 days, 1:05, load'), 3 * 86400 + 3900)
        self.assertEqual([webui.signal_bars(r) for r in (5, 15, 63)], [0, 1, 4])


class Spotify(Fixture):
    def test_sign_in_round_trip(self):
        self.assertFalse(self.bridge.spotify()['authorized'])
        url = self.bridge.spotify_begin()['url']
        self.assertIn('code_challenge_method=S256', url)
        state = self.bridge.spotify_pending['state']
        with self.assertRaises(webui.ApiError) as caught:
            self.bridge.spotify_finish('http://127.0.0.1:8888/callback?code=abc&state=wrong')
        self.assertIn('different sign-in', caught.exception.message)
        self.bridge.spotify_finish('http://127.0.0.1:8888/callback?code=abc&state=' + state)
        self.assertEqual(self.exchanges, ['abc'])
        status = self.bridge.spotify()
        self.assertTrue(status['authorized'])
        self.assertEqual(status['account'], {'name': 'Dee', 'plan': 'premium'})
        self.bridge.spotify_sign_out()
        self.assertFalse(self.bridge.spotify()['authorized'])

    def test_declined_and_settings(self):
        self.bridge.spotify_begin()
        with self.assertRaises(webui.ApiError) as caught:
            self.bridge.spotify_finish('http://127.0.0.1:8888/callback?error=access_denied&state=x')
        self.assertIn('declined', caught.exception.message)
        with self.assertRaises(webui.ApiError):
            self.bridge.save_spotify({'client_id': 'not-an-id', 'name': 'Spotify', 'enabled': True})
        self.bridge.save_spotify({'client_id': 'b' * 32, 'name': 'Tunes', 'enabled': False})
        settings = self.saved()['spotify_music_service']
        self.assertEqual((settings['client_id'], settings['name'], settings['enabled']), ('b' * 32, 'Tunes', False))
        self.assertEqual(settings['token_file'], 'spotify-token.json')


class HomeAssistantSettings(Fixture):
    def test_status_test_and_save(self):
        status = self.bridge.home_assistant()
        self.assertTrue(status['connected'])
        self.assertEqual(status['media_players'], 4)
        self.assertTrue(self.bridge.test_home_assistant(self.fake.url, fake_ha.TOKEN)['ok'])
        bad = self.bridge.test_home_assistant(self.fake.url, 'wrong-token')
        self.assertFalse(bad['ok'])
        self.assertIn('rejected', bad['message'])
        with self.assertRaises(webui.ApiError):
            self.bridge.save_home_assistant('not a url', '')
        self.bridge.save_home_assistant(self.fake.url + '/', 'new-token')
        self.assertEqual((self.dir / 'ha-token').read_text().strip(), 'new-token')
        self.assertEqual(self.saved()['home_assistant']['url'], self.fake.url)
        self.assertEqual(oct((self.dir / 'ha-token').stat().st_mode & 0o777), '0o600')


class SpeakerSource(Fixture):
    """Switching the controller's rooms between Home Assistant and Sonos (fake_sonos.py)."""
    KITCHEN, BEDROOM = 'RINCON_000E58AAAAAA01400', 'RINCON_000E58CCCCCC01400'

    def setUp(self):
        super().setUp()
        import fake_sonos
        self.sonos = fake_sonos.FakeHousehold()
        self.sonos.add(self.KITCHEN, 'Kitchen')
        self.sonos.add(self.BEDROOM, 'Bedroom').transport = 'PLAYING'
        self.host = self.sonos.players[self.KITCHEN].base[len('http://'):]
        self.addCleanup(self.sonos.close)

    def test_sonos_settings_are_checked_tested_and_saved(self):
        for bad in ('http://x', 'a b; rm', ['ok'] * 17):
            with self.assertRaises(webui.ApiError):
                self.bridge.save_sonos(bad)
        self.assertIn('Found 2 Sonos speakers: Bedroom, Kitchen.', self.bridge.test_sonos(self.host)['message'])
        self.assertFalse(self.bridge.test_sonos('127.0.0.1:9')['ok'])
        result = self.bridge.save_sonos(self.host + ', ')
        self.assertFalse(result['applied'])   # Home Assistant is still the source
        self.assertEqual(self.saved()['sonos'], {'hosts': [self.host]})
        self.assertEqual(self.bridge.sonos()['speakers'], ['Bedroom', 'Kitchen'])

    def test_switching_sets_rooms_aside_and_brings_them_back(self):
        ha_rooms = self.saved()['rooms']
        self.bridge.save_sonos([self.host])
        with self.assertRaises(webui.ApiError):
            self.bridge.switch_source('spotify')
        with patch('webui.os.kill'):
            self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen'}, {'key': 'media_player.patio'}],
                                       'library_entity': 'media_player.patio'})
            ha_rooms = self.saved()['rooms']
            self.bridge.switch_source('sonos')
        config = self.saved()
        self.assertEqual(config['backend'], 'sonos')
        self.assertEqual([(r['backend'], r['id']) for r in config['rooms']],
                         [('sonos', self.KITCHEN), ('sonos', self.BEDROOM)])   # every Sonos speaker, first time
        self.assertEqual(config['stashed_sources']['ha'], {'rooms': ha_rooms,
                                                           'music_menu': {'library_entity': 'media_player.patio'}})
        self.assertNotIn('music_menu', config)
        ha_slots = {r['entity']: r['slot'] for r in ha_rooms}
        self.assertEqual({k: v for k, v in config['player_slots'].items() if k in ha_slots}, ha_slots)
        self.assertFalse({r['slot'] for r in config['rooms']} & set(ha_slots.values()))   # Sonos takes unused slots
        listed = self.bridge.speakers()
        rows = {r['key']: r for r in listed['speakers']}
        self.assertEqual((listed['source'], listed['source_connected']), ('sonos', True))
        bedroom = rows['sonos:' + self.BEDROOM]
        self.assertEqual((bedroom['name'], bedroom['shown'], bedroom['state'], bedroom['sub']),
                         ('Bedroom', True, 'playing', '127.0.0.1'))
        # Rename one, hide the other, read the Music menu from it.
        with patch('webui.os.kill'):
            self.bridge.save_speakers({'rooms': [{'key': 'sonos:' + self.BEDROOM, 'name': 'Den'}],
                                       'library': 'sonos:' + self.BEDROOM})
        config = self.saved()
        self.assertEqual(config['rooms'], [{'backend': 'sonos', 'id': self.BEDROOM, 'name': 'Den',
                                            'last_known_name': 'Bedroom',
                                            'slot': config['player_slots']['sonos:' + self.BEDROOM]}])
        self.assertEqual(config['music_menu'], {'library': {'backend': 'sonos', 'id': self.BEDROOM}})
        with self.assertRaises(webui.ApiError):   # a Home Assistant speaker is not on offer now
            self.bridge.save_speakers({'rooms': [{'key': 'media_player.kitchen'}]})
        self.assertEqual(self.bridge.overview()['source'], 'Sonos')
        # Back to Home Assistant: its rooms and library return; Sonos's are kept aside.
        with patch('webui.os.kill'):
            self.bridge.switch_source('ha')
        config = self.saved()
        self.assertEqual((config['backend'], config['rooms']), ('ha', ha_rooms))
        self.assertEqual(config['music_menu'], {'library_entity': 'media_player.patio'})
        self.assertEqual(config['stashed_sources']['sonos']['music_menu'],
                         {'library': {'backend': 'sonos', 'id': self.BEDROOM}})
        # The players read the same config.
        names, refs, _, connected = zoneplayers.load_rooms(self.config, lambda record: None)
        try:
            self.assertEqual(refs, [('ha', 'media_player.kitchen'), ('ha', 'media_player.patio')])
        finally:
            for backend in connected.values(): backend.close()


class Http(Fixture):
    def setUp(self):
        super().setUp()
        self.auth = webui.Auth(self.data)
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, self.logs.append))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, header=True):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        headers = {'Content-Type': 'application/json'}
        if header: headers['X-Bridge'] = '1'
        if self.cookie: headers['Cookie'] = self.cookie
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        cookie = r.getheader('Set-Cookie')
        if cookie: self.cookie = cookie.split(';')[0]
        c.close()
        return r.status, (json.loads(data) if r.getheader('Content-Type') == 'application/json' else data)

    def test_setup_login_and_guards(self):
        self.assertEqual(self.call('GET', '/api/session')[1], {'authenticated': False, 'password_set': False})
        self.assertEqual(self.call('GET', '/api/speakers')[0], 401)
        self.assertEqual(self.call('POST', '/api/setup', {'password': 'short'})[0], 400)
        self.assertEqual(self.call('POST', '/api/setup', {'password': 'long enough'}, header=False)[0], 403)
        self.assertEqual(self.call('POST', '/api/setup', {'password': 'long enough'})[0], 200)
        self.assertEqual(self.call('GET', '/api/speakers')[0], 200)
        self.assertEqual(self.call('POST', '/api/setup', {'password': 'another one'})[0], 409)
        self.call('POST', '/api/logout')
        self.assertEqual(self.call('GET', '/api/speakers')[0], 401)
        self.assertEqual(self.call('POST', '/api/login', {'password': 'wrong one'})[0], 401)
        self.assertEqual(self.call('POST', '/api/login', {'password': 'long enough'})[0], 200)
        self.assertEqual(self.call('GET', '/api/overview')[1]['home_assistant'], True)
        self.assertNotIn('long enough', (self.dir / 'webui.json').read_text())
        self.assertEqual(oct((self.dir / 'webui.json').stat().st_mode & 0o777), '0o600')

    def test_static_files_and_traversal(self):
        status, body = self.call('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'soft', body)   # the soft-key bar
        self.assertEqual(self.call('GET', '/static/app.js')[0], 200)
        self.assertEqual(self.call('GET', '/static/../webui.py')[0], 404)
        self.assertEqual(self.call('GET', '/static/%2e%2e/webui.py')[0], 404)

    def test_controller_network_is_refused(self):
        handler = webui.make_handler(self.bridge, self.auth, self.logs.append)
        probe = handler.__new__(handler)
        for address, allowed in [('192.168.69.52', False), ('192.168.1.20', True), ('::ffff:192.168.69.60', False)]:
            probe.client_address = (address, 1234)
            self.assertEqual(probe.allowed_client(), allowed, address)


class Listening(unittest.TestCase):
    def test_every_ipv4_interface_only(self):
        server = webui.listen(0, webui.make_handler(None, webui.Auth(webui.DataFile(Path(tempfile.mkdtemp()) / 'd.json')),
                                                    lambda r: None))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual(server.server_address[0], '0.0.0.0')
            c = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            c.request('GET', '/api/session')
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            server.shutdown(); server.server_close()


class PlayerSide(unittest.TestCase):
    """zoneplayers: slots fix each room's player identity; the library speaker browses."""
    def test_slots_fix_identity_and_library(self):
        directory = Path(tempfile.mkdtemp())
        config = directory / 'config.json'
        write(config, {'rooms': [{'name': 'Den', 'slot': 4}, {'name': 'Spare'}],
                       'music_menu': {'library_entity': 'media_player.kitchen'}})
        names, refs, slots, connected = zoneplayers.load_rooms(config, lambda r: None)
        self.assertEqual((names, refs, slots, connected), (['Den', 'Spare'], [None, None], [4, 1], {}))
        self.assertEqual(zoneplayers.library_ref(config), ('ha', 'media_player.kitchen'))
        house = zoneplayers.Household(zp, HOUSEHOLD, names, ['127.0.0.1'] * 2, lambda r: None, port=0, slots=slots)
        self.assertEqual([p.config.udn for p in house.players],
                         ['RINCON_02690000000501400', 'RINCON_02690000000201400'])
        topology = house.topology()
        self.assertIn('ZoneName="Den" Icon="x-rincon-roomicon:tvroom"', topology)       # den draws the TV room picture
        self.assertIn('ZoneName="Spare" Icon="x-rincon-roomicon:living"', topology)     # no match: as before
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], lambda r: None, port=0,
                                      icons=['pool'])
        self.assertIn('Icon="x-rincon-roomicon:pool"', house.topology())
        write(config, {'rooms': [{'name': 'Den', 'icon': 'office'}, {'name': 'Spare'}]})
        self.assertEqual(zoneplayers.configured_icons(config), ['office', None])
        write(config, {'rooms': [{'name': 'A', 'slot': 1}, {'name': 'B', 'slot': 1}]})
        with self.assertRaises(ValueError):
            zoneplayers.load_rooms(config, lambda r: None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
