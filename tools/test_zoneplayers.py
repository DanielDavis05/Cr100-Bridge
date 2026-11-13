#!/usr/bin/env python3
"""Offline tests for zoneplayers.py: two virtual players on loopback, real HTTP/SOAP/GENA."""
import http.client
import http.server
import queue
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zoneplayers

zp = zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)
zp.PROFILE = dict(zp.PROFILES['7.1'])
HOUSEHOLD = 'Sonos_jaD8ziPCjc9kzCsP5dqnxkpvqN'
M_SEARCH = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\n'
            'ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nX-RINCON-HOUSEHOLD: %s\r\n\r\n')


def soap(player, path, service, action, args=''):
    stype = player.state.services[service]['serviceType']
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action} xmlns:u="{stype}">{args}</u:{action}></s:Body></s:Envelope>').encode()
    c = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=5)
    c.request('POST', path, body, {'Content-Type': 'text/xml', 'SOAPACTION': f'"{stype}#{action}"'})
    r = c.getresponse(); data = r.read(); c.close()
    return r.status, data


def groups(topology_xml):
    root = ET.fromstring(topology_xml)
    visible = [[m.get('ZoneName') for m in g if m.get('Invisible') != '1'] for g in root.iter('ZoneGroup')]
    return sorted(sorted(members) for members in visible if members)


class Household(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Living Room', 'Kitchen'], ['127.0.0.1'] * 2,
                                           self.logs.append, port=0)
        self.house.start('127.0.0.1', ssdp=False)
        self.living, self.kitchen = self.house.players

    def tearDown(self):
        self.house.close()

    def test_identities_and_descriptions(self):
        self.assertEqual(len({p.config.udn for p in self.house.players}), 2)
        for player in self.house.players:
            c = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=5)
            c.request('GET', '/xml/device_description.xml'); r = c.getresponse(); body = r.read().decode()
            self.assertEqual(r.status, 200)
            self.assertIn(player.config.udn, body); self.assertIn(player.config.room, body)
            self.assertIn('34.16-37101', body)  # 7.1 profile

    def test_topology_lists_every_room_from_any_player(self):
        for player in self.house.players:
            status, body = soap(player, '/ZoneGroupTopology/Control', 'ZoneGroupTopology', 'GetZoneGroupState')
            self.assertEqual(status, 200)
            state = ET.fromstring(body).find('.//ZoneGroupState').text
            self.assertEqual(groups(state), [['Kitchen'], ['Living Room']])

    def test_grouping_notifies_subscribers_on_other_players(self):
        events = queue.Queue()

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                events.put(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200); self.end_headers()
            def log_message(self, *a): pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # The CR100 subscribes to topology on one player only.
            c = http.client.HTTPConnection('127.0.0.1', self.living.config.port, timeout=5)
            c.request('SUBSCRIBE', '/ZoneGroupTopology/Event', headers={
                'CALLBACK': f'<http://127.0.0.1:{server.server_port}/zgt>', 'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
            self.assertEqual(c.getresponse().status, 200); c.close()
            initial = events.get(timeout=5)
            self.assertIn(b'Kitchen', initial)

            def zone_groups(event):
                text = ET.fromstring(event).find('.//ZoneGroupState').text
                return groups(text)

            # Link the kitchen into the living room's group, through the kitchen.
            uri = 'x-rincon:' + self.living.config.udn
            status, _ = soap(self.kitchen, '/MediaRenderer/AVTransport/Control', 'AVTransport', 'SetAVTransportURI',
                             f'<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
            self.assertEqual(status, 200)
            self.assertEqual(zone_groups(events.get(timeout=5)), [['Kitchen', 'Living Room']])
            self.assertEqual(self.kitchen.state.group_id, self.living.config.udn + ':1')

            status, _ = soap(self.kitchen, '/MediaRenderer/AVTransport/Control', 'AVTransport',
                             'BecomeCoordinatorOfStandaloneGroup', '<InstanceID>0</InstanceID>')
            self.assertEqual(status, 200)
            self.assertEqual(zone_groups(events.get(timeout=5)), [['Kitchen'], ['Living Room']])

            # Renaming a room on one player also reaches the subscriber on the other.
            status, _ = soap(self.kitchen, '/DeviceProperties/Control', 'DeviceProperties', 'SetZoneAttributes',
                             '<DesiredZoneName>Den</DesiredZoneName><DesiredIcon></DesiredIcon><DesiredConfiguration>1</DesiredConfiguration>')
            self.assertEqual(status, 200)
            self.assertEqual(zone_groups(events.get(timeout=5)), [['Den'], ['Living Room']])
        finally:
            server.shutdown()

    def test_independent_volume(self):
        soap(self.kitchen, '/MediaRenderer/RenderingControl/Control', 'RenderingControl', 'SetVolume',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>55</DesiredVolume>')
        self.assertEqual((self.kitchen.state.volume, self.living.state.volume), (55, 20))

    def test_ssdp_answers_each_player_for_matching_household_only(self):
        for player in self.house.players:
            responses = player.search_responses((M_SEARCH % HOUSEHOLD).encode())
            self.assertEqual(len(responses), 1)
            text = responses[0].decode()
            self.assertIn('LOCATION: ' + player.state.location, text)
            self.assertIn('X-RINCON-HOUSEHOLD: ' + HOUSEHOLD, text)
            self.assertEqual(player.search_responses((M_SEARCH % 'Sonos_other').encode()), [])

    def test_bad_join_target_is_ignored(self):
        self.assertFalse(self.house.join(self.kitchen.config.udn, 'RINCON_NOPE01400'))
        self.assertFalse(self.house.join(self.kitchen.config.udn, self.kitchen.config.udn))
        self.assertFalse(self.house.leave(self.kitchen.config.udn))


class AlarmPresentation(unittest.TestCase):
    def setUp(self):
        from alarms import AlarmProviderRegistry, AlarmService
        from test_alarms import Clock, Player, Provider
        from test_backends import MemoryBackend
        self.clock, self.player, self.provider = Clock(), Player(), Provider()
        self.backend = MemoryBackend('memory')
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio'], ['127.0.0.1'] * 2,
            lambda _: None, port=0, backends=[self.backend], refs=[('memory', 'kitchen'), ('memory', 'patio')])
        self.addCleanup(self.house.close)
        registry = AlarmProviderRegistry(); registry.register('radio', self.provider)
        self.service = AlarmService(':memory:', providers=registry, executor=self.player.executor(),
            clock=self.clock, log=lambda _: None, max_attempts=1)
        self.house.alarms = self.service
        self.kitchen, self.patio = self.house.players
        # Sonos reports PLAYING without track metadata for its buzzer transport.
        self.kitchen.state.backend.transport = 'PLAYING'
        self.baseline = self.kitchen.state.backend_values()
        self.house.start('127.0.0.1', ssdp=False)

    def start_alarm(self, provider='chime'):
        alarm = self.service.save({'room': self.kitchen.config.udn, 'time': '12:00:00',
            'media_ref': {'provider': provider, 'content_id': 'default' if provider == 'chime' else 'station:s1'}})
        self.service.run_now(alarm['id'])
        return alarm

    def assert_chime(self, alarm):
        values = self.kitchen.state.backend_values()
        for name in ('AVTransportURI', 'CurrentTrackURI', 'CurrentURI', 'TrackURI'):
            self.assertEqual(values[name], 'x-rincon-buzzer:0')
        for name in ('NumberOfTracks', 'CurrentTrack', 'NrTracks', 'Track'):
            self.assertEqual(values[name], '1')
        for name in ('AVTransportURIMetaData', 'CurrentTrackMetaData', 'CurrentURIMetaData', 'TrackMetaData'):
            metadata = ET.fromstring(values[name])
            self.assertEqual(metadata.find('.//{http://purl.org/dc/elements/1.1/}title').text, alarm['title'])
        last_change = ET.fromstring(self.kitchen.state.last_change())
        for name in ('AVTransportURI', 'CurrentTrackURI', 'NumberOfTracks', 'CurrentTrackMetaData'):
            self.assertEqual(last_change.find('.//{*}' + name).get('val'), values[name])
        self.assertEqual(self.patio.state.backend_values()['AVTransportURI'], '')
        status, body = soap(self.kitchen, '/MediaRenderer/AVTransport/Control', 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(ET.fromstring(body).find('.//CurrentURI').text, 'x-rincon-buzzer:0')
        self.assertEqual(ET.fromstring(body).find('.//NrTracks').text, '1')

    def test_playing_chime_has_source_title_and_one_track_until_stopped(self):
        self.assertEqual((self.baseline['AVTransportURI'], self.baseline['NumberOfTracks']), ('', '0'))
        alarm = self.start_alarm()
        self.assert_chime(alarm)
        self.service.stop(self.kitchen.config.udn)
        self.assertEqual(self.kitchen.state.backend_values(), self.baseline)

    def test_chime_metadata_does_not_override_reported_backend_pause(self):
        self.start_alarm()
        self.kitchen.state.backend.transport = 'PAUSED_PLAYBACK'
        values = self.kitchen.state.backend_values()
        self.assertEqual(values['AVTransportURI'], 'x-rincon-buzzer:0')
        self.assertEqual(values['TransportState'], 'PAUSED_PLAYBACK')
        self.assertEqual(values['CurrentTransportState'], 'PAUSED_PLAYBACK')

    def test_alarm_panel_uses_the_group_visible_to_the_requesting_controller(self):
        self.house.coordinator[self.patio.config.udn] = self.kitchen.config.udn
        self.house.controller_resolver = lambda _: 'controller-a'
        self.house.controller_config = {'controllers': {'controller-a': {'visible_components': ['memory:patio']}}}
        self.service.group_id_resolver = lambda room: self.house.coordinator[room] + ':1'
        alarm = self.service.save({'room': self.patio.config.udn, 'time': '12:00:00',
                                  'media_ref': {'provider': 'chime', 'content_id': 'default'}})
        self.service.run_now(alarm['id'])
        status, body = soap(self.patio, '/MediaRenderer/AVTransport/Control', 'AVTransport',
                            'GetRunningAlarmProperties', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(ET.fromstring(body).find('.//AlarmID').text, str(alarm['id']))
        self.assertEqual(ET.fromstring(body).find('.//GroupID').text, self.patio.config.udn + ':1')

    def test_regular_radio_alarm_retains_its_backend_source_values(self):
        self.start_alarm('radio')
        self.assertFalse(self.service.store.runs()[-1]['fallback'])
        self.assertEqual(self.kitchen.state.backend_values(), self.baseline)

    def test_radio_fallback_chime_keeps_alarm_title_and_snooze_removes_overlay(self):
        from alarms import AlarmError
        self.provider.failure = AlarmError('Station unavailable')
        alarm = self.start_alarm('radio')
        self.assertTrue(self.service.store.runs()[-1]['fallback'])
        self.assert_chime(alarm)
        self.service.snooze(self.kitchen.config.udn)
        self.assertEqual(self.kitchen.state.backend_values(), self.baseline)


if __name__ == '__main__':
    unittest.main(verbosity=2)
