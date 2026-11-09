"""Explicit firmware actions exercise the production HTTP/SOAP player path."""
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import zoneplayers
from test_zoneplayers import HOUSEHOLD, soap, zp
from ui_events import (UIEventError, UIEventGateway, UIResult, decode_params,
                       encode_request)


def decode_result(wire):
    version, request_id, status, code, message, data = map(unquote, wire.split('|'))
    return dict(version=version, request_id=request_id, status=status, code=code,
                message=message, data=decode_params(data))


class UIEventsHTTP(unittest.TestCase):
    def setUp(self):
        self.logs, self.calls = [], []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.path.write_text(json.dumps({'spotify_music_service': {'enabled': True, 'autoplay': False,
                                                                   'client_id': 'keep'}, 'unrelated': 42}))
        self.identities = {'127.0.0.1': 'controller-a'}
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Living', 'Kitchen'],
            ['127.0.0.1'] * 2, self.logs.append, port=0, config_path=self.path,
            controller_resolver=self.identities.get)
        self.house.start('127.0.0.1', ssdp=False)
        self.addCleanup(self.house.close)
        self.player, self.other = self.house.players
        def setting(context, params):
            if set(params) != {'value'}:
                raise UIEventError('invalid_parameters', 'A value is required')
            self.calls.append((context, params.copy()))
            return UIResult('Applied to ' + context.player.config.room, {'value': params['value']})
        self.house.ui_events.register('test.setting', setting)

    def request(self, action, args, player=None):
        document = ''.join('<%s>%s</%s>' % (key, escape(value), key) for key, value in args.items())
        return soap(player or self.player, '/SystemProperties/Control', 'SystemProperties', action, document)

    def send(self, request_id='request-1', action='test.setting', params=None, room=None, player=None):
        return self.request('SetString', {'VariableName': 'CR100_UIEvent',
            'StringValue': encode_request(request_id, action, room or (player or self.player).config.udn,
                                           {'value': '2'} if params is None else params)}, player)

    def result(self, request_id='request-1', player=None):
        status, document = self.request('GetString', {'VariableName': 'CR100_UIResult:' + request_id}, player)
        self.assertEqual(status, 200, document)
        return decode_result(ET.fromstring(document).find('.//StringValue').text)

    def test_explicit_set_dispatches_with_controller_and_selected_room_context(self):
        self.assertEqual(self.send()[0], 200)
        context, params = self.calls[0]
        self.assertEqual(context.controller_id, 'controller-a')
        self.assertEqual(context.controller_ip, '127.0.0.1')
        self.assertIs(context.player, self.player)
        self.assertIs(context.state, self.player.state)
        self.assertIs(context.household, self.house)
        self.assertIsNone(context.room)
        self.assertEqual(context.room_udn, self.player.config.udn)
        self.assertEqual(params, {'value': '2'})
        self.assertEqual(self.result()['data'], {'value': '2'})

    def test_results_and_capabilities_are_read_only(self):
        for _ in range(3):
            self.assertEqual(self.result()['code'], 'not_found')
            status, document = self.request('GetString', {'VariableName': 'CR100_UICapabilities'})
            self.assertEqual(status, 200)
            self.assertIn(b'test.setting', document)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request('GetString', {'VariableName': 'CR100_UIEvent'})[0], 500)
        self.assertEqual(self.request('SetString', {'VariableName': 'CR100_UICapabilities', 'StringValue': ''})[0], 500)

    def test_duplicate_retries_across_endpoints_do_not_apply_another_room(self):
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.send(params={'value': '3'})[0], 500)
        self.assertEqual(self.send(player=self.other)[0], 500)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.result()['data']['value'], '2')

    def test_concurrent_identical_requests_execute_once(self):
        errors = []
        def run():
            try:
                if self.send()[0] != 200: errors.append('HTTP failure')
            except Exception as error: errors.append(error)
        workers = [threading.Thread(target=run) for _ in range(6)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 1)

    def test_result_is_scoped_to_controller_and_unknown_source_is_rejected(self):
        self.send()
        self.identities['127.0.0.1'] = 'controller-b'
        self.assertEqual(self.result()['code'], 'not_found')
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(len(self.calls), 2)
        self.identities.clear()
        self.assertEqual(self.send(request_id='new')[0], 500)
        self.assertEqual(len(self.calls), 2)

    def test_wrong_room_and_unknown_action_have_honest_results_without_mutation(self):
        self.assertEqual(self.send(room=self.other.config.udn)[0], 200)
        self.assertEqual(self.result()['code'], 'room_mismatch')
        self.assertEqual(self.send('unknown', 'unknown.action')[0], 200)
        self.assertEqual(self.result('unknown')['code'], 'unsupported_action')
        self.assertEqual(self.calls, [])

    def test_cached_hidden_component_is_rejected_before_ui_dispatch(self):
        self.house.controller_config = {'controllers': {'controller-a': {'visible_components': ['sim:0']}}}
        self.assertEqual(self.send(player=self.other)[0], 404)
        self.assertEqual(self.calls, [])

    def test_invalid_provider_results_and_long_failures_retain_failure_receipts(self):
        calls = []
        def failed(context, params):
            calls.append(context.request_id)
            raise UIEventError('upstream_error', 'x' * 2000)
        def invalid(context, params):
            calls.append(context.request_id)
            return UIResult('x' * 2000)
        self.house.ui_events.register('test.long', failed)
        self.house.ui_events.register('test.invalid', invalid)
        for action in ('test.long', 'test.invalid'):
            self.send(action, action, {})
            self.send(action, action, {})
            self.assertEqual(self.result(action)['status'], 'error')
        self.assertEqual(calls, ['test.long', 'test.invalid'])

    def test_expected_and_unexpected_failures_do_not_report_success_or_leak_details(self):
        def failed(context, params): raise RuntimeError('secret-credential')
        self.house.ui_events.register('test.fail', failed)
        self.send('bad-params', params={})
        self.assertEqual(self.result('bad-params')['code'], 'invalid_parameters')
        self.send('failure', 'test.fail')
        result = self.result('failure')
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('secret', result['message'])
        self.assertNotIn('secret', json.dumps(self.logs))

    def test_ping_info_and_utf8_query_round_trip(self):
        self.send('ping', 'bridge.ping', {})
        self.assertEqual(self.result('ping')['data'], {'version': '1'})
        self.house.build = {'display': 'test-release', 'software': 'aabbcc'}
        self.send('info', 'bridge.info', {})
        self.assertEqual(self.result('info')['data']['room'], 'Living')
        self.assertEqual(self.result('info')['data']['build'], 'test-release')
        self.assertEqual(self.result('info')['data']['revision'], 'aabbcc')
        self.assertEqual(self.result('info')['message'],
                         'CR100 Bridge. Build: test-release (aabbcc). Room: Living.')
        self.send(params={'value': 'François & A=B | 25%'})
        self.assertEqual(self.result()['data']['value'], 'François & A=B | 25%')

    def test_bridge_info_message_fits_native_printable_ascii_decoder_and_keeps_exact_data(self):
        self.house.build = {'display': 'release-' + 'x' * 200, 'software': 'a' * 128}
        self.player.config.room = 'François 音楽' + 'x' * 200
        self.send('bounded-info', 'bridge.info', {})
        info = self.result('bounded-info')
        self.assertEqual(info['status'], 'ok')
        self.assertTrue(all(32 <= ord(char) <= 126 for char in info['message']))
        self.assertLessEqual(len(info['message']), 200)
        self.assertIn('Room: Fran?ois ??', info['message'])
        self.assertEqual(info['data']['room'], self.player.config.room[:128])
        self.assertEqual(info['data']['build'], self.house.build['display'][:128])
        self.assertEqual(info['data']['revision'], self.house.build['software'])

    def test_autoplay_write_preserves_config_and_transport_without_restart(self):
        before = [(p.state.uri, p.state.transport, p.state.volume) for p in self.house.players]
        self.send('auto', 'spotify.autoplay.set', {'desired': 'true'})
        self.assertEqual(self.result('auto')['data'], {'desired': 'true', 'scope': 'household', 'saved': 'true'})
        config = json.loads(self.path.read_text())
        self.assertIs(config['spotify_music_service']['autoplay'], True)
        self.assertEqual(config['spotify_music_service']['client_id'], 'keep')
        self.assertEqual(config['unrelated'], 42)
        self.assertEqual([(p.state.uri, p.state.transport, p.state.volume) for p in self.house.players], before)
        self.send('invalid-auto', 'spotify.autoplay.set', {'desired': 'toggle'})
        self.assertEqual(self.result('invalid-auto')['code'], 'invalid_parameters')
        self.assertIs(json.loads(self.path.read_text())['spotify_music_service']['autoplay'], True)

    def test_v3_autoplay_readback_tracks_saved_config_without_mutation(self):
        def read():
            status, document = self.request('GetString', {'VariableName': 'CR100_SpotifyAutoplay'})
            self.assertEqual(status, 200, document)
            return ET.fromstring(document).find('.//StringValue').text
        before = self.path.read_bytes()
        self.assertEqual(read(), 'off')
        self.assertEqual(self.path.read_bytes(), before)
        self.send('on-readback', 'spotify.autoplay.set', {'desired': 'true'})
        self.assertEqual(read(), 'on')
        self.path.write_text(json.dumps({'music_services': []}))
        self.assertEqual(read(), 'unavailable')
        self.path.write_text('invalid json')
        self.assertEqual(read(), 'unavailable')
        self.assertEqual(self.request('SetString', {'VariableName': 'CR100_SpotifyAutoplay', 'StringValue': 'on'})[0], 500)

    def test_v3_room_capabilities_use_current_verified_room_not_virtual_model(self):
        def read():
            status, document = self.request('GetString', {'VariableName': 'CR100_RoomCapabilities'})
            self.assertEqual(status, 200, document)
            return ET.fromstring(document).find('.//StringValue').text
        self.assertEqual(read(), 'v1|')  # simulated ZP80 advertises no real hardware
        caps = {'bass', 'treble', 'channel_volume', 'led', 'line_in_level', 'untrusted'}
        provider = types.SimpleNamespace(capabilities=lambda key: caps if key == 'verified-room' else set())
        room = types.SimpleNamespace(key='verified-room', native_settings_provider=lambda: provider)
        self.player.state.backend = room
        try:
            self.assertEqual(read(), 'v1|balance|bass|led|line_in_level|treble|')
            caps.clear()
            self.assertEqual(read(), 'v1|')  # stale/rebound provider cannot retain old controls
            room.native_settings_provider = lambda: None
            self.assertEqual(read(), 'v1|')
        finally:
            self.player.state.backend = None
        self.assertEqual(self.request('SetString', {'VariableName': 'CR100_RoomCapabilities', 'StringValue': 'v1|bass|'})[0], 500)
        self.assertEqual(self.calls, [])

    def test_autoplay_explicit_provider_config_and_unavailable_service(self):
        self.path.write_text(json.dumps({'music_services': [{'provider': 'spotify', 'autoplay': True}]}))
        self.send('off', 'spotify.autoplay.set', {'desired': 'false'})
        self.assertEqual(self.result('off')['status'], 'ok')
        self.assertIs(json.loads(self.path.read_text())['music_services'][0]['autoplay'], False)
        self.path.write_text(json.dumps({'music_services': []}))
        self.send('missing', 'spotify.autoplay.set', {'desired': 'true'})
        self.assertEqual(self.result('missing')['code'], 'unavailable')
        self.assertEqual(json.loads(self.path.read_text()), {'music_services': []})

    def test_radio_setting_uses_existing_native_route_once_and_confirms_durable_readback(self):
        from native_radio import NativeRadioService
        from radio_catalog import RadioCatalog
        from radio_store import RadioStore
        from radiotime import RadioTimeClient
        def no_external(*args, **kwargs):
            raise AssertionError('UI setting must not contact an external service')
        path = Path(self.temp.name) / 'radio.json'
        store = RadioStore(path)
        catalog = RadioCatalog(RadioTimeClient(no_external), store)
        service = NativeRadioService('127.0.0.1', 0, catalog, log=self.logs.append)
        self.addCleanup(service.close)
        self.house.radio_service = service
        choices = catalog.children('settings:quality')
        chosen = next(item for item in choices if item.metadata['value'] == 'low')
        before = store.snapshot()
        service.get_metadata({'id': 'settings:quality'})
        self.assertEqual(store.snapshot(), before)
        native_calls = []
        original = service.native_action
        def observed(*args, **kwargs):
            native_calls.append(args)
            return original(*args, **kwargs)
        service.native_action = observed
        token = 'F00080000' + chosen.id
        self.send('radio', 'radio.setting.apply', {'token': token})
        self.send('radio', 'radio.setting.apply', {'token': token})
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(native_calls[0][:2], ('SystemProperties', 'SetString'))
        self.assertEqual(native_calls[0][2]['VariableName'], 'CR100_RadioAction')
        self.assertIs(native_calls[0][3], self.player.state)
        result = self.result('radio')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['value'], 'low')
        self.assertEqual(RadioStore(path).settings()['quality'], 'low')
        self.assertEqual(store.favorites(), [])
        self.send('bad-radio', 'radio.setting.apply', {'token': 'station:s1'})
        self.assertEqual(self.result('bad-radio')['code'], 'invalid_parameters')
        self.assertEqual(len(native_calls), 1)

    def test_radio_action_is_unavailable_without_service(self):
        self.send('radio', 'radio.setting.apply', {'token': 'settings-action:refresh'})
        self.assertEqual(self.result('radio')['code'], 'unavailable')

    def test_session_ids_are_fresh_bounded_and_read_only(self):
        before = self.path.read_bytes()
        state = [(p.state.uri, p.state.transport, p.state.volume) for p in self.house.players]
        sessions = []
        for player in (self.player, self.player, self.other):
            status, document = self.request('GetString', {'VariableName': 'CR100_UISession'}, player)
            self.assertEqual(status, 200, document)
            value = ET.fromstring(document).find('.//StringValue').text
            self.assertRegex(value, r'^[0-9a-f]{24}$')
            sessions.append(value)
        self.assertEqual(len(set(sessions)), 3)
        self.assertEqual(self.house.ui_events.receipts, {})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([(p.state.uri, p.state.transport, p.state.volume) for p in self.house.players], state)
        self.assertEqual(self.request('SetString', {'VariableName': 'CR100_UISession', 'StringValue': 'chosen'})[0], 500)

    def test_session_id_read_requires_resolved_controller_identity(self):
        self.identities.clear()
        status, document = self.request('GetString', {'VariableName': 'CR100_UISession'})
        self.assertEqual(status, 500, document)
        self.assertEqual(self.house.ui_events.receipts, {})
        self.assertEqual(self.calls, [])

    def test_malformed_envelopes_fail_before_dispatch(self):
        for wire in ('v2|r|bridge.ping||', 'v1|r|test.setting||value%3D1%26value%3D2',
                     'v1|r|test.setting||bad%3D%2500', 'v1|r|test.setting||bad%3D%25ZZ',
                     'v1|r|test.setting||x%3D' + 'a' * 4096, 'v1|r|bridge.ping|'):
            with self.subTest(wire=wire[:60]):
                self.assertEqual(self.request('SetString', {'VariableName': 'CR100_UIEvent', 'StringValue': wire})[0], 500)
        self.assertEqual(self.calls, [])

    def test_provider_registration_reuses_generic_gateway(self):
        class Provider:
            def ui_event_handlers(self):
                return {'provider.choice': {'handler': lambda context, params: UIResult('Provider done'), 'scope': 'room'}}
        self.house.ui_events.register_provider(Provider())
        self.send('provider', 'provider.choice', {})
        self.assertEqual(self.result('provider')['message'], 'Provider done')
        with self.assertRaises(ValueError): self.house.ui_events.register_provider(Provider())

    def test_receipt_ttl_and_capacity_are_bounded(self):
        ticks = [100.0]
        gateway = UIEventGateway(self.house, capacity=2, ttl=10, clock=lambda: ticks[0])
        self.house.ui_events = gateway
        for index in range(3): self.send(str(index), 'bridge.ping', {})
        self.assertEqual(len(gateway.receipts), 2)
        self.assertEqual(self.result('0')['code'], 'not_found')
        ticks[0] += 11
        self.assertEqual(self.result('2')['code'], 'not_found')
        self.assertEqual(len(gateway.receipts), 0)


if __name__ == '__main__': unittest.main()
