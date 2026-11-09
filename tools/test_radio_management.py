"""Authenticated Radio HTTP management, with public-network and renderer I/O faked."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import webui
from radio_catalog import RadioCatalog
from radio_management import RadioManagement, state_paths
from radio_store import RadioStore
from radiotime import RadioTimeClient


class FakeAlarms:
    def __init__(self):
        self.rows = []
    def list(self):
        return copy.deepcopy(self.rows)
    def save(self, alarm):
        self.rows = [alarm if row['id'] == alarm['id'] else row for row in self.rows]
        return alarm


class RadioHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'rooms': [], 'radio': {'state_file': 'persistent/radio.json'}}))
        self.state = self.root / 'state.json'
        self.state.write_text('{}')
        self.run = self.root / 'run'
        self.run.mkdir()
        self.logs, self.requests = [], []
        self.data = webui.DataFile(self.root / 'web.json')
        self.bridge = webui.Bridge(self.config, self.state, self.run, self.root / 'players.pid',
                                   self.data, self.logs.append, managed=False)
        self.client = RadioTimeClient(self.transport, min_request_interval=0)
        self.store = RadioStore(state_paths(self.config)[0])
        self.catalog = RadioCatalog(self.client, self.store)
        self.alarms = FakeAlarms()
        self.management = RadioManagement(self.config, store=self.store, client=self.client,
                                          catalog=self.catalog, alarms=self.alarms)
        self.bridge._radio_management = self.management
        self.auth = webui.Auth(self.data)
        self.auth.set_password('a sufficiently long password')
        self.cookie = '%s=%s' % (webui.SESSION_COOKIE, self.auth.new_session())
        self.server = webui.Server(('127.0.0.1', 0), webui.make_handler(self.bridge, self.auth, self.logs.append))
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.bridge.close()

    def transport(self, url, **kwargs):
        self.requests.append((url, kwargs))
        parsed = urlsplit(url)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        if parsed.hostname == 'api.zippopotam.us':
            return {'places': [{'place name': 'Boston', 'state abbreviation': 'MA',
                                'latitude': '42.3576', 'longitude': '-71.0514'}]}
        if parsed.hostname == 'bad.test':
            return 404, {'Content-Type': 'text/html'}, b''
        if parsed.hostname == 'html.test':
            return 200, {'Content-Type': 'text/html'}, b''
        if parsed.hostname == 'radio.test':
            return 200, {'Content-Type': 'audio/mpeg'}, b''
        if query.get('c') == 'lang':
            return {'head': {'status': '200'}, 'body': [
                dict(type='link', text='English', guide_id='l203', URL='http://opml.radiotime.com/Browse.ashx?c=lang&filter=l203'),
                dict(type='link', text='French', guide_id='l210', URL='http://opml.radiotime.com/Browse.ashx?c=lang&filter=l210')]}
        if query.get('id') in ('r0', 'r1'):
            target, label = ('r1', 'North America') if query['id'] == 'r0' else ('r2', 'Boston')
            return {'head': {'status': '200', 'title': label}, 'body': [
                dict(type='link', text=label, URL='http://opml.radiotime.com/Browse.ashx?id=' + target)]}
        if query.get('id') == 's404':
            return {'head': {'status': '404', 'fault': 'Station missing', 'fault_code': 'id'}, 'body': []}
        if parsed.path.endswith('Describe.ashx'):
            return {'head': {'status': '200'}, 'body': [dict(element='station', guide_id=query['id'],
                    name='Public Station', logo='https://images.test/logo.jpg', is_available=True)]}
        row = dict(type='audio', text='Public Station', guide_id='s1', item='station',
                   URL='http://opml.radiotime.com/Tune.ashx?id=s1')
        return {'head': {'status': '200'}, 'body': [row]}

    def call(self, method, path='/api/radio', body=None, authenticated=True, bridge_header=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Cookie'] = self.cookie
        if bridge_header:
            headers['X-Bridge'] = '1'
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        result = (response.status, json.loads(response.read()))
        connection.close()
        return result

    def post(self, command, body=None):
        return self.call('POST', '/api/radio/' + command, body or {})

    def test_auth_and_deliberate_write_guards(self):
        self.assertEqual(self.call('GET', authenticated=False)[0], 401)
        self.assertEqual(self.call('PUT', body={'quality': 'low'}, bridge_header=False)[0], 403)
        status, state = self.call('GET')
        self.assertEqual(status, 200)
        self.assertTrue(state['artwork_enabled'])
        self.assertEqual(state['settings']['quality'], 'auto')
        self.assertEqual(self.requests, [])

    def test_settings_and_location_persist_without_touching_bridge_config(self):
        config_before = self.config.read_bytes()
        status, result = self.call('PUT', body={'settings': {'quality': 'low', 'show_podcasts': True}})
        self.assertEqual(status, 200)
        self.assertEqual(result['settings']['quality'], 'low')
        self.assertEqual(RadioStore(state_paths(self.config)[0]).settings()['quality'], 'low')
        status, result = self.post('location', {'zip': '02110'})
        self.assertEqual(status, 200)
        self.assertEqual(result['settings']['location']['label'], 'Boston, MA')
        self.assertEqual(result['radio_location_update_id'], 1)
        status, _ = self.post('location', {'location': {'label': 'Only a label'}})
        self.assertEqual(status, 400)
        before = self.store.snapshot()
        self.assertEqual(self.call('PUT', body={'unknown': True})[0], 400)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.config.read_bytes(), config_before)

    def test_custom_crud_url_probe_and_shared_favorites(self):
        status, data = self.post('custom', {'name': 'Local custom', 'url': 'https://radio.test/live.mp3',
                                           'logo': 'https://images.test/custom.jpg'})
        self.assertEqual(status, 200)
        identity = data['custom_stations'][0]['id']
        self.assertEqual(data['favorites'][0]['id'], identity)
        status, result = self.post('test-url', {'url': 'https://radio.test/live.mp3'})
        self.assertEqual(status, 200)
        self.assertFalse(result['playback_started'])
        self.assertTrue(result['streams'][0]['metadata']['verified'])
        self.assertEqual(self.requests[-1][1]['method'], 'HEAD')
        self.assertEqual(self.post('custom', {'operation': 'edit', 'id': identity, 'name': 'Renamed',
                                             'url': 'https://radio.test/new.mp3'})[0], 200)
        other = RadioManagement(self.config)
        self.assertEqual(other.snapshot()['custom_stations'][0]['title'], 'Renamed')
        self.assertEqual(self.post('custom', {'operation': 'delete', 'id': identity})[0], 200)
        self.assertEqual(self.call('GET')[1]['favorites'], [])

    def test_favorites_history_reset_and_refresh_do_not_remove_saved_sources(self):
        self.assertEqual(self.post('favorite-add', {'id': 'station:s1'})[0], 200)
        self.assertEqual(self.post('favorite-add', {'id': 'station:s1'})[0], 200)
        self.assertEqual(len(self.store.favorites()), 1)
        status, result = self.post('favorite-edit', {'id': 'station:s1', 'title': 'My station'})
        self.assertEqual(status, 200)
        self.assertEqual(result['favorites'][0]['title'], 'My station')
        RadioStore(state_paths(self.config)[0]).record_history(self.store.favorites()[0])
        self.store.add_search('radio query')
        self.assertEqual(len(self.call('GET')[1]['history']), 1)
        before = self.store.snapshot()
        self.assertEqual(self.post('refresh')[0], 200)
        after = self.store.snapshot()
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertEqual(after['radio_directory_update_id'], before['radio_directory_update_id'] + 1)
        self.assertEqual(after['favorites'], before['favorites'])
        self.assertEqual(self.post('history-clear')[0], 200)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.searches(), [])
        self.assertEqual(self.post('reset')[0], 200)
        self.assertEqual(len(self.store.favorites()), 1)
        self.assertEqual(self.post('favorite-remove', {'id': 'station:s1'})[0], 200)
        self.assertEqual(self.store.favorites(), [])

    def test_export_import_validation_and_alarm_reference_guard(self):
        item = self.store.add_custom('Alarm station', 'https://radio.test/live.mp3')
        self.alarms.rows = [dict(id=1, enabled=True, media_ref={'provider': 'radio', 'content_id': item['id']})]
        status, exported = self.post('export')
        self.assertEqual(status, 200)
        self.assertEqual(exported['version'], 1)
        self.assertEqual(self.post('custom', {'operation': 'delete', 'id': item['id']})[0], 400)
        replacement = RadioStore().export_data()
        self.assertEqual(self.post('import', {'data': replacement})[0], 400)
        self.assertIsNotNone(self.store.custom(item['id']))
        invalid = copy.deepcopy(replacement)
        invalid['settings']['quality'] = 'invalid'
        self.assertEqual(self.post('import', {'data': invalid, 'disable_alarms': True})[0], 400)
        self.assertTrue(self.alarms.rows[0]['enabled'])
        self.assertEqual(self.post('import', {'data': replacement, 'disable_alarms': True})[0], 200)
        self.assertFalse(self.alarms.rows[0]['enabled'])
        self.assertEqual(self.store.custom_stations(), [])
        self.assertEqual(self.post('import', {'data': exported})[0], 200)
        self.assertIsNotNone(self.store.custom(item['id']))

    def test_browse_search_and_typed_upstream_failures(self):
        status, page = self.post('browse', {'id': '0'})
        self.assertEqual(status, 200)
        self.assertIn('Podcasts', [row['title'] for row in page['items']])
        self.assertEqual(self.post('browse', {'start': -1})[0], 400)
        self.assertEqual(self.post('search', {'query': 'station', 'kind': 'stations'})[0], 200)
        self.assertEqual(self.post('test-url', {'url': 'https://bad.test/live.mp3'})[0], 404)
        self.assertEqual(self.post('test-url', {'url': 'https://html.test/live.mp3'})[0], 404)
        self.assertEqual(self.post('favorite-add', {'id': 'station:s404'})[0], 404)

    def test_language_picker_uses_readable_choices_and_validates_filter_identity(self):
        status, data = self.post('language-options')
        self.assertEqual(status, 200)
        self.assertIn({'id': 'l203', 'label': 'English'}, data['languages'])
        before = self.store.snapshot()
        for settings in ({'preferred_language': 'English', 'language_filter': True},
                         {'preferred_language': 'l99999', 'language_filter': True},
                         {'preferred_language': '', 'language_filter': True}):
            self.assertEqual(self.call('PUT', body={'settings': settings})[0], 400)
        self.assertEqual(self.store.snapshot(), before)
        status, data = self.call('PUT', body={'settings': {'preferred_language': 'l203', 'language_filter': True}})
        self.assertEqual(status, 200)
        self.assertEqual(data['settings']['preferred_language'], 'l203')
        self.assertTrue(data['settings']['language_filter'])

    def test_city_picker_browsing_is_read_only_and_apply_saves_a_real_leaf(self):
        before = self.store.snapshot()
        status, world = self.post('location-browse')
        self.assertEqual((status, world['locations'][0]['label']), (200, 'North America'))
        status, places = self.post('location-browse', {'id': 'r1', 'ancestors': ['r0']})
        self.assertEqual(places['locations'], [{'id': 'r2', 'label': 'Boston'}])
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.post('location-select', {'id': 'r0', 'label': 'World'})[0], 400)
        self.assertEqual(self.post('location-browse', {'id': 'r2', 'ancestors': [{}]})[0], 400)
        status, result = self.post('location-select', {'id': 'r2', 'label': 'Boston', 'ancestors': ['r0', 'r1']})
        self.assertEqual(status, 200)
        self.assertEqual(result['settings']['location'], {'region_id': 'r2', 'label': 'Boston'})
        self.assertEqual(result['radio_location_update_id'], 1)


if __name__ == '__main__':
    unittest.main()
