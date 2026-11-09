"""A small fake Sonos household for offline tests: players on loopback speaking local UPnP.

Each FakePlayer is one player (its own HTTP port, as http://127.0.0.1:<port>) answering
what the Sonos backend uses: AVTransport, RenderingControl, ContentDirectory Browse over
a `library` of DIDL entries, and ZoneGroupTopology from the shared FakeHousehold; GENA
SUBSCRIBE/UNSUBSCRIBE with an initial NOTIFY, and notify() to send more. Transport goes
to coordinators as on a real player (a member answers 800 for queue actions). Every
action is recorded in `calls` as (action, {argument: value}).
"""
from __future__ import annotations

import html
import http.client
import http.server
import itertools
import math
from pathlib import Path
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from sonos_upnp import DIDL_OPEN, SERVICES

PATHS = {path: name for name, (path, _) in SERVICES.items()}
PATHS['/DeviceProperties'] = 'DeviceProperties'
PICTURE_PATH = '/getaa'
FIXTURES = Path(__file__).resolve().parent / 'zp80' / 'fixtures' / 'xml'
SETTINGS_SERVICES = ('RenderingControl', 'DeviceProperties', 'AVTransport', 'AudioIn')
SETTINGS_ACTIONS = frozenset(('GetBass', 'SetBass', 'GetTreble', 'SetTreble',
    'GetLoudness', 'SetLoudness', 'GetSupportsOutputFixed', 'GetOutputFixed',
    'SetOutputFixed', 'GetLEDState', 'SetLEDState', 'GetRemainingSleepTimerDuration',
    'ConfigureSleepTimer', 'SetAudioInputAttributes', 'GetLineInLevel', 'SetLineInLevel',
    'GetZoneInfo', 'GetZoneAttributes'))


def entry(object_id, title, parent, upnp_class='object.item.audioItem.musicTrack', container=False, res='',
          artist='', album='', art='', res_md=''):
    """One DIDL-Lite entry as a player sends it."""
    tag = 'container' if container else 'item'
    fields = '<dc:title>%s</dc:title><upnp:class>%s</upnp:class>' % (html.escape(title), upnp_class)
    if artist: fields += '<dc:creator>%s</dc:creator>' % html.escape(artist)
    if album: fields += '<upnp:album>%s</upnp:album>' % html.escape(album)
    if art: fields += '<upnp:albumArtURI>%s</upnp:albumArtURI>' % html.escape(art)
    if res: fields += '<res protocolInfo="%s:*:*:*">%s</res>' % (res.split(':', 1)[0], html.escape(res))
    if res_md: fields += '<r:resMD>%s</r:resMD>' % html.escape(res_md)
    return '<%s id="%s" parentID="%s" restricted="true">%s</%s>' % (tag, html.escape(object_id), html.escape(parent),
                                                                    fields, tag)


class FakeHousehold:
    def __init__(self):
        self.players = {}           # uuid -> FakePlayer
        self.coordinator = {}       # uuid -> its coordinator's uuid
        self.library = {}           # object ID -> [entry XML], shared by every player

    def add(self, uuid, name, **state):
        player = FakePlayer(self, uuid, name, **state)
        self.players[uuid] = player
        self.coordinator[uuid] = uuid
        return player

    def zone_group_state(self):
        groups = []
        for coordinator in dict.fromkeys(self.coordinator.values()):
            members = ''.join('<ZoneGroupMember UUID="%s" Location="%s/xml/device_description.xml" ZoneName="%s"/>'
                              % (u, self.players[u].base, html.escape(self.players[u].name))
                              for u, c in self.coordinator.items() if c == coordinator)
            groups.append('<ZoneGroup Coordinator="%s" ID="%s:1">%s</ZoneGroup>' % (coordinator, coordinator, members))
        return '<ZoneGroupState><ZoneGroups>%s</ZoneGroups></ZoneGroupState>' % ''.join(groups)

    def regroup(self, uuid, coordinator):
        self.coordinator[uuid] = coordinator
        for player in self.players.values(): player.notify('ZoneGroupTopology')

    def close(self):
        for player in self.players.values(): player.close()


class FakePlayer:
    def __init__(self, household, uuid, name, volume=20, line_in_connected=None,
                 line_in_name='Line-In', line_in_icon='AudioComponent', settings=False,
                 supports_output_fixed=True, model='ZP80'):
        self.household, self.uuid, self.name = household, uuid, name
        self.volume, self.mute = volume, False
        self.line_in_connected, self.line_in_name, self.line_in_icon = line_in_connected, line_in_name, line_in_icon
        self.transport, self.play_mode = 'STOPPED', 'NORMAL'
        self.uri, self.uri_metadata = '', ''
        self.queue, self.track_number, self.position = [], 0, '0:00:00'
        self.track_metadata, self.track_duration = '', '0:00:00'
        self.actions = 'Set, Stop, Pause, Play, Next, Previous, Seek'
        # Native settings fixtures are opt-in so existing playback tests retain
        # their original discovery/identity behavior.
        self.settings, self.model = settings, model
        self.channel_volumes = {'LF': 100, 'RF': 100}
        self.channel_mutes = {'LF': False, 'RF': False}
        self.bass, self.treble, self.loudness = 0, 0, False
        self.led, self.output_fixed = 'On', False
        self.supports_output_fixed = supports_output_fixed
        self.line_in_levels = (1, 1)
        self.sleep_deadline, self.sleep_generation = None, 0
        self.disabled_settings_actions = set()
        self.queue_update = 0
        self.reject = {}            # action -> UPnP error code to answer with
        self.refuse_service = None  # a music service number (2311) whose share links fail, as without an account
        self.calls, self.subscribers = [], {}
        self.sids = itertools.count(1)
        self.lock = threading.Lock()
        player = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                status, data = player.control(self.path, self.headers.get('SOAPACTION', ''), body)
                self.reply(status, data.encode(), {'Content-Type': 'text/xml; charset="utf-8"'})

            def do_SUBSCRIBE(self):
                service = PATHS.get(self.path.rsplit('/Event', 1)[0])
                sid = self.headers.get('SID')
                if sid:
                    status = 200 if sid in player.subscribers else 412
                    return self.reply(status, b'', {'SID': sid, 'TIMEOUT': 'Second-1800'})
                callback = re.search(r'<([^>]+)>', self.headers.get('CALLBACK', '')).group(1)
                sid = 'uuid:%s_sub%d' % (player.uuid, next(player.sids))
                player.subscribers[sid] = (service, callback, itertools.count())
                self.reply(200, b'', {'SID': sid, 'TIMEOUT': 'Second-1800'})
                threading.Thread(target=player.send, args=(sid,), daemon=True).start()

            def do_UNSUBSCRIBE(self):
                player.subscribers.pop(self.headers.get('SID'), None)
                self.reply(200, b'', {})

            def do_GET(self):
                if self.path.startswith(PICTURE_PATH):
                    from fake_ha import picture_bytes
                    return self.reply(200, picture_bytes(), {'Content-Type': 'image/jpeg'})
                if player.settings:
                    if self.path == '/xml/device_description.xml':
                        return self.reply(200, player.description(), {'Content-Type': 'text/xml'})
                    match = re.fullmatch(r'/xml/([A-Za-z]+)1\.xml', self.path)
                    if match and match.group(1) in player.settings_services():
                        return self.reply(200, player.scpd(match.group(1)), {'Content-Type': 'text/xml'})
                self.reply(404, b'', {})

            def reply(self, status, data, headers):
                self.send_response(status)
                for name, value in headers.items(): self.send_header(name, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown(); self.server.server_close()

    def settings_services(self):
        return tuple(service for service in SETTINGS_SERVICES
                     if service != 'AudioIn' or self.line_in_connected is not None)

    def description(self):
        """Native discovery contract for settings/identity integration tests."""
        ns = {'d': 'urn:schemas-upnp-org:device-1-0'}
        text = (FIXTURES / 'device_description.xml').read_text()
        for key, value in {'UUID': self.uuid, 'MODEL': self.model, 'HOST': '127.0.0.1',
                           'NAME': self.name, 'DISPLAY_NAME': self.model,
                           'SW_VERSION': '57.23-74170', 'HW_VERSION': '1.1.1.1-1',
                           'SERIAL_NUM': '00-0E-58-FA-KE-00:0', 'DISPLAY_VERSION': '11.16.1'}.items():
            text = text.replace('#' + key + '#', html.escape(value))
        root = ET.fromstring(text)
        device = root.find('d:device', ns)
        for tag, value in (('UDN', 'uuid:' + self.uuid), ('modelNumber', self.model),
                           ('modelName', 'Sonos ' + self.model), ('friendlyName', self.name),
                           ('serialNum', '00-0E-58-FA-KE-00:0'), ('softwareVersion', '57.23-74170')):
            element = device.find('d:' + tag, ns)
            if element is not None: element.text = value
        # Services can be embedded in the MediaRenderer/MediaServer child devices.
        for owner in root.iter():
            for child in list(owner):
                if child.tag.rpartition('}')[2] != 'serviceList': continue
                for service in list(child):
                    kind = service.findtext('d:serviceType', namespaces=ns) or ''
                    if kind.rsplit(':', 2)[-2] not in self.settings_services():
                        child.remove(service)
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)

    def scpd(self, service):
        ns = {'s': 'urn:schemas-upnp-org:service-1-0'}
        root = ET.parse(FIXTURES / (service + '1.xml')).getroot()
        actions = root.find('s:actionList', ns)
        for action in list(actions):
            name = action.findtext('s:name', namespaces=ns)
            if name in self.disabled_settings_actions or not hasattr(self, 'do_' + name):
                actions.remove(action)
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)

    @property
    def coordinator(self):
        return self.household.coordinator[self.uuid] == self.uuid

    # --- state as a player reports it ------------------------------------------------
    def play_track_metadata(self, title, artist='', album='', art='', duration='0:03:00', stream_content=''):
        """What plays now (CurrentTrackMetaData)."""
        extra = '<r:streamContent>%s</r:streamContent>' % html.escape(stream_content) if stream_content else ''
        self.track_metadata = DIDL_OPEN + entry('-1', title, '-1', artist=artist, album=album, art=art) \
            .replace('</item>', extra + '</item>') + '</DIDL-Lite>'
        self.track_duration = duration

    def avt_values(self):
        return {'TransportState': self.transport, 'CurrentPlayMode': self.play_mode,
                'NumberOfTracks': str(len(self.queue) if self.uri.startswith('x-rincon-queue:') else 1 if self.uri else 0),
                'CurrentTrack': str(self.track_number), 'CurrentTrackDuration': self.track_duration,
                'CurrentTrackMetaData': self.track_metadata, 'AVTransportURI': self.uri,
                'AVTransportURIMetaData': self.uri_metadata, 'CurrentTransportActions': self.actions}

    def event_body(self, service):
        if service == 'AVTransport':
            values = self.avt_values()
            change = '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/"><InstanceID val="0">%s</InstanceID></Event>' \
                % ''.join('<%s val="%s"/>' % (k, html.escape(v)) for k, v in values.items())
            properties = {'LastChange': change}
        elif service == 'RenderingControl':
            properties = {'LastChange': '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/"><InstanceID val="0">'
                          '<Volume channel="Master" val="%d"/><Volume channel="LF" val="%d"/>'
                          '<Volume channel="RF" val="%d"/><Mute channel="Master" val="%d"/>'
                          '%s</InstanceID></Event>' % (self.volume, self.channel_volumes['LF'],
                          self.channel_volumes['RF'], self.mute,
                          '<Bass val="%d"/><Treble val="%d"/><Loudness channel="Master" val="%d"/>'
                          '<OutputFixed val="%d"/>' % (self.bass, self.treble, self.loudness, self.output_fixed)
                          if self.settings else '')}
        elif service == 'ContentDirectory':
            properties = {'ContainerUpdateIDs': 'Q:0,%d' % self.queue_update}
        elif service == 'ZoneGroupTopology':
            properties = {'ZoneGroupState': self.household.zone_group_state()}
        elif service == 'AudioIn':
            properties = {'AudioInputName': self.line_in_name, 'Icon': self.line_in_icon,
                          'LineInConnected': str(int(bool(self.line_in_connected))),
                          'LeftLineInLevel': str(self.line_in_levels[0]),
                          'RightLineInLevel': str(self.line_in_levels[1])}
        else:
            properties = {}
        return ('<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">%s</e:propertyset>' % ''.join(
            '<e:property><%s>%s</%s></e:property>' % (k, html.escape(v), k) for k, v in properties.items())).encode()

    def send(self, sid):
        found = self.subscribers.get(sid)
        if not found: return
        service, callback, sequence = found
        parts = urlsplit(callback)
        try:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
            connection.request('NOTIFY', parts.path, self.event_body(service), {
                'Content-Type': 'text/xml', 'NT': 'upnp:event', 'NTS': 'upnp:propchange', 'SID': sid,
                'SEQ': str(next(sequence))})
            connection.getresponse().read(); connection.close()
        except OSError:
            pass

    def notify(self, service):
        for sid, (subscribed, _, _) in list(self.subscribers.items()):
            if subscribed == service: self.send(sid)

    # --- actions ----------------------------------------------------------------------
    def control(self, path, soap_action, body):
        service_type, _, action = soap_action.strip('"').partition('#')
        root = ET.fromstring(body)
        call = root.find('.//{%s}%s' % (service_type, action))
        args = {child.tag: child.text or '' for child in call} if call is not None else {}
        with self.lock:
            self.calls.append((action, args))
            code = self.reject.get(action)
            if action in self.disabled_settings_actions or action in SETTINGS_ACTIONS and not self.settings:
                code = 401
            if code is None:
                try:
                    handler = getattr(self, 'do_' + action, None)
                    if handler is None: raise UPnPError(401)
                    result = handler(args)
                except UPnPError as error:
                    code = error.code
                except (KeyError, ValueError):
                    code = 402
        if code is not None:
            return 500, ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                         '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
                         '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d</errorCode></UPnPError>'
                         '</detail></s:Fault></s:Body></s:Envelope>') % code
        return 200, ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                     '<u:%sResponse xmlns:u="%s">%s</u:%sResponse></s:Body></s:Envelope>') % (
            action, service_type, ''.join('<%s>%s</%s>' % (k, html.escape(str(v)), k) for k, v in (result or {}).items()),
            action)

    def need_coordinator(self):
        if not self.coordinator: raise UPnPError(800)

    def do_GetTransportInfo(self, args):
        return {'CurrentTransportState': self.transport if self.coordinator else 'PLAYING',
                'CurrentTransportStatus': 'OK', 'CurrentSpeed': '1'}

    def do_GetPositionInfo(self, args):
        return {'Track': self.track_number, 'TrackDuration': self.track_duration, 'TrackMetaData': self.track_metadata,
                'TrackURI': '', 'RelTime': self.position, 'AbsTime': 'NOT_IMPLEMENTED'}

    def do_GetMediaInfo(self, args):
        values = self.avt_values()
        return {'NrTracks': values['NumberOfTracks'], 'CurrentURI': self.uri, 'CurrentURIMetaData': self.uri_metadata}

    def do_GetTransportSettings(self, args): return {'PlayMode': self.play_mode}
    def do_GetCurrentTransportActions(self, args): return {'Actions': self.actions if self.coordinator else 'Start, Stop'}
    def do_GetVolume(self, args):
        channel = args.get('Channel', 'Master')
        if channel == 'Master': return {'CurrentVolume': self.volume}
        if channel in self.channel_volumes: return {'CurrentVolume': self.channel_volumes[channel]}
        raise UPnPError(402)
    def do_GetMute(self, args):
        channel = args.get('Channel', 'Master')
        if channel == 'Master': return {'CurrentMute': int(self.mute)}
        if channel in self.channel_mutes: return {'CurrentMute': int(self.channel_mutes[channel])}
        raise UPnPError(402)
    def do_GetAudioInputAttributes(self, args):
        if self.line_in_connected is None: raise UPnPError(401)
        return {'CurrentName': self.line_in_name, 'CurrentIcon': self.line_in_icon}
    def do_SetVolume(self, args):
        if self.output_fixed: raise UPnPError(501)
        channel, value = args.get('Channel', 'Master'), int(args['DesiredVolume'])
        if not 0 <= value <= 100: raise UPnPError(402)
        if channel == 'Master': self.volume = value
        elif channel in self.channel_volumes: self.channel_volumes[channel] = value
        else: raise UPnPError(402)
    def do_SetMute(self, args):
        channel = args.get('Channel', 'Master')
        if args.get('DesiredMute') not in ('0', '1'): raise UPnPError(402)
        if channel == 'Master': self.mute = args['DesiredMute'] == '1'
        elif channel in self.channel_mutes: self.channel_mutes[channel] = args['DesiredMute'] == '1'
        else: raise UPnPError(402)

    def do_GetBass(self, args): return {'CurrentBass': self.bass}
    def do_GetTreble(self, args): return {'CurrentTreble': self.treble}
    def do_SetBass(self, args): self.set_tone('bass', args['DesiredBass'])
    def do_SetTreble(self, args): self.set_tone('treble', args['DesiredTreble'])

    def set_tone(self, name, value):
        value = int(value)
        if not -10 <= value <= 10: raise UPnPError(402)
        if self.output_fixed: raise UPnPError(501)
        setattr(self, name, value)

    def do_GetLoudness(self, args):
        if args.get('Channel') != 'Master': raise UPnPError(402)
        return {'CurrentLoudness': int(self.loudness)}

    def do_SetLoudness(self, args):
        if self.output_fixed: raise UPnPError(501)
        if args.get('Channel') != 'Master' or args.get('DesiredLoudness') not in ('0', '1'):
            raise UPnPError(402)
        self.loudness = args['DesiredLoudness'] == '1'

    def do_GetSupportsOutputFixed(self, args): return {'CurrentSupportsFixed': int(self.supports_output_fixed)}
    def do_GetOutputFixed(self, args): return {'CurrentFixed': int(self.output_fixed)}
    def do_SetOutputFixed(self, args):
        if not self.supports_output_fixed: raise UPnPError(401)
        if args.get('DesiredFixed') not in ('0', '1'): raise UPnPError(402)
        self.output_fixed = args['DesiredFixed'] == '1'

    def do_GetLEDState(self, args): return {'CurrentLEDState': self.led}
    def do_SetLEDState(self, args):
        if args.get('DesiredLEDState') not in ('On', 'Off'): raise UPnPError(402)
        self.led = args['DesiredLEDState']

    def do_ConfigureSleepTimer(self, args):
        self.need_coordinator()
        duration = args['NewSleepTimerDuration']
        if duration == '': self.sleep_deadline = None
        else:
            match = re.fullmatch(r'(\d{1,2}):([0-5]\d):([0-5]\d)', duration)
            if not match: raise UPnPError(402)
            hours, minutes, seconds = map(int, match.groups())
            if hours >= 24: raise UPnPError(402)
            self.sleep_deadline = time.monotonic() + hours * 3600 + minutes * 60 + seconds
        self.sleep_generation += 1

    def do_GetRemainingSleepTimerDuration(self, args):
        self.need_coordinator()
        duration = ''
        if self.sleep_deadline is not None:
            seconds = max(0, math.ceil(self.sleep_deadline - time.monotonic()))
            duration = '%d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)
        return {'RemainingSleepTimerDuration': duration, 'CurrentSleepTimerGeneration': self.sleep_generation}

    def do_SetAudioInputAttributes(self, args):
        if self.line_in_connected is None: raise UPnPError(401)
        self.line_in_name, self.line_in_icon = args['DesiredName'], args['DesiredIcon']

    def do_GetLineInLevel(self, args):
        if self.line_in_connected is None: raise UPnPError(401)
        return {'CurrentLeftLineInLevel': self.line_in_levels[0], 'CurrentRightLineInLevel': self.line_in_levels[1]}

    def do_SetLineInLevel(self, args):
        if self.line_in_connected is None: raise UPnPError(401)
        values = int(args['DesiredLeftLineInLevel']), int(args['DesiredRightLineInLevel'])
        if any(not -(2**31) <= value < 2**31 for value in values): raise UPnPError(402)
        self.line_in_levels = values

    def do_GetZoneInfo(self, args):
        return {'SerialNumber': '00-0E-58-FA-KE-00:0', 'SoftwareVersion': '57.23-74170',
                'DisplaySoftwareVersion': '11.16.1', 'HardwareVersion': '1.1.1.1-1',
                'IPAddress': '127.0.0.1', 'MACAddress': '00:0E:58:FA:KE:00', 'ExtraInfo': ''}

    def do_GetZoneAttributes(self, args):
        return {'CurrentZoneName': self.name, 'CurrentIcon': '', 'CurrentConfiguration': '1'}

    def do_Play(self, args): self.need_coordinator(); self.transport = 'PLAYING'
    def do_Pause(self, args): self.need_coordinator(); self.transport = 'PAUSED_PLAYBACK'
    def do_Stop(self, args): self.need_coordinator(); self.transport = 'STOPPED'
    def do_Next(self, args): self.need_coordinator(); self.track_number += 1
    def do_Previous(self, args): self.need_coordinator(); self.track_number = max(1, self.track_number - 1)

    def do_Seek(self, args):
        self.need_coordinator()
        if args['Unit'] == 'TRACK_NR': self.track_number = int(args['Target'])
        else: self.position = args['Target']

    def do_SetPlayMode(self, args): self.need_coordinator(); self.play_mode = args['NewPlayMode']

    def do_SetAVTransportURI(self, args):
        uri = args['CurrentURI']
        if uri.startswith('x-rincon:'):
            threading.Thread(target=self.household.regroup, args=(self.uuid, uri[len('x-rincon:'):]), daemon=True).start()
            return
        self.uri, self.uri_metadata = uri, args.get('CurrentURIMetaData', '')

    def do_BecomeCoordinatorOfStandaloneGroup(self, args):
        threading.Thread(target=self.household.regroup, args=(self.uuid, self.uuid), daemon=True).start()

    def do_AddURIToQueue(self, args):
        self.need_coordinator()
        if self.refuse_service and 'SA_RINCON%d_' % self.refuse_service in args.get('EnqueuedURIMetaData', ''):
            raise UPnPError(800)
        first = int(args.get('DesiredFirstTrackNumberEnqueued') or 0) or len(self.queue) + 1
        self.queue.insert(first - 1, (args['EnqueuedURI'], args.get('EnqueuedURIMetaData', '')))
        self.queue_update += 1
        return {'FirstTrackNumberEnqueued': first, 'NumTracksAdded': 1, 'NewQueueLength': len(self.queue)}

    def do_RemoveTrackFromQueue(self, args):
        self.need_coordinator()
        number = int(args['ObjectID'].rpartition('/')[2])
        if not 0 < number <= len(self.queue): raise UPnPError(800)
        del self.queue[number - 1]; self.queue_update += 1

    def do_RemoveAllTracksFromQueue(self, args):
        self.need_coordinator()
        if not self.queue: raise UPnPError(804)
        self.queue = []; self.queue_update += 1

    def do_GetZoneGroupState(self, args):
        return {'ZoneGroupState': self.household.zone_group_state()}

    def do_Browse(self, args):
        object_id, start, count = args['ObjectID'], int(args['StartingIndex']), int(args['RequestedCount'])
        if object_id == 'AI:' and self.line_in_connected is not None:
            children = [entry('AI:0', self.line_in_name, 'AI:', 'object.item.audioItem',
                              res='x-rincon-stream:' + self.uuid)]
        elif object_id == 'Q:0':
            children = [entry('Q:0/%d' % n, uri.rsplit('/', 1)[-1], 'Q:0', res=uri, art=PICTURE_PATH + '?u=%d' % n)
                        for n, (uri, _) in enumerate(self.queue, 1)]
        elif object_id in self.household.library:
            children = self.household.library[object_id]
        else:
            found = [e for items in self.household.library.values() for e in items if 'id="%s"' % object_id in e]
            if args['BrowseFlag'] == 'BrowseMetadata' and found:
                return {'Result': DIDL_OPEN + found[0] + '</DIDL-Lite>', 'NumberReturned': 1, 'TotalMatches': 1,
                        'UpdateID': 1}
            raise UPnPError(701)
        if args['BrowseFlag'] == 'BrowseMetadata':
            parent = object_id.rpartition('/')[0] or '0'
            container = entry(object_id, object_id, parent, 'object.container', container=True)
            return {'Result': DIDL_OPEN + container + '</DIDL-Lite>', 'NumberReturned': 1, 'TotalMatches': 1,
                    'UpdateID': 1}
        page = children[start:start + count] if count else children[start:]
        return {'Result': DIDL_OPEN + ''.join(page) + '</DIDL-Lite>', 'NumberReturned': len(page),
                'TotalMatches': len(children), 'UpdateID': self.queue_update}


class UPnPError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
