"""Actual HA WebSockets and player HTTP exercise smart device guards and events."""
import asyncio
import copy
import http.client
import http.server
import json
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import fake_ha
import ha_backend
from ha_client import HomeAssistant
from ha_hub import HAHub
import smart_device_service
import zoneplayers
from test_zoneplayers import HOUSEHOLD, zp
from ui_events import decode_params, encode_request

SP = '/SystemProperties/Control'
CD = '/MediaServer/ContentDirectory/Control'
DC = '{http://purl.org/dc/elements/1.1/}'
UPNP = '{urn:schemas-upnp-org:metadata-1-0/upnp/}'


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.01)
    return False


def decoded(wire):
    version, request, status, code, message, data = map(unquote, wire.split('|'))
    return {'version': version, 'request': request, 'status': status, 'code': code,
            'message': message, 'data': decode_params(data)}


class Notifications:
    def __init__(self, address):
        self.items = queue.Queue()
        items = self.items
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                items.put(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200); self.end_headers()
            def log_message(self, *_): pass
        self.server = http.server.ThreadingHTTPServer((address, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self): return 'http://%s:%s/' % self.server.server_address

    def statuses(self):
        statuses = []
        while True:
            try: body = self.items.get_nowait()
            except queue.Empty: return statuses
            root = ET.fromstring(body)
            for element in root.iter():
                if element.tag.endswith('LastChange') and element.text:
                    for field in ET.fromstring(element.text).iter():
                        if field.tag.endswith('TransportStatus'): statuses.append(field.get('val'))

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)


class SmartDevicesHTTP(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture())
        self.addCleanup(self.fake.close)
        self.hub = HAHub(HomeAssistant(self.fake.url, fake_ha.TOKEN, self.logs.append), self.logs.append)
        self.addCleanup(self.hub.close)
        self.hub.start()
        self.assertTrue(self.hub.ready.wait(5))
        self.backend = ha_backend.HABackend(self.hub.client, self.logs.append)
        self.addCleanup(self.backend.close)
        self.config = {'rooms': [{'entity': 'media_player.lantern'}, {'entity': 'media_player.workshop'}]}
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Lantern', 'Workshop'], ['127.0.0.1'] * 2,
            self.logs.append, port=0, backends=[self.backend],
            refs=[('ha', 'media_player.lantern'), ('ha', 'media_player.workshop')],
            devices_hub=self.hub, devices_config=self.config)
        self.addCleanup(self.house.close)
        self.house.start('127.0.0.1', ssdp=False)
        self.player, self.other = self.house.players
        self.service = self.house.smart_devices
        self.assertTrue(wait_for(lambda: self.service.revision >= 1))
        self.sequence = 0

    def soap(self, path, service, action, args, player=None, address='127.0.0.1'):
        player = player or self.player
        fields = ''.join('<%s>%s</%s>' % (key, escape(str(value)), key) for key, value in args.items())
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                '<s:Body><u:%s xmlns:u="urn:schemas-upnp-org:service:%s:1">%s</u:%s>'
                '</s:Body></s:Envelope>' % (action, service, fields, action))
        connection = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=15,
                                                source_address=(address, 0))
        try:
            connection.request('POST', path, body, {'SOAPAction': '"urn:schemas-upnp-org:service:%s:1#%s"' % (service, action)})
            reply = connection.getresponse()
            return reply.status, reply.read()
        finally: connection.close()

    def prop(self, name, player=None, address='127.0.0.1'):
        if address != '127.0.0.1':
            # macOS exposes only 127.0.0.1 without a privileged interface alias.
            # Feed other source identities through the same gateway entry point.
            return self.house.ui_events.native_action(player or self.player, 'SystemProperties',
                'GetString', {'VariableName': name}, address)['StringValue']
        status, body = self.soap(SP, 'SystemProperties', 'GetString', {'VariableName': name}, player, address)
        self.assertEqual(status, 200, body)
        return ET.fromstring(body).find('.//StringValue').text or ''

    def browse(self, container='SD:', player=None, start=0, count=500):
        status, body = self.soap(CD, 'ContentDirectory', 'Browse', {'ObjectID': container,
            'BrowseFlag': 'BrowseDirectChildren', 'Filter': '*', 'StartingIndex': start,
            'RequestedCount': count, 'SortCriteria': ''}, player)
        self.assertEqual(status, 200, body)
        envelope = ET.fromstring(body)
        rows = list(ET.fromstring(envelope.find('.//Result').text))
        return {row.get('id'): {'title': row.find(DC + 'title').text,
                'class': row.find(UPNP + 'class').text,
                'view': row.find(UPNP + 'class').text.rsplit('.', 1)[-1]} for row in rows}, int(envelope.find('.//TotalMatches').text)

    def send(self, action, params, player=None, address='127.0.0.1'):
        self.sequence += 1
        request = 'devices-test-%d' % self.sequence
        player = player or self.player
        if address != '127.0.0.1':
            self.house.ui_events.dispatch(encode_request(request, action, player.config.udn, params), player, address)
            return request
        status, body = self.soap(SP, 'SystemProperties', 'SetString', {'VariableName': 'CR100_UIEvent',
            'StringValue': encode_request(request, action, player.config.udn, params)}, player, address)
        self.assertEqual(status, 200, body)
        return request

    def result(self, request, player=None, address='127.0.0.1'):
        return decoded(self.prop('CR100_UIResult:' + request, player, address))

    def finished(self, request):
        return wait_for(lambda: self.result(request)['data'].get('pending') != 'true')

    def token(self, rows, row): return {'view': rows[row]['view'], 'id': row}

    def test_browse_paging_metadata_and_capability_reads_never_call_services(self):
        rows, total = self.browse()
        self.assertIn('SD:all:lantern', rows)
        self.assertIn('light.lantern_counter', rows)
        self.assertNotIn('light.lantern_all', rows)
        page, page_total = self.browse(start=1, count=2)
        self.assertEqual((len(page), page_total), (2, total))
        for container in ('SD:other', 'SD:floor:upper', 'SD:a:landing', 'SD:a:lantern:lights', 'SD:a:lantern:scenes'):
            self.browse(container)
        status, _ = self.soap(CD, 'ContentDirectory', 'Browse', {'ObjectID': 'light.lantern_counter',
            'BrowseFlag': 'BrowseMetadata', 'Filter': '*', 'StartingIndex': 0, 'RequestedCount': 0, 'SortCriteria': ''})
        self.assertEqual(status, 200)
        self.assertEqual(self.prop('CR100_DevicesAvailable'), '1')
        self.assertIn('devices.v1', decoded(self.prop('CR100_UICapabilities'))['data']['features'])
        self.assertEqual(self.fake.service_requests, [])

    def test_informational_component_does_not_advertise_devices(self):
        informational = next(player for player in self.house.devices() if player.state.informational)
        self.assertEqual(self.prop('CR100_DevicesAvailable'), '1')
        result = self.house.ui_events.native_action(informational, 'SystemProperties', 'GetString',
            {'VariableName': 'CR100_DevicesAvailable'}, '127.0.0.1')
        self.assertEqual(result, {'StringValue': '0'})
        self.assertEqual(self.fake.service_requests, [])

    def test_other_rooms_over_one_hundred_areas_pages_without_duplicate_folders_or_writes(self):
        for area in self.fake.areas: area['floor_id'] = None
        for number in range(105):
            area, entity = 'paging_%03d' % number, 'switch.paging_%03d' % number
            self.fake.areas.append({'area_id': area, 'name': 'Paging %03d' % number, 'floor_id': None})
            self.fake.entities.append({'entity_id': entity, 'platform': 'test', 'area_id': area,
                'device_id': None, 'disabled_by': None, 'hidden_by': None, 'entity_category': None})
            self.fake.states[entity] = {'entity_id': entity, 'state': 'off',
                                       'attributes': {'friendly_name': 'Paging Switch %03d' % number}}
            self.fake.set(entity, 'off')
        self.assertTrue(self.hub.refresh())
        first, total = self.browse('SD:other', count=500)
        second, second_total = self.browse('SD:other', start=len(first), count=500)
        expected = {'SD:a:' + area['area_id'] for area in self.fake.areas if area['area_id'] != 'lantern'}
        self.assertEqual((len(first), len(second), total, second_total), (100, 7, 107, 107))
        self.assertFalse(set(first) & set(second))
        self.assertEqual(set(first) | set(second), expected)
        self.assertEqual(len({row['view'] for row in (*first.values(), *second.values())}), 1)
        self.assertEqual(self.fake.service_requests, [])

    def test_absolute_all_lights_and_level_fan_and_activation(self):
        root, _ = self.browse()
        members = {'light.lantern_ceiling', 'light.lantern_counter_left', 'light.lantern_counter_right'}
        request = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.result(request)['status'], 'ok')
        domain, action, targets, _ = self.fake.service_requests[-1]
        self.assertEqual((domain, action, set(targets)), ('light', 'turn_off', members))
        request = self.send('devices.level', {**self.token(root, 'SD:all:lantern'), 'value': '60'})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.fake.service_requests[-1][3], {'brightness_pct': 60})
        request = self.send('devices.level', {**self.token(root, 'fan.lantern_extractor'), 'value': '70'})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.fake.service_requests[-1][:2], ('fan', 'set_percentage'))
        scenes, _ = self.browse('SD:a:lantern:scenes')
        request = self.send('devices.activate', self.token(scenes, 'scene.lantern_evening'))
        self.assertTrue(self.finished(request))
        self.assertEqual(self.result(request)['message'], 'Started')
        self.assertTrue(all(service != 'toggle' for _, service, _, _ in self.fake.service_requests))

    def test_level_gesture_holds_list_updates_and_publishes_once_when_the_light_is_quiet(self):
        root, _ = self.browse()
        key = self.player.config.udn, 'SD:'
        with patch.object(smart_device_service, 'LEVEL_HOLD', .7), \
                patch.object(smart_device_service, 'LEVEL_CHANGE_QUIET', .5):
            self.fake.settle = {'steps': 5, 'span': .5}
            before = self.service.updates.get(key, 1)
            tokens = {view.udn: view.token for view in self.service.views.values() if view.container == 'SD:'}
            for value in ('50', '40', '30'):
                request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': value})
                self.assertTrue(self.finished(request))
                time.sleep(.15)
            # Several state changes have arrived, yet nothing was published mid-gesture.
            self.assertGreater(self.service.state_versions['light.lantern_ceiling'], 2)
            self.assertEqual(self.service.updates.get(key, 1), before)
            self.assertTrue(wait_for(lambda: self.service.updates.get(key, 1) > before, 6))
            published = self.service.updates[key]
            self.assertTrue(wait_for(lambda: self.fake.settling == {}, 4))
            time.sleep(1.2)
            self.assertEqual(self.service.updates[key], published)
            self.assertEqual(self.service.deferred, {})
            # The token survives: holding never replaces a view.
            self.assertTrue(all(view.token == tokens[view.udn] for view in self.service.views.values()
                                if view.container == 'SD:'))
        self.assertEqual([c[2] for c in self.fake.calls if c[0] == 'turn_on'],
                         [{'brightness_pct': 50}, {'brightness_pct': 40}, {'brightness_pct': 30}])

    def test_level_hold_starts_at_receipt_and_lasts_while_the_call_is_in_flight(self):
        root, _ = self.browse()
        observed = []
        original = self.service.client.call_service
        def slow(*args, **kwargs):
            time.sleep(.6)  # longer than the patched LEVEL_HOLD below
            with self.service.condition:
                observed.append(self.service._held('light.lantern_ceiling', self.service.clock()))
            return original(*args, **kwargs)
        with patch.object(smart_device_service, 'LEVEL_HOLD', .2), \
                patch.object(smart_device_service, 'LEVEL_CHANGE_QUIET', .1), \
                patch.object(self.service.client, 'call_service', slow):
            request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': '45'})
            self.assertIn('light.lantern_ceiling', self.service.level_holds)  # held from receipt
            self.assertTrue(self.finished(request))
            self.assertEqual(observed, [True])
            self.assertFalse(self.service.level_holds.get('light.lantern_ceiling', {}).get('busy'))
            # A failing call also releases its in-flight hold.
            self.fake.fail_services = True
            request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': '35'})
            self.assertTrue(self.finished(request))
            self.assertFalse(self.service.level_holds.get('light.lantern_ceiling', {}).get('busy'))
            self.assertTrue(wait_for(lambda: 'light.lantern_ceiling' not in self.service.level_holds, 3))

    def test_failed_level_action_is_published_and_reported_despite_a_hold(self):
        root, _ = self.browse()
        key = self.player.config.udn, 'SD:'
        with patch.object(smart_device_service, 'LEVEL_HOLD', 30), \
                patch.object(smart_device_service, 'LEVEL_HOLD_MAX', 60):
            request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': '40'})
            self.assertTrue(self.finished(request))
            self.assertIn('light.lantern_ceiling', self.service.level_holds)
            before = self.service.updates.get(key, 1)
            self.fake.fail_services = True
            request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': '30'})
            self.assertTrue(self.finished(request))
            self.assertEqual(self.result(request)['code'], 'device_failed')
            # The failure's list refresh is not held back by the gesture.
            self.assertTrue(wait_for(lambda: self.service.updates.get(key, 1) > before, 2))

    def test_one_pass_sends_one_content_directory_notify_per_room(self):
        self.browse(); self.browse('SD:other')
        calls = []
        original = self.player.publish_changed
        def counted(service):
            calls.append((service, self.service.event_ids(self.player.config.udn)))
            return original(service)
        with patch.object(self.player, 'publish_changed', counted):
            udn = self.player.config.udn
            with self.service.condition:
                before = {c: self.service.updates.get((udn, c), 1) for c in ('SD:', 'SD:other')}
                with self.service._batched():
                    self.service._publish(udn, 'SD:', bypass=True)
                    self.service._publish(udn, 'SD:other', bypass=True)
                    # Nothing leaves while the pass is still running.
                    self.assertEqual(calls, [])
            self.assertEqual(len(calls), 1)
            self.assertIn('SD:,%d' % (before['SD:'] + 1), calls[0][1])
            self.assertIn('SD:other,%d' % (before['SD:other'] + 1), calls[0][1])
            # Outside a pass, a publish still notifies at once.
            with self.service.condition: self.service._publish(udn, 'SD:', bypass=True)
            self.assertEqual(len(calls), 2)
            self.assertIsNone(self.service.notify_batch)

    def test_hold_expires_at_its_maximum_and_power_actions_are_not_held(self):
        root, _ = self.browse()
        key = self.player.config.udn, 'SD:'
        before = self.service.updates.get(key, 1)
        request = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
        self.assertTrue(self.finished(request))
        self.assertTrue(wait_for(lambda: self.service.updates.get(key, 1) > before))
        self.assertEqual(self.service.level_holds, {})
        with patch.object(smart_device_service, 'LEVEL_HOLD_MAX', .3), \
                patch.object(smart_device_service, 'LEVEL_HOLD', 30):
            self.fake.settle = {'steps': 2, 'span': .2}
            before = self.service.updates[key]
            request = self.send('devices.level', {**self.token(root, 'light.lantern_ceiling'), 'value': '10'})
            self.assertTrue(self.finished(request))
            self.assertTrue(wait_for(lambda: self.service.updates[key] > before, 3))

    def test_power_press_lists_the_requested_state_at_once_and_a_quick_second_press_reverses_it(self):
        root, _ = self.browse()
        row = 'light.lantern_ceiling'
        self.assertTrue(root[row]['class'].startswith('object.item.sdOn.'))
        release, entered = threading.Event(), threading.Event()
        original = self.service.client.call_service
        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with patch.object(self.service.client, 'call_service', slow):
            key = self.player.config.udn, 'SD:'
            before = self.service.updates.get(key, 1)
            first = self.send('devices.power', {**self.token(root, row), 'desired': 'off'})
            # Published on receipt, before HA has answered, and listed as off.
            self.assertGreater(self.service.updates.get(key, 1), before)
            rows, _ = self.browse()
            self.assertTrue(rows[row]['class'].startswith('object.item.sdOff.'), rows[row])
            self.assertEqual(rows[row]['view'], root[row]['view'])
            self.assertTrue(entered.wait(3))
            # A second press made from the refreshed row asks for on, and wins.
            second = self.send('devices.power', {**self.token(rows, row), 'desired': 'on'})
            rows, _ = self.browse()
            self.assertTrue(rows[row]['class'].startswith('object.item.sdOn.'), rows[row])
            release.set()
            self.assertTrue(self.finished(first) and self.finished(second))
        self.assertEqual([c[:2] for c in self.fake.service_requests[-2:]], [('light', 'turn_off'), ('light', 'turn_on')])
        self.assertTrue(wait_for(lambda: self.fake.states[row]['state'] == 'on'))
        self.assertTrue(wait_for(lambda: row not in self.service.power_targets, 5))
        rows, _ = self.browse()
        self.assertTrue(rows[row]['class'].startswith('object.item.sdOn.'))

    def test_a_stale_on_event_during_a_pending_off_does_not_flip_the_row_back(self):
        root, _ = self.browse()
        row = 'light.lantern_ceiling'
        seen = []
        original_call, original_replace = self.service.client.call_service, self.service._replace_view
        def late(*args, **kwargs):
            # HA reports an older "on" transition while the off is still in flight.
            self.fake.set(row, 'on', brightness=120)
            self.assertTrue(wait_for(lambda: self.service.client.states[row]['attributes'].get('brightness') == 120, 3))
            time.sleep(.2)
            return original_call(*args, **kwargs)
        def watch(view, rows, signature):
            seen.extend(r.on for r in rows if r.row_id == row)
            return original_replace(view, rows, signature)
        with patch.object(self.service.client, 'call_service', late), \
                patch.object(self.service, '_replace_view', watch):
            request = self.send('devices.power', {**self.token(root, row), 'desired': 'off'})
            self.assertTrue(self.finished(request))
            self.assertTrue(wait_for(lambda: row not in self.service.power_targets, 5))
            self.browse()
        # The validation refresh before the press is registered still lists it on;
        # from the first "off" on, it is never listed as on again.
        self.assertIn(False, seen)
        self.assertNotIn(True, seen[seen.index(False):])
        self.assertGreater(len(seen) - seen.index(False), 2)

    def test_failed_power_press_lists_the_actual_state_again(self):
        root, _ = self.browse()
        row = 'switch.lantern_plug'
        state = root[row]['class'].split('.')[2]
        desired = 'off' if state == 'sdOn' else 'on'
        self.fake.fail_services = True
        request = self.send('devices.power', {**self.token(root, row), 'desired': desired})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.result(request)['code'], 'device_failed')
        self.assertNotIn(row, self.service.power_targets)
        rows, _ = self.browse()
        self.assertEqual(rows[row]['class'].split('.')[2], state)

    def test_rejected_power_press_never_lists_a_requested_state(self):
        root, _ = self.browse()
        row = 'switch.lantern_plug'
        self.send('devices.power', {'view': 'stale-token', 'id': row, 'desired': 'off'})
        self.assertEqual(self.service.power_targets, {})

    def test_pending_power_display_expires_if_ha_never_reports_the_state(self):
        root, _ = self.browse()
        row = 'switch.lantern_plug'
        state = root[row]['class'].split('.')[2]
        desired = 'off' if state == 'sdOn' else 'on'
        original = self.service.client.call_service
        def ignored(*args, **kwargs): return None  # HA accepts but the state never changes
        with patch.object(smart_device_service, 'CONFIRM_TIMEOUT', .3), \
                patch.object(self.service.client, 'call_service', ignored):
            request = self.send('devices.power', {**self.token(root, row), 'desired': desired})
            self.assertTrue(self.finished(request))
            self.assertTrue(wait_for(lambda: row not in self.service.power_targets, 3))
        rows, _ = self.browse()
        self.assertEqual(rows[row]['class'].split('.')[2], state)

    def test_state_refresh_retains_token_but_rename_foreign_row_and_foreign_room_reject(self):
        root, _ = self.browse()
        before = root['SD:all:lantern']['view']
        self.fake.set('light.lantern_counter_left', 'off')
        self.assertTrue(wait_for(lambda: self.hub.client.state('light.lantern_counter_left')['state'] == 'off'))
        refreshed, _ = self.browse()
        self.assertEqual(refreshed['SD:all:lantern']['view'], before)
        foreign, _ = self.browse(player=self.other)
        for params in ({'view': before, 'id': 'light.workshop_bench', 'desired': 'off'},
                       {'view': foreign['SD:all:workshop']['view'], 'id': 'SD:all:workshop', 'desired': 'off'},
                       {'view': 'another-session-' + before, 'id': 'SD:all:lantern', 'desired': 'off'}):
            request = self.send('devices.power', params)
            self.assertEqual(self.result(request)['code'], 'list_changed')
        self.fake.registry_entries['switch.lantern_plug'] = {'name': 'Changed Plug'}
        self.assertTrue(self.hub.refresh())
        request = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
        self.assertEqual(self.result(request)['code'], 'list_changed')
        self.assertEqual(self.fake.service_requests, [])

    def test_queue_returns_promptly_latest_wins_and_overlapping_targets_are_replaced(self):
        root, _ = self.browse()
        individual, _ = self.browse('SD:a:lantern:lights')
        entered, release = threading.Event(), threading.Event()
        actual = self.hub.client.call_service
        calls, in_flight, maximum = [], [0], [0]
        def blocked(domain, service, targets, data=None, **kwargs):
            in_flight[0] += 1; maximum[0] = max(maximum[0], in_flight[0])
            try:
                calls.append((domain, service, targets, data))
                if len(calls) == 1:
                    entered.set()
                    self.assertTrue(release.wait(10))
                return actual(domain, service, targets, data, **kwargs)
            finally: in_flight[0] -= 1
        with patch.object(self.hub.client, 'call_service', side_effect=blocked):
            try:
                before = time.monotonic()
                first = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
                self.assertLess(time.monotonic() - before, .5)
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.result(first)['data']['pending'], 'true')
                old = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'on'})
                member = self.send('devices.power', {**self.token(individual, 'light.lantern_counter_left'), 'desired': 'off'})
                newest = self.send('devices.level', {**self.token(root, 'SD:all:lantern'), 'value': '25'})
            finally: release.set()
            self.assertTrue(self.finished(newest))
            self.assertTrue(self.finished(old))
            self.assertTrue(self.finished(member))
        self.assertEqual(maximum[0], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][3], {'brightness_pct': 25})
        self.assertEqual(set(calls[-1][2]), {'light.lantern_ceiling', 'light.lantern_counter_left', 'light.lantern_counter_right'})

    def test_queued_action_rechecks_identity_immediately_before_ha_call(self):
        root, _ = self.browse()
        entered, release = threading.Event(), threading.Event()
        actual = self.hub.client.call_service
        calls = []
        def blocked(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                entered.set(); self.assertTrue(release.wait(10))
            return actual(*args, **kwargs)
        with patch.object(self.hub.client, 'call_service', side_effect=blocked):
            try:
                self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
                self.assertTrue(entered.wait(3))
                queued = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
                self.fake.registry_entries['switch.lantern_plug'] = {'name': 'New Plug'}
                self.assertTrue(self.hub.refresh())
            finally: release.set()
            self.assertTrue(self.finished(queued))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.result(queued)['code'], 'list_changed')

    def test_queued_action_expires_before_call_instead_of_firing_after_ui_budget(self):
        root, _ = self.browse()
        scenes, _ = self.browse('SD:a:lantern:scenes')
        entered, release = threading.Event(), threading.Event()
        actual, calls = self.hub.client.call_service, []
        def blocked(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                entered.set(); self.assertTrue(release.wait(10))
            return actual(*args, **kwargs)
        with patch.object(self.hub.client, 'call_service', side_effect=blocked):
            try:
                self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
                self.assertTrue(entered.wait(3))
                queued = self.send('devices.activate', self.token(scenes, 'scene.lantern_evening'))
                with self.service.condition:
                    job = next(job for job in self.service.pending if job.context.request_id == queued)
                    job.started = self.service.clock() - smart_device_service.ACTION_TIMEOUT - .1
            finally: release.set()
            self.assertTrue(self.finished(queued))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.result(queued)['code'], 'busy')
        self.assertNotIn('activation_count', self.hub.client.state('scene.lantern_evening')['attributes'])

    def test_timed_out_service_quarantines_overlap_until_actual_result_but_other_targets_proceed(self):
        root, _ = self.browse()
        individual, _ = self.browse('SD:a:lantern:lights')
        entered, release = threading.Event(), threading.Event()
        actual_result = self.fake.result
        delayed = [False]

        async def delay_first_service_result(ws, message_id, result):
            if result == {'context': {}} and not delayed[0]:
                delayed[0] = True
                entered.set()

                async def acknowledge_later():
                    while not release.is_set(): await asyncio.sleep(.01)
                    await actual_result(ws, message_id, result)

                # Keep reading commands, like HA's asynchronous service handler;
                # the first operation remains unacknowledged until released.
                asyncio.create_task(acknowledge_later())
                return
            return await actual_result(ws, message_id, result)

        with patch.object(self.fake, 'result', side_effect=delay_first_service_result), \
                patch.object(smart_device_service, 'ACTION_TIMEOUT', .35):
            try:
                first = self.send('devices.power', {**self.token(root, 'light.lantern_counter'), 'desired': 'off'})
                self.assertTrue(entered.wait(3))
                queued = self.send('devices.power', {**self.token(individual, 'light.lantern_counter_left'), 'desired': 'on'})
                self.assertTrue(self.finished(first))
                self.assertEqual(self.result(first)['code'], 'device_failed')
                self.assertTrue(self.finished(queued))
                self.assertEqual(self.result(queued)['code'], 'busy')
                overlap = self.send('devices.power', {**self.token(individual, 'light.lantern_counter_left'), 'desired': 'on'})
                self.assertTrue(self.finished(overlap))
                self.assertEqual(self.result(overlap)['code'], 'busy')
                unrelated = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
                self.assertTrue(self.finished(unrelated))
                self.assertEqual(self.result(unrelated)['status'], 'ok')
                self.assertEqual(len(self.fake.calls), 2)
                self.assertEqual(self.fake.calls[-1][1], ['switch.lantern_plug'])
                self.assertEqual(self.hub.client.state('light.lantern_counter_left')['state'], 'off')
            finally:
                release.set()
            self.assertTrue(wait_for(lambda: all(entry[1].is_set() for entry in self.service.uncertain)))
            retry = self.send('devices.power', {**self.token(individual, 'light.lantern_counter_left'), 'desired': 'on'})
            self.assertTrue(self.finished(retry))
            self.assertEqual(self.result(retry)['status'], 'ok')
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(self.fake.calls[-1][1], ['light.lantern_counter_left'])

    def test_timed_out_service_is_released_after_disconnect_and_reconnect(self):
        root, _ = self.browse()
        actual_result = self.fake.result
        entered = threading.Event()

        async def suppress_first_service_result(ws, message_id, result):
            if result == {'context': {}} and not entered.is_set():
                entered.set()
                return
            return await actual_result(ws, message_id, result)

        with patch.object(self.fake, 'result', side_effect=suppress_first_service_result), \
                patch.object(smart_device_service, 'ACTION_TIMEOUT', .3), \
                patch('ha_client.RECONNECT_DELAY', .05):
            first = self.send('devices.power', {**self.token(root, 'light.lantern_counter'), 'desired': 'off'})
            self.assertTrue(entered.wait(3)); self.assertTrue(self.finished(first))
            self.assertEqual(self.result(first)['code'], 'device_failed')
            self.assertEqual(len(self.service.uncertain), 1)
            receipt = self.service.uncertain[0][1]
            self.fake.set_available(False)
            self.assertTrue(wait_for(lambda: not self.hub.client.connected.is_set()))
            self.assertFalse(receipt.is_set())
            self.fake.set_available(True)
            self.assertTrue(wait_for(lambda: self.hub.client.connected.is_set()))
            root, _ = self.browse()
            overlap = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'on'})
            self.assertTrue(self.finished(overlap))
            self.assertEqual(self.result(overlap)['status'], 'ok')
            unrelated = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
            self.assertTrue(self.finished(unrelated))
            self.assertEqual(self.result(unrelated)['status'], 'ok')
            self.assertFalse(receipt.is_set())
        self.assertEqual(len(self.fake.calls), 3)

    def test_actual_late_ha_failure_acknowledges_timed_out_target(self):
        root, _ = self.browse()
        entered, release = threading.Event(), threading.Event()
        actual_result = self.fake.result

        async def delay_first_failure(ws, message_id, result):
            if result == {'context': {}} and not entered.is_set():
                entered.set()

                async def reject_later():
                    while not release.is_set(): await asyncio.sleep(.01)
                    await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                              'error': {'code': 'failed', 'message': 'Delayed rejection'}}))

                asyncio.create_task(reject_later())
                return
            return await actual_result(ws, message_id, result)

        with patch.object(self.fake, 'result', side_effect=delay_first_failure), \
                patch.object(smart_device_service, 'ACTION_TIMEOUT', .3):
            try:
                first = self.send('devices.power', {**self.token(root, 'light.lantern_ceiling'), 'desired': 'off'})
                self.assertTrue(entered.wait(3)); self.assertTrue(self.finished(first))
                self.assertEqual(self.result(first)['code'], 'device_failed')
                receipt = self.service.uncertain[0][1]
                self.assertFalse(receipt.is_set())
            finally: release.set()
            self.assertTrue(receipt.wait(3))
            retry = self.send('devices.power', {**self.token(root, 'light.lantern_ceiling'), 'desired': 'on'})
            self.assertTrue(self.finished(retry))
            self.assertEqual(self.result(retry)['status'], 'ok')
        self.assertEqual(len(self.fake.calls), 2)

    def test_disconnect_during_call_releases_unacknowledged_target_after_reconnect(self):
        root, _ = self.browse()
        actual_result = self.fake.result
        dropped = threading.Event()

        async def disconnect_first_service(ws, message_id, result):
            if result == {'context': {}} and not dropped.is_set():
                dropped.set()
                self.fake.available = False
                await ws.close(code=1013, reason='Test disconnect during service')
                return
            return await actual_result(ws, message_id, result)

        with patch.object(self.fake, 'result', side_effect=disconnect_first_service), \
                patch('ha_client.RECONNECT_DELAY', .05):
            first = self.send('devices.power', {**self.token(root, 'light.lantern_counter'), 'desired': 'off'})
            self.assertTrue(dropped.wait(3)); self.assertTrue(self.finished(first))
            self.assertEqual(self.result(first)['code'], 'device_failed')
            self.assertEqual(len(self.service.uncertain), 1)
            receipt = self.service.uncertain[0][1]
            self.assertFalse(receipt.is_set())
            self.fake.set_available(True)
            self.assertTrue(wait_for(lambda: self.hub.client.connected.is_set()))
            root, _ = self.browse()
            overlap = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'on'})
            self.assertTrue(self.finished(overlap))
            self.assertEqual(self.result(overlap)['status'], 'ok')
            unrelated = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
            self.assertTrue(self.finished(unrelated))
            self.assertEqual(self.result(unrelated)['status'], 'ok')
            self.assertFalse(receipt.is_set())
        self.assertEqual(len(self.fake.calls), 3)

    def test_partial_overlap_keeps_only_unsuperseded_members_of_queued_all_lights(self):
        root, _ = self.browse()
        individual, _ = self.browse('SD:a:lantern:lights')
        entered, release = threading.Event(), threading.Event()
        actual, calls = self.hub.client.call_service, []
        def blocked(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                entered.set(); self.assertTrue(release.wait(10))
            return actual(*args, **kwargs)
        with patch.object(self.hub.client, 'call_service', side_effect=blocked):
            try:
                self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
                self.assertTrue(entered.wait(3))
                older = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'on'})
                newer = self.send('devices.power', {**self.token(individual, 'light.lantern_counter_left'), 'desired': 'off'})
            finally: release.set()
            self.assertTrue(self.finished(newer)); self.assertTrue(self.finished(older))
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(calls[1][2]), {'light.lantern_ceiling', 'light.lantern_counter_right'})
        self.assertEqual(calls[2][2], ['light.lantern_counter_left'])
        self.assertEqual(self.hub.client.state('light.lantern_counter_left')['state'], 'off')

    def test_unassigned_device_events_do_not_rebuild_the_catalogue(self):
        entity = 'light.unassigned_test'
        self.fake.entities.append({'entity_id': entity, 'platform': 'test', 'area_id': None,
                                   'device_id': None, 'disabled_by': None, 'hidden_by': None, 'entity_category': None})
        self.fake.states[entity] = {'entity_id': entity, 'state': 'off',
                                    'attributes': {'friendly_name': 'Unassigned Test'}}
        self.assertTrue(self.hub.refresh())
        self.assertTrue(wait_for(lambda: entity in self.service.catalogue.registry_entities))
        with self.service.condition: revision = self.service.revision
        self.fake.set(entity, 'on')
        self.assertTrue(wait_for(lambda: self.hub.client.state(entity).get('state') == 'on'))
        time.sleep(.15)
        self.assertEqual(self.service.revision, revision)

    def test_external_updates_coalesce_with_trailing_publish_and_unrelated_states_are_ignored(self):
        self.browse()
        self.browse(player=self.other)
        now = [100.0]
        with self.service.condition:
            self.service.clock = lambda: now[0]
            self.service.updates.clear(); self.service.last_publish.clear(); self.service.trailing.clear()
        key = self.player.config.udn, 'SD:'
        other_key = self.other.config.udn, 'SD:'
        self.fake.set('light.lantern_ceiling', brightness=150)
        self.assertTrue(wait_for(lambda: key in self.service.updates))
        first = self.service.updates[key]
        now[0] += .2
        self.fake.set('light.lantern_ceiling', brightness=140)
        self.assertTrue(wait_for(lambda: key in self.service.trailing))
        self.assertEqual(self.service.updates[key], first)
        self.assertNotIn(other_key, self.service.updates)
        revision = self.service.revision
        self.fake.set('sensor.lantern_temperature', '22')
        self.assertTrue(wait_for(lambda: self.hub.client.state('sensor.lantern_temperature')['state'] == '22'))
        time.sleep(.15)
        self.assertEqual(self.service.revision, revision)
        now[0] = 102.1
        with self.service.condition: self.service.condition.notify()
        self.assertTrue(wait_for(lambda: self.service.updates[key] == first + 1))
        self.assertNotIn(key, self.service.trailing)

    def refresh_batch(self, changes, *, owned=(), registry_change=False):
        """Hand the real worker one deterministic batch of cached HA events."""
        self.browse()
        self.browse(player=self.other)
        now = [1000.0]
        lantern = self.player.config.udn, 'SD:'
        workshop = self.other.config.udn, 'SD:'
        with self.service.condition:
            self.service.clock = lambda: now[0]
            self.service.changed_entities.clear(); self.service.refresh_all = False
            self.service.updates.clear(); self.service.last_publish.clear(); self.service.trailing.clear()
            self.service.own_until = {entity: now[0] + 10 for entity in owned}
            self.service._publish(*lantern, bypass=True)
            self.service._publish(*workshop, bypass=True)
            before = dict(self.service.updates)
            tokens = {view.udn: view.token for view in self.service.views.values() if view.container == 'SD:'}
            for entity, values in changes.items():
                state = copy.deepcopy(self.hub.client.states[entity])
                state['attributes'].update(values.get('attributes', {}))
                if 'state' in values: state['state'] = values['state']
                self.hub.client.states[entity] = state
            if registry_change:
                with self.hub.lock:
                    entry = next(e for e in self.hub.registries['entities'] if e['entity_id'] == 'light.lantern_ceiling')
                    entry['name'] = 'Lantern Renamed Ceiling'; self.hub.revision += 1
                self.service.refresh_all = True
            self.service.changed_entities.update(changes)
            revision = self.service.revision
            self.service.condition.notify()
        self.assertTrue(wait_for(lambda: self.service.revision > revision))
        return now, lantern, workshop, before, tokens

    def assert_external_workshop_is_capped(self, workshop, before):
        with self.service.condition:
            self.assertEqual(self.service.updates[workshop], before[workshop])
            self.assertEqual(self.service.trailing[workshop], 1002.0)
        self.assertEqual(self.fake.service_requests, [])

    def test_co_batched_own_lantern_does_not_bypass_external_workshop_cap(self):
        now, lantern, workshop, before, tokens = self.refresh_batch({
            'light.lantern_ceiling': {'attributes': {'brightness': 230}},
            'light.workshop_bench': {'attributes': {'brightness': 128}}}, owned=['light.lantern_ceiling'])
        with self.service.condition:
            self.assertEqual(self.service.updates[lantern], before[lantern] + 1)
            self.assertNotIn(lantern, self.service.trailing)
            self.assertTrue(all(view.token == tokens[view.udn] for view in self.service.views.values()
                                if view.container == 'SD:'))
        self.assert_external_workshop_is_capped(workshop, before)
        now[0] = 1002.1
        with self.service.condition: self.service.condition.notify()
        self.assertTrue(wait_for(lambda: self.service.updates[workshop] == before[workshop] + 1))
        self.assertNotIn(workshop, self.service.trailing)

    def test_external_only_update_stays_capped_while_unrelated_own_window_is_active(self):
        _, lantern, workshop, before, _ = self.refresh_batch({
            'light.workshop_bench': {'attributes': {'brightness': 128}}}, owned=['light.lantern_ceiling'])
        self.assertEqual(self.service.updates[lantern], before[lantern])
        self.assert_external_workshop_is_capped(workshop, before)

    def test_registry_structure_refresh_does_not_bypass_unrelated_external_state(self):
        _, lantern, workshop, before, tokens = self.refresh_batch({
            'light.workshop_bench': {'attributes': {'brightness': 128}}}, registry_change=True)
        self.assertEqual(self.service.updates[lantern], before[lantern] + 1)
        view = next(view for view in self.service.views.values() if view.udn == lantern[0])
        self.assertNotEqual(view.token, tokens[lantern[0]])
        self.assert_external_workshop_is_capped(workshop, before)

    def test_row_availability_bypasses_only_its_affected_container(self):
        _, lantern, workshop, before, tokens = self.refresh_batch({
            'light.lantern_ceiling': {'state': 'unavailable'},
            'light.workshop_bench': {'attributes': {'brightness': 128}}})
        self.assertEqual(self.service.updates[lantern], before[lantern] + 1)
        view = next(view for view in self.service.views.values() if view.udn == lantern[0])
        self.assertEqual(view.token, tokens[lantern[0]])
        self.assert_external_workshop_is_capped(workshop, before)

    def test_own_member_event_bypasses_recursive_native_group_and_all_lights_dependencies(self):
        # A native group in Workshop aggregates a native group from Lantern.
        # Its repaint depends on a member two levels down, while Workshop's
        # synthetic All Lights targets only its own separate physical lamps.
        entity = 'light.workshop_nested_group'
        with self.hub.lock:
            self.hub.registries['entities'].append({'entity_id': entity, 'platform': 'hue', 'area_id': 'workshop'}); self.hub.revision += 1
        self.hub.client.states[entity] = {'entity_id': entity, 'state': 'on', 'attributes': {
            'friendly_name': 'Workshop Nested Group', 'entity_id': ['light.lantern_counter'],
            'supported_color_modes': ['brightness'], 'brightness': 102}}
        _, lantern, workshop, before, _ = self.refresh_batch({
            'light.lantern_counter_left': {'attributes': {'brightness': 230}},
            entity: {'attributes': {'brightness': 200}}}, owned=['light.lantern_counter_left'])
        with self.service.condition:
            self.assertEqual(self.service.updates[lantern], before[lantern] + 1)
            self.assertEqual(self.service.updates[workshop], before[workshop] + 1)
            self.assertNotIn(lantern, self.service.trailing)
            self.assertNotIn(workshop, self.service.trailing)
        self.assertEqual(self.fake.service_requests, [])

    def nested_light_groups(self, mode='hidden'):
        outer, inner = 'light.lantern_outer', 'light.workshop_inner'
        self.fake.entities.append({'entity_id': outer, 'platform': 'hue', 'area_id': 'lantern'})
        entry = {'entity_id': inner, 'platform': 'hue', 'area_id': 'workshop'}
        if mode == 'hidden': entry['hidden_by'] = 'integration'
        elif mode == 'no-area': entry['area_id'] = None
        if mode != 'missing-registry': self.fake.entities.append(entry)
        for entity, name, members in [(outer, 'Lantern Outer', [inner]),
                                      (inner, 'Workshop Inner', ['light.workshop_bench'])]:
            self.fake.states[entity] = {'entity_id': entity, 'state': 'on', 'attributes': {
                'friendly_name': name, 'entity_id': members, 'brightness': 153,
                'supported_color_modes': ['brightness']}}
        self.assertTrue(self.hub.refresh())
        self.fake.set(outer); self.fake.set(inner)
        self.assertTrue(wait_for(lambda: self.hub.client.state(inner).get('attributes', {}).get('entity_id')
                                == ['light.workshop_bench']))
        root, _ = self.browse()
        self.assertIn(outer, root)
        self.assertNotIn(inner, self.service.catalogue.entities)
        self.assertIn(inner, self.service.entity_index)
        self.assertNotIn('light.unreferenced_raw_group', self.service.entity_index)
        return root, outer, inner

    def changed_nested_group_revokes_old_view(self, mode):
        root, outer, inner = self.nested_light_groups(mode)
        token = root[outer]['view']
        registry_reads = len(self.fake.registry_requests)
        self.fake.states[inner]['attributes']['entity_id'] = ['light.workshop_overhead']
        self.fake.set(inner)
        self.assertTrue(wait_for(lambda: token not in self.service.tokens))
        request = self.send('devices.power', {**self.token(root, outer), 'desired': 'off'})
        self.assertEqual(self.result(request)['code'], 'list_changed')
        fresh, _ = self.browse()
        self.assertNotEqual(fresh[outer]['view'], token)
        self.assertEqual(self.fake.service_requests, [])
        self.assertEqual(len(self.fake.registry_requests), registry_reads)

    def test_hidden_inner_membership_event_revokes_outer_view_without_registry_read(self):
        self.changed_nested_group_revokes_old_view('hidden')

    def test_unassigned_inner_membership_event_revokes_outer_view_without_registry_read(self):
        self.changed_nested_group_revokes_old_view('no-area')

    def test_missing_registry_inner_membership_event_revokes_outer_view(self):
        self.changed_nested_group_revokes_old_view('missing-registry')

    def test_unavailable_inner_with_cleared_attributes_retains_outer_token_until_members_change(self):
        root, outer, inner = self.nested_light_groups()
        token = root[outer]['view']
        revision = self.service.revision
        self.fake.states[inner]['attributes'] = {}
        self.fake.set(inner, 'unavailable')
        self.assertTrue(wait_for(lambda: self.service.revision > revision))
        self.assertIn(token, self.service.tokens)
        self.assertIn(inner, self.service.entity_index)
        self.fake.states[inner]['attributes']['entity_id'] = ['light.workshop_overhead']
        self.fake.set(inner, 'on')
        self.assertTrue(wait_for(lambda: token not in self.service.tokens))
        self.assertEqual(self.fake.service_requests, [])

    def test_queued_outer_action_rechecks_filtered_inner_membership_before_ha_call(self):
        root, outer, inner = self.nested_light_groups()
        entered, release = threading.Event(), threading.Event()
        actual, calls = self.hub.client.call_service, []
        def blocked(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                entered.set(); self.assertTrue(release.wait(10))
            return actual(*args, **kwargs)
        with patch.object(self.hub.client, 'call_service', side_effect=blocked):
            try:
                self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
                self.assertTrue(entered.wait(3))
                queued = self.send('devices.power', {**self.token(root, outer), 'desired': 'off'})
                self.fake.states[inner]['attributes']['entity_id'] = ['light.workshop_overhead']
                self.fake.set(inner)
                self.assertTrue(wait_for(lambda: self.hub.client.state(inner)['attributes']['entity_id']
                                        == ['light.workshop_overhead']))
            finally: release.set()
            self.assertTrue(self.finished(queued))
        self.assertEqual(self.result(queued)['code'], 'list_changed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fake.calls, [('turn_off', ['switch.lantern_plug'], {})])

    def test_disconnect_and_return_replace_open_lists_and_reject_actions(self):
        root, _ = self.browse()
        with patch('ha_client.RECONNECT_DELAY', .05):
            self.fake.set_available(False)
            self.assertTrue(wait_for(lambda: not self.hub.client.connected.is_set()))
            self.assertEqual(self.prop('CR100_DevicesAvailable'), '0')
            unavailable, total = self.browse()
            self.assertEqual((total, unavailable['SD:unavailable']['title']), (1, 'Home Assistant unavailable'))
            request = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
            self.assertEqual(self.result(request)['code'], 'unavailable')
            self.fake.set_available(True)
            self.assertTrue(wait_for(lambda: self.hub.client.connected.is_set()))
            restored, _ = self.browse()
            self.assertIn('SD:all:lantern', restored)
            self.assertEqual(self.prop('CR100_DevicesAvailable'), '1')
        self.assertEqual(self.fake.service_requests, [])

    def test_group_membership_invalidates_old_tokens_and_union_is_bridge_owned(self):
        root, _ = self.browse()
        members = ['media_player.lantern', 'media_player.workshop']
        self.fake.set(members[0], group_members=members)
        self.fake.set(members[1], group_members=members)
        self.assertTrue(wait_for(lambda: self.hub.client.state(members[1])['attributes']['group_members'] == members))
        self.house.sync_groups()
        request = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
        self.assertEqual(self.result(request)['code'], 'list_changed')
        grouped, _ = self.browse()
        self.assertIn('SD:all:group', grouped)
        request = self.send('devices.power', {**self.token(grouped, 'SD:all:group'), 'desired': 'off'})
        self.assertTrue(self.finished(request))
        self.assertEqual(len(self.fake.service_requests[-1][2]), 5)

    def test_unavailable_unsupported_and_malformed_actions_are_rejected_without_ha_calls(self):
        root, _ = self.browse()
        scenes, _ = self.browse('SD:a:lantern:scenes')
        for action, params in (
            ('devices.level', {**self.token(root, 'switch.lantern_plug'), 'value': '50'}),
            ('devices.power', {**self.token(scenes, 'scene.lantern_evening'), 'desired': 'on'}),
            ('devices.activate', self.token(root, 'switch.lantern_plug')),
            ('devices.level', {**self.token(root, 'SD:all:lantern'), 'value': '101'}),
            ('devices.level', {**self.token(root, 'SD:all:lantern'), 'value': '-1'}),
            ('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'toggle'}),
        ):
            request = self.send(action, params)
            self.assertEqual(self.result(request)['code'], 'unsupported_action')
        self.fake.set('fan.lantern_extractor', 'unavailable')
        self.assertTrue(wait_for(lambda: self.hub.client.state('fan.lantern_extractor')['state'] == 'unavailable'))
        request = self.send('devices.level', {**self.token(root, 'fan.lantern_extractor'), 'value': '60'})
        self.assertEqual(self.result(request)['code'], 'unavailable')
        self.assertEqual(self.fake.service_requests, [])
        actions = [record for record in self.logs if record.get('event') == 'smart-device-action']
        self.assertEqual(len(actions), 7)
        self.assertTrue(all(record['row_id'] for record in actions))
        self.assertTrue(all('view' not in record and 'token' not in record for record in actions))

    def test_action_and_structure_bypass_cap_and_confirm_only_missing_state_updates(self):
        root, _ = self.browse()
        now = [200.0]
        key = self.player.config.udn, 'SD:'
        with self.service.condition:
            self.service.clock = lambda: now[0]
            self.service.updates.clear(); self.service.last_publish.clear(); self.service.trailing.clear()
            self.service._publish(*key, bypass=True)
        first = self.service.updates[key]
        # Own action state updates bypass an already consumed external cap.
        request = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
        self.assertTrue(self.finished(request))
        self.assertTrue(wait_for(lambda: self.service.updates[key] > first))
        self.assertEqual(self.service.confirmations, [])

        own_version = self.service.updates[key]
        # A registry rename is structural and bypasses the same cap immediately.
        self.fake.registry_entries['switch.lantern_plug'] = {'name': 'New Plug'}
        self.assertTrue(self.hub.refresh())
        self.assertTrue(wait_for(lambda: self.service.updates[key] > own_version))
        root, _ = self.browse()
        # A successful service with no resulting state event still publishes at
        # three seconds; a prior ordinary action must not leave a second timer.
        with patch.object(self.hub.client, 'call_service', return_value={'context': {}}):
            request = self.send('devices.power', {**self.token(root, 'switch.lantern_plug'), 'desired': 'off'})
            self.assertTrue(self.finished(request))
        self.assertEqual(len(self.service.confirmations), 1)
        before_timeout = self.service.updates[key]
        now[0] += 3.1
        with self.service.condition: self.service.condition.notify()
        self.assertTrue(wait_for(lambda: self.service.updates[key] == before_timeout + 1))
        self.assertEqual(self.service.confirmations, [])

    def test_device_unavailable_bypasses_external_cap_without_changing_view_token(self):
        root, _ = self.browse()
        original_token = root['fan.lantern_extractor']['view']
        now = [300.0]
        key = self.player.config.udn, 'SD:'
        with self.service.condition:
            self.service.clock = lambda: now[0]
            self.service.updates.clear(); self.service.last_publish.clear(); self.service.trailing.clear()
            self.service._publish(*key, bypass=True)
        first = self.service.updates[key]
        now[0] += .1
        self.fake.set('fan.lantern_extractor', 'unavailable')
        self.assertTrue(wait_for(lambda: self.service.updates[key] > first))
        updated, _ = self.browse()
        self.assertIn('Unavailable', updated['fan.lantern_extractor']['title'])
        self.assertEqual(updated['fan.lantern_extractor']['view'], original_token)
        self.assertNotIn(key, self.service.trailing)

    def test_failure_notify_is_scoped_ordered_and_fallback_receipt_is_retained(self):
        first, second = Notifications('127.0.0.1'), Notifications('127.0.0.1')
        self.addCleanup(first.close); self.addCleanup(second.close)
        for address, callback in (('127.0.0.1', first),):
            connection = http.client.HTTPConnection('127.0.0.1', self.player.config.port, timeout=10,
                                                    source_address=(address, 0))
            try:
                connection.request('SUBSCRIBE', '/MediaRenderer/AVTransport/Event', headers={
                    'CALLBACK': '<' + callback.url + '>', 'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
                reply = connection.getresponse(); reply.read(); self.assertEqual(reply.status, 200)
            finally: connection.close()
        with self.player.state.lock:
            self.player.state.subscriptions['uuid:other-controller'] = {
                'service': 'AVTransport', 'callback': second.url, 'source': '127.0.0.2',
                'expires': time.monotonic() + 300, 'sequence': 0, 'notify_lock': threading.Lock()}
        root, _ = self.browse()
        self.fake.fail_services = True
        request = self.send('devices.power', {**self.token(root, 'SD:all:lantern'), 'desired': 'off'})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.prop('CR100_DevicesFeedback:' + request), 'notify')
        self.assertTrue(wait_for(lambda: first.items.qsize() >= 2))
        time.sleep(.1)
        statuses = first.statuses()
        error_index = statuses.index('ERROR_DEVICES')
        self.assertEqual(statuses[error_index + 1], 'OK')
        self.assertNotIn('ERROR_DEVICES', second.statuses())
        self.assertEqual(self.result(request)['code'], 'device_failed')
        self.assertEqual(self.result(request, address='127.0.0.2')['code'], 'not_found')
        # A request with no matching live SID must freeze deferred feedback,
        # even when a stale firmware preflight claimed a notify channel.
        with self.player.request_context('127.0.0.3'):
            issued = self.service.browse(self.player.state, {'ObjectID': 'SD:', 'BrowseFlag': 'BrowseDirectChildren',
                'StartingIndex': 0, 'RequestedCount': 500})
        issued_row = next(row for row in ET.fromstring(issued['Result']) if row.get('id') == 'SD:all:lantern')
        other_token = issued_row.find(UPNP + 'class').text.rsplit('.', 1)[-1]
        request = self.send('devices.power', {'view': other_token, 'id': 'SD:all:lantern',
                            'desired': 'off', 'feedback': 'notify'}, address='127.0.0.3')
        self.assertTrue(wait_for(lambda: self.result(request, address='127.0.0.3')['data'].get('pending') != 'true'))
        self.assertEqual(self.prop('CR100_DevicesFeedback:' + request, address='127.0.0.3'), 'deferred')
        self.assertEqual(self.result(request, address='127.0.0.3')['code'], 'device_failed')
        # One-shot rows deliberately choose deferred feedback even with a live
        # SID, so their Started/error result cannot race a second error dialog.
        first.statuses()
        scenes, _ = self.browse('SD:a:lantern:scenes')
        request = self.send('devices.activate', {**self.token(scenes, 'scene.lantern_evening'),
                                                'feedback': 'deferred'})
        self.assertTrue(self.finished(request))
        self.assertEqual(self.prop('CR100_DevicesFeedback:' + request), 'deferred')
        self.assertEqual(self.result(request)['code'], 'device_failed')
        time.sleep(.1)
        self.assertNotIn('ERROR_DEVICES', first.statuses())


class NativeArea(unittest.TestCase):
    def test_native_sonos_room_uses_shared_ha_registry_identifiers(self):
        class Native(backends.Backend):
            name = 'sonos'
            def room(self, key): return backends.Room(self, key)
        fake = fake_ha.FakeHA(**fake_ha.smart_device_fixture())
        self.addCleanup(fake.close)
        hub = HAHub(HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None), lambda _: None)
        self.addCleanup(hub.close); hub.start(); self.assertTrue(hub.ready.wait(5))
        backend = Native(lambda _: None)
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Lantern'], ['127.0.0.1'], lambda _: None,
            port=0, backends=[backend], refs=[('sonos', 'RINCON_00000000001101400')], devices_hub=hub)
        self.addCleanup(house.close)
        self.assertEqual(house.smart_devices.status()['rooms'], [{'room': 'Lantern', 'area': 'Lantern'}])
        self.assertTrue(house.smart_devices.available())
        self.assertEqual(fake.service_requests, [])


if __name__ == '__main__': unittest.main()
