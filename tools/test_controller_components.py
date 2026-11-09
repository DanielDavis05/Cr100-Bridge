"""Controller-local component views through real HTTP, subscriptions, and SSDP."""
import copy
import http.client
import http.server
import queue
import socket
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import zoneplayers
from test_about_system import BUILD, BRIDGE_IP, IdentityBackend, SUB
from test_ha_backend import AVT, RC, field
from test_zoneplayers import HOUSEHOLD, M_SEARCH, groups, zp

A, B, C, D = ('00:0e:58:01:02:03', '00:0e:58:04:05:06',
              '00:0e:58:07:08:09', '00:0e:58:0a:0b:0c')
IP_A, IP_B, IP_C, IP_D, UNKNOWN = ('127.0.0.2', '127.0.0.3', '127.0.0.4', '127.0.0.5', '127.0.0.6')
ADDRESSES = {IP_A: A, IP_B: B, IP_C: C, IP_D: D}
CONFIG = {'controllers': {
    A: {'visible_components': ['memory:den', 'sonos:' + SUB, 'cr100-bridge']},
    B: {'visible_components': ['memory:hall']},
    C: {'visible_components': ['sonos:' + SUB]},
    D: {'visible_components': []},
}}
ZGT = '/ZoneGroupTopology/Control'


class SourceAddresses:
    """Use real source binds, with an accepted-peer shim on hosts lacking 127/8.

    Linux provides the entire loopback subnet. macOS normally configures only
    127.0.0.1; there, clients still use real TCP sockets and the test server maps
    their source ports to logical peer addresses at the accept boundary.
    """
    def __init__(self, servers, addresses):
        self.native, self.sources, self.originals = True, {}, []
        for address in addresses:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((address, 0))
            except OSError:
                self.native = False
            finally:
                probe.close()
            if not self.native: break
        if self.native: return
        for server in servers:
            original = server.get_request
            def get_request(original=original):
                connection, source = original()
                return connection, (self.sources.get(source[1], source[0]), source[1])
            self.originals.append((server, original))
            server.get_request = get_request

    def connection(self, host, port, source, timeout=3):
        if self.native:
            return http.client.HTTPConnection(host, port, timeout=timeout, source_address=(source, 0))
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
            client.bind(('127.0.0.1', 0))
            self.sources[client.getsockname()[1]] = source
            client.connect((host, port))
        except BaseException:
            client.close()
            raise
        connection.sock = client
        return connection

    def close(self):
        for server, original in self.originals: server.get_request = original
        self.sources.clear()


def make_house():
    backend = IdentityBackend()
    house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall', 'TV'], ['127.0.0.1'] * 3,
        lambda _: None, port=0, backends=[backend], refs=[('memory', key) for key in ('den', 'hall', 'tv')],
        build=BUILD, bridge_ip=BRIDGE_IP, controller_config=copy.deepcopy(CONFIG),
        controller_resolver=ADDRESSES.get, controller_addresses=lambda: dict(ADDRESSES))
    return house, backend


def request(player, source, method, path, body=None, headers=None):
    addresses = getattr(player.http, '_test_source_addresses', None)
    connection = (addresses.connection('127.0.0.1', player.config.port, source) if addresses else
                  http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=3,
                                             source_address=(source, 0)))
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def soap(player, source, service, action, arguments='', path=None, extra_headers=None):
    service_type = player.state.services[service]['serviceType']
    if path is None:
        path = {'ZoneGroupTopology': ZGT, 'AVTransport': AVT, 'RenderingControl': RC,
                'DeviceProperties': '/DeviceProperties/Control'}[service]
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action} xmlns:u="{service_type}">{arguments}</u:{action}></s:Body></s:Envelope>').encode()
    headers = {'Content-Type': 'text/xml', 'SOAPACTION': f'"{service_type}#{action}"', **(extra_headers or {})}
    return request(player, source, 'POST', path, body, headers)


def topology_body(body):
    return ET.fromstring(body).find('.//ZoneGroupState').text


class ControllerComponentsHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.house, cls.backend = make_house()
        cls.house.start('127.0.0.1', ssdp=False)
        # Refreshes and notifications are driven explicitly in these tests.
        cls.house.stop.set()
        cls.den, cls.hall, cls.tv = cls.house.players
        cls.sub = cls.house.about_players[SUB]
        cls.bridge = cls.house.about_players[zoneplayers.BRIDGE_KEY]
        cls.sources = SourceAddresses([player.http for player in cls.house.devices()], [*ADDRESSES, UNKNOWN])
        for player in cls.house.devices(): player.http._test_source_addresses = cls.sources
        for player in cls.house.devices(): player.config.callback_network = '127.0.0.0/8'

    @classmethod
    def tearDownClass(cls):
        cls.sources.close()
        cls.house.close()

    def setUp(self):
        self.house.controller_config = copy.deepcopy(CONFIG)
        self.backend.calls.clear()
        self.backend.groups.clear()
        self.house.sync_groups()
        for player in self.house.devices():
            with player.state.lock:
                player.state.subscriptions.clear()
                player.published.clear()

    def test_http_topology_and_about_endpoints_use_actual_controller_identity(self):
        for source, endpoint, wanted, hidden in (
            (IP_A, self.den, {'Den', 'Den Sub', 'CR100 Bridge'}, [self.hall, self.tv]),
            (IP_B, self.hall, {'Hall'}, [self.den, self.tv, self.sub, self.bridge]),
        ):
            status, body, _ = soap(endpoint, source, 'ZoneGroupTopology', 'GetZoneGroupState')
            self.assertEqual(status, 200)
            topology = topology_body(body)
            self.assertEqual({member.get('ZoneName') for member in ET.fromstring(topology).iter('ZoneGroupMember')}, wanted)
            for player in hidden:
                self.assertNotIn(player.config.udn, topology)
                self.assertNotIn('ZoneName="' + player.config.room + '"', topology)
                status, _, _ = request(player, source, 'GET', '/xml/device_description.xml')
                self.assertEqual(status, 404)
        for player in (self.den, self.sub, self.bridge):
            status, body, _ = request(player, IP_A, 'GET', '/xml/device_description.xml')
            self.assertEqual(status, 200)
            self.assertIn(player.config.udn.encode(), body)
        self.assertEqual(request(self.hall, IP_B, 'GET', '/xml/device_description.xml')[0], 200)

    def test_inheritance_empty_selection_and_bonded_parent_requirement(self):
        all_devices = {player.config.udn for player in self.house.devices()}
        self.assertEqual({player.config.udn for player in self.house.devices(UNKNOWN)}, all_devices)
        self.assertEqual(self.house.devices(IP_D), [])
        self.assertEqual(groups(self.house.topology(IP_D)), [])
        # The bonded device was selected explicitly, but neither owning speaker
        # was selected for this controller, so it cannot appear by itself.
        self.assertEqual(self.house.devices(IP_C), [])
        self.assertNotIn(self.sub.config.udn, self.house.topology(IP_C))
        for source in (IP_C, IP_D):
            for player in self.house.devices():
                self.assertEqual(request(player, source, 'GET', '/xml/device_description.xml')[0], 404)
        status, body, _ = soap(self.tv, UNKNOWN, 'ZoneGroupTopology', 'GetZoneGroupState')
        self.assertEqual(status, 200)
        self.assertEqual(groups(topology_body(body)), [['Den'], ['Hall'], ['TV']])

    def test_cached_hidden_urls_commands_and_identity_headers_do_not_bypass_selection(self):
        for path in ('/xml/device_description.xml', '/xml/AVTransport1.xml', '/status/peer', '/img/icon-ZP80.png'):
            status, _, _ = request(self.hall, IP_A, 'GET', path,
                                  headers={'X-Forwarded-For': IP_B, 'X-Controller-MAC': B})
            self.assertEqual(status, 404)
        for service, action, args in (
            ('AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>'),
            ('RenderingControl', 'SetVolume',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>75</DesiredVolume>'),
        ):
            status, _, _ = soap(self.hall, IP_A, service, action, args,
                                extra_headers={'X-Forwarded-For': IP_B, 'X-Controller-MAC': B})
            self.assertEqual(status, 404)
        status, _, _ = request(self.hall, IP_A, 'SUBSCRIBE', '/ZoneGroupTopology/Event', headers={
            'CALLBACK': '<http://127.0.0.1:12345/hidden>', 'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
        self.assertEqual(status, 404)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.hall.state.backend.volume, 20)
        self.assertEqual(self.hall.state.subscriptions, {})

    def test_hidden_group_target_is_rejected_before_backend_mutation(self):
        before = dict(self.house.coordinator)
        status, _, _ = soap(self.den, IP_A, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>x-rincon:' + self.hall.config.udn +
            '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 500)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.house.coordinator, before)

    def test_hidden_real_coordinator_has_visible_proxy_without_changing_actual_group(self):
        self.backend.groups.update(den=['den', 'hall'], hall=['den', 'hall'])
        self.house.sync_groups()
        real_group = dict(self.house.coordinator)
        status, body, _ = soap(self.hall, IP_B, 'ZoneGroupTopology', 'GetZoneGroupState')
        self.assertEqual(status, 200)
        text = topology_body(body)
        topology = ET.fromstring(text)
        self.assertEqual(groups(text), [['Hall']])
        group = next(topology.iter('ZoneGroup'))
        self.assertEqual(group.get('Coordinator'), self.hall.config.udn)
        self.assertEqual(group.get('ID'), self.hall.config.udn + ':1')
        self.assertNotIn(self.den.config.udn, text)
        self.assertNotIn('Den', text)
        status, body, _ = soap(self.hall, IP_B, 'ZoneGroupTopology', 'GetZoneGroupAttributes')
        self.assertEqual(status, 200)
        self.assertNotIn(self.den.config.udn.encode(), body)
        self.assertNotIn(b'Den', body)
        self.assertEqual(field(body, 'CurrentZoneGroupID'), self.hall.config.udn + ':1')
        self.assertEqual(self.house.coordinator, real_group)
        self.assertEqual(self.house.coordinator[self.hall.config.udn], self.den.config.udn)
        self.assertEqual({player.config.udn for player in self.house.members(self.hall.config.udn)},
                         {self.den.config.udn, self.hall.config.udn})
        self.assertEqual(self.backend.groups, {'den': ['den', 'hall'], 'hall': ['den', 'hall']})

    def callback_server(self):
        events = queue.Queue()
        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                events.put((self.path, self.headers['SID'], self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, events

    def subscribe(self, player, source, server, path, service='ZoneGroupTopology'):
        event_path = next(path for path, name in player.state.event_paths.items() if name == service)
        status, _, headers = request(player, source, 'SUBSCRIBE', event_path, headers={
            'CALLBACK': f'<http://127.0.0.1:{server.server_port}/{path}>',
            'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
        self.assertEqual(status, 200)
        return headers['SID']

    def test_initial_and_changed_notifications_use_subscriber_not_shared_callback_identity(self):
        server, events = self.callback_server()
        self.subscribe(self.den, IP_A, server, 'a')
        self.subscribe(self.hall, IP_B, server, 'b')
        initial = {path: body for path, _, body in (events.get(timeout=3), events.get(timeout=3))}
        self.assertEqual(groups(topology_body(initial['/a'])), [['Den']])
        self.assertEqual(groups(topology_body(initial['/b'])), [['Hall']])
        self.assertNotIn(self.hall.config.udn.encode(), initial['/a'])
        self.assertNotIn(self.den.config.udn.encode(), initial['/b'])
        self.backend.groups.update(den=['den', 'hall'], hall=['den', 'hall'])
        self.house.sync_groups()
        self.den.publish('ZoneGroupTopology')
        changed = {path: body for path, _, body in (events.get(timeout=3), events.get(timeout=3))}
        self.assertEqual(groups(topology_body(changed['/a'])), [['Den']])
        self.assertEqual(groups(topology_body(changed['/b'])), [['Hall']])
        self.assertNotIn(self.den.config.udn.encode(), changed['/b'])
        self.assertNotIn(b'Den', changed['/b'])
        for player, source, path in ((self.den, IP_A, '/a'), (self.hall, IP_B, '/b')):
            status, body, _ = soap(player, source, 'ZoneGroupTopology', 'GetZoneGroupState')
            self.assertEqual(status, 200)
            self.assertEqual(topology_body(body), topology_body(changed[path]))

    def test_subscription_sid_cannot_be_renewed_or_removed_by_another_requester(self):
        server, events = self.callback_server()
        sid = self.subscribe(self.den, IP_A, server, 'owner')
        events.get(timeout=3)
        # UNKNOWN inherits every endpoint, so denial depends on SID ownership,
        # independently of the component visibility check on the endpoint.
        for method in ('SUBSCRIBE', 'UNSUBSCRIBE'):
            status, _, _ = request(self.den, UNKNOWN, method, '/ZoneGroupTopology/Event',
                                   headers={'SID': sid, 'TIMEOUT': 'Second-300'})
            self.assertEqual(status, 412)
        self.assertEqual(request(self.den, IP_A, 'SUBSCRIBE', '/ZoneGroupTopology/Event',
                                 headers={'SID': sid, 'TIMEOUT': 'Second-300'})[0], 200)
        self.assertEqual(request(self.den, IP_A, 'UNSUBSCRIBE', '/ZoneGroupTopology/Event',
                                 headers={'SID': sid})[0], 200)

    def test_a_policy_change_revokes_an_existing_endpoint_subscription(self):
        server, events = self.callback_server()
        sid = self.subscribe(self.den, IP_A, server, 'revoked')
        events.get(timeout=3)
        self.house.controller_config['controllers'][A]['visible_components'] = []
        self.assertEqual(request(self.den, IP_A, 'GET', '/xml/device_description.xml')[0], 404)
        self.den.notify(sid)
        self.assertNotIn(sid, self.den.state.subscriptions)
        with self.assertRaises(queue.Empty): events.get_nowait()
        self.assertEqual(request(self.den, IP_A, 'SUBSCRIBE', '/ZoneGroupTopology/Event',
                                 headers={'SID': sid, 'TIMEOUT': 'Second-300'})[0], 404)


class ControllerComponentsReadout(unittest.TestCase):
    callback_server = ControllerComponentsHTTP.callback_server
    subscribe = ControllerComponentsHTTP.subscribe

    def open_house(self, names, backends=None, refs=None):
        house = zoneplayers.Household(zp, HOUSEHOLD, names, ['127.0.0.1'] * len(names), lambda _: None,
            port=0, backends=backends, refs=refs, build=BUILD, bridge_ip=BRIDGE_IP,
            controller_resolver=ADDRESSES.get)
        self.addCleanup(house.close)
        house.start('127.0.0.1', ssdp=False)
        house.stop.set()
        addresses = SourceAddresses([player.http for player in house.devices()], [IP_A])
        self.addCleanup(addresses.close)
        for player in house.devices():
            player.http._test_source_addresses = addresses
            player.config.callback_network = '127.0.0.0/8'
        return house

    def test_cached_simulated_group_uri_is_masked_in_actions_and_notifications(self):
        house = self.open_house(['Hidden room', 'Visible room'])
        hidden, visible = house.players
        original_uri = 'x-rincon:' + hidden.config.udn
        status, _, _ = soap(visible, IP_A, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>' + original_uri +
            '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        self.assertEqual(visible.state.uri, original_uri)
        house.controller_config = {'controllers': {A: {'visible_components': ['sim:1']}}}
        status, body, _ = soap(visible, IP_A, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertNotIn(hidden.config.udn.encode(), body)
        self.assertEqual(field(body, 'CurrentURI'), 'x-rincon:' + visible.config.udn)
        server, events = self.callback_server()
        self.subscribe(visible, IP_A, server, 'transport', service='AVTransport')
        self.subscribe(visible, IP_A, server, 'group', service='GroupManagement')
        notifications = {path: body for path, _, body in (events.get(timeout=3), events.get(timeout=3))}
        for body in notifications.values():
            self.assertNotIn(hidden.config.udn.encode(), body)
            self.assertNotIn(b'Hidden room', body)
            self.assertIn(visible.config.udn.encode(), body)
        self.assertEqual(visible.state.uri, original_uri, 'The filtered readout must not alter real transport state')
        self.assertEqual(house.coordinator[visible.config.udn], hidden.config.udn)

    def test_hidden_line_in_source_uses_generic_metadata_without_changing_playback(self):
        from test_line_in import LineInBackend
        backend = LineInBackend()
        house = self.open_house(['Hidden source room', 'Visible destination'], backends=[backend],
                                refs=[(backend.name, 'source'), (backend.name, 'destination')])
        hidden, visible = house.players
        status, _, _ = soap(visible, IP_A, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>x-rincon-stream:' + hidden.config.udn +
            '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        house.controller_config = {'controllers': {A: {'visible_components': [backend.name + ':destination']}}}
        status, body, _ = soap(visible, IP_A, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        metadata = field(body, 'TrackMetaData')
        self.assertIn('Line-In', metadata)
        self.assertNotIn('Turntable', metadata)
        self.assertNotIn('Hidden source room', metadata)
        self.assertNotIn(hidden.config.udn.encode(), body)
        server, events = self.callback_server()
        self.subscribe(visible, IP_A, server, 'input', service='AVTransport')
        _, _, event = events.get(timeout=3)
        self.assertNotIn(hidden.config.udn.encode(), event)
        self.assertNotIn(b'Turntable', event)
        self.assertEqual(visible.state.backend.line_in_source(), 'source')


class ControllerComponentsDiscovery(unittest.TestCase):
    def setUp(self):
        self.house, self.backend = make_house()
        self.addCleanup(self.house.close)

    def run_discovery(self, receive):
        stop = threading.Event()
        sent = []
        class Datagram:
            def __init__(self, listener=False): self.listener = listener
            def setsockopt(self, *_): pass
            def bind(self, *_): pass
            def settimeout(self, *_): pass
            def close(self): pass
            def sendto(self, packet, destination): sent.append((packet, destination))
            def recvfrom(self, _): return receive(sent, stop)
        sockets = []
        def create(*_):
            result = Datagram(listener=not sockets)
            sockets.append(result)
            return result
        with patch.object(zoneplayers.socket, 'socket', side_effect=create):
            self.house.ssdp_loop('127.0.0.1', stop)
        return sent

    def test_ssdp_search_and_unsolicited_notices_never_advertise_hidden_components(self):
        search = (M_SEARCH % HOUSEHOLD).encode()
        incoming = [(search, (IP_A, 12001)), (search, (IP_B, 12002)),
                    (search, (IP_C, 12003)), (search, (IP_D, 12004))]
        def receive(sent, stop):
            if incoming: return incoming.pop(0)
            stop.set()
            raise socket.timeout()
        sent = self.run_discovery(receive)
        self.assertTrue(sent)
        self.assertFalse(any(destination[0] == zp.SSDP_GROUP for _, destination in sent),
                         'A multicast advertisement would reveal restricted components to every controller')
        for source in ADDRESSES:
            wanted = {player.config.udn for player in self.house.devices(source)}
            actual = []
            for packet, destination in sent:
                if destination[0] != source: continue
                actual.append(packet)
                for player in self.house.devices():
                    if player.config.udn not in wanted:
                        self.assertNotIn(player.config.udn.encode(), packet)
            if wanted:
                self.assertTrue(any(packet.startswith(b'HTTP/1.1 200') for packet in actual))
                self.assertTrue(any(packet.startswith(b'NOTIFY ') for packet in actual))
            else:
                self.assertEqual(actual, [])

    def test_policy_change_withdraws_old_announcements_before_any_new_response(self):
        changed_at = []
        previous = {player.config.udn for player in self.house.devices(IP_A)}
        def receive(sent, stop):
            if not changed_at:
                changed_at.append(len(sent))
                self.house.controller_config['controllers'][A]['visible_components'] = []
                self.house.discovery_generation += 1
                return (M_SEARCH % HOUSEHOLD).encode(), (IP_A, 12001)
            stop.set()
            raise socket.timeout()
        sent = self.run_discovery(receive)
        before = [packet for packet, address in sent[:changed_at[0]] if address[0] == IP_A]
        after = [packet for packet, address in sent[changed_at[0]:] if address[0] == IP_A]
        for udn in previous:
            self.assertTrue(any(udn.encode() in packet and b'NTS: ssdp:alive' in packet for packet in before))
            self.assertTrue(any(udn.encode() in packet and b'NTS: ssdp:byebye' in packet for packet in after))
        self.assertTrue(after)
        self.assertTrue(all(b'NTS: ssdp:byebye' in packet for packet in after),
                        'After withdrawing a component, neither alive notices nor search responses may expose it')


if __name__ == '__main__': unittest.main()
