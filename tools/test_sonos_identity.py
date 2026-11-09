"""Offline identities: explicit bonds, endpoint boundaries and bounded cache refresh."""
import html
import io
import threading
import unittest
from types import SimpleNamespace

from sonos_identity import ERROR_INTERVAL, REFRESH_INTERVAL, IdentityInventory, topology_inventory

A, B, C, D, E = ('RINCON_%s01400' % letter for letter in 'ABCDE')
BASES = {uuid: 'http://10.0.0.%d:1400' % number for number, uuid in enumerate((A, B, C, D, E), 10)}


def member(uuid, extra='', children=''):
    return '<ZoneGroupMember UUID="%s" Location="%s/xml/device_description.xml" ZoneName="Room %s" %s>%s</ZoneGroupMember>' \
        % (uuid, BASES[uuid], uuid, extra, children)


def topology(children=True):
    satellite = '<Satellite UUID="%s" Location="%s/xml/device_description.xml" ZoneName="Cinema" Invisible="1"/>' \
        % (C, BASES[C]) if children else ''
    return '<ZoneGroups><ZoneGroup Coordinator="%s" ID="%s:1">%s%s%s%s</ZoneGroup></ZoneGroups>' % (
        B, B, member(A, 'ChannelMapSet="%s:LF,LF;%s:RF,RF"' % (A, D), satellite), member(B),
        member(D, 'Invisible="1"'), member(E, 'Invisible="1"'))


def description(uuid, version='57.23-74170', display='11.16.1'):
    return ('<root xmlns="urn:schemas-upnp-org:device-1-0"><device><UDN>uuid:%s</UDN>'
            '<roomName>Original Room</roomName><modelName>Sonos Model %s</modelName>'
            '<softwareVersion>%s</softwareVersion><displayVersion>%s</displayVersion>'
            '<hardwareVersion>1.%s</hardwareVersion><serialNum>00-11-22-%s:1</serialNum>'
            '<extraVersion>extra %s</extraVersion></device></root>') % (uuid, uuid, version, display, uuid, uuid, uuid)


class Opener:
    def __init__(self):
        self.calls, self.responses = [], {}
        for uuid, base in BASES.items(): self.responses[base + '/xml/device_description.xml'] = description(uuid)
        self.set_topology(topology())

    def set_topology(self, text):
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                '<GetZoneGroupStateResponse><ZoneGroupState>%s</ZoneGroupState>'
                '</GetZoneGroupStateResponse></s:Body></s:Envelope>') % html.escape(text)
        for base in BASES.values(): self.responses[base + '/ZoneGroupTopology/Control'] = body

    def open(self, request, timeout):
        url = request if isinstance(request, str) else request.full_url
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception): raise response
        return io.BytesIO(response.encode() if isinstance(response, str) else response)


class Inventory(unittest.TestCase):
    def setUp(self):
        self.now, self.logs, self.opener = 0, [], Opener()
        self.inventory = IdentityInventory(self.logs.append, self.opener, lambda: self.now)
        self.seeds = {'cinema': {'uuid': A, 'base': BASES[A], 'room_name': 'Cinema'}}

    def test_selected_room_includes_explicit_bonds_but_not_grouped_unselected_room(self):
        self.assertEqual(self.inventory.refresh(self.seeds), {'cinema'})
        root = self.inventory.identity('cinema')
        self.assertEqual((root['id'], root['name'], root['model'], root['serial'], root['ip']),
                         (A, 'Cinema', 'Sonos Model ' + A, '00-11-22-%s:1' % A, '10.0.0.10'))
        self.assertEqual([device['id'] for device in self.inventory.bonded_devices('cinema')], [C, D])
        self.assertNotIn(BASES[B] + '/xml/device_description.xml', self.opener.calls)
        self.assertNotIn(BASES[E] + '/xml/device_description.xml', self.opener.calls)
        self.assertIsNone(self.inventory.identity('unselected'))
        root['model'] = 'mutated caller copy'
        self.assertEqual(self.inventory.identity('cinema')['model'], 'Sonos Model ' + A)
        children = self.inventory.bonded_devices('cinema'); children[0]['model'] = 'mutated'
        self.assertEqual(self.inventory.bonded_devices('cinema')[0]['model'], 'Sonos Model ' + C)

    def test_every_bonded_product_uses_its_own_versions_not_the_playback_coordinator(self):
        self.opener.responses[BASES[C] + '/xml/device_description.xml'] = description(C, '80.1-12345', '16.2')
        self.opener.responses[BASES[D] + '/xml/device_description.xml'] = description(D, '81.2-67890', '17.0')
        self.inventory.refresh(self.seeds)
        children = {device['id']: device for device in self.inventory.bonded_devices('cinema')}
        self.assertEqual((children[C]['software'], children[C]['display']), ('80.1-12345', '16.2'))
        self.assertEqual((children[D]['software'], children[D]['display']), ('81.2-67890', '17.0'))

    def test_cache_avoids_http_and_survives_failures_then_refreshes_removed_bonds(self):
        self.inventory.refresh(self.seeds)
        original = self.inventory.identity('cinema')
        count = len(self.opener.calls)
        self.now = 1
        self.assertEqual(self.inventory.refresh(self.seeds), set())
        self.assertEqual(len(self.opener.calls), count)
        saved = dict(self.opener.responses)
        self.opener.responses = {url: OSError('offline') for url in saved}
        self.now = REFRESH_INTERVAL + 1
        self.inventory.refresh(self.seeds)
        self.assertEqual(self.inventory.identity('cinema'), original)
        self.assertEqual(len(self.inventory.bonded_devices('cinema')), 2)
        count = len(self.opener.calls)
        self.now += 1
        self.inventory.refresh(self.seeds)
        self.assertEqual(len(self.opener.calls), count)
        self.opener.responses = saved
        self.opener.set_topology(topology(children=False))
        self.opener.responses[BASES[A] + '/xml/device_description.xml'] = description(A, '99.1-12345', '20.0')
        self.now += ERROR_INTERVAL
        self.assertEqual(self.inventory.refresh(self.seeds), {'cinema'})
        self.assertEqual(self.inventory.identity('cinema')['software'], '99.1-12345')
        self.assertEqual([device['id'] for device in self.inventory.bonded_devices('cinema')], [D])
        self.assertEqual(self.inventory.refresh({}), {'cinema'})
        self.assertIsNone(self.inventory.identity('cinema'))

    def test_registry_fallback_is_used_without_contacting_unapproved_endpoints(self):
        for base in ('http://127.0.0.1:1400', 'http://10.0.0.10:1234', 'https://10.0.0.10:1400',
                     'http://user:secret@10.0.0.10:1400', 'http://10.0.0.10:1400.evil.test',
                     'http://example.com:1400', None):
            self.inventory.refresh({'cinema': {'uuid': A, 'base': base, 'room_name': 'Cinema',
                                    'fallback': {'model': 'Sonos Arc', 'display': '17.0'}}})
            self.assertEqual(self.inventory.identity('cinema')['model'], 'Sonos Arc')
            self.assertEqual(self.inventory.identity('cinema')['software'], '')
        self.assertEqual(self.opener.calls, [])

    def test_virtual_bridge_is_not_reimported_even_if_registry_provides_a_fallback(self):
        self.inventory.refresh({'bridge': {'uuid': 'RINCON_02690000000101400', 'base': BASES[A],
                                          'fallback': {'model': 'CR100 Bridge'}}})
        self.assertIsNone(self.inventory.identity('bridge'))
        self.assertEqual(self.opener.calls, [])

    def test_wrong_udn_and_oversize_xml_do_not_replace_last_good_metadata(self):
        self.inventory.refresh(self.seeds)
        original = self.inventory.identity('cinema')
        self.opener.responses[BASES[A] + '/xml/device_description.xml'] = description(B)
        self.now = REFRESH_INTERVAL + 1
        self.inventory.refresh(self.seeds)
        self.assertEqual(self.inventory.identity('cinema'), original)
        self.opener.responses[BASES[A] + '/xml/device_description.xml'] = b'x' * (1024 * 1024 + 1)
        self.now += ERROR_INTERVAL + 1
        self.inventory.refresh(self.seeds)
        self.assertEqual(self.inventory.identity('cinema'), original)

    def test_request_budget_is_fair_even_when_caller_runs_at_the_refresh_interval(self):
        self.inventory = IdentityInventory(self.logs.append, self.opener, lambda: self.now, max_requests=2)
        seeds = {uuid: {'uuid': uuid, 'base': BASES[uuid], 'room_name': uuid} for uuid in (A, B, E)}
        for _ in range(8):
            before = len(self.opener.calls)
            self.inventory.refresh(seeds)
            self.assertLessEqual(len(self.opener.calls) - before, 2)
            self.now += REFRESH_INTERVAL
        for uuid in (A, B, E): self.assertTrue(self.inventory.identity(uuid)['model'], uuid)
        self.assertTrue(all(value['model'] for value in self.inventory.bonded_devices(A)))


class Topology(unittest.TestCase):
    def test_home_theater_channel_map_and_nested_satellite_parent_are_explicit(self):
        text = '<ZoneGroups><ZoneGroup Coordinator="%s" ID="group">%s%s%s</ZoneGroup></ZoneGroups>' % (
            A, member(A, 'HTSatChanMapSet="%s:LF,RF;%s:SW"' % (A, C)),
            member(C, 'Invisible="1"'), member(B))
        records, bonds = topology_inventory(text)
        self.assertEqual(bonds[A], {C})
        self.assertEqual(bonds[B], set())
        self.assertEqual(records[C]['base'], BASES[C])

    def test_bridge_recursion_and_unrelated_cross_group_channel_map_are_ignored(self):
        bridge = 'RINCON_02690000000101400'
        text = '<ZoneGroups><ZoneGroup Coordinator="%s" ID="one">%s%s</ZoneGroup>' \
               '<ZoneGroup Coordinator="%s" ID="two">%s</ZoneGroup></ZoneGroups>' % (
                   A, member(A, 'ChannelMapSet="%s:LF;%s:RF"' % (A, B)),
                   '<ZoneGroupMember UUID="%s" Location="http://10.0.0.2:1400/xml/device_description.xml"/>' % bridge,
                   B, member(B))
        records, bonds = topology_inventory(text)
        self.assertNotIn(bridge, records)
        self.assertEqual(bonds[A], set())


class BackendWorker(unittest.TestCase):
    def test_plain_sonos_identity_remains_sonos_before_discovery_and_while_offline(self):
        import sonos_backend
        inventory = SimpleNamespace(identity=lambda key: None)
        backend = sonos_backend.SonosBackend(listener=SimpleNamespace(close=lambda: None),
                                             identity_inventory=inventory, log=lambda item: None)
        self.assertEqual(backend.identity(A)['model'], 'Sonos (details unavailable)')
        backend.speakers[A] = sonos_backend.Speaker(A, BASES[A], 'Cinema')
        value = backend.identity(A)
        self.assertEqual((value['id'], value['name'], value['ip'], value['software']),
                         (A, 'Cinema', '10.0.0.10', ''))
        backend.speakers.clear()
        self.assertEqual(backend.identity(A)['id'], A)
        inventory.identity = lambda key: {'id': key, 'model': '', 'software': ''}
        self.assertEqual(backend.identity(A)['model'], 'Sonos (details unavailable)')
        inventory.identity = lambda key: None
        self.assertIsNone(backend.identity('not-a-sonos-id'))

    def test_plain_sonos_worker_publishes_identity_changes_without_holding_playback_lock(self):
        import sonos_backend

        class InventoryStub:
            def refresh(self, seeds):
                free = []
                def probe():
                    acquired = backend.lock.acquire(blocking=False)
                    free.append(acquired)
                    if acquired: backend.lock.release()
                thread = threading.Thread(target=probe)
                thread.start(); thread.join(1)
                self.assert_unlocked = free == [True]
                self.seeds = seeds
                return {A}
            def identity(self, key): return {'id': key}
            def bonded_devices(self, key): return [{'id': C}]

        inventory = InventoryStub()
        backend = sonos_backend.SonosBackend(listener=SimpleNamespace(close=lambda: None),
                                             identity_inventory=inventory, log=lambda item: None)
        backend.speakers[A] = sonos_backend.Speaker(A, BASES[A], 'Cinema')
        changes = []
        def changed(uuid):
            changes.append(uuid)
            backend.stop.set()
        backend.listeners.append(changed)
        backend.run_identities()
        self.assertTrue(inventory.assert_unlocked)
        self.assertEqual(inventory.seeds[A]['base'], BASES[A])
        self.assertEqual(changes, [A])
        self.assertEqual(backend.identity(A)['id'], A)
        self.assertEqual(backend.bonded_devices(A), [{'id': C}])


if __name__ == '__main__': unittest.main()
