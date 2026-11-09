"""Stock-CR100 SMAPI controls against the public cached Room contract.

Real HTTP is loopback only. Recording rooms represent both playback adapters;
native provider/Sonos HTTP validation has its own settings integration suite.
"""
import concurrent.futures
import copy
import http.client
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from backends import Backend, BackendError, Room
from room_capabilities import RoomBinding
from smapi_stub import SMAPI_NS, SOAP_NS
from speaker_settings import SpeakerSettingsService, SETTINGS
from test_smapi_stub import NS, smapi


A = 'RINCON_000E58AAAAAA01400'
B = 'RINCON_000E58BBBBBB01400'


class CachedBackend(Backend):
    def __init__(self, name):
        super().__init__(lambda _: None)
        self.name, self.calls, self.rooms, self.bindings = name, [], {}, {}
        self.coordinators = {}

    def room(self, key):
        if key not in self.rooms:
            self.rooms[key] = CachedRoom(self, key)
            self.bindings[key] = RoomBinding(platform='sonos', native_id=A,
                native_url='http://192.0.2.10:1400', group_key=('sonos', 'household'))
            self.coordinators[key] = key
        return self.rooms[key]

    def binding(self, key): return self.bindings.get(key, RoomBinding())
    def settings_binding(self, key): return self.binding(key)
    def settings_coordinator(self, key): return self.coordinators.get(key)


class CachedRoom(Room):
    available = True

    def __init__(self, backend, key):
        super().__init__(backend, key)
        self.operations = set(SETTINGS)
        self.data = {'Bass': '0', 'Treble': '-2', 'Loudness': '1', 'LEDState': 'On',
            'SupportsOutputFixed': '1', 'OutputFixed': '0',
            'VolumeChannels': {'LF': '100', 'RF': '100'},
            'RemainingSleepTimerDuration': '', 'CurrentSleepTimerGeneration': '7'}
        self.fail, self.confirm, self.after_call = None, True, None
        self.transport_state = ('PLAYING', 'queue:untouched', 19, False)

    def operation_capabilities(self):
        operations = set(self.operations) if self.available else set()
        if self.data['OutputFixed'] == '1': operations -= {'bass', 'treble', 'loudness', 'channel_volume'}
        return frozenset(operations)

    def settings_values(self): return copy.deepcopy(self.data) if self.available else {}

    def position(self): raise AssertionError('Settings used the transport/network path')
    def action(self, *args): raise AssertionError('Settings used the playback/ordinary Room action path')

    def settings_action(self, service, action, args):
        args = dict(args)
        self.backend.calls.append((self.key, service, action, args))
        if self.fail == (action, args.get('Channel')): raise BackendError('native readback failed')
        if self.confirm:
            if action in ('SetBass', 'SetTreble', 'SetLoudness'):
                name = action[3:]; self.data[name] = args['Desired' + name]
            elif action == 'SetLEDState': self.data['LEDState'] = args['DesiredLEDState']
            elif action == 'SetOutputFixed': self.data['OutputFixed'] = args['DesiredFixed']
            elif action == 'SetVolume': self.data['VolumeChannels'][args['Channel']] = args['DesiredVolume']
            elif action == 'ConfigureSleepTimer':
                self.data['RemainingSleepTimerDuration'] = args['NewSleepTimerDuration']
                self.data['CurrentSleepTimerGeneration'] = str(int(self.data['CurrentSleepTimerGeneration']) + 1)
            else: raise AssertionError('Unexpected native setting')
        if self.after_call: self.after_call(action, args)
        return {}


class SpeakerSettings(unittest.TestCase):
    def setUp(self):
        self.now, self.logs = 100.0, []
        self.sonos, self.ha = CachedBackend('sonos'), CachedBackend('ha')
        self.direct, self.hasonos = self.sonos.room('member'), self.ha.room('media_player.den')
        self.generic = self.ha.room('media_player.generic')
        self.ha.bindings[self.generic.key] = RoomBinding(platform='cast')
        self.rows = [('sonos-member', 'Living Room', self.direct),
                     ('ha-den', 'Den', self.hasonos), ('ha-generic', 'Generic ZP80', self.generic)]
        self.service = SpeakerSettingsService('127.0.0.1', 0, lambda: self.rows,
            log=self.logs.append, clock=lambda: self.now)
        self.service.start()
        self.addCleanup(self.service.close)

    def browse(self, ident='root', **args):
        status, document = smapi(self.service, 'getMetadata', dict(id=ident, index=0, count=100, **args))
        self.assertEqual(status, 200, ET.tostring(document))
        return [(node.findtext('s:id', namespaces=NS), node.findtext('s:title', namespaces=NS))
                for node in document.findall('.//s:mediaCollection', NS) + document.findall('.//s:mediaMetadata', NS)]

    def room_id(self, title='Living Room'):
        return next(ident for ident, name in self.browse() if name == title)

    def folder(self, setting, room='Living Room'):
        return next(ident for ident, _ in self.browse(self.room_id(room)) if ident.endswith(':' + setting))

    def choice(self, setting, title, room='Living Room'):
        return next(ident for ident, name in self.browse(self.folder(setting, room)) if name == title)

    def apply(self, ident, status=200):
        actual, document = smapi(self.service, 'createItem', {'favorite': ident})
        self.assertEqual(actual, status, ET.tostring(document))
        return document

    @staticmethod
    def result(document): return document.findtext('.//s:createItemResult', namespaces=NS)

    def version(self):
        status, document = smapi(self.service, 'getLastUpdate', {})
        self.assertEqual(status, 200)
        version = document.findtext('.//s:catalog', namespaces=NS)
        self.assertGreaterEqual(int(version), 1)
        self.assertLessEqual(int(version), 2147483647)
        return version

    def test_catalogue_digest_high_bit_remains_positive_legacy_signed_integer(self):
        with patch('speaker_settings.hashlib.sha256') as digest:
            digest.return_value.hexdigest.return_value = 'a' * 64
            digest.return_value.digest.return_value = b'\xff' * 32
            self.assertEqual(self.version(), '2')

    def test_http_reads_and_nonplayable_metadata_never_change_room_or_log_credentials(self):
        before = copy.deepcopy(self.direct.data), self.direct.transport_state
        ident = self.choice('bass', 'Bass +4')
        for method in ('getMediaMetadata', 'getExtendedMetadata'):
            status, document = smapi(self.service, method, {'id': ident})
            self.assertEqual(status, 200)
            self.assertEqual(document.findtext('.//s:canPlay', namespaces=NS), 'false')
            self.assertEqual(document.findtext('.//s:canAddToFavorites', namespaces=NS), 'true')
            self.assertEqual(document.findtext('.//s:duration', namespaces=NS), '0')
        for method, args in (('search', {'id': 'search', 'term': 'Room', 'index': 0, 'count': 100}),
                             ('getLastUpdate', {}), ('getScrollIndices', {'id': 'root'})):
            self.assertEqual(smapi(self.service, method, args)[0], 200)
        self.assertEqual((self.direct.data, self.direct.transport_state), before)
        self.assertEqual(self.sonos.calls + self.ha.calls, [])
        self.assertEqual(self.service.requests, [])
        self.assertTrue(all(set(record) == {'event', 'method', 'status'} for record in self.logs[1:]))
        self.assertNotIn('deviceId', repr(self.logs))
        self.assertNotIn(ident, repr(self.logs))

    def test_apply_only_issued_value_then_future_browse_reports_confirmed_value(self):
        ident = self.choice('bass', 'Bass +4')
        version = self.version()
        receipt = self.result(self.apply(ident))
        self.assertTrue(receipt.startswith('ss:receipt:'))
        self.assertEqual(self.sonos.calls, [('member', 'RenderingControl', 'SetBass', {'InstanceID': '0', 'DesiredBass': '4'})])
        self.assertIn(('Bass +4 (current)'), [title for _, title in self.browse(self.folder('bass'))])
        self.assertNotEqual(version, self.version())
        self.assertEqual(self.result(self.apply(ident)), receipt)
        self.assertEqual(len(self.sonos.calls), 1)

    def test_component_policy_scopes_http_catalogue_and_cached_actions(self):
        room = self.room_id()
        ident = self.choice('bass', 'Bass +4')
        requested = []
        def allowed(stable_id, source):
            requested.append((stable_id, source))
            return stable_id == 'ha-den'
        self.service.component_allowed = allowed
        self.assertEqual([title for _, title in self.browse() if title != 'How to apply a setting'], ['Den'])
        for method, args in (('getMetadata', {'id': room, 'index': 0, 'count': 100}),
                             ('getMediaMetadata', {'id': ident}), ('getExtendedMetadata', {'id': ident}),
                             ('createItem', {'favorite': ident})):
            status, document = smapi(self.service, method, args)
            self.assertEqual(status, 500)
            self.assertNotIn(b'Living Room', ET.tostring(document))
        self.assertTrue(requested)
        self.assertTrue(all(source == '127.0.0.1' for _, source in requested))
        self.assertEqual(self.sonos.calls + self.ha.calls, [])
        self.assertIsNone(getattr(self.service.request, 'address', None))

    def test_hidden_receipt_cannot_reveal_metadata_or_replay_success(self):
        ident = self.choice('led', 'Status light Off')
        result = self.result(self.apply(ident))
        self.service.component_allowed = lambda stable_id, address: False
        for method, args in (('getMediaMetadata', {'id': ident}), ('getExtendedMetadata', {'id': ident}),
                             ('createItem', {'favorite': ident})):
            status, document = smapi(self.service, method, args)
            self.assertEqual(status, 500)
            self.assertNotIn(b'Living Room', ET.tostring(document))
            self.assertNotIn(result.encode(), ET.tostring(document))
        self.assertEqual(len(self.sonos.calls), 1)
        self.service.component_allowed = lambda stable_id, address: True
        self.assertEqual(self.result(self.apply(ident)), result)
        self.assertEqual(len(self.sonos.calls), 1)

    def test_dispatch_context_distinguishes_two_controllers(self):
        self.service.component_allowed = lambda stable_id, source: source == '192.168.69.50'
        with self.service.request_context('192.168.69.50'):
            visible = self.service.dispatch('getMetadata', {'id': 'root'})
            with self.service.request_context('192.168.69.51'):
                hidden = self.service.dispatch('getMetadata', {'id': 'root'})
            self.assertIn('Living Room', self.service.dispatch('getMetadata', {'id': 'root'}))
        self.assertIn('Living Room', visible)
        self.assertNotIn('Living Room', hidden)
        self.assertIsNone(getattr(self.service.request, 'address', None))

    def test_two_http_controllers_have_separate_catalogues_and_cannot_use_each_others_cached_tokens(self):
        from test_controller_components import IP_A, IP_B, SourceAddresses
        sources = SourceAddresses([self.service.server], [IP_A, IP_B])
        self.addCleanup(sources.close)
        allowed = {IP_A: 'sonos-member', IP_B: 'ha-den'}
        self.service.component_allowed = lambda stable_id, source: allowed.get(source) == stable_id

        def call(source, method, args):
            root = ET.Element('{%s}Envelope' % SOAP_NS)
            body = ET.SubElement(root, '{%s}Body' % SOAP_NS)
            action = ET.SubElement(body, '{%s}%s' % (SMAPI_NS, method))
            for name, value in args.items(): ET.SubElement(action, '{%s}%s' % (SMAPI_NS, name)).text = str(value)
            connection = sources.connection('127.0.0.1', self.service.port, source)
            try:
                # Claimed identity never replaces the accepted HTTP peer.
                connection.request('POST', '/smapi', ET.tostring(root),
                                   {'Content-Type': 'text/xml', 'X-Forwarded-For': IP_A})
                response = connection.getresponse()
                return response.status, ET.fromstring(response.read())
            finally:
                connection.close()

        def browse(source, ident):
            status, document = call(source, 'getMetadata', {'id': ident, 'index': 0, 'count': 100})
            self.assertEqual(status, 200, ET.tostring(document))
            return {node.findtext('s:title', namespaces=NS): node.findtext('s:id', namespaces=NS)
                    for node in document.findall('.//s:mediaCollection', NS) + document.findall('.//s:mediaMetadata', NS)}

        first, second = browse(IP_A, 'root'), browse(IP_B, 'root')
        self.assertIn('Living Room', first)
        self.assertNotIn('Den', first)
        self.assertIn('Den', second)
        self.assertNotIn('Living Room', second)
        folder = browse(IP_A, first['Living Room'])['Bass: +0']
        ident = browse(IP_A, folder)['Bass +4']
        for method, args in (('getMediaMetadata', {'id': ident}), ('createItem', {'favorite': ident})):
            status, document = call(IP_B, method, args)
            self.assertEqual(status, 500)
            self.assertNotIn(b'Living Room', ET.tostring(document))
        self.assertEqual(self.sonos.calls, [])
        status, document = call(IP_A, 'createItem', {'favorite': ident})
        self.assertEqual(status, 200, ET.tostring(document))
        self.assertEqual(len(self.sonos.calls), 1)
        self.assertEqual(call(IP_B, 'createItem', {'favorite': ident})[0], 500)
        self.assertEqual(len(self.sonos.calls), 1)

    def test_concurrent_duplicate_apply_runs_once_and_receipt_eviction_never_reexecutes(self):
        self.service.receipt_limit = 1
        ident = self.choice('led', 'Status light Off')
        barrier = threading.Barrier(2)
        def send():
            barrier.wait(timeout=3)
            return smapi(self.service, 'createItem', {'favorite': ident})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(self.result(results[0][1]), self.result(results[1][1]))
        self.assertEqual(len(self.sonos.calls), 1)
        other = self.choice('bass', 'Bass +2')
        self.apply(other)
        self.apply(ident, status=500)
        self.assertEqual(len(self.sonos.calls), 2)

    def test_fake_adapter_contract_same_for_direct_sonos_and_ha_sonos_generic_is_informational(self):
        for room in ('Living Room', 'Den'):
            folders = [ident.rpartition(':')[2] for ident, _ in self.browse(self.room_id(room))]
            self.assertEqual(set(folders), {'bass', 'treble', 'loudness', 'led', 'output_fixed', 'balance', 'sleep_timer'})
            self.apply(self.choice('treble', 'Treble +3', room))
        self.assertEqual(self.sonos.calls[0][1:], self.ha.calls[0][1:])
        for binding in (RoomBinding(platform='cast'), RoomBinding(platform='sonos')):
            self.ha.bindings[self.generic.key] = binding
            rows = self.browse(self.room_id('Generic ZP80'))
            self.assertEqual([title for _, title in rows], ['No supported speaker settings available'])
            self.assertEqual(self.browse(rows[0][0]), [])
        self.assertFalse(any(call[0] == self.generic.key for call in self.ha.calls))

    def test_fixed_output_hides_eq_balance_but_keeps_variable_restore(self):
        stale = self.choice('bass', 'Bass +2')
        self.apply(self.choice('output_fixed', 'Line-out Fixed'))
        folders = self.browse(self.room_id())
        self.assertFalse(any(ident.endswith(('bass', 'treble', 'loudness', 'balance')) for ident, _ in folders))
        self.assertTrue(any(title == 'Line-out: Fixed' for _, title in folders))
        self.apply(stale, status=500)
        self.apply(self.choice('output_fixed', 'Line-out Variable'))
        self.assertTrue(any(ident.endswith(':bass') for ident, _ in self.browse(self.room_id())))

    def test_balance_uses_two_absolute_member_channel_calls_and_preserves_transport(self):
        before = self.direct.transport_state
        self.apply(self.choice('balance', 'Balance Right +50'))
        self.assertEqual(self.sonos.calls, [
            ('member', 'RenderingControl', 'SetVolume', {'InstanceID': '0', 'Channel': 'LF', 'DesiredVolume': '50'}),
            ('member', 'RenderingControl', 'SetVolume', {'InstanceID': '0', 'Channel': 'RF', 'DesiredVolume': '100'})])
        self.assertEqual(self.direct.transport_state, before)
        self.assertIn('Balance: Right +50', [name for _, name in self.browse(self.room_id())])

    def test_partial_balance_failure_is_deduplicated_and_reports_actual_readback(self):
        self.direct.fail = ('SetVolume', 'RF')
        ident = self.choice('balance', 'Balance Right +50')
        document = self.apply(ident, status=500)
        self.assertIn('may have changed', document.findtext('.//faultstring'))
        self.apply(ident, status=500)
        self.assertEqual(len(self.sonos.calls), 2)
        self.assertEqual(self.direct.data['VolumeChannels'], {'LF': '50', 'RF': '100'})

    def test_rebind_between_balance_channels_never_writes_second_target(self):
        def rebind(action, args):
            self.sonos.bindings['member'] = RoomBinding(platform='sonos', native_id=B, native_url='http://192.0.2.11:1400')
        self.direct.after_call = rebind
        self.apply(self.choice('balance', 'Balance Right +50'), status=500)
        self.assertEqual(len(self.sonos.calls), 1)

    def test_timer_countdown_does_not_expire_actions_or_churn_catalogue_generation_does(self):
        self.direct.data['RemainingSleepTimerDuration'] = '0:10:00'
        ident, version = self.choice('sleep_timer', 'Sleep after 15 minutes'), self.version()
        self.direct.data['RemainingSleepTimerDuration'] = '0:09:59'
        self.now += 1
        self.assertEqual(self.version(), version)
        self.apply(ident)
        self.assertEqual(self.direct.data['RemainingSleepTimerDuration'], '0:15:00')
        stale = self.choice('sleep_timer', 'Sleep after 30 minutes')
        self.direct.data['CurrentSleepTimerGeneration'] = '99'
        self.apply(stale, status=500)
        self.apply(self.choice('sleep_timer', 'Cancel sleep timer'))
        self.assertEqual(self.sonos.calls[-1][3]['NewSleepTimerDuration'], '')

    def test_timer_coordinator_change_and_missing_coordinator_withdraw_cached_action(self):
        leader = self.sonos.room('leader')
        self.sonos.bindings['leader'] = RoomBinding(platform='sonos', native_id=B, native_url='http://192.0.2.11:1400')
        ident = self.choice('sleep_timer', 'Sleep after 15 minutes')
        self.sonos.coordinators['member'] = leader.key
        self.apply(ident, status=500)
        self.sonos.coordinators['member'] = None
        self.assertFalse(any(ident.endswith(':sleep_timer') for ident, _ in self.browse(self.room_id())))
        self.assertEqual(self.sonos.calls, [])

    def test_stale_unissued_altered_unavailable_changed_binding_or_state_never_write(self):
        self.apply('ss:a:invented', status=500)
        mutations = (
            lambda: self.sonos.bindings.update(member=RoomBinding(platform='sonos', native_id=B, native_url='http://192.0.2.11:1400')),
            lambda: self.direct.operations.discard('bass'),
            lambda: self.direct.data.update(Bass='5'),
            lambda: setattr(self.direct, 'available', False),
            lambda: self.rows.clear(),
            lambda: setattr(self, 'now', self.now + self.service.action_ttl),
        )
        for mutate in mutations:
            self.rows = [('sonos-member', 'Living Room', self.direct)]
            self.direct.available, self.direct.operations, self.direct.data['Bass'] = True, set(SETTINGS), '0'
            self.sonos.bindings['member'] = RoomBinding(platform='sonos', native_id=A, native_url='http://192.0.2.10:1400')
            ident = self.choice('bass', 'Bass +4')
            self.apply(ident + 'altered', status=500)
            mutate()
            self.apply(ident, status=500)
        self.assertEqual(self.sonos.calls, [])

    def test_unconfirmed_backend_and_native_failure_never_publish_success_and_are_deduplicated(self):
        self.direct.confirm = False
        ident = self.choice('bass', 'Bass +4')
        self.apply(ident, status=500)
        self.apply(ident, status=500)
        self.assertEqual(self.direct.data['Bass'], '0')
        self.assertEqual(len(self.sonos.calls), 1)
        self.direct.confirm, self.direct.fail = True, ('SetTreble', None)
        self.apply(self.choice('treble', 'Treble +4'), status=500)
        self.assertEqual(self.direct.data['Treble'], '-2')

    def test_action_and_receipt_tables_are_bounded_and_expired_tokens_cannot_apply(self):
        self.service.action_limit, self.service.receipt_limit = 5, 2
        stale = self.choice('bass', 'Bass +10')
        self.browse(self.folder('treble'))
        self.assertLessEqual(len(self.service.actions), 5)
        self.assertLessEqual(len(self.service.action_keys), 5)
        self.apply(stale, status=500)
        ident = self.choice('led', 'Status light Off')
        self.now += self.service.action_ttl
        self.apply(ident, status=500)
        self.assertFalse(self.service.actions)
        self.assertFalse(self.service.action_keys)
        self.assertEqual(self.sonos.calls, [])

    def test_play_delete_and_wrong_create_arguments_fail_cleanly(self):
        ident = self.choice('bass', 'Bass +4')
        for method in ('getMediaURI', 'deleteItem'):
            self.assertEqual(smapi(self.service, method, {'id': ident})[0], 500)
        for args in ({'favorite': 'root'}, {'id': ident}, {'favorite': ident, 'title': 'forged'}):
            self.assertEqual(smapi(self.service, 'createItem', args)[0], 500)
        self.assertTrue(self.service.preflight_playback)
        with self.assertRaises(ValueError): self.service.resolve_playback('ss:a:anything')
        self.assertTrue(self.service.owns('x-sonos-http:ss%3aa%3aforged.mp3?sid=244'))
        self.assertFalse(self.service.owns('x-sonos-http:spotify%3atrack%3aid.mp3?sid=240'))
        self.assertFalse(self.service.owns('http://music.invalid/bass:track.mp3'))
        self.assertEqual(self.sonos.calls, [])

    def test_http_malformed_or_duplicate_argument_faults_do_not_mutate(self):
        ident = self.choice('bass', 'Bass +4')
        malformed = (b'<!DOCTYPE x><x/>', b'<wrong/>',
            ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
             '<createItem xmlns="%s"><favorite>%s</favorite><favorite>%s</favorite></createItem>'
             '</s:Body></s:Envelope>' % (SMAPI_NS, ident, ident)).encode())
        for body in malformed:
            connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=3)
            connection.request('POST', '/smapi', body, {'Content-Type': 'text/xml'})
            response = connection.getresponse()
            self.assertEqual(response.status, 500)
            self.assertEqual(ET.fromstring(response.read()).findtext('.//faultcode'), 's:Client.InvalidRequest')
            connection.close()
        self.assertEqual(self.sonos.calls, [])


if __name__ == '__main__': unittest.main()
