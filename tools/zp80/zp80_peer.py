#!/usr/bin/env python3
"""Small software ZonePlayer for the isolated CR100 emulator network.

Discovery, UPnP descriptions, SOAP control, and GENA notifications use the
contracts extracted from the user's firmware. This is a synthetic single-room
player with empty media collections and simulated transport/volume state.
It implements no radio enrollment, authentication bypass, or audio decoder.
"""
from __future__ import annotations

if __name__ == '__main__':
    print('{"stage":"loading-python"}', flush=True)

def _startup_stage(stage):
    # Fixed, bounded notices localize cold import stalls without more imports.
    if __name__ == '__main__': print('{"stage":"' + stage + '"}', flush=True)

_startup_stage('importing-cli')
import argparse
from collections import deque
from reprlib import recursive_repr
_startup_stage('importing-formatting')
import datetime
import html
_startup_stage('importing-http')
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
_startup_stage('importing-network')
import ipaddress
import json
from pathlib import Path
import re
import socket
import socketserver
import struct
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
_startup_stage('importing-xml')
try:
    import xml.etree.ElementTree as ET
except ImportError:
    try:
        from . import xml_compat as ET
    except ImportError:
        import xml_compat as ET

_startup_stage('importing-netstart')
try:
    from .netstart_peer import NetstartResponder
except ImportError:
    from netstart_peer import NetstartResponder

_startup_stage('defining-peer')
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SERVICE_NS = 'urn:schemas-upnp-org:service:'
SOAP_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
EVENT_NS = 'urn:schemas-upnp-org:event-1-0'
DEVICE_NS = 'urn:schemas-upnp-org:device-1-0'
SCPD_NS = 'urn:schemas-upnp-org:service-1-0'
ZONE_PLAYER = 'urn:schemas-upnp-org:device:ZonePlayer:1'
SSDP_GROUP = '239.255.255.250'
SERVER = 'Linux/2.4.22 UPnP/1.0 Sonos/24.1-75101 (ZP80)'
# Version identity reported by the peer. '7.1' copies the user's physical ZP80
# (device description and ZoneGroupMember captured 2026-10-30).
PROFILES = {
    '4.3': {'sw': '24.1-75101', 'display': '4.3', 'hw': '1.1.1.2-2',
            'mincompat': '24.0-00000', 'legacycompat': '24.0-00000',
            'server': 'Linux/2.4.22 UPnP/1.0 Sonos/24.1-75101 (ZP80)'},
    '7.1': {'sw': '34.16-37101', 'display': '7.1', 'hw': '1.1.16.3-1',
            'mincompat': '33.0-00000', 'legacycompat': '25.0-00000',
            'server': 'Linux UPnP/1.0 Sonos/34.16-37101 (ZP80)'},
    # Copies the user's newer physical ZP80 at 192.168.5.40 (captured 2026-10-31):
    # firmware 57.4-83220, display 11.2.4, legacy floor 36.0 (blocks the CR100).
    '11.2': {'sw': '57.4-83220', 'display': '11.2.4', 'hw': '1.1.16.3-1.0',
             'mincompat': '56.0-00000', 'legacycompat': '36.0-00000',
             'server': 'Linux UPnP/1.0 Sonos/57.4-83220 (ZP80)'},
}
PROFILE = PROFILES['4.3']
BROWSER_REQUEST_TIMEOUT = 1.0
BROWSER_NOTIFY_QUEUE_LIMIT = 32


def xml_bytes(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


class Config:
    bind: str = '127.0.0.1'
    advertise_ip: str = '127.0.0.1'
    port: int = 1400
    household: str = 'Sonos_EMULATED_CR100'
    mac: str = '02:00:00:00:00:80'
    room: str = 'Software ZP80'
    ssdp_port: int = 1900
    callback_network: str | None = None
    netstart_port: int = 6969
    boot_sequence: int = 1
    browser_workers: bool = False

    __match_args__ = ('bind', 'advertise_ip', 'port', 'household', 'mac', 'room',
                     'ssdp_port', 'callback_network', 'netstart_port', 'boot_sequence', 'browser_workers')
    __hash__ = None

    def __init__(self, bind: str = '127.0.0.1', advertise_ip: str = '127.0.0.1',
                 port: int = 1400, household: str = 'Sonos_EMULATED_CR100',
                 mac: str = '02:00:00:00:00:80', room: str = 'Software ZP80',
                 ssdp_port: int = 1900, callback_network: str | None = None,
                 netstart_port: int = 6969, boot_sequence: int = 1,
                 browser_workers: bool = False) -> None:
        # Explicit initialization avoids importing dataclasses/inspect/dis/ast
        # in the constrained guest. Preserve field order and validation hooks.
        self.bind = bind; self.advertise_ip = advertise_ip; self.port = port
        self.household = household; self.mac = mac; self.room = room
        self.ssdp_port = ssdp_port; self.callback_network = callback_network
        self.netstart_port = netstart_port; self.boot_sequence = boot_sequence
        self.browser_workers = browser_workers
        self.__post_init__()

    @recursive_repr()
    def __repr__(self):
        return type(self).__qualname__ + '(' + ', '.join(
            name + '=' + repr(getattr(self, name)) for name in self.__match_args__) + ')'

    def __eq__(self, other):
        if type(self) is not type(other): return NotImplemented
        return tuple(getattr(self, name) for name in self.__match_args__) == tuple(
            getattr(other, name) for name in self.__match_args__)

    def __post_init__(self):
        ipaddress.IPv4Address(self.bind)
        ipaddress.IPv4Address(self.advertise_ip)
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', self.mac):
            raise ValueError('MAC must contain six colon-separated hexadecimal bytes')
        if not self.household or len(self.household.encode()) > 32 or any(c in self.household for c in '\r\n\0'):
            raise ValueError('Household must contain 1–32 bytes without control characters')
        if not 0 <= self.port <= 65535 or not 0 <= self.ssdp_port <= 65535 or not 0 <= self.netstart_port <= 65535:
            raise ValueError('Port is out of range')
        if self.callback_network:
            ipaddress.IPv4Network(self.callback_network)
        if not 1 <= self.boot_sequence <= 0xffffffff:
            raise ValueError('Boot sequence must be a positive u32')
        if not isinstance(self.browser_workers, bool):
            raise ValueError('Browser workers must be enabled with a boolean')

    @property
    def udn(self):
        return 'RINCON_' + self.mac.replace(':', '').upper() + '01400'


class UPnPError(Exception):
    def __init__(self, code, description):
        self.code, self.description = code, description
        super().__init__(description)


class PeerHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer normally performs a reverse DNS lookup here. This peer
        # operates on an isolated numeric-IP network with no DNS service.
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]

    def get_request(self):
        request, address = super().get_request()
        if self.peer.config.browser_workers:
            request.settimeout(BROWSER_REQUEST_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if self.peer.config.browser_workers:
            # serve_forever already has its own dedicated thread. A new guest
            # pthread per request is prohibitively expensive in browser TCG.
            return socketserver.TCPServer.process_request(self, request, client_address)
        return super().process_request(request, client_address)

    def handle_error(self, request, client_address):
        if self.peer.config.browser_workers and isinstance(sys.exc_info()[1],
                (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class PeerState:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.volume, self.mute = 20, 0
        self.bass, self.treble, self.loudness = 0, 0, 1
        self.transport = 'STOPPED'
        self.uri, self.metadata, self.play_mode = '', '', 'NORMAL'
        self.led = 'On'
        self.update_item = ''
        self.properties = {'R_AutoUpdate': '0', 'R_UpdateAvailable': '0',
                           'R_ShowNSSServers': '0', 'UsageData': '0'}
        self.subscriptions = {}
        self.services = {}
        self.control_paths = {}
        self.event_paths = {}
        self.actions = {}
        cache_path = FIXTURES / 'service_contracts.json'
        if cache_path.exists():
            contracts = json.loads(cache_path.read_text())
            self.services = contracts['services']
            self.control_paths = contracts['control_paths']
            self.event_paths = contracts['event_paths']
            self.actions = contracts['actions']
            return
        root = ET.parse(FIXTURES / 'xml/device_description.xml').getroot()
        for service in root.iter('{%s}service' % DEVICE_NS):
            fields = {local_name(x.tag): x.text for x in service}
            path = fields['controlURL']
            name = path.split('/')[-2]
            self.services[name] = fields
            self.control_paths[path] = name
            self.event_paths[fields['eventSubURL']] = name
            scpd = ET.parse(FIXTURES / fields['SCPDURL'].lstrip('/')).getroot()
            ns = {'s': SCPD_NS}
            types = {x.findtext('s:name', namespaces=ns): x.findtext('s:dataType', namespaces=ns)
                     for x in scpd.findall('s:serviceStateTable/s:stateVariable', ns)}
            actions = {}
            for action in scpd.findall('s:actionList/s:action', ns):
                name_action = action.findtext('s:name', namespaces=ns)
                args = []
                for arg in action.findall('s:argumentList/s:argument', ns):
                    args.append((arg.findtext('s:name', namespaces=ns),
                                 arg.findtext('s:direction', namespaces=ns),
                                 types.get(arg.findtext('s:relatedStateVariable', namespaces=ns), 'string')))
                actions[name_action] = args
            self.actions[name] = actions

    @property
    def base_url(self):
        return 'http://%s:%d' % (self.config.advertise_ip, self.config.port)

    @property
    def location(self):
        return self.base_url + '/xml/device_description.xml'

    @property
    def group_id(self):
        return self.config.udn + ':1'

    def description(self):
        replacements = {'HOST': self.config.advertise_ip, 'DISPLAY_NAME': 'ZonePlayer 80',
            'MODEL': 'ZP80', 'SW_VERSION': PROFILE['sw'], 'HW_VERSION': PROFILE['hw'],
            'SERIAL_NUM': self.config.mac.replace(':', '-').upper() + ':0',
            'UUID': self.config.udn, 'SW_MINCOMPATVER': PROFILE['mincompat'],
            'SW_LEGACYCOMPATVER': PROFILE['legacycompat'], 'DISPLAY_VERSION': PROFILE['display'],
            # Exact ZP80 row in the CR100's stock model table at0x6df4ec.
            'EXTRA_VERSION': 'software-peer', 'NAME': self.config.room, 'ZONETYPE': '1',
            'FEATURE1': '0x00310001', 'FEATURE2': '0x00006172', 'FEATURE3': '0x00030021', 'INT_SPEAKER_SIZE': '0',
            'BASS_EXT': '0', 'SAT_GAIN_OFFSET': '0', 'MEMORY': '32', 'FLASH': '8',
            'AMP_ONTIME': '0', 'QPLAY_SUPPORT': ''}
        result = (FIXTURES / 'xml/device_description.xml').read_text()
        for name, value in replacements.items():
            result = result.replace('#' + name + '#', html.escape(value, quote=True))
        return result.encode()

    def topology(self):
        root = ET.Element('ZoneGroups')
        group = ET.SubElement(root, 'ZoneGroup', Coordinator=self.config.udn, ID=self.group_id)
        ET.SubElement(group, 'ZoneGroupMember', UUID=self.config.udn, Location=self.location,
            ZoneName=self.config.room, Icon='x-rincon-roomicon:living', Configuration='1',
            SoftwareVersion=PROFILE['sw'], MinCompatibleVersion=PROFILE['mincompat'],
            LegacyCompatibleVersion=PROFILE['legacycompat'], BootSeq=str(self.config.boot_sequence), WirelessMode='0',
            ChannelFreq='2412', BehindWifiExtender='0', WifiEnabled='1')
        return ET.tostring(root, encoding='unicode')

    def last_change(self, rendering=False):
        ns = ('urn:schemas-upnp-org:metadata-1-0/RCS/' if rendering
              else 'urn:schemas-upnp-org:metadata-1-0/AVT/')
        root = ET.Element('Event', xmlns=ns)
        instance = ET.SubElement(root, 'InstanceID', val='0')
        if rendering:
            for channel in ('Master', 'LF', 'RF'):
                ET.SubElement(instance, 'Volume', channel=channel, val=str(self.volume))
                ET.SubElement(instance, 'Mute', channel=channel, val=str(self.mute))
                ET.SubElement(instance, 'Loudness', channel=channel, val=str(self.loudness))
            ET.SubElement(instance, 'Bass', val=str(self.bass))
            ET.SubElement(instance, 'Treble', val=str(self.treble))
            ET.SubElement(instance, 'OutputFixed', val='0')
        else:
            for key, value in {'TransportState': self.transport, 'TransportStatus': 'OK',
                'CurrentPlayMode': self.play_mode, 'CurrentCrossfadeMode': '0',
                'NumberOfTracks': '0', 'CurrentTrack': '0', 'CurrentTrackDuration': '0:00:00',
                'CurrentMediaDuration': '0:00:00', 'AVTransportURI': self.uri,
                'AVTransportURIMetaData': self.metadata, 'CurrentTrackURI': self.uri,
                'CurrentTrackMetaData': self.metadata, 'NextAVTransportURI': '',
                'NextAVTransportURIMetaData': '', 'CurrentTransportActions': 'Play,Stop,Pause',
                'TransportPlaySpeed': '1', 'CurrentValidPlayModes': 'NORMAL,REPEAT_ALL,SHUFFLE_NOREPEAT'}.items():
                ET.SubElement(instance, key, val=value)
        return ET.tostring(root, encoding='unicode')

    def event_values(self, service):
        with self.lock:
            if service == 'ZoneGroupTopology':
                return {'ZoneGroupState': self.topology(), 'ZoneGroupName': self.config.room,
                        'ZoneGroupID': self.group_id, 'ZonePlayerUUIDsInGroup': self.config.udn,
                        'AvailableSoftwareUpdate': '', 'ThirdPartyMediaServersX': '<MediaServers/>',
                        'AlarmRunSequence': ''}
            if service == 'DeviceProperties':
                return {'ZoneName': self.config.room, 'Icon': 'x-rincon-roomicon:living',
                        'Configuration': '1', 'Invisible': '0', 'SettingsReplicationState': '',
                        'IsZoneBridge': '0', 'ChannelMapSet': '', 'HTSatChanMapSet': '',
                        'HTFreq': '0', 'WirelessMode': '0', 'ChannelFreq': '2412',
                        'BehindWifiExtender': '0', 'WifiEnabled': '1'}
            if service == 'AVTransport': return {'LastChange': self.last_change()}
            if service == 'RenderingControl': return {'LastChange': self.last_change(True)}
            if service == 'GroupRenderingControl':
                return {'GroupVolume': str(self.volume), 'GroupMute': str(self.mute), 'GroupVolumeChangeable': '1'}
            if service == 'ContentDirectory':
                return {'SystemUpdateID': '1', 'ContainerUpdateIDs': '', 'ShareListUpdateID': '1',
                        'ShareIndexInProgress': '0', 'ShareIndexLastError': '',
                        'UserRadioUpdateID': '1', 'RadioLocationUpdateID': '1',
                        'FavoritesUpdateID': '1', 'SavedQueuesUpdateID': '1', 'RadioFavoritesUpdateID': '1',
                        'FavoritePresetsUpdateID': '1', 'ShareListRefreshState': '',
                        'RecentlyPlayedUpdateID': '1', 'Browseable': '1'}
            if service == 'AlarmClock':
                return {'AlarmListVersion': '1', 'TimeGeneration': '1',
                        'TimeServer': '', 'TimeZone': '0', 'TimeFormat': '24H', 'DateFormat': 'YMD',
                        'DailyIndexRefreshTime': '02:00:00'}
            if service == 'MusicServices': return {'ServiceListVersion': '1'}
            if service == 'SystemProperties': return {'UpdateID': '1', 'ThirdPartyHash': ''}
            if service == 'GroupManagement':
                return {'GroupCoordinatorIsLocal': '1', 'LocalGroupUUID': self.group_id,
                        'ResetVolumeAfter': '0', 'VolumeAVTransportURI': self.uri}
            if service == 'AudioIn':
                return {'AudioInputName': 'Line-In', 'Icon': '', 'LineInConnected': '0',
                        'LeftLineInLevel': '0', 'RightLineInLevel': '0', 'Playing': '0'}
            if service == 'ConnectionManager':
                return {'SourceProtocolInfo': 'http-get:*:audio/mpeg:*',
                        'SinkProtocolInfo': 'http-get:*:audio/mpeg:*', 'CurrentConnectionIDs': '0'}
            if service == 'Queue':
                return {'LastChange': '<Event xmlns="urn:schemas-sonos-com:metadata-1-0/Queue/"><QueueID val="0"><UpdateID val="1"/></QueueID></Event>'}
            return {}

    def propertyset(self, service):
        root = ET.Element('e:propertyset', {'xmlns:e': EVENT_NS})
        for key, value in self.event_values(service).items():
            ET.SubElement(ET.SubElement(root, 'e:property'), key).text = str(value)
        return xml_bytes(root)

    def action(self, service, action, args):
        with self.lock:
            if action not in self.actions.get(service, {}):
                raise UPnPError(401, 'Invalid Action')
            expected = self.actions[service][action]
            if any(name not in args for name, direction, _ in expected if direction == 'in'):
                raise UPnPError(402, 'Invalid Args')
            defaults = {name: ('0' if kind in ('boolean', 'ui4', 'ui2', 'i4', 'i2', 'int') else '')
                        for name, direction, kind in expected if direction == 'out'}
            changed = []
            result = {}
            if service == 'ZoneGroupTopology':
                if action == 'GetZoneGroupState': result = {'ZoneGroupState': self.topology()}
                elif action == 'GetZoneGroupAttributes':
                    result = {'CurrentZoneGroupName': self.config.room,
                              'CurrentZoneGroupID': self.group_id,
                              'CurrentZonePlayerUUIDsInGroup': self.config.udn}
                elif action == 'CheckForUpdate': result = {'UpdateItem': self.update_item}
                elif action in ('BeginSoftwareUpdate', 'SubmitDiagnostics'):
                    raise UPnPError(501, 'Action not implemented by software peer')
            elif service == 'DeviceProperties':
                if action == 'GetHouseholdID': result = {'CurrentHouseholdID': self.config.household}
                elif action == 'GetZoneAttributes':
                    result = {'CurrentZoneName': self.config.room, 'CurrentIcon': 'x-rincon-roomicon:living', 'CurrentConfiguration': '1'}
                elif action == 'GetZoneInfo':
                    result = {'SerialNumber': self.config.mac.replace(':', '-').upper() + ':0',
                        'SoftwareVersion': PROFILE['sw'], 'DisplaySoftwareVersion': PROFILE['display'],
                        'HardwareVersion': PROFILE['hw'], 'IPAddress': self.config.advertise_ip,
                        'MACAddress': self.config.mac.upper(), 'CopyrightInfo': 'Synthetic software peer',
                        'ExtraInfo': 'emulated ZP80', 'HTAudioIn': '0'}
                elif action == 'GetLEDState': result = {'CurrentLEDState': self.led}
                elif action == 'SetLEDState': self.led = args['DesiredLEDState']; changed = [service]
                elif action == 'SetZoneAttributes': self.config.room = args['DesiredZoneName']; changed = [service, 'ZoneGroupTopology']
            elif service == 'SystemProperties':
                if action in ('GetString', 'GetStringX'): result = {'StringValue': self.properties.get(args['VariableName'], '')}
                elif action in ('SetString', 'SetStringX'): self.properties[args['VariableName']] = args['StringValue']; changed = [service]
                elif action in ('Remove', 'RemoveX'): self.properties.pop(args['VariableName'], None); changed = [service]
                elif 'Account' in action or 'Credential' in action or action == 'GetWebCode':
                    raise UPnPError(501, 'Online accounts are unavailable in software peer')
            elif service in ('RenderingControl', 'GroupRenderingControl'):
                if action in ('SetVolume', 'SetGroupVolume', 'RampToVolume', 'SetRelativeVolume', 'SetRelativeGroupVolume'):
                    value = self.volume + int(args['Adjustment']) if 'Adjustment' in args else int(args['DesiredVolume'])
                    self.volume = max(0, min(100, value)); changed = ['RenderingControl', 'GroupRenderingControl']
                elif action in ('SetMute', 'SetGroupMute'):
                    self.mute = int(args['DesiredMute'] in ('1', 'true', 'yes')); changed = ['RenderingControl', 'GroupRenderingControl']
                elif action in ('SetBass', 'SetTreble', 'SetLoudness'):
                    field = action[3:].lower(); setattr(self, field, int(args['Desired' + action[3:]])); changed = ['RenderingControl']
                result = {'CurrentVolume': str(self.volume), 'NewVolume': str(self.volume),
                    'CurrentMute': str(self.mute), 'CurrentBass': str(self.bass), 'CurrentTreble': str(self.treble),
                    'CurrentLoudness': str(self.loudness), 'CurrentSupportsFixed': '1',
                    'CurrentFixed': '0', 'MinValue': '-10240', 'MaxValue': '0', 'RampTime': '0'}
            elif service == 'AVTransport':
                if action in ('Play', 'Pause', 'Stop'):
                    self.transport = {'Play': 'PLAYING', 'Pause': 'PAUSED_PLAYBACK', 'Stop': 'STOPPED'}[action]; changed = [service]
                elif action == 'SetAVTransportURI': self.uri = args['CurrentURI']; self.metadata = args['CurrentURIMetaData']; changed = [service]
                elif action == 'SetPlayMode': self.play_mode = args['NewPlayMode']; changed = [service]
                result = {'CurrentTransportState': self.transport, 'CurrentTransportStatus': 'OK', 'CurrentSpeed': '1',
                    'NrTracks': '0', 'MediaDuration': '0:00:00', 'CurrentURI': self.uri,
                    'CurrentURIMetaData': self.metadata, 'NextURI': '', 'NextURIMetaData': '',
                    'PlayMedium': 'NETWORK', 'RecordMedium': 'NOT_IMPLEMENTED', 'WriteStatus': 'NOT_IMPLEMENTED',
                    'Track': '0', 'TrackDuration': '0:00:00', 'TrackMetaData': self.metadata,
                    'TrackURI': self.uri, 'RelTime': '0:00:00', 'AbsTime': '0:00:00',
                    'RelCount': '2147483647', 'AbsCount': '2147483647', 'PlayMedia': 'NETWORK',
                    'RecMedia': 'NOT_IMPLEMENTED', 'RecQualityModes': 'NOT_IMPLEMENTED',
                    'PlayMode': self.play_mode, 'RecQualityMode': 'NOT_IMPLEMENTED',
                    'Actions': 'Play,Stop,Pause', 'RemainingSleepTimerDuration': '', 'CurrentSleepTimerGeneration': '0'}
            elif service == 'ContentDirectory':
                result = {'Result': '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"/>',
                          'NumberReturned': '0', 'TotalMatches': '0', 'UpdateID': '1', 'Id': '1',
                          'IsBrowseable': '1', 'IsIndexing': '0', 'SearchCaps': '', 'SortCaps': 'dc:title',
                          'AlbumArtistDisplayOption': 'WMP', 'LastIndexChange': '0'}
            elif service == 'MusicServices':
                result = {'AvailableServiceDescriptorList': '<Services/>', 'AvailableServiceTypeList': '', 'AvailableServiceListVersion': '1'}
            elif service == 'AlarmClock':
                now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                result = {'CurrentTimeFormat': '24H', 'CurrentDateFormat': 'YMD', 'Index': '0',
                    'AutoAdjustDst': '0', 'CurrentUTCTime': now, 'CurrentLocalTime': now,
                    'HouseholdUTCTime': now, 'CurrentTimeZone': 'UTC', 'CurrentTimeGeneration': '1',
                    'CurrentAlarmList': '<Alarms/>', 'CurrentAlarmListVersion': '1',
                    'CurrentDailyIndexRefreshTime': '02:00:00'}
            elif service == 'ConnectionManager':
                result = {'Source': 'http-get:*:audio/mpeg:*', 'Sink': 'http-get:*:audio/mpeg:*',
                    'ConnectionIDs': '0', 'RcsID': '0', 'AVTransportID': '0', 'ProtocolInfo': 'http-get:*:audio/mpeg:*',
                    'PeerConnectionManager': '', 'PeerConnectionID': '-1', 'Direction': 'Input', 'Status': 'OK'}
            elif service == 'AudioIn':
                result = {'CurrentName': 'Line-In', 'CurrentIcon': '', 'CurrentLeftLineInLevel': '0',
                          'CurrentRightLineInLevel': '0', 'CurrentAudioInputName': 'Line-In'}
            defaults.update({key: str(value) for key, value in result.items() if key in defaults})
            return defaults, changed


def soap_response(service_type, action, values):
    root = ET.Element('s:Envelope', {'xmlns:s': SOAP_NS, 's:encodingStyle': 'http://schemas.xmlsoap.org/soap/encoding/'})
    response = ET.SubElement(ET.SubElement(root, 's:Body'), 'u:' + action + 'Response', {'xmlns:u': service_type})
    for name, value in values.items(): ET.SubElement(response, name).text = str(value)
    return xml_bytes(root)


def soap_fault(error):
    root = ET.Element('s:Envelope', {'xmlns:s': SOAP_NS})
    fault = ET.SubElement(ET.SubElement(root, 's:Body'), 's:Fault')
    ET.SubElement(fault, 'faultcode').text = 's:Client'
    ET.SubElement(fault, 'faultstring').text = 'UPnPError'
    detail = ET.SubElement(ET.SubElement(fault, 'detail'), 'UPnPError', xmlns='urn:schemas-upnp-org:control-1-0')
    ET.SubElement(detail, 'errorCode').text = str(error.code)
    ET.SubElement(detail, 'errorDescription').text = error.description
    return xml_bytes(root)


class PeerHandler(BaseHTTPRequestHandler):
    server_version = SERVER
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def handle(self):
        if not self.server.peer.config.browser_workers:
            return super().handle()
        try:
            return super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # A timed-out controller may close before headers or body finish.
            # Do not turn that expected disconnect into a traceback storm.
            self.close_connection = True

    def log_message(self, fmt, *args):
        print(json.dumps({'time': time.time(), 'remote': self.client_address[0],
                          'request': fmt % args}), flush=True)

    @property
    def peer(self): return self.server.peer

    def reply(self, code, content=b'', content_type='text/xml; charset="utf-8"', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Connection', 'close')
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD': self.wfile.write(content)

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/xml/device_description.xml': self.reply(200, self.peer.state.description())
        elif path.startswith('/xml/') and path[5:] in {x.name for x in (FIXTURES / 'xml').glob('*1.xml')}:
            self.reply(200, (FIXTURES / path.lstrip('/')).read_bytes())
        elif path == '/img/icon-ZP80.png': self.reply(200, (FIXTURES / 'img/icon-ZP80.png').read_bytes(), 'image/png')
        elif path == '/status/peer':
            self.reply(200, json.dumps({'uuid': self.peer.config.udn, 'household': self.peer.config.household,
                'location': self.peer.state.location, 'room': self.peer.config.room,
                'audio': 'simulated state only'}).encode(), 'application/json')
        else: self.reply(404, b'Not found', 'text/plain')

    def do_POST(self):
        service = self.peer.state.control_paths.get(urlsplit(self.path).path)
        if not service: self.reply(404, b'Not found', 'text/plain'); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 262144: raise UPnPError(402, 'Invalid Args')
            document = ET.fromstring(self.rfile.read(length))
            body = document.find('{%s}Body' % SOAP_NS)
            if body is None or len(body) != 1: raise UPnPError(402, 'Invalid Args')
            call = body[0]
            action = local_name(call.tag)
            service_type = self.peer.state.services[service]['serviceType']
            if call.tag != '{%s}%s' % (service_type, action): raise UPnPError(401, 'Invalid Action')
            args = {local_name(x.tag): x.text or '' for x in call}
            values, changed = self.peer.state.action(service, action, args)
            print(json.dumps({'time': time.time(), 'service': service, 'action': action, 'result': 'ok'}), flush=True)
            self.reply(200, soap_response(service_type, action, values), headers={'EXT': ''})
            for name in changed: self.peer.publish(name)
        except (ET.ParseError, ValueError): self.reply(500, soap_fault(UPnPError(402, 'Invalid Args')))
        except UPnPError as exc: self.reply(500, soap_fault(exc))

    def do_SUBSCRIBE(self):
        service = self.peer.state.event_paths.get(urlsplit(self.path).path)
        if not service: self.reply(404); return
        sid = self.headers.get('SID')
        timeout_header = self.headers.get('TIMEOUT', 'Second-1800')
        try:
            seconds = max(30, min(3600, int(timeout_header.split('-')[-1])))
        except ValueError: seconds = 1800
        with self.peer.state.lock:
            now = time.monotonic()
            if sid:
                subscription = self.peer.state.subscriptions.get(sid)
                if not subscription or subscription['service'] != service or subscription['expires'] < now:
                    self.reply(412); return
                subscription['expires'] = now + seconds
            else:
                callback = self.headers.get('CALLBACK', '')
                if self.headers.get('NT', '').lower() != 'upnp:event' or not re.fullmatch(r'<http://[^<>]+>', callback):
                    self.reply(412); return
                callback = callback[1:-1]
                if not self.peer.valid_callback(callback, self.client_address[0]): self.reply(412); return
                sid = 'uuid:' + str(uuid.uuid4())
                self.peer.state.subscriptions[sid] = {'service': service, 'callback': callback,
                    'expires': now + seconds, 'sequence': 0, 'notify_lock': threading.Lock()}
        self.reply(200, headers={'SID': sid, 'TIMEOUT': 'Second-%d' % seconds})
        if not self.headers.get('SID'):
            if self.peer.config.browser_workers:
                self.peer.queue_notification(sid, delay=.05)
            else:
                timer = threading.Timer(.05, self.peer.notify, args=(sid,))
                timer.daemon = True; timer.start()

    def do_UNSUBSCRIBE(self):
        sid = self.headers.get('SID')
        with self.peer.state.lock:
            subscription = self.peer.state.subscriptions.get(sid)
            if not subscription or self.peer.state.event_paths.get(urlsplit(self.path).path) != subscription['service']:
                self.reply(412); return
            del self.peer.state.subscriptions[sid]
        self.reply(200)


class ZP80Peer:
    def __init__(self, config=None):
        self.config = config or Config()
        self.state = PeerState(self.config)
        self.http = PeerHTTPServer((self.config.bind, self.config.port), PeerHandler)
        self.http.peer = self
        self.config.port = self.http.server_port
        self.stop_event = threading.Event()
        self.http_thread = None
        self.ssdp_thread = None
        self.ssdp_socket = None
        self.notification_thread = None
        self.notification_condition = threading.Condition() if self.config.browser_workers else None
        self.notification_queue = deque()
        self.notification_pending = set()
        self.notification_connection = None
        self.netstart = NetstartResponder(self.config.udn, self.config.household,
            bind='127.0.0.1' if ipaddress.IPv4Address(self.config.bind).is_loopback else '0.0.0.0',
            broadcast='127.0.0.1' if ipaddress.IPv4Address(self.config.bind).is_loopback else '255.255.255.255',
            port=self.config.netstart_port, log=lambda record: print(json.dumps(record), flush=True))

    def valid_callback(self, callback, source):
        parsed = urlsplit(callback)
        try:
            address = ipaddress.IPv4Address(parsed.hostname)
            parsed.port
        except (ipaddress.AddressValueError, TypeError, ValueError): return False
        if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.fragment: return False
        return str(address) == source or bool(self.config.callback_network and address in ipaddress.IPv4Network(self.config.callback_network))

    def notify(self, sid):
        with self.state.lock:
            subscription = self.state.subscriptions.get(sid)
            if not subscription or subscription['expires'] < time.monotonic():
                self.state.subscriptions.pop(sid, None); return
        with subscription['notify_lock']:
            body = self.state.propertyset(subscription['service'])
            destination = urlsplit(subscription['callback'])
            headers = {'Content-Type': 'text/xml; charset="utf-8"', 'NT': 'upnp:event',
                       'NTS': 'upnp:propchange', 'SID': sid, 'SEQ': str(subscription['sequence'])}
            connection = http.client.HTTPConnection(destination.hostname, destination.port or 80, timeout=3)
            if self.config.browser_workers:
                with self.notification_condition:
                    if self.stop_event.is_set():return
                    self.notification_connection = connection
            try:
                connection.request('NOTIFY', destination.path or '/', body, headers)
                if self.config.browser_workers and self.stop_event.is_set():return
                response = connection.getresponse(); response.read(4096)
                subscription['sequence'] = (subscription['sequence'] + 1) % 4294967296
                print(json.dumps({'time': time.time(), 'notify': subscription['service'], 'status': response.status}), flush=True)
            except OSError as exc:
                print(json.dumps({'time': time.time(), 'notify': subscription['service'], 'error': str(exc)}), flush=True)
            finally:
                connection.close()
                if self.config.browser_workers:
                    with self.notification_condition:
                        if self.notification_connection is connection:self.notification_connection = None

    def queue_notification(self, sid, delay=0):
        """Coalesce pending state for a SID; keep a bounded FIFO of distinct SIDs."""
        with self.notification_condition:
            if self.stop_event.is_set():return False
            if sid in self.notification_pending:return True
            if len(self.notification_queue) >= BROWSER_NOTIFY_QUEUE_LIMIT:return False
            self.notification_queue.append((sid, time.monotonic() + delay))
            self.notification_pending.add(sid)
            self.notification_condition.notify()
            return True

    def notification_loop(self):
        while not self.stop_event.is_set():
            with self.notification_condition:
                while not self.stop_event.is_set():
                    if not self.notification_queue:
                        self.notification_condition.wait();continue
                    sid, due = self.notification_queue[0]
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self.notification_condition.wait(remaining);continue
                    self.notification_queue.popleft();self.notification_pending.remove(sid)
                    break
                else:return
            self.notify(sid)

    def publish(self, service):
        with self.state.lock:
            ids = [sid for sid, sub in self.state.subscriptions.items() if sub['service'] == service]
        for sid in ids:
            if self.config.browser_workers:self.queue_notification(sid)
            else:threading.Thread(target=self.notify, args=(sid,), daemon=True).start()

    def ssdp_targets(self):
        return ('upnp:rootdevice', 'uuid:' + self.config.udn, ZONE_PLAYER)

    def ssdp_packet(self, target, alive=False):
        usn = 'uuid:' + self.config.udn
        if target != usn: usn += '::' + target
        headers = {'CACHE-CONTROL': 'max-age=1800', 'LOCATION': self.state.location,
                   'SERVER': PROFILE['server'], 'USN': usn, 'X-RINCON-HOUSEHOLD': self.config.household,
                   'X-RINCON-BOOTSEQ': str(self.config.boot_sequence)}
        if alive:
            first = 'NOTIFY * HTTP/1.1'
            headers.update({'HOST': SSDP_GROUP + ':1900', 'NT': target, 'NTS': 'ssdp:alive'})
        else:
            first = 'HTTP/1.1 200 OK'
            headers.update({'EXT': '', 'ST': target})
        return (first + '\r\n' + ''.join('%s: %s\r\n' % pair for pair in headers.items()) + '\r\n').encode()

    def search_responses(self, packet):
        try:
            lines = packet.decode('ascii').split('\r\n')
            if lines[0].upper() != 'M-SEARCH * HTTP/1.1': return []
            headers = {key.strip().upper(): value.strip() for line in lines[1:] if ':' in line
                       for key, value in [line.split(':', 1)]}
            if headers.get('MAN', '').strip('"').lower() != 'ssdp:discover': return []
            household = headers.get('X-RINCON-HOUSEHOLD', '')
            if household and household != self.config.household: return []
            target = headers.get('ST', '')
            targets = self.ssdp_targets() if target == 'ssdp:all' else (target,) if target in self.ssdp_targets() else ()
            return [self.ssdp_packet(value) for value in targets]
        except UnicodeDecodeError: return []

    def ssdp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.ssdp_socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', self.config.ssdp_port))
            membership = socket.inet_aton(SSDP_GROUP) + socket.inet_aton(self.config.advertise_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.config.advertise_ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.settimeout(1)
            next_alive = 0
            while not self.stop_event.is_set():
                if time.monotonic() >= next_alive:
                    for target in self.ssdp_targets(): sock.sendto(self.ssdp_packet(target, alive=True), (SSDP_GROUP, 1900))
                    next_alive = time.monotonic() + 20
                try: packet, source = sock.recvfrom(8192)
                except socket.timeout: continue
                for response in self.search_responses(packet): sock.sendto(response, source)
        except OSError as exc:
            if not self.stop_event.is_set(): print(json.dumps({'ssdp_error': str(exc)}), flush=True)
        finally: sock.close(); self.ssdp_socket = None

    def start(self, ssdp=True, netstart=True):
        if self.config.browser_workers:
            if self.http_thread is not None:raise RuntimeError('Browser workers already started')
            self.notification_thread = threading.Thread(target=self.notification_loop, name='zp80-notify', daemon=True)
            self.notification_thread.start()
        self.http_thread = threading.Thread(target=self.http.serve_forever, name='zp80-http', daemon=True)
        self.http_thread.start()
        if ssdp:
            self.ssdp_thread = threading.Thread(target=self.ssdp_loop, name='zp80-ssdp', daemon=True)
            self.ssdp_thread.start()
        if netstart: self.netstart.start()

    def close(self):
        self.stop_event.set()
        if self.config.browser_workers:
            with self.notification_condition:
                self.notification_queue.clear();self.notification_pending.clear()
                connection = self.notification_connection
                self.notification_condition.notify_all()
            if connection is not None:
                active_socket = connection.sock
                if active_socket is not None:
                    try:active_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                # The worker owns close(); shutdown wakes its blocked receive
                # without concurrently closing HTTPResponse's file object.
        self.netstart.close()
        self.http.shutdown(); self.http.server_close()
        if self.http_thread: self.http_thread.join(timeout=3)
        if self.ssdp_thread: self.ssdp_thread.join(timeout=3)
        if self.notification_thread: self.notification_thread.join(timeout=4)


def main():
    _startup_stage('parsing-arguments')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--advertise-ip', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=1400)
    parser.add_argument('--household', default='Sonos_EMULATED_CR100')
    parser.add_argument('--mac', default='02:00:00:00:00:80')
    parser.add_argument('--room', default='Software ZP80')
    parser.add_argument('--ssdp-port', type=int, default=1900)
    parser.add_argument('--callback-network', help='Additional isolated IPv4 subnet allowed for controller callbacks')
    parser.add_argument('--no-ssdp', action='store_true')
    parser.add_argument('--netstart-port', type=int, default=6969)
    parser.add_argument('--boot-sequence', type=int, default=1,
                        help='Increment after metadata changes so controllers refresh device capabilities')
    parser.add_argument('--no-netstart', action='store_true')
    parser.add_argument('--browser-workers', action='store_true',
                        help='Reuse bounded HTTP/notification workers for slow browser emulation')
    parser.add_argument('--profile', choices=sorted(PROFILES), default='4.3',
                        help='Version identity to report (7.1 copies the physical ZP80)')
    parser.add_argument('--update-item', type=Path,
                        help='File holding the inner UpdateItem XML returned by CheckForUpdate')
    for f in ('sw', 'display', 'mincompat', 'legacycompat'):
        parser.add_argument('--' + f, help='Override the %s field of the chosen profile (for the compat-floor sweep)' % f)
    args = parser.parse_args()
    global PROFILE
    PROFILE = dict(PROFILES[args.profile])
    for f in ('sw', 'display', 'mincompat', 'legacycompat'):
        if getattr(args, f) is not None:
            PROFILE[f] = getattr(args, f)
    PeerHandler.server_version = PROFILE['server']
    print(json.dumps({'stage': 'loading-contracts-and-binding', 'time': time.time()}), flush=True)
    try:
        peer = ZP80Peer(Config(**{key: value for key, value in vars(args).items()
                                  if key not in ('no_ssdp', 'no_netstart', 'profile', 'update_item',
                                                 'sw', 'display', 'mincompat', 'legacycompat')}))
        if args.update_item: peer.state.update_item = args.update_item.read_text().strip()
    except (ValueError, OSError) as exc: parser.error(str(exc))
    peer.start(ssdp=not args.no_ssdp, netstart=not args.no_netstart)
    print(json.dumps({'ready': True, 'location': peer.state.location,
                      'household': peer.config.household, 'uuid': peer.config.udn}), flush=True)
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: pass
    finally: peer.close()


if __name__ == '__main__': main()
