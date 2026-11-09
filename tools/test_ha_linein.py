"""HA registry -> AudioIn discovery, and line-in actions/state through HA."""
import unittest
import copy
import html
import io
from types import SimpleNamespace

import ha_backend
from backends import BackendError
from ha_linein import SonosLineIn, source_uuid
from sonos_identity import IdentityInventory

KITCHEN, PATIO = 'media_player.kitchen', 'media_player.patio'
UUID1, UUID2 = 'RINCON_000E5800000101400', 'RINCON_000E5800000201400'
BASE1, BASE2 = 'http://10.0.0.130:1400', 'http://10.0.0.131:1400'


class HA:
    def __init__(self):
        self.listeners, self.calls = [], []
        self.states = {entity: {'state': 'playing', 'attributes': {'friendly_name': name,
            'supported_features': ha_backend.PLAY_MEDIA | ha_backend.PLAY | ha_backend.STOP | ha_backend.SEEK | ha_backend.NEXT,
            'group_members': [entity], 'media_title': 'Line-in'}}
            for entity, name in ((KITCHEN, 'Kitchen'), (PATIO, 'Patio'))}
        self.entities = [{'entity_id': entity, 'platform': 'sonos', 'device_id': str(index), 'unique_id': uuid}
                         for index, (entity, uuid) in enumerate(((KITCHEN, UUID1), (PATIO, UUID2)))]
        self.devices = [{'id': str(index), 'configuration_url': base + '/support/review'}
                        for index, base in enumerate((BASE1, BASE2))]
        self.closed = False

    def call(self, payload, timeout):
        return self.entities if payload['type'] == 'config/entity_registry/list' else self.devices

    def call_service(self, domain, service, entity, data=None, **kwargs):
        self.calls.append((domain, service, entity, data))

    def state(self, entity): return self.states.get(entity, {})
    def close(self): self.closed = True


class Listener:
    def __init__(self): self.subscriptions, self.removed, self.closed = [], [], False
    def subscribe(self, base, service, callback):
        item = (base, service, callback)
        self.subscriptions.append(item)
        callback({'AudioInputName': 'Turntable' if base == BASE1 else 'CD Player',
                  'Icon': 'Phonograph', 'LineInConnected': '1' if base == BASE1 else '0'})
        return SimpleNamespace(healthy=True)
    def unsubscribe(self, subscription): self.removed.append(subscription)
    def close(self): self.closed = True


class Inventory:
    """A cached reader: line-in unit tests must never contact their example LAN."""
    def __init__(self):
        self.seeds, self.identities, self.bonded, self.refreshes = {}, {}, {}, 0
        self.pending_changes = set()

    def refresh(self, seeds):
        self.refreshes += 1
        self.seeds = copy.deepcopy(seeds)
        changed, self.pending_changes = self.pending_changes, set()
        return changed

    def identity(self, entity):
        return copy.deepcopy(self.identities.get(entity) or (self.seeds.get(entity) or {}).get('fallback'))

    def bonded_devices(self, entity): return copy.deepcopy(self.bonded.get(entity) or [])


class Inputs(unittest.TestCase):
    def setUp(self):
        self.ha, self.listener, self.changes, self.reads = HA(), Listener(), [], []
        self.inventory = Inventory()
        self.inputs = SonosLineIn(self.ha, lambda: self.changes.append(True), lambda item: None,
                                 self.listener, self.read, self.inventory)

    def read(self, base, timeout):
        self.reads.append((base, timeout))
        return {'name': 'Audio Component', 'icon': 'AudioComponent'}

    def test_connection_is_authoritative_audioin_event_and_cross_room_identity_is_kept(self):
        self.assertTrue(self.inputs.refresh())
        self.assertEqual(self.inputs.sources(), [{'id': KITCHEN, 'name': 'Turntable', 'room_name': 'Kitchen',
                                                 'icon': 'Phonograph', 'uri': 'x-rincon-stream:' + UUID1}])
        self.assertEqual(self.inputs.entity(UUID1), KITCHEN)
        self.assertEqual(self.inputs.uuid(PATIO), UUID2)
        self.listener.subscriptions[0][2]({'LineInConnected': '0'})
        self.assertEqual(self.inputs.sources(), [])
        self.listener.subscriptions[1][2]({'LineInConnected': '1', 'AudioInputName': 'Tape Deck'})
        self.assertEqual(self.inputs.sources()[0]['name'], 'Tape Deck')
        self.assertEqual(len(self.changes), 5)  # initial event x2, registry, cable x2
        self.inputs.refresh()
        self.assertEqual(len(self.listener.subscriptions), 2)

    def test_registry_removal_and_address_change_remove_old_connection(self):
        self.inputs.refresh()
        self.ha.entities.pop()
        self.ha.devices[0]['configuration_url'] = 'http://10.0.0.132:1400/support/review'
        self.inputs.refresh()
        self.assertEqual(len(self.listener.removed), 2)
        self.assertEqual(len(self.listener.subscriptions), 3)
        self.assertEqual(self.inputs.sources(), [])  # new endpoint's initial event says disconnected
        self.inputs.close()
        self.assertTrue(self.listener.closed)

    def test_only_sonos_media_players_with_registry_origins_are_contacted(self):
        self.ha.entities[0]['platform'] = 'other'
        self.ha.devices[1]['configuration_url'] = 'http://10.0.0.131:1400.evil.test/'
        self.inputs.refresh()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.listener.subscriptions, [])

    def test_unavailable_and_expired_sources_are_withdrawn(self):
        self.inputs.refresh()
        self.ha.states[KITCHEN]['state'] = 'unavailable'
        self.assertEqual(self.inputs.sources(), [])
        self.ha.states[KITCHEN]['state'] = 'playing'
        self.inputs.subscriptions[UUID1][1].healthy = False
        self.assertEqual(self.inputs.sources(), [])

    def test_source_uri_is_exact_and_not_a_tv_or_arbitrary_uri(self):
        self.assertEqual(source_uuid('x-rincon-stream:' + UUID1), UUID1)
        for uri in (None, '', 'x-sonos-htastream:' + UUID1 + ':spdif',
                    'x-rincon-stream:' + UUID1 + '?query', 'x-rincon-stream:http://other/'):
            self.assertIsNone(source_uuid(uri))


class Identity(unittest.TestCase):
    def setUp(self):
        self.ha, self.inventory, self.listener, self.changes, self.logs = HA(), Inventory(), Listener(), [], []
        self.inputs = SonosLineIn(self.ha, lambda: self.changes.append(True), self.logs.append,
                                 listener=self.listener, inventory=self.inventory, reader=self.no_input)

    @staticmethod
    def no_input(base, timeout):
        from sonos_upnp import UPnPFault
        raise UPnPFault('GetAudioInputAttributes', 401)

    def test_sonos_identity_does_not_require_audioin_and_getters_only_read_cache(self):
        self.inventory.identities[KITCHEN] = {'id': UUID1, 'name': 'Kitchen', 'model': 'Sonos Era 100',
            'software': '85.0-63100', 'display': '16.1', 'hardware': '1.53.1.4-1.2',
            'serial': '00-11-22-33-44-55:A', 'ip': '10.0.0.130', 'extra': ''}
        self.inventory.bonded[KITCHEN] = [{'id': 'RINCON_000E5800000301400', 'name': 'Kitchen Sub',
                                         'model': 'Sonos Sub', 'software': '85.0-63100'}]
        self.assertTrue(self.inputs.refresh())
        self.assertEqual(self.listener.subscriptions, [])
        self.assertEqual(self.inputs.sources(), [])
        self.inputs.start = lambda: None
        backend = ha_backend.HABackend(self.ha, self.logs.append, line_in=self.inputs)
        try:
            for _ in range(3):
                identity = backend.identity(KITCHEN)
                self.assertEqual(identity['software'], '85.0-63100')
                self.assertEqual(identity['model'], 'Sonos Era 100')
                self.assertEqual(backend.bonded_devices(KITCHEN)[0]['model'], 'Sonos Sub')
                identity['model'] = 'Changed caller copy'
            self.assertEqual(self.inventory.refreshes, 1)
            self.assertEqual(self.inventory.seeds[KITCHEN]['uuid'], UUID1)
            self.assertEqual(self.inventory.seeds[KITCHEN]['base'], BASE1)
        finally:
            backend.close()

    def test_registry_metadata_is_a_truthful_fallback_without_an_endpoint(self):
        self.ha.devices[0].update(model='Sonos One', sw_version='11.16.1', hw_version='1.8',
                                  serial_number='00-11-22-33-44-55:A')
        self.ha.devices[0].pop('configuration_url')
        self.assertTrue(self.inputs.refresh())
        identity = self.inputs.identity(KITCHEN)
        self.assertEqual((identity['model'], identity['display'], identity['hardware']),
                         ('Sonos One', '11.16.1', '1.8'))
        self.assertEqual(identity['serial'], '00-11-22-33-44-55:A')
        self.assertEqual(identity['software'], '')  # HA's friendly release is not a firmware build.
        self.assertEqual(identity['ip'], '')
        self.assertIsNone(self.inventory.seeds[KITCHEN]['base'])
        self.assertEqual(self.inputs.bonded_devices(KITCHEN), [])

    def test_non_sonos_is_virtual_and_registry_removal_withdraws_cached_hardware(self):
        self.inventory.identities[KITCHEN] = {'id': UUID1, 'model': 'Sonos One'}
        self.inventory.bonded[KITCHEN] = [{'id': 'sub', 'model': 'Sonos Sub'}]
        self.ha.entities[1]['platform'] = 'cast'
        self.inputs.refresh()
        self.assertIsNone(self.inputs.identity(PATIO))
        self.assertEqual(self.inputs.bonded_devices(PATIO), [])
        self.ha.entities = [self.ha.entities[1]]
        self.inputs.refresh()
        self.assertIsNone(self.inputs.identity(KITCHEN))
        self.assertEqual(self.inputs.bonded_devices(KITCHEN), [])

    def test_identity_changes_notify_even_when_registry_and_inputs_are_unchanged(self):
        self.inputs.refresh()
        self.changes.clear()
        self.inventory.pending_changes.add(KITCHEN)
        self.inputs.refresh()
        self.assertEqual(self.changes, [True])

    def test_identity_retries_use_cached_seeds_without_repolling_ha_registries(self):
        self.inputs.refresh()
        self.changes.clear()
        self.inventory.pending_changes.add(KITCHEN)
        def fail(payload, timeout): raise AssertionError('The registry must not be called')
        self.ha.call = fail
        self.inputs.refresh_identities()
        self.assertEqual(self.inventory.refreshes, 2)
        self.assertEqual(self.inventory.seeds[KITCHEN]['uuid'], UUID1)
        self.assertEqual(self.changes, [True])

    def test_registry_failure_keeps_last_known_identity_without_logging_private_content(self):
        self.inputs.refresh()
        previous = self.inputs.identity(KITCHEN)
        def fail(payload, timeout): raise RuntimeError('private token value')
        self.ha.call = fail
        self.assertFalse(self.inputs.refresh())
        self.assertEqual(self.inputs.identity(KITCHEN), previous)
        self.assertNotIn('private token value', str(self.logs))

    def test_registry_anchor_reads_real_metadata_and_bonds_without_an_audioin_service(self):
        sub = 'RINCON_000E5800000301400'
        sub_base = 'http://10.0.0.132:1400'
        self.ha.entities[0].pop('unique_id')
        self.ha.devices[0].update(identifiers=[['sonos', UUID1]], model='Old registry label', sw_version='15.0')
        self.ha.entities[1]['platform'] = 'cast'
        topology = ('<ZoneGroups><ZoneGroup Coordinator="%s" ID="%s:1">'
            '<ZoneGroupMember UUID="%s" Location="%s/xml/device_description.xml" ZoneName="Kitchen">'
            '<Satellite UUID="%s" Location="%s/xml/device_description.xml" ZoneName="Kitchen Sub" Invisible="1"/>'
            '</ZoneGroupMember><ZoneGroupMember UUID="%s" Location="%s/xml/device_description.xml" ZoneName="Patio"/>'
            '</ZoneGroup></ZoneGroups>') % (UUID1, UUID1, UUID1, BASE1, sub, sub_base, UUID2, BASE2)
        requests = []
        software = ['85.0-63100']

        class Opener:
            def open(self, request, timeout):
                url = request if isinstance(request, str) else request.full_url
                requests.append(url)
                if url == BASE1 + '/ZoneGroupTopology/Control':
                    return io.BytesIO(('<Envelope><Body><ZoneGroupState>%s</ZoneGroupState></Body></Envelope>'
                                       % html.escape(topology)).encode())
                uuid, model = (UUID1, 'Sonos Era 100') if url.startswith(BASE1) else (sub, 'Sonos Sub')
                return io.BytesIO(('<root><device><UDN>uuid:%s</UDN><modelName>%s</modelName>'
                    '<softwareVersion>%s</softwareVersion><displayVersion>16.1</displayVersion>'
                    '<hardwareVersion>1.53.1.4-1.2</hardwareVersion><serialNum>00-11-22-33-44-55:A</serialNum>'
                    '</device></root>' % (uuid, model, software[0])).encode())

        # Keep the deliberately private example origins within this fake reader.
        now = [0]
        self.inputs.inventory = IdentityInventory(self.logs.append, opener=Opener(), clock=lambda: now[0])
        self.assertTrue(self.inputs.refresh())
        identity = self.inputs.identity(KITCHEN)
        self.assertEqual((identity['model'], identity['display'], identity['software']),
                         ('Sonos Era 100', '16.1', '85.0-63100'))
        self.assertEqual((identity['hardware'], identity['serial'], identity['ip']),
                         ('1.53.1.4-1.2', '00-11-22-33-44-55:A', '10.0.0.130'))
        self.assertEqual([device['id'] for device in self.inputs.bonded_devices(KITCHEN)], [sub])
        self.assertIsNone(self.inputs.identity(PATIO))
        self.assertEqual(self.listener.subscriptions, [])
        self.assertNotIn(BASE2 + '/xml/device_description.xml', requests)
        count = len(requests)
        self.inputs.identity(KITCHEN)
        self.inputs.bonded_devices(KITCHEN)
        self.assertEqual(len(requests), count)
        software[0], now[0] = '85.0-63101', 61
        self.inputs.refresh_identities()
        self.assertEqual(self.inputs.identity(KITCHEN)['software'], '85.0-63101')
        self.ha.devices[0].pop('configuration_url')
        count = len(requests)
        self.inputs.refresh()
        self.assertEqual(self.inputs.identity(KITCHEN)['model'], 'Sonos Era 100')
        self.assertEqual(self.inputs.identity(KITCHEN)['software'], '85.0-63101')
        self.assertEqual(self.inputs.bonded_devices(KITCHEN)[0]['model'], 'Sonos Sub')
        self.assertEqual(len(requests), count)


class Room(unittest.TestCase):
    def setUp(self):
        self.ha, self.listener = HA(), Listener()
        self.inputs = SonosLineIn(self.ha, lambda: None, lambda item: None, self.listener,
                                 lambda base, timeout: {'name': 'Audio Component'}, Inventory())
        self.inputs.refresh()
        self.inputs.start = lambda: None
        self.backend = ha_backend.HABackend(self.ha, lambda item: None, line_in=self.inputs)
        self.room = self.backend.room(PATIO)

    def tearDown(self): self.backend.close()

    def test_remote_input_plays_through_ha_without_enqueue_or_queue_mutation(self):
        self.room.play_line_in(KITCHEN)
        self.assertEqual(self.ha.calls, [('media_player', 'play_media', PATIO,
                                        {'media_content_id': 'x-rincon-stream:' + UUID1,
                                         'media_content_type': 'music'})])
        with self.assertRaises(BackendError): self.room.play_line_in(PATIO)

    def test_self_and_remote_playing_input_are_not_queue_tracks_or_seekable(self):
        for uuid, expected in ((UUID1, KITCHEN), (UUID2, PATIO)):
            self.ha.states[PATIO]['attributes'].update(media_content_id='x-rincon-stream:' + uuid,
                media_position=500, queue_position=4, queue_size=20)
            self.assertEqual(self.room.line_in_source(), expected)
            self.assertEqual(self.room.queue_numbers(), (0, 0))
            self.assertEqual(self.room.position(), 0)
            self.assertEqual(self.room.capabilities(), frozenset())
        self.room.pause()
        self.assertEqual(self.ha.calls[-1][1], 'media_stop')
        self.ha.states[PATIO]['state'] = 'paused'
        self.assertEqual(self.room.transport, 'STOPPED')
        values = self.room.values('virtual-room', 'x-rincon-stream:virtual-source')
        self.assertEqual(values['AVTransportURI'], 'x-rincon-stream:virtual-source')
        self.assertEqual(values['CurrentTrackMetaData'], 'NOT_IMPLEMENTED')
        self.assertEqual(values['RelTime'], 'NOT_IMPLEMENTED')
        self.assertEqual(values['CurrentTransportActions'], 'Play,Stop,Pause')
        self.room.use_queue()
        self.assertEqual(self.ha.calls[-1], ('sonos', 'play_queue', PATIO, {'queue_position': 3}))
        self.ha.states[PATIO]['attributes']['media_content_id'] = 'x-sonos-spotify:track'
        self.assertIsNone(self.room.line_in_source())
        self.assertIn('seek', self.room.capabilities())
        self.ha.states[PATIO]['attributes'].update(media_content_id=None, source='Line-in')
        self.assertIsNone(self.room.line_in_source())  # the label must not invent a local source


if __name__ == '__main__': unittest.main()
