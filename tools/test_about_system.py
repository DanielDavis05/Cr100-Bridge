"""About shows physical identities without changing the CR100 compatibility persona."""
import http.client
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import zoneplayers
from test_backends import MemoryBackend
from test_ha_backend import AVT, RC, field, wait_for
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

DP = '/DeviceProperties/Control'
BUILD = {'software': '12ab34cd', 'display': 'v1.2.0', 'hardware': 'Virtual component',
         'extra': 'Bridge build: v1.2.0-2-g12ab34cd-dirty\nGit commit: ' + '12ab34cd' * 5}
BRIDGE_IP = '192.0.2.200'
P1, P2, SUB, OTHER = ('RINCON_REAL_ZP80', 'RINCON_REAL_ERA100', 'RINCON_REAL_SUB', 'RINCON_UNSELECTED')
NS = {'d': 'urn:schemas-upnp-org:device-1-0'}


def device(uuid, name, model, software, display, ip):
    return {'id': uuid, 'name': name, 'model': model, 'software': software, 'display': display,
            'hardware': '1.53.1.4-1.2', 'serial': '00-11-22-33-44-55:A', 'ip': ip,
            'extra': 'Device-specific firmware details'}


class IdentityBackend(MemoryBackend):
    def __init__(self):
        super().__init__('memory')
        self.identities = {
            'den': device(P1, 'Den', 'Sonos ZP80', '57.4-83220', '11.2.4', '192.0.2.10'),
            'hall': device(P2, 'Hall', 'Sonos Era 100', '85.0-63100', '16.1', '192.0.2.11'),
        }
        sub = device(SUB, 'Den Sub', 'Sonos Sub', '85.0-63100', '16.1', '192.0.2.12')
        self.bonds = {'den': [sub, self.identities['hall']], 'hall': [sub],
                      'unselected': [device(OTHER, 'Other room', 'Sonos Sub', '85.0-63100', '16.1', '192.0.2.13')]}

    def identity(self, room_id):
        value = self.identities.get(room_id)
        return dict(value) if value else None

    def bonded_devices(self, room_id):
        return [dict(value) for value in self.bonds.get(room_id, ())]


class AboutSystem(unittest.TestCase):
    def setUp(self):
        self.backend = IdentityBackend()
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall', 'TV'], ['127.0.0.1'] * 3,
                                           lambda record: None, port=0, backends=[self.backend],
                                           refs=[('memory', key) for key in ('den', 'hall', 'tv')],
                                           build=BUILD, bridge_ip=BRIDGE_IP)
        self.addCleanup(self.house.close)
        self.house.start('127.0.0.1', ssdp=False)
        # Individual tests drive refreshes/events explicitly; no periodic worker
        # should race an assertion about what a CR100 HTTP request reads.
        self.house.stop.set()
        self.den, self.hall, self.tv = self.house.players
        self.bridge = self.house.about_players[zoneplayers.BRIDGE_KEY]

    def description(self, player):
        connection = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=3)
        try:
            connection.request('GET', '/xml/device_description.xml')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return ET.fromstring(response.read()).find('d:device', NS)
        finally:
            connection.close()

    def info(self, player):
        status, body = soap(player, DP, 'DeviceProperties', 'GetZoneInfo')
        self.assertEqual(status, 200)
        return {name: field(body, name) for name in ('SerialNumber', 'SoftwareVersion',
                    'DisplaySoftwareVersion', 'HardwareVersion', 'IPAddress', 'ExtraInfo')}

    def test_mixed_actual_versions_use_normal_about_rows_and_one_discovery_version(self):
        topology = ET.fromstring(self.house.topology())
        members = topology.findall('.//ZoneGroupMember')
        self.assertEqual(len(members), len(self.house.devices()))
        for member in members:
            self.assertEqual(member.get('SoftwareVersion'), zp.PROFILE['sw'])
            self.assertEqual(member.get('MinCompatibleVersion'), zp.PROFILE['mincompat'])
            self.assertEqual(member.get('LegacyCompatibleVersion'), zp.PROFILE['legacycompat'])
        for player, key in ((self.den, 'den'), (self.hall, 'hall')):
            expected = self.backend.identities[key]
            description = self.description(player)
            self.assertEqual(description.findtext('d:displayName', namespaces=NS), expected['model'])
            self.assertEqual(description.findtext('d:softwareVersion', namespaces=NS), zp.PROFILE['sw'])
            self.assertEqual(description.findtext('d:minCompatibleVersion', namespaces=NS), zp.PROFILE['mincompat'])
            self.assertEqual(description.findtext('d:legacyCompatibleVersion', namespaces=NS), zp.PROFILE['legacycompat'])
            # Omitting feature tags invokes a CR100 fallback that can reuse the
            # real GetZoneInfo firmware for compatibility discovery.
            for tag in ('feature1', 'feature2', 'feature3'):
                self.assertIsNotNone(description.find('d:' + tag, NS))
            info = self.info(player)
            self.assertEqual(info['SoftwareVersion'], expected['software'])
            self.assertEqual(info['DisplaySoftwareVersion'], expected['display'])
            self.assertEqual(info['HardwareVersion'], expected['hardware'])
            self.assertEqual(info['SerialNumber'], expected['serial'])
            self.assertEqual(info['IPAddress'], expected['ip'])
        self.assertNotIn('57.4-83220', self.house.topology())
        self.assertNotIn('85.0-63100', self.house.topology())

    def test_non_sonos_and_about_only_bridge_report_build_identity(self):
        for player in (self.tv, self.bridge):
            info = self.info(player)
            self.assertEqual(info['SoftwareVersion'], BUILD['software'])
            self.assertEqual(info['DisplaySoftwareVersion'], BUILD['display'])
            self.assertEqual(info['ExtraInfo'], BUILD['extra'])
            self.assertEqual(info['IPAddress'], BRIDGE_IP)
        self.assertEqual(self.description(self.tv).findtext('d:displayName', namespaces=NS),
                         'Bridge Virtual Component')
        description = self.description(self.bridge)
        self.assertEqual(description.findtext('d:displayName', namespaces=NS), 'CR100 Bridge')
        self.assertEqual(description.findtext('d:modelNumber', namespaces=NS), 'ZoneBridge')
        self.assertEqual(description.findtext('d:zoneType', namespaces=NS), '4')
        members = ET.fromstring(self.house.topology()).findall('.//ZoneGroupMember')
        bridge = next(member for member in members if member.get('UUID') == self.bridge.config.udn)
        self.assertEqual((bridge.get('Invisible'), bridge.get('IsZoneBridge')), ('1', '1'))
        self.assertEqual(groups(self.house.topology()), [['Den'], ['Hall'], ['TV']])
        self.assertEqual(len({p.config.port for p in self.house.devices()}), len(self.house.devices()))
        self.assertEqual(len({p.config.udn for p in self.house.devices()}), len(self.house.devices()))
        other = zoneplayers.Household(zp, HOUSEHOLD, ['Other'], ['127.0.0.1'], lambda record: None,
                                      port=0, build=BUILD, bridge_ip=BRIDGE_IP)
        self.addCleanup(other.close)
        self.assertEqual(other.about_players[zoneplayers.BRIDGE_KEY].config.udn, self.bridge.config.udn)

    def test_selected_bonded_hardware_is_deduplicated_without_duplicate_primary_entries(self):
        self.assertEqual(set(self.house.about_players), {SUB, zoneplayers.BRIDGE_KEY})
        self.assertNotIn(P2, self.house.about_players)
        self.assertNotIn(OTHER, self.house.about_players)
        sub = self.house.about_players[SUB]
        self.assertEqual(self.description(sub).findtext('d:displayName', namespaces=NS), 'Sonos Sub')
        self.assertEqual(self.info(sub)['IPAddress'], '192.0.2.12')
        self.assertEqual(self.info(sub)['SoftwareVersion'], '85.0-63100')
        members = ET.fromstring(self.house.topology()).findall('.//ZoneGroupMember')
        hidden = next(member for member in members if member.get('UUID') == sub.config.udn)
        self.assertEqual(hidden.get('Invisible'), '1')

    def test_backend_metadata_change_refreshes_about_and_boot_sequence(self):
        old_boot, old_udn = self.den.config.boot_sequence, self.den.config.udn
        self.backend.identities['den'].update(model='Sonos PLAY:5', software='57.23-74170', display='11.16.1')
        self.backend.changed('den')
        self.assertTrue(wait_for(lambda: self.den.config.boot_sequence > old_boot))
        self.assertEqual(self.den.config.udn, old_udn)
        self.assertEqual(self.description(self.den).findtext('d:displayName', namespaces=NS), 'Sonos PLAY:5')
        self.assertEqual(self.info(self.den)['SoftwareVersion'], '57.23-74170')
        member = next(member for member in ET.fromstring(self.house.topology()).findall('.//ZoneGroupMember')
                      if member.get('UUID') == old_udn)
        self.assertEqual(member.get('BootSeq'), str(self.den.config.boot_sequence))
        self.assertEqual(member.get('SoftwareVersion'), zp.PROFILE['sw'])

    def test_removed_bond_closes_endpoint_and_disappears_from_topology(self):
        sub = self.house.about_players[SUB]
        port, udn = sub.config.port, sub.config.udn
        self.backend.bonds['den'] = [self.backend.identities['hall']]
        self.backend.bonds['hall'] = []
        self.house.refresh_identities()
        self.assertNotIn(SUB, self.house.about_players)
        self.assertNotIn(udn, self.house.topology())
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
        try:
            with self.assertRaises(OSError):
                connection.request('GET', '/xml/device_description.xml')
                connection.getresponse()
        finally:
            connection.close()

    def test_informational_components_have_no_playback_or_group_controls(self):
        for player in (self.bridge, self.house.about_players[SUB]):
            for path, service, action, args in (
                (AVT, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>'),
                (RC, 'RenderingControl', 'SetVolume',
                 '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>80</DesiredVolume>'),
                (AVT, 'AVTransport', 'SetAVTransportURI', '<InstanceID>0</InstanceID><CurrentURI>x-rincon:'
                 + self.den.config.udn + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')):
                status, body = soap(player, path, service, action, args)
                self.assertEqual(status, 500)
                self.assertEqual(ET.fromstring(body).findtext(
                    './/{urn:schemas-upnp-org:control-1-0}errorCode'), '501')
            self.assertFalse(self.house.join(player.config.udn, self.den.config.udn))
            self.assertFalse(self.house.join(self.den.config.udn, player.config.udn))
        self.assertEqual(self.backend.calls, [])

    def test_radio_overlay_skips_about_reads_and_unknown_components(self):
        radio = Mock()
        radio.transport_action.return_value = None
        radio.session.return_value = None
        self.house.radio_playback = radio
        for player in (self.bridge, self.house.about_players[SUB]):
            for path, service, action, args in (
                ('/SystemProperties/Control', 'SystemProperties', 'GetRDM', ''),
                ('/SystemProperties/Control', 'SystemProperties', 'GetString',
                 '<VariableName>R_TrialZPSerial</VariableName>')):
                status, body = soap(player, path, service, action, args)
                self.assertEqual(status, 200, body)
        supplied = {'CurrentTransportState': 'STOPPED', 'CurrentTrackMetaData': 'unchanged'}
        for udn in (self.bridge.config.udn, self.house.about_players[SUB].config.udn, 'RINCON_UNKNOWN'):
            self.assertIs(self.house.source_values(udn, supplied), supplied)
        self.assertEqual(supplied['CurrentTrackMetaData'], 'unchanged')
        radio.values.assert_not_called()

    def test_radio_overlay_still_applies_to_configured_room_reads(self):
        radio = Mock()
        radio.transport_action.return_value = None
        radio.session.return_value = None
        radio.values.side_effect = lambda udn, values, base: dict(values, CurrentTransportState='PLAYING')
        self.house.radio_playback = radio
        status, body = soap(self.den, AVT, 'AVTransport', 'GetTransportInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'CurrentTransportState'), 'PLAYING')
        self.assertEqual(radio.values.call_args.args[0], self.den.config.udn)
        self.assertEqual(radio.values.call_args.args[2],
                         'http://%s:%d' % (self.den.config.advertise_ip, self.den.config.port))

    def test_about_http_uses_snapshots_without_backend_readers_or_live_git(self):
        with patch.object(self.backend, 'identity', side_effect=AssertionError('backend read on HTTP')) as primary, \
             patch.object(self.backend, 'bonded_devices', side_effect=AssertionError('bond read on HTTP')) as bonded, \
             patch.object(zoneplayers.build_info, 'about', side_effect=AssertionError('Git read on HTTP')):
            for player in self.house.devices():
                self.description(player)
                self.info(player)
            primary.assert_not_called()
            bonded.assert_not_called()

    def test_about_fields_are_bounded_without_splitting_utf8_or_breaking_xml(self):
        value = 'Era & <🏡> café\x00\x1b' * 100
        self.backend.identities['den'].update({key: value for key in
            ('model', 'serial', 'software', 'display', 'hardware', 'ip', 'extra')})
        self.house.refresh_identities()
        description = self.description(self.den)
        label = description.findtext('d:displayName', namespaces=NS)
        self.assertLessEqual(len(label.encode('utf-8')), 64)
        self.assertIn('🏡', label)
        info = self.info(self.den)
        for name, text in info.items():
            self.assertLessEqual(len(text.encode('utf-8')), 2048 if name == 'ExtraInfo' else 64)
            self.assertNotIn('\x00', text)
            self.assertNotIn('\x1b', text)
            self.assertNotIn('\ufffd', text)


class EmptyAboutSystem(unittest.TestCase):
    def test_no_rooms_still_serve_a_read_only_bridge_zone(self):
        house = zoneplayers.Household(zp, HOUSEHOLD, [], [], lambda record: None, port=0,
                                     build=BUILD, bridge_ip=BRIDGE_IP)
        try:
            bridge = house.about_players[zoneplayers.BRIDGE_KEY]
            self.assertEqual(house.devices(), [bridge])
            self.assertEqual(groups(house.topology()), [['CR100 Bridge']])
            house.start('127.0.0.1', ssdp=False)
            self.assertFalse(house.refresh_identities(refresh_ip=True))
            description = ET.fromstring(bridge.state.description()).find('d:device', NS)
            self.assertEqual(description.findtext('d:modelNumber', namespaces=NS), 'ZP80')
            self.assertEqual(description.findtext('d:displayName', namespaces=NS), 'CR100 Bridge')
            self.assertEqual(bridge.state.event_values('DeviceProperties')['Invisible'], '0')
            status, body = soap(bridge, DP, 'DeviceProperties', 'GetZoneInfo')
            self.assertEqual(status, 200)
            self.assertEqual(field(body, 'IPAddress'), BRIDGE_IP)
            self.assertEqual(soap(bridge, AVT, 'AVTransport', 'Play',
                                  '<InstanceID>0</InstanceID><Speed>1</Speed>')[0], 500)
        finally:
            house.close()


if __name__ == '__main__': unittest.main()
