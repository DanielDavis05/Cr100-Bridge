#!/usr/bin/env python3
"""A household of virtual ZonePlayers for the CR100 on the SonosNet TAP.

Each player is the emulator's software ZP80 (`cr100-emulator/network/zp80_peer.py`,
verified against the original CR100 runtime: discovery, topology, browsing,
volume/mute), with its own IP on the TAP, port 1400, UUID, room name and
transport/volume state. This module adds what a multi-room household needs:

- one ZoneGroupTopology shared by all players, so the CR100 sees every room;
- grouping: SetAVTransportURI x-rincon:<UUID> joins that player's group and
  BecomeCoordinatorOfStandaloneGroup leaves it; topology events go out on every
  player, wherever the CR100 subscribed;
- one SSDP listener answering the CR100's M-SEARCH (ST ZonePlayer:1, filtered on
  X-RINCON-HOUSEHOLD) for every player, each reply sent from that player's IP.

Netstart is not used here: the radio side (`netstart_pair.py`) already paired the
CR100 with the bridge household. Players report the '7.1' profile, a copy of the
physical ZP80 this CR100 (4.3.2) runs against.

Rooms can be backed by real players through a backend (`backends.py`; Home Assistant
media_player entities in `ha_backend.py`): transport, volume, mute, grouping and
now-playing with album art then follow the real speakers. Rooms without one stay
simulated. The Music menu (favourites, library, Sonos playlists, radio, each room's
queue) comes from the backend's content (`ha_content.py` for Home Assistant).

Usage: sudo python3 zoneplayers.py --state bridge-state.json --rooms "Living Room" Kitchen
       sudo python3 zoneplayers.py --state bridge-state.json --config bridge-config.json
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import backends
import build_info
import room_icons
from alarms import AlarmError
from backends import LINE_IN_PREFIX, BackendError
from content import ContentError, ContentSource, Node
from controller_visibility import BRIDGE_COMPONENT, component_visible, room_component_key, visible_components
from ha_backend import HomeAssistantDisconnected
from spotify_controllers import ControllerNotices

_HERE = Path(__file__).resolve().parent
# The player implementation and protocol fixtures ship with this distribution.
DEFAULT_PEER_DIR = _HERE / 'zp80'
SSDP_PORT = 1900
ALIVE_INTERVAL = 600   # the CR100 finds players by M-SEARCH; each alive round is a burst to it
DUPLICATE_WINDOW = 0.5   # the CR100 sends each M-SEARCH to multicast and broadcast
PUBLISH_DELAY = 0.2      # s; backends (Home Assistant) often report one change as several updates
MAX_SLOTS = 38           # player addresses .11-.48, below the DHCP range (.50-.99)
IDENTITY_INTERVAL = 1.0
UPSTREAM_INTERVAL = 30.0
BRIDGE_KEY = 'cr100-bridge'
MIN_CONTROLLER_VERSION = '24.1-00000'  # firmware 4.3; 4.3.2 is 25.2-50130


def about_text(value, limit=64):
    """Respect the CR100's byte-sized strings without splitting UTF-8 characters."""
    value = ''.join(c for c in str(value or '') if ord(c) >= 32 or c == '\n')
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def component_mac(key):
    digest = bytearray(hashlib.sha256(key.encode()).digest()[:4])
    digest[0] = 0x80 | (digest[0] & 0x7f)  # separate from the visible room slots
    return '02:69:' + ':'.join('%02x' % b for b in digest)


def upstream_address(targets, controller_ip):
    """Select the real-network source address using routes, without sending traffic."""
    isolated = ipaddress.ip_network(controller_ip + '/24', strict=False)
    for target in list(dict.fromkeys(targets)) + ['192.0.2.1']:
        if not target: continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9))
                address = ipaddress.IPv4Address(probe.getsockname()[0])
            if not (address.is_loopback or address.is_unspecified or address.is_link_local or address in isolated):
                return str(address)
        except (OSError, ValueError):
            continue
    return ''


def load_peer_module(peer_dir):
    sys.path.insert(0, str(peer_dir))
    import zp80_peer
    return zp80_peer


def configure_profile(zp, name='7.1'):
    """Keep the player's protocol persona while admitting original 4.3 controllers.

    Both discovery floors must agree: old controllers use the legacy floor when
    offered, while the regular floor is also present in descriptions/topology.
    Leave the captured reference profiles intact for standalone emulator use.
    """
    zp.PROFILE = dict(zp.PROFILES[name], mincompat=MIN_CONTROLLER_VERSION,
                      legacycompat=MIN_CONTROLLER_VERSION)
    zp.PeerHandler.server_version = zp.PROFILE['server']


def player_mac(slot):
    """Locally administered, stable per room slot (see load_rooms)."""
    return '02:69:00:00:00:%02x' % (slot + 1)


QUEUE_ACTIONS = ('AddURIToQueue', 'RemoveTrackFromQueue', 'RemoveAllTracksFromQueue')
AVT_EVENT_KEYS = ('TransportState', 'CurrentPlayMode', 'NumberOfTracks', 'CurrentTrack', 'CurrentTrackDuration',
                  'CurrentMediaDuration', 'AVTransportURI', 'AVTransportURIMetaData', 'CurrentTrackURI',
                  'CurrentTrackMetaData', 'CurrentTransportActions')


def build_classes(zp):
    class HouseholdState(zp.PeerState):
        def __init__(self, config, household, backend=None, identity=None, informational=False, bridge=False):
            super().__init__(config)
            self.household = household
            self.backend = backend   # a backends.Room, or None for a simulated room
            self.http_source = threading.local()
            self.identity = identity
            self.informational, self.is_bridge = informational, bridge

        def description(self):
            # Only the display label changes. Numeric versions/model capabilities
            # stay compatible, including explicit feature tags that prevent the
            # legacy GetZoneInfo fallback from entering compatibility discovery.
            text = super().description().decode()
            profile = self.household.discovery_profile(getattr(self.http_source, 'address', None))
            for tag, key in (('softwareVersion', 'sw'), ('minCompatibleVersion', 'mincompat'),
                             ('legacyCompatibleVersion', 'legacycompat')):
                text = re.sub(r'(<%s>)[^<]*(</%s>)' % (tag, tag),
                              lambda m, key=key: m[1] + html.escape(profile[key]) + m[2], text)
            model = (self.identity or {}).get('model') or 'Bridge Virtual Component'
            text = text.replace('<displayName>ZonePlayer 80</displayName>',
                                '<displayName>%s</displayName>' % html.escape(about_text(model)))
            if self.is_bridge and not self.household.bridge_as_zone(getattr(self.http_source, 'address', None)):
                for old, new in (('<modelNumber>ZP80</modelNumber>', '<modelNumber>ZoneBridge</modelNumber>'),
                                 ('<zoneType>1</zoneType>', '<zoneType>4</zoneType>'),
                                 ('<feature1>0x00310001</feature1>', '<feature1>0x00000000</feature1>'),
                                 ('<feature2>0x00006172</feature2>', '<feature2>0x00008173</feature2>'),
                                 ('<feature3>0x00030021</feature3>', '<feature3>0x00030000</feature3>')):
                    text = text.replace(old, new)
            return text.encode()

        def topology(self):
            return self.household.topology(getattr(self.http_source, 'address', None))

        def backend_values(self):
            if not self.backend:
                values = dict(TransportState=self.transport, CurrentPlayMode=self.play_mode,
                              NumberOfTracks='0', CurrentTrack='0', CurrentTrackDuration='0:00:00',
                              CurrentMediaDuration='0:00:00', AVTransportURI=self.uri,
                              AVTransportURIMetaData=self.metadata, CurrentTrackURI=self.uri,
                              CurrentTrackMetaData=self.metadata, CurrentTransportActions='Play,Stop,Pause')
                return self.alarm_values(self.household.source_values(self.config.udn, values))
            source = self.backend.line_in_source()
            uri = self.household.line_in_uri(self.backend.backend, source) if source is not None else None
            metadata = ''
            if source is not None:
                source_player = self.household.by_ref.get((self.backend.ref[0], source))
                source_visible = source_player is None or self.household.component_allowed(
                    source_player, getattr(self.http_source, 'address', None))
                info = self.household.line_in_input((self.backend.backend.name, source))
                # An empty media title makes the CR100 display "Internet Radio",
                # even though it correctly identifies the source room by URI.
                if not source_visible: uri = LINE_IN_PREFIX + self.config.udn
                node = Node('AI:0', 'AI:', info['name'] if info and source_visible else 'Line-In', source,
                            'line_in', False, 'object.item.audioItem', uri=uri)
                metadata = self.household.line_in_content.didl([node], self.config.udn)
            return self.alarm_values(self.household.source_values(self.config.udn, self.backend.values(self.config.udn, uri, metadata)))

        def alarm_values(self, values):
            alarms = self.household.alarms
            if alarms is None: return values
            try: runs = [run for run in alarms.active(self.config.udn) if run['status'] == 'playing']
            except AlarmError: return values  # Informational components have no alarm source.
            if not runs: return values
            run = runs[-1]
            if not run.get('fallback') and run['context']['media_ref']['provider'] != 'chime': return values
            # Buzzer playback has no backend track title; publish its alarm source
            # in events and SOAP results so the controller can display it.
            uri = 'x-rincon-buzzer:0'
            metadata = backends.didl(run['snapshot']['title'], uri=uri, item_id='alarm:' + run['id'])
            values.update(AVTransportURI=uri, CurrentTrackURI=uri, CurrentURI=uri, TrackURI=uri,
                          AVTransportURIMetaData=metadata, CurrentTrackMetaData=metadata,
                          CurrentURIMetaData=metadata, TrackMetaData=metadata,
                          NumberOfTracks='1', CurrentTrack='1', NrTracks='1', Track='1')
            return values

        def last_change(self, rendering=False):
            if not self.backend and not (self.household.radio_playback and
                                         self.household.radio_playback.session(self.config.udn) and not rendering):
                return self.household.mask_component_uris(self.config.udn, getattr(self.http_source, 'address', None),
                                                          super().last_change(rendering))
            ns = ('urn:schemas-upnp-org:metadata-1-0/RCS/' if rendering
                  else 'urn:schemas-upnp-org:metadata-1-0/AVT/')
            root = zp.ET.Element('Event', xmlns=ns)
            instance = zp.ET.SubElement(root, 'InstanceID', val='0')
            if rendering:
                settings = self.backend.settings_values()
                volumes = settings.get('VolumeChannels', {})
                mutes = settings.get('MuteChannels', {})
                loudness = settings.get('LoudnessChannels')
                for channel in ('Master', 'LF', 'RF'):
                    # Master retains the backend's normal volume route; physical
                    # LF/RF balance channels must not inherit master volume.
                    volume = self.backend.volume if channel == 'Master' else volumes.get(channel, 100)
                    mute = self.backend.mute if channel == 'Master' else mutes.get(channel, 0)
                    zp.ET.SubElement(instance, 'Volume', channel=channel, val=str(volume))
                    zp.ET.SubElement(instance, 'Mute', channel=channel, val=str(mute))
                    if loudness is None or channel in loudness:
                        zp.ET.SubElement(instance, 'Loudness', channel=channel,
                                         val=str(loudness[channel] if loudness is not None else self.loudness))
                zp.ET.SubElement(instance, 'Bass', val=str(settings.get('Bass', self.bass)))
                zp.ET.SubElement(instance, 'Treble', val=str(settings.get('Treble', self.treble)))
                zp.ET.SubElement(instance, 'OutputFixed', val=str(settings.get('OutputFixed', '0')))
            else:
                values = self.backend_values()
                zp.ET.SubElement(instance, 'TransportStatus', val='OK')
                for key in AVT_EVENT_KEYS: zp.ET.SubElement(instance, key, val=values[key])
                for key, value in (('CurrentCrossfadeMode', '0'), ('NextAVTransportURI', ''),
                                   ('NextAVTransportURIMetaData', ''), ('TransportPlaySpeed', '1'),
                                   ('CurrentValidPlayModes', self.backend.valid_play_modes() if self.backend else 'NORMAL')):
                    zp.ET.SubElement(instance, key, val=value)
                generation = self.backend.settings_values().get('CurrentSleepTimerGeneration') if self.backend else None
                if self.household.alarms:
                    alarm_values = self.household.alarms.event_values('AVTransport', self.config.udn)
                    generation = alarm_values.pop('SleepTimerGeneration', generation)
                    if alarm_values:
                        root.set('xmlns:r', 'urn:schemas-rinconnetworks-com:metadata-1-0/')
                        for key, value in alarm_values.items():
                            zp.ET.SubElement(instance, 'r:' + key, val=str(value))
                if generation is not None:
                    root.set('xmlns:r', 'urn:schemas-rinconnetworks-com:metadata-1-0/')
                    zp.ET.SubElement(instance, 'r:SleepTimerGeneration', val=str(generation))
                # Only while Autoplay shows: a change of icon state then alters the
                # event body, so publish_changed notifies and R13 re-reads its icon.
                # Without Spotify Autoplay the event is byte-identical to before.
                gateway = getattr(self.household, 'ui_events', None)
                autoplay = gateway.autoplay_now(self.player) if gateway is not None and self.backend else 'off'
                if autoplay != 'off':
                    root.set('xmlns:r', 'urn:schemas-rinconnetworks-com:metadata-1-0/')
                    zp.ET.SubElement(instance, 'r:CR100Autoplay', val=autoplay)
            return zp.ET.tostring(root, encoding='unicode')

        def event_values(self, service):
            values = super().event_values(service)
            if self.household.alarms:
                values.update(self.household.alarms.event_values(service, self.config.udn))
            if self.household.radio_store and service == 'ContentDirectory':
                radio = self.household.radio_store.snapshot()
                values.update(UserRadioUpdateID=str(radio['radio_favorites_update_id'] + radio['radio_directory_update_id']),
                              RadioFavoritesUpdateID=str(radio['radio_favorites_update_id']),
                              RadioLocationUpdateID=str(radio['radio_location_update_id']))
            address = getattr(self.http_source, 'address', None)
            if service == 'ZoneGroupTopology':
                members = self.household.members(self.config.udn, address)
                values.update(ZoneGroupName=', '.join(p.config.room for p in members) or self.config.room,
                              ZoneGroupID=self.group_id,
                              ZonePlayerUUIDsInGroup=','.join(p.config.udn for p in members) or self.config.udn)
                available = getattr(self.household.updates, 'available_update', None)
                if callable(available):
                    values['AvailableSoftwareUpdate'] = available(address)
            elif service == 'GroupManagement':
                values.update(GroupCoordinatorIsLocal='1' if self.household.view_coordinator(self.config.udn, address)
                              == self.config.udn else '0', LocalGroupUUID=self.group_id)
            if service == 'DeviceProperties':
                values['Icon'] = ('x-rincon-roomicon:zoneextender' if self.informational
                                  else room_icons.uri(self.player.icon))
            if service == 'DeviceProperties' and self.informational:
                fallback = self.is_bridge and self.household.bridge_as_zone(address)
                values.update(Invisible='0' if fallback else '1',
                              IsZoneBridge='1' if self.is_bridge and not fallback else '0')
            if self.backend and service == 'GroupRenderingControl':
                volume, mute = self.household.group_volume(self.config.udn, address)
                rooms = [player.state.backend for player in self.household.members(self.config.udn, address)
                         if player.state.backend]
                changeable = bool(rooms) and all(room.supports('volume') for room in rooms)
                values.update(GroupVolume=str(volume), GroupMute=str(mute),
                              GroupVolumeChangeable='1' if changeable else '0')
            elif self.backend and service == 'ContentDirectory':
                values['ContainerUpdateIDs'] = 'Q:0,%d,AI:,%d' % (
                    self.backend.queue_version(), self.household.line_in_version)
            elif self.backend and service == 'Queue':
                values['LastChange'] = ('<Event xmlns="urn:schemas-sonos-com:metadata-1-0/Queue/"><QueueID val="0">'
                                        '<UpdateID val="%d"/></QueueID></Event>' % self.backend.queue_version())
            elif service == 'MusicServices' and self.household.music_service:
                values['ServiceListVersion'] = self.household.music_service.descriptor()['AvailableServiceListVersion']
            elif service == 'ZoneGroupTopology' and self.household.music_service:
                # Only the X wire event decodes and parses account XML into the object array
                # consumed by the Music menu. The plain event remains a string and is ignored
                # by the native household dispatcher. Also replaces the peer's empty X event.
                values['ThirdPartyMediaServersX'] = self.household.music_service_accounts
            if service == 'AudioIn':
                source = self.household.line_in_input(self.backend.ref) if self.backend else None
                playing = self.backend and any(
                    p.state.backend and p.state.backend.backend is self.backend.backend
                    and p.state.backend.line_in_source() == self.backend.key
                    and p.state.backend.transport == 'PLAYING' for p in self.household.players)
                values.update(AudioInputName=source['name'] if source else 'Line-In',
                              Icon=source.get('icon', '') if source else '',
                              LineInConnected='1' if source else '0', Playing='1' if playing else '0')
                if self.backend:
                    settings = self.backend.settings_values()
                    values.update({key: settings[key] for key in
                                   ('AudioInputName', 'Icon', 'LeftLineInLevel', 'RightLineInLevel') if key in settings})
            if service == 'ContentDirectory' and self.household.smart_devices:
                device_ids = self.household.smart_devices.event_ids(self.config.udn)
                if device_ids:
                    values['ContainerUpdateIDs'] = ','.join(filter(None, (values.get('ContainerUpdateIDs'), device_ids)))
            return {key: self.household.mask_component_uris(self.config.udn, address, value)
                    if isinstance(value, str) else value for key, value in values.items()}

        def music_service_action(self, service, action, args):
            """Advertise the service and route playable providers into the Sonos queue."""
            stub = self.household.music_service
            if service == 'MusicServices':
                self.household.log({'event': 'music-services', 'room': self.config.room, 'action': action, 'args': args})
                return stub.descriptor() if action == 'ListAvailableServices' else None
            uri = args.get('EnqueuedURI') or args.get('CurrentURI') or args.get('EnqueuedURIs') or ''
            metadata = args.get('EnqueuedURIMetaData') or args.get('CurrentURIMetaData') or args.get('EnqueuedURIsMetaData') or ''
            if service == 'AVTransport' and stub.owns(uri, metadata):
                self.household.log({'event': 'service-play', 'room': self.config.room, 'action': action, 'args': args})
                resolver = getattr(stub, 'resolve_playback', None)
                if resolver is None:
                    raise zp.UPnPError(501, 'Action Failed: the stub service does not play')
                if action not in ('AddURIToQueue', 'SetAVTransportURI'):
                    raise zp.UPnPError(501, 'Action Failed: unsupported music service operation')
                try:
                    if hasattr(stub, 'controller_resolver') or getattr(stub, 'preflight_playback', False):
                        provider_uri, kind = resolver(uri, metadata,
                                                      controller_ip=getattr(self.http_source, 'address', None))
                    else:
                        provider_uri, kind = resolver(uri, metadata)
                    coordinator = self.household.by_udn[self.household.coordinator[self.config.udn]]
                    backend = coordinator.state.backend
                    if backend is None:
                        raise BackendError('This room has no Sonos playback backend')
                    policy = self.household.playback_policy_for(uri, metadata)
                    if action == 'AddURIToQueue':
                        as_next = args.get('EnqueueAsNext') in ('1', 'true')
                        result = backend.enqueue_provider(provider_uri, kind, as_next)
                        callback = getattr(policy, 'queue_added', None)
                        if callable(callback):
                            callback(backend, provider_uri, result, as_next=as_next)
                        return result
                    callback = getattr(policy, 'play_provider', None)
                    if callable(callback) and callback(backend, provider_uri, kind):
                        return {}
                    backend.play_provider(provider_uri, kind)
                    return {}
                except (ValueError, BackendError) as error:
                    raise self.speaker_fault(service, action, error) from error
            return None

        def browse(self, args):
            """ContentDirectory Browse from the backend's content; a simulated room shows the
            household's content but has no queue of its own. The Music menu comes from the
            configured library speaker when there is one (a non-Sonos room has no Sonos
            library of its own); each room's queue always comes from its own speaker."""
            object_id = args['ObjectID']
            devices = self.household.smart_devices
            if devices and (object_id.startswith('SD:') or (
                    args.get('BrowseFlag') == 'BrowseMetadata' and devices.handles_metadata(object_id))):
                return devices.browse(self, args)
            if object_id.startswith('AI:'):
                return self.household.browse_line_in(self, args)
            own = self.backend.ref if self.backend else None
            if object_id.startswith('Q:'): ref = own
            else: ref = self.household.library or own or self.household.default_ref
            backend = self.household.backends.get(ref[0]) if ref else None
            content = backend.content if backend else None
            if not content: return None
            try:
                result, returned, total = content.browse(ref[1], self.config.udn, object_id, args['BrowseFlag'],
                                                         int(args['StartingIndex'] or 0), int(args['RequestedCount'] or 0))
            except ContentError as error:
                raise zp.UPnPError(error.code, str(error))
            update = self.backend.queue_version() if self.backend and object_id.startswith('Q:') else 1
            return {'Result': result, 'NumberReturned': str(returned), 'TotalMatches': str(total),
                    'UpdateID': str(update)}

        def speaker_fault(self, service, action, error, prefix='Action Failed: '):
            rendering = service in ('RenderingControl', 'GroupRenderingControl')
            if (isinstance(error, HomeAssistantDisconnected) and (rendering or service in ('AVTransport', 'AudioIn'))
                    and not action.startswith(('Get', 'List', 'Browse', 'Check', 'Find'))):
                address = getattr(self.http_source, 'address', None)
                # The stock volume overlay hides transport errors until it closes.
                self.household.controller_notices(address, str(error),
                                                  status='ERROR_HOME_ASSISTANT', cooldown=10,
                                                  settle=3.0 if rendering else None)
            code = getattr(error, 'code', 501)
            return zp.UPnPError(code if isinstance(code, int) else 501, prefix + str(error))

        def action(self, service, action, args):
            address = getattr(self.http_source, 'address', None)
            self.household.check_component_action(self.config.udn, address, service, action, args)
            if service == 'SystemProperties' and (args.get('VariableName', '').startswith('CR100_UI')
                    or args.get('VariableName', '').startswith('CR100_DevicesFeedback:')
                    or args.get('VariableName') in ('CR100_RoomCapabilities', 'CR100_SpotifyAutoplay',
                                                    'CR100_SpotifyAutoplayNow',
                                                    'CR100_DevicesAvailable', 'CR100_DevicesAvailable:climate',
                                                    'CR100_DevicesFeedback')):
                from ui_events import UIEventError
                values = self.validate_action(service, action, args)
                try:
                    handled = self.household.ui_events.native_action(self.player, service, action, args, address)
                except UIEventError as error:
                    raise zp.UPnPError(402, str(error)) from error
                if handled is not None:
                    values.update(handled)
                    return values, []
            if self.household.updates:
                from online_updates import UpdateError
                from update_transport import UpdateTransportError
                try:
                    handled = self.household.updates.soap(self, service, action, args, address)
                except (UpdateError, UpdateTransportError) as error:
                    raise zp.UPnPError(402 if isinstance(error, UpdateError) and error.status == 400 else 501,
                                       str(error)) from error
                if handled is not None:
                    return handled
            if self.informational and not action.startswith(('Get', 'List', 'Browse', 'Check')):
                raise zp.UPnPError(501, 'Informational component has no playback controls')
            # Check real routes before the simulated peer changes URI/EQ/clock
            # state. The compatibility persona is not a hardware capability.
            self.validate_action(service, action, args)
            source_coordinator = self.household.coordinator.get(self.config.udn, self.config.udn)
            try:
                supplied = self.household.source_action(self, service, action, args)
            except Exception as error:
                if isinstance(error, zp.UPnPError): raise
                from alarms import AlarmError
                from radiotime import RadioTimeError
                if not isinstance(error, (BackendError, AlarmError, RadioTimeError, ContentError, ValueError, KeyError)): raise
                raise self.speaker_fault(service, action, error, prefix='') from error
            if supplied is not None:
                self.household.source_action_committed(source_coordinator, service, action, args)
                if service == 'AVTransport' and action == 'AddURIToQueue':
                    policy_room = self.household.playback_room(self, service, action, args)
                    if policy_room is not None:
                        self.household.after_playback_action(policy_room, service, action, args, result=supplied)
                values = self.validate_action(service, action, args)
                values.update({key: str(value) for key, value in supplied.items() if key in values})
                values.update({key: value for key, value in self.household.source_values(self.config.udn, {}).items()
                               if key in values})
                return values, [service]
            # Validate a scoped Spotify operation before the base peer mutates URI
            # state. A denied cached item must not leak into subsequent events.
            stub = self.household.music_service
            if stub and (hasattr(stub, 'controller_resolver') or getattr(stub, 'preflight_playback', False)) and service == 'AVTransport' \
                    and action in ('AddURIToQueue', 'SetAVTransportURI'):
                uri = args.get('EnqueuedURI') or args.get('CurrentURI') or ''
                metadata = args.get('EnqueuedURIMetaData') or args.get('CurrentURIMetaData') or ''
                if stub.owns(uri, metadata):
                    try:
                        stub.resolve_playback(uri, metadata, controller_ip=getattr(self.http_source, 'address', None))
                    except ValueError as error:
                        raise zp.UPnPError(501, 'Action Failed: %s' % error) from error
            if self.backend:
                try:
                    self.check_backend_action(service, action, args)
                except BackendError as error:
                    raise self.speaker_fault(service, action, error) from error
            read_only = action.startswith(('Get', 'List', 'Browse', 'Check', 'Find'))
            policy_room = self.household.playback_room(self, service, action, args)
            policy_owner, intercepted_by, policy_result = None, None, None
            if not read_only and policy_room is not None:
                if service == 'AVTransport' and action in ('AddURIToQueue', 'SetAVTransportURI') and stub:
                    uri = args.get('EnqueuedURI') or args.get('CurrentURI') or ''
                    metadata = args.get('EnqueuedURIMetaData') or args.get('CurrentURIMetaData') or ''
                    if stub.owns(uri, metadata):
                        policy_owner = self.household.playback_policy_for(uri, metadata)
                try:
                    policy_result, intercepted_by = self.household.before_playback_action(
                        policy_room, service, action, args)
                except (BackendError, ValueError) as error:
                    raise self.speaker_fault(service, action, error) from error
            if self.backend and not read_only and service != 'SystemProperties':
                # The real backend owns mutations; never stage them in the
                # standalone peer, especially when a later call can fail.
                values, changed = self.validate_action(service, action, args), [service]
            else:
                values, changed = super().action(service, action, args)
            if service == 'DeviceProperties' and action == 'GetZoneInfo':
                identity = self.identity or self.household.virtual_identity()
                for field, key in (('SerialNumber', 'serial'), ('SoftwareVersion', 'software'),
                                   ('DisplaySoftwareVersion', 'display'), ('HardwareVersion', 'hardware'),
                                   ('IPAddress', 'ip')):
                    values[field] = about_text(identity.get(key) or 'Unavailable')
                values['ExtraInfo'] = about_text(identity.get('extra'), 2048)
                return values, changed
            if service == 'DeviceProperties' and action == 'GetZoneAttributes':
                values['CurrentIcon'] = ('x-rincon-roomicon:zoneextender' if self.informational
                                         else room_icons.uri(self.player.icon))
            if service == 'ZoneGroupTopology' and action == 'GetZoneGroupAttributes':
                members = self.household.members(self.config.udn, address)
                values.update(CurrentZoneGroupName=', '.join(p.config.room for p in members) or self.config.room,
                              CurrentZoneGroupID=self.group_id,
                              CurrentZonePlayerUUIDsInGroup=','.join(p.config.udn for p in members) or self.config.udn)
            udn = self.config.udn
            extra = policy_result
            if extra is None and self.household.music_service:
                extra = self.music_service_action(service, action, args)
            try:
                if extra is not None or service == 'MusicServices':
                    if extra is not None and service == 'AVTransport':
                        changed = list(changed) + ['ContentDirectory', 'Queue']
                elif service == 'ContentDirectory' and action == 'Browse':
                    extra = self.browse(args)
                elif self.backend and backends.setting_operation(service, action, args):
                    # Existing Line-In inventory already has verified, cached
                    # source attributes, even before the settings worker is ready.
                    source = self.household.line_in_input(self.backend.ref) \
                        if service == 'AudioIn' and action == 'GetAudioInputAttributes' else None
                    try:
                        extra = self.backend.action(service, action, args)
                    except BackendError:
                        if source is None: raise
                        extra = None
                    if extra is None and source is not None:
                        extra = {'CurrentName': source['name'], 'CurrentIcon': source.get('icon', '')}
                    if extra is None and not read_only:
                        raise BackendError('No implemented native settings route')
                elif service == 'AVTransport' and action == 'SetAVTransportURI' \
                        and args.get('CurrentURI', '').startswith('x-rincon:'):
                    if self.household.join(udn, args['CurrentURI'][len('x-rincon:'):]):
                        changed = list(changed) + ['ZoneGroupTopology']
                elif service == 'AVTransport' and action == 'BecomeCoordinatorOfStandaloneGroup':
                    if self.household.leave(udn):
                        changed = list(changed) + ['ZoneGroupTopology']
                elif service == 'AVTransport' and action == 'SetAVTransportURI' \
                        and args.get('CurrentURI', '').startswith(LINE_IN_PREFIX):
                    self.household.play_line_in(udn, args['CurrentURI'])
                elif service == 'AudioIn' and self.backend:
                    if action == 'GetAudioInputAttributes':
                        source = self.household.line_in_input(self.backend.ref)
                        extra = {'CurrentName': source['name'] if source else 'Line-In',
                                 'CurrentIcon': source.get('icon', '') if source else ''}
                    elif action in ('StartTransmissionToGroup', 'StopTransmissionToGroup'):
                        target = self.household.by_udn.get(args['CoordinatorID'])
                        if not target or not target.state.backend:
                            raise BackendError('Unknown line-in destination')
                        if target.state.backend.backend is not self.backend.backend:
                            raise BackendError('Line-in destination uses another backend')
                        if action == 'StartTransmissionToGroup':
                            self.household.play_line_in(target.config.udn, LINE_IN_PREFIX + udn)
                        elif target.state.backend.line_in_source() == self.backend.key:
                            target.state.backend.stop()
                    elif action.startswith(('Set', 'Select')):
                        raise BackendError('Line-in settings are managed on the source player')
                elif self.backend and service == 'GroupRenderingControl':
                    self.household.group_action(udn, action, args, address)
                elif self.backend:
                    extra = self.backend.action(service, action, args)
                    if service == 'AVTransport' and action in QUEUE_ACTIONS:
                        changed = list(changed) + ['ContentDirectory', 'Queue']
            except BackendError as error:
                raise self.speaker_fault(service, action, error) from error
            self.household.source_action_committed(source_coordinator, service, action, args)
            if not read_only and policy_room is not None:
                # Owned provider operations already went through play_provider /
                # queue_added. An intercepted command likewise belongs to the
                # policy that handled it. Other policies still see the mutation.
                self.household.after_playback_action(policy_room, service, action, args, result=extra,
                                                     exclude=(policy_owner, intercepted_by))
            if extra: values.update({key: value for key, value in extra.items() if key in values})
            if self.backend:
                if service == 'RenderingControl' and action == 'GetSupportsOutputFixed':
                    if not extra or 'CurrentSupportsFixed' not in extra:
                        values['CurrentSupportsFixed'] = '0'
                if backends.setting_operation(service, action, args):
                    # Native settings already supplied exact cached readback.
                    # Avoid ordinary now-playing/position reads on these paths.
                    if service == 'DeviceProperties' and action == 'GetAutoplayRoomUUID':
                        target = self.household.component_target(values.get('RoomUUID', ''))
                        values['RoomUUID'] = (target.config.udn if target is not None
                                              and self.household.component_allowed(target, address) else '')
                    return values, changed
                if service in ('AVTransport', 'RenderingControl', 'GroupRenderingControl'):
                    current = self.backend_values()
                    if service == 'GroupRenderingControl':
                        volume, mute = self.household.group_volume(udn, address)
                        current.update(CurrentVolume=str(volume), CurrentMute=str(mute), NewVolume=str(volume))
                    elif service == 'RenderingControl':
                        current['NewVolume'] = current['CurrentVolume']
                    values.update({key: value for key, value in current.items() if key in values})
            if not self.backend:
                values.update({key: value for key, value in self.household.source_values(udn, {}).items() if key in values})
            return {key: self.household.mask_component_uris(udn, address, value)
                    if isinstance(value, str) else value for key, value in values.items()}, changed

        def check_backend_action(self, service, action, args):
            """Implemented control routes only; cached reads keep legacy UI working."""
            room = self.backend
            if service == 'ContentDirectory' and action == 'Browse':
                if args.get('ObjectID', '').startswith('Q:'): room.require('queue_browse')
                return
            if service == 'MusicServices':
                if action == 'ListAvailableServices': return
                raise BackendError('Music-service account administration is not implemented')
            if action.startswith(('Get', 'List', 'Browse', 'Check', 'Find')): return
            setting = backends.setting_operation(service, action, args)
            if setting:
                room.require(setting)
                return
            if service == 'AVTransport':
                uri = args.get('EnqueuedURI') or args.get('CurrentURI') or ''
                metadata = args.get('EnqueuedURIMetaData') or args.get('CurrentURIMetaData') or ''
                stub = self.household.music_service
                if action in ('AddURIToQueue', 'SetAVTransportURI') and stub and stub.owns(uri, metadata):
                    coordinator = self.household.by_udn[self.household.coordinator[self.config.udn]].state.backend
                    if coordinator is None: raise BackendError('This room has no playback backend')
                    coordinator.require('provider_enqueue' if action == 'AddURIToQueue' else 'provider_play')
                    return
                if action == 'SetAVTransportURI':
                    if uri.startswith('x-rincon:'):
                        target = self.household.by_udn.get(uri[len('x-rincon:'):])
                        if not target or not target.state.backend or target.config.udn == self.config.udn:
                            raise BackendError('Unknown or invalid group destination')
                        coordinator = self.household.by_udn[self.household.coordinator[target.config.udn]].state.backend
                        if coordinator is None or not coordinator.can_group_with(room):
                            raise BackendError('These rooms cannot form a compatible group')
                    elif uri.startswith(LINE_IN_PREFIX): room.require('line_in')
                    elif uri.startswith('x-rincon-queue:'): room.require('queue_use')
                    else: room.require('play_item')
                    return
                operations = {'Play': 'play', 'Pause': 'pause', 'Stop': 'stop', 'Next': 'next',
                              'Previous': 'previous', 'AddURIToQueue': 'queue_add',
                              'RemoveTrackFromQueue': 'queue_remove', 'RemoveAllTracksFromQueue': 'queue_clear',
                              'BecomeCoordinatorOfStandaloneGroup': 'group'}
                if action == 'Next' and not room.supports('next'):
                    # A player omits Next on the last queue row; an armed
                    # continuation policy (Spotify Autoplay) still handles it.
                    policy_room = self.household.playback_room(self, service, action, args)
                    if policy_room is not None and self.household.playback_claims(policy_room, service, action, args):
                        return
                if action in operations: room.require(operations[action]); return
                if action == 'Seek':
                    room.require('queue_play' if args.get('Unit') == 'TRACK_NR' else 'seek'); return
                if action == 'SetPlayMode':
                    mode = backends.MODE_SETTINGS.get(args.get('NewPlayMode'))
                    if mode is None: raise BackendError('Unsupported play mode')
                    current = room.shuffle_repeat()
                    if mode[0] != current[0]: room.require('shuffle')
                    if mode[1] != current[1]: room.require('repeat')
                    if not room.supports('shuffle') and not room.supports('repeat'):
                        raise BackendError('Play mode controls are not implemented for this source')
                    return
            elif service == 'RenderingControl':
                if args.get('Channel', 'Master') != 'Master':
                    raise BackendError('Per-channel rendering controls are not implemented')
                if action in ('SetVolume', 'RampToVolume', 'SetRelativeVolume'): room.require('volume'); return
                if action == 'SetMute': room.require('mute'); return
            elif service == 'GroupRenderingControl':
                if action in ('SetGroupVolume', 'SetRelativeGroupVolume', 'SetGroupMute'):
                    operation = 'mute' if action == 'SetGroupMute' else 'volume'
                    for player in self.household.members(self.config.udn, getattr(self.http_source, 'address', None)):
                        if player.state.backend: player.state.backend.require(operation)
                    return
            elif service == 'AudioIn' and action in ('StartTransmissionToGroup', 'StopTransmissionToGroup'):
                target = self.household.by_udn.get(args.get('CoordinatorID'))
                if not target or not target.state.backend or target.state.backend.backend is not room.backend:
                    raise BackendError('Unknown or incompatible line-in destination')
                target.state.backend.require('line_in')
                if action == 'StopTransmissionToGroup': target.state.backend.require('stop')
                return
            elif service == 'SystemProperties' and action in ('SetString', 'SetStringX', 'Remove', 'RemoveX'):
                # Only existing virtual compatibility preferences, not physical
                # library/input/account settings that have no implementation.
                if args.get('VariableName') in ('R_AutoUpdate', 'R_UpdateAvailable', 'R_ShowNSSServers', 'UsageData'):
                    return
            raise BackendError('%s.%s is not implemented by the bridge' % (service, action))

        @property
        def group_id(self):
            return self.household.group_id(self.config.udn, getattr(self.http_source, 'address', None))

    class ArtHandler(zp.PeerHandler):
        def component_request(self, method):
            if not self.peer.household.udn_allowed(self.peer.config.udn, self.client_address[0]):
                self.close_connection = True
                self.reply(404, b'Not found', 'text/plain')
                return
            with self.peer.request_context(self.client_address[0]):
                if self.peer.household.updates:
                    self.peer.household.updates.observe(self.client_address[0], self.headers.get('User-Agent', ''))
                return method()

        def do_POST(self): return self.component_request(super().do_POST)
        def do_SUBSCRIBE(self): return self.component_request(super().do_SUBSCRIBE)
        def do_UNSUBSCRIBE(self): return self.component_request(super().do_UNSUBSCRIBE)

        def do_GET(self):
            return self.component_request(self.component_get)

        def component_get(self):
            path = urlsplit(self.path).path
            stores = [backend.art for backend in self.peer.household.backends.values() if backend.art]
            if self.peer.household.radio_playback: stores.append(self.peer.household.radio_playback.art)
            if not (path.startswith('/art/') and path.endswith('.jpg') and stores): return super().do_GET()
            key = path[len('/art/'):-len('.jpg')]
            image = next((image for image in (store.get(key) for store in stores) if image), None)
            if image: self.reply(200, image, 'image/jpeg', headers={'Cache-Control': 'max-age=86400'})
            else: self.reply(404, b'Not found', 'text/plain')

    class VirtualPlayer(zp.ZP80Peer):
        def __init__(self, config, household, backend=None, identity=None, informational=False, bridge=False):
            super().__init__(config)
            self.state = HouseholdState(self.config, household, backend, identity, informational, bridge)
            self.state.player = self
            self.household = household
            self.http.RequestHandlerClass = ArtHandler
            self.published = {}

        def ssdp_packet(self, target, alive=False):
            packet = super().ssdp_packet(target, alive)
            address = getattr(self.state.http_source, 'address', None)
            boot = self.household.discovery_boot_sequence(self, address)
            packet = re.sub(rb'(?im)(X-RINCON-BOOTSEQ: )\d+',
                            lambda m: m[1] + str(boot).encode(), packet)
            profile = self.household.discovery_profile(address)
            return packet.replace(('Sonos/' + zp.PROFILE['sw']).encode(), ('Sonos/' + profile['sw']).encode())

        @contextlib.contextmanager
        def request_context(self, address):
            previous = getattr(self.state.http_source, 'address', None)
            self.state.http_source.address = address
            try: yield
            finally: self.state.http_source.address = previous

        def notification_body(self, subscription):
            address = subscription.get('source') or urlsplit(subscription.get('callback', '')).hostname
            with self.request_context(address): return self.state.propertyset(subscription['service'])

        def notify(self, sid, body_override=None):
            with self.state.lock:
                sub = self.state.subscriptions.get(sid)
                if not sub: return
                source = sub.get('source') or urlsplit(sub.get('callback', '')).hostname
                if not self.household.udn_allowed(self.config.udn, source):
                    self.state.subscriptions.pop(sid, None)
                    return
            return super().notify(sid, body_override)

        def publish(self, service):
            if service == 'ZoneGroupTopology':
                for player in self.household.devices(): player.publish_changed(service)
            else:
                self.publish_changed(service)

        def publish_changed(self, service):
            """NOTIFY subscribers only when the evented values differ from the last NOTIFY."""
            if getattr(self.household, 'closed', False): return
            # This cache detects household changes, not one requester's view.
            # Each SID's actual body is rebuilt in notification_body().
            with self.request_context(None): body = self.state.propertyset(service)
            with self.state.lock:
                if self.published.get(service) == body: return
                self.published[service] = body
            zp.ZP80Peer.publish(self, service)

    return HouseholdState, VirtualPlayer


class Household:
    def __init__(self, zp, household, rooms, addresses, log=print, port=1400, ssdp_port=SSDP_PORT,
                 backends=None, refs=None, music_service=None, boot_sequence=1, slots=None, library=None,
                 icons=None, bridge_ip=None, build=None, controller_config=None, controller_resolver=None,
                 controller_addresses=None, listen_ip=None, config_path=None,
                 devices_hub=None, devices_config=None):
        """rooms: names; backends: the connected backends.Backend objects; refs: each room's
        (backend name, room id), or None for a simulated room; music_service: a
        smapi_stub.SmapiStub to advertise, or None; slots: each room's fixed player number
        (its UUID), default its position; library: the (backend name, room id) whose Music
        menu every room browses, default each room's own; icons: each room's room_icons name,
        default guessed from its name."""
        self.zp, self.log, self.ssdp_port = zp, log, ssdp_port
        self.controller_config = controller_config or {}
        self.updates = None
        self.controller_resolver = controller_resolver or (lambda _: None)
        self.controller_addresses = controller_addresses or getattr(controller_resolver, 'addresses', lambda: {})
        self.discovery_sources = set()
        # Validate saved policies before binding any player HTTP listeners.
        for mac in self.controller_config.get('controllers') or {}: visible_components(self.controller_config, mac)
        self.backends = {backend.name: backend for backend in backends or ()}
        self.music_service = music_service
        self.radio_service = self.radio_playback = self.radio_store = self.alarms = None
        self.alarm_runtime = None
        self.radio_transcoder = None
        self.radio_revision = None
        self.alarm_revision = None
        self.playback_policies = music_service_playback_policies(music_service)
        self.music_service_accounts = None
        if music_service:
            from music_service_events import encode_media_servers
            # Keep one encoded value per household so unchanged topology events stay equal.
            self.music_service_accounts = encode_media_servers(music_service.media_servers(), household)
        self.lock = threading.RLock()
        self.build = dict(build if build is not None else build_info.about())
        self.bridge_ip, self.fixed_bridge_ip = bridge_ip or '', bridge_ip is not None
        # The bridge must survive an empty speaker selection and keep the same
        # discovery endpoint when rooms disappear or are reordered.
        self.bridge_host = zp.Config(bind=listen_ip or next(iter(addresses), '127.0.0.1'),
                                     advertise_ip=listen_ip or next(iter(addresses), '127.0.0.1'),
                                     port=port if listen_ip is not None else 0, household=household,
                                     mac=component_mac(BRIDGE_KEY), room='CR100 Bridge',
                                     boot_sequence=boot_sequence)
        self.reachable_components = frozenset()
        self.about_players = {}
        self.started, self.discovery_generation = False, 0
        self.ssdp_thread = None
        self.stop = threading.Event()
        self.identity_thread = None
        self.coordinator = {}   # player UDN -> its group coordinator's UDN
        self.smart_devices = None
        refs = refs or [None] * len(rooms)
        for ref in refs:
            if ref and ref[0] not in self.backends: raise ValueError('Rooms of backend %r need it connected' % ref[0])
        self.default_ref = next((ref for ref in refs if ref), None)   # browses for simulated rooms
        self.library = library
        slots = list(range(len(rooms))) if slots is None else slots
        icons = icons or [None] * len(rooms)
        _, VirtualPlayer = build_classes(zp)
        self.players = []
        if not len(rooms) == len(addresses) == len(refs) == len(slots): raise ValueError('Need one address per room')
        if len(set(slots)) != len(slots): raise ValueError('Room slots must be unique')
        for room, ip, ref, slot, icon in zip(rooms, addresses, refs, slots, icons):
            config = zp.Config(bind=ip, advertise_ip=ip, port=port, household=household,
                               mac=player_mac(slot), room=room, boot_sequence=boot_sequence)
            backend = self.backends[ref[0]].room(ref[1]) if ref else None
            player = VirtualPlayer(config, self, backend)
            player.component_key = room_component_key(ref, slot)
            player.component_parents = set()
            player.icon = room_icons.effective(room, icon)
            self.players.append(player)
            self.coordinator[config.udn] = config.udn
        self.by_udn = {p.config.udn: p for p in self.players}
        for service in getattr(music_service, 'services', [music_service]):
            if service is not None and hasattr(service, 'component_allowed'):
                service.component_allowed = self.udn_allowed
        self.controller_notices = ControllerNotices(self.players, log)
        notice_services = [service for service in music_service_controllers(music_service)
                           if hasattr(service, 'on_locked')]
        self.spotify_notices = self.controller_notices if notice_services else None
        for service in notice_services:
            if service.on_locked is None:
                service.on_locked = self.spotify_notices
        self.by_ref = {p.state.backend.ref: p for p in self.players if p.state.backend}
        self.line_in_content = ContentSource()
        self.line_in_version, self.seen_line_in = 1, None
        self.closed = False
        self.dirty, self.flush_timer = set(), None
        for name, backend in self.backends.items():
            backend.listeners.append(lambda key, name=name: self.on_change((name, key)))
        if self.backends: self.sync_groups()
        self.refresh_identities()
        from ui_events import UIEventGateway, register_spotify_autoplay
        self.ui_events = UIEventGateway(self, require_controller=controller_resolver is not None)
        register_spotify_autoplay(self.ui_events, config_path)
        for provider in getattr(music_service, 'services', [music_service]):
            if provider is not None: self.ui_events.register_provider(provider)
        if devices_hub is None:
            devices_hub = next((getattr(getattr(backend, 'ha', None), 'ha_hub', None)
                                for backend in self.backends.values()
                                if getattr(getattr(backend, 'ha', None), 'ha_hub', None)), None)
        if devices_hub is not None:
            from smart_device_service import SmartDeviceService
            self.smart_devices = SmartDeviceService(self, devices_hub,
                                                     devices_config if devices_config is not None else controller_config)

    # --- About identities (cached independently of playback/discovery versions) ---

    def source_values(self, udn, values):
        if self.radio_playback:
            player = self.by_udn.get(udn)
            if player is None: return values  # Informational components have no playback source.
            return self.radio_playback.values(udn, values, 'http://%s:%d' % (
                player.config.advertise_ip, player.config.port))
        return values

    def source_action(self, state, service, action, args):
        if self.alarms:
            result = self.alarms.action(service, action, args, state.config.udn)
            if result is not None:
                if service == 'AVTransport' and action == 'GetRunningAlarmProperties' and result.get('AlarmID', '0') != '0':
                    # The alarm panel resolves this against the controller's topology view.
                    result['GroupID'] = state.group_id
                return result
        if self.radio_service:
            handler = getattr(self.radio_service, 'native_action', None)
            if handler:
                result = handler(service, action, args, state)
                if result is not None: return result
        if self.radio_playback:
            return self.radio_playback.transport_action(state, service, action, args)
        return None

    def source_action_committed(self, coordinator, service, action, args):
        # A denied provider selection or failed renderer command must retain
        # the previous Radio source and alarm duration ownership. Use the
        # original coordinator even if the successful action changed grouping.
        if self.alarm_runtime:
            player = self.by_udn.get(coordinator)
            if player and player.state.backend:
                self.alarm_runtime.before_action(coordinator, service, action, args, coordinator=False)
        if self.radio_playback:
            self.radio_playback.after_transport_action(coordinator, service, action, args)

    def settings_rooms(self, source_ip=None):
        """Configured speakers only; bonded/About entries never become targets."""
        return [(player.config.udn, player.config.room, player.state.backend)
                for player in self.players if player.state.backend is not None and self.udn_allowed(player.config.udn, source_ip)]

    def playback_policy_for(self, uri, metadata=''):
        service = self.music_service
        selector = getattr(service, 'playback_policy_for', None)
        return selector(uri, metadata) if callable(selector) else getattr(service, 'playback_policy', None)

    def playback_room(self, player, service, action, args):
        """Current output coordinator for optional provider transport policies."""
        if service not in ('AVTransport', 'AlarmClock', 'GroupManagement', 'ZoneGroupTopology', 'AudioIn'):
            return None
        if service == 'AudioIn' and action in ('StartTransmissionToGroup', 'StopTransmissionToGroup'):
            player = self.by_udn.get(args.get('CoordinatorID'))
            if player is None:
                return None
        coordinator = self.by_udn.get(self.coordinator.get(player.config.udn, player.config.udn), player)
        return coordinator.state.backend

    def playback_claims(self, room, service, action, args):
        """Whether a policy will handle an action the room itself does not offer now."""
        for policy in self.playback_policies:
            claims = getattr(policy, 'claims_action', None)
            try:
                if callable(claims) and claims(room, service, action, args):
                    return True
            except Exception:
                continue
        return False

    def before_playback_action(self, room, service, action, args):
        for policy in self.playback_policies:
            callback = getattr(policy, 'before_action', None)
            if callable(callback):
                result = callback(room, service, action, args)
                if result is not None:
                    return result, policy
        return None, None

    def after_playback_action(self, room, service, action, args, result=None, exclude=()):
        for policy in self.playback_policies:
            if any(policy is excluded for excluded in exclude):
                continue
            callback = getattr(policy, 'after_action', None)
            if callable(callback):
                try:
                    callback(room, service, action, args, result=result)
                except Exception as error:
                    # The player action has succeeded: failing its response here
                    # would cause a controller retry and duplicate queue writes.
                    self.log({'event': 'playback-policy-update-failed', 'error_type': type(error).__name__})
    def virtual_identity(self, bridge=False):
        return dict(self.build, id=BRIDGE_KEY if bridge else '', name='CR100 Bridge' if bridge else '',
                    model='CR100 Bridge' if bridge else 'Bridge Virtual Component', serial='Virtual component',
                    hardware='Virtual bridge' if bridge else 'Virtual component', ip=self.bridge_ip)

    def bridge_as_zone(self, source_ip=None):
        """Show a diagnostic zone only when this controller has no reachable room.

        Explicit component selections still apply, including hiding everything.
        Availability is cached by refresh_identities; this performs no network I/O.
        """
        return not any(player.component_key in self.reachable_components
                       and self.component_allowed(player, source_ip) for player in self.players)

    def devices(self, source_ip=None):
        with self.lock:
            players = list(self.players) + list(self.about_players.values())
            return [player for player in players if self.component_allowed(player, source_ip)]

    def discovery_profile(self, source_ip=None):
        return self.updates.profile(source_ip) if self.updates else self.zp.PROFILE

    def discovery_boot_sequence(self, player, source_ip=None):
        extra = self.updates.generation(source_ip) if self.updates else 0
        return (player.config.boot_sequence + extra - 1) % 0xffffffff + 1

    def updates_changed(self):
        """Publish fresh per-controller floors without resetting radio association."""
        with self.lock:
            if self.closed: return
            self.discovery_generation += 1
        if self.started:
            for player in self.devices():
                with player.state.lock:
                    player.published.pop('ZoneGroupTopology', None)
                player.publish_changed('ZoneGroupTopology')

    def component_allowed(self, player, source_ip=None):
        mac = self.controller_resolver(source_ip) if source_ip is not None else None
        if not component_visible(self.controller_config, mac, player.component_key): return False
        parents = player.component_parents
        return not parents or any(component_visible(self.controller_config, mac, key) for key in parents)

    def udn_allowed(self, udn, source_ip=None):
        with self.lock:
            player = self.by_udn.get(udn) or next((p for p in self.about_players.values() if p.config.udn == udn), None)
            return player is not None and self.component_allowed(player, source_ip)

    def refresh_identities(self, refresh_ip=False):
        """Cache snapshots and reconcile hidden endpoints; getters here perform no I/O.

        Informational components share a room's isolated IP on their own UPnP HTTP
        ports, preserving all 38 room slots and avoiding additional TAP addresses.
        """
        primary, bonded, targets, parents = [], {}, [], {}
        for player in self.players:
            room = player.state.backend
            identity = room.backend.identity(room.key) if room else None
            primary.append((player, identity))
            if identity and identity.get('ip'): targets.append(identity['ip'])
        selected = {i.get('id') for _, i in primary if i and i.get('id')}
        for player, identity in primary:
            room = player.state.backend
            if room:
                for device in room.backend.bonded_devices(room.key):
                    key = device.get('id')
                    if key and key not in selected:
                        bonded[key] = dict(device)
                        parents.setdefault(key, set()).add(player.component_key)
        reachable = frozenset(player.component_key for player in self.players
                              if player.state.backend is None or player.state.backend.available)
        if refresh_ip and not self.fixed_bridge_ip:
            for backend in self.backends.values():
                host = urlsplit(getattr(getattr(backend, 'ha', None), 'url', '')).hostname
                if host: targets.append(host)
            self.bridge_ip = upstream_address(targets, self.bridge_host.advertise_ip)
        removed, changed = [], False
        with self.lock:
            if self.closed: return False
            availability_changed = reachable != self.reachable_components
            self.reachable_components = reachable
            for player, identity in primary:
                snapshot = dict(identity) if identity else self.virtual_identity()
                if player.state.identity != snapshot:
                    initialized = player.state.identity is not None
                    player.state.identity = snapshot
                    if initialized:
                        player.config.boot_sequence = player.config.boot_sequence % 0xffffffff + 1
                    changed = True
            wanted = dict(bonded)
            wanted[BRIDGE_KEY] = self.virtual_identity(bridge=True)
            for key in set(self.about_players) - set(wanted):
                removed.append(self.about_players.pop(key)); changed = True
            _, VirtualPlayer = build_classes(self.zp)
            for key, identity in wanted.items():
                player = self.about_players.get(key)
                if player is None:
                    host = self.bridge_host if key == BRIDGE_KEY else self.players[0].config
                    name = identity.get('name') or identity.get('model') or 'Sonos component'
                    config = self.zp.Config(bind=host.bind, advertise_ip=host.advertise_ip,
                                            port=host.port if key == BRIDGE_KEY else 0,
                                            household=host.household, mac=component_mac(key), room=about_text(name),
                                            boot_sequence=host.boot_sequence)
                    player = VirtualPlayer(config, self, identity=dict(identity), informational=True,
                                           bridge=key == BRIDGE_KEY)
                    player.component_key = BRIDGE_COMPONENT if key == BRIDGE_KEY else 'sonos:' + key
                    player.component_parents = set(parents.get(key, ()))
                    player.icon = 'living'
                    self.about_players[key] = player
                    if self.started: player.start(ssdp=False, netstart=False)
                    changed = True
                elif player.state.identity != identity:
                    player.state.identity = dict(identity)
                    player.config.room = about_text(identity.get('name') or identity.get('model') or 'Sonos component')
                    player.config.boot_sequence = player.config.boot_sequence % 0xffffffff + 1
                    changed = True
                if player.component_parents != set(parents.get(key, ())):
                    player.component_parents = set(parents.get(key, ()))
                    changed = True
                if key == BRIDGE_KEY and availability_changed:
                    # A CR100 caches descriptions. A fresh boot sequence tells it
                    # to re-read the visible-zone/hidden-bridge persona on recovery.
                    player.config.boot_sequence = player.config.boot_sequence % 0xffffffff + 1
                    changed = True
            if changed: self.discovery_generation += 1
        for player in removed:
            if player.http_thread: player.close()
            else: player.http.server_close()
        if changed and self.started and self.updates:
            mapping = {}
            for player in self.devices():
                mapping.setdefault(player.config.advertise_ip, []).append(player.config.udn)
            self.updates.transport.set_players(mapping)
        if changed and self.started:
            for player in self.devices():
                for service in ('ZoneGroupTopology', 'DeviceProperties'):
                    player.publish_changed(service)
        return changed

    def identity_loop(self):
        next_ip = 0
        while not self.stop.wait(IDENTITY_INTERVAL):
            try:
                now = time.monotonic()
                self.refresh_identities(refresh_ip=now >= next_ip)
                if self.updates: self.updates.tick()
                if self.radio_store:
                    revision = self.radio_store.snapshot()['revision']
                    if revision != self.radio_revision:
                        self.radio_revision = revision
                        for player in self.players:
                            player.publish_changed('ContentDirectory')
                            player.publish_changed('SystemProperties')
                if self.alarms:
                    revision = self.alarms.store.version
                    if revision != self.alarm_revision:
                        self.alarm_revision = revision
                        for player in self.players: player.publish_changed('AlarmClock')
                if now >= next_ip: next_ip = now + UPSTREAM_INTERVAL
            except Exception as error:
                self.log({'event': 'about-refresh-error', 'error': repr(error)})

    # --- backends -----------------------------------------------------------------
    def on_change(self, ref):
        """Runs on the backend's thread: collect changes, publish them together shortly after."""
        player = self.by_ref.get(ref)
        with self.lock:
            if self.closed: return
            if player is not None: self.dirty.add(player.config.udn)
            else:
                # A source need not be one of the displayed rooms. Its cable/name
                # changes still invalidate every room's Line-In menu.
                self.dirty.update(p.config.udn for p in self.players
                                  if p.state.backend and p.state.backend.ref[0] == ref[0])
            if not self.dirty: return
            if self.flush_timer is None:
                self.flush_timer = threading.Timer(PUBLISH_DELAY, self.flush)
                self.flush_timer.daemon = True
                self.flush_timer.start()

    def flush(self):
        with self.lock:
            if self.closed: return
            dirty, self.dirty, self.flush_timer = self.dirty, set(), None
        self.refresh_identities()
        if self.sync_groups(): self.players[0].publish('ZoneGroupTopology')
        sources = tuple((name, tuple(sorted(source.items()))) for name, backend in self.backends.items()
                        for source in backend.line_in_sources())
        if sources != self.seen_line_in:
            self.seen_line_in = sources
            self.line_in_version += 1
            dirty.update(self.by_udn)
        coordinators = {self.coordinator[udn] for udn in dirty}
        for udn in dirty:
            player = self.by_udn[udn]
            # ContentDirectory and Queue go out only when the queue version moved (publish_changed).
            for service in ('AVTransport', 'RenderingControl', 'ContentDirectory', 'Queue'): player.publish(service)
        for udn in dirty | coordinators: self.by_udn[udn].publish('GroupRenderingControl')
        # A destination's playback changes the source's AudioIn.Playing event.
        for player in self.players: player.publish('AudioIn')

    # --- line-in -----------------------------------------------------------------
    def line_in_input(self, ref):
        backend = self.backends.get(ref[0])
        return next((s for s in backend.line_in_sources() if s['id'] == ref[1]), None) if backend else None

    def line_in_uri(self, backend, source_id):
        player = self.by_ref.get((backend.name, source_id))
        if player: return LINE_IN_PREFIX + player.config.udn
        source = self.line_in_input((backend.name, source_id))
        return source['uri'] if source else LINE_IN_PREFIX + source_id

    def line_in_nodes(self, ref, source_ip=None):
        player = self.by_ref.get(ref) if ref else None
        if player is not None and not self.udn_allowed(player.config.udn, source_ip): return []
        source = self.line_in_input(ref) if ref else None
        if source is None: return []
        uri = self.line_in_uri(self.backends[ref[0]], source['id'])
        # The CR100 enumerates source players, then browses each player's AI: and
        # appends that player's room name itself.
        return [Node('AI:0', 'AI:', source['name'], source['id'], 'line_in', False,
                     'object.item.audioItem', uri=uri)]

    def browse_line_in(self, state, args):
        nodes = self.line_in_nodes(state.backend.ref if state.backend else None,
                                   getattr(state.http_source, 'address', None))
        object_id = args['ObjectID']
        if args['BrowseFlag'] == 'BrowseMetadata':
            if object_id == 'AI:':
                page = [Node('AI:', '0', 'Line-In', None, None, True, 'object.container',
                             playable=False, child_count=len(nodes))]
            else:
                page = [n for n in nodes if n.object_id == object_id]
                if not page: raise self.zp.UPnPError(701, 'No such line-in source')
            total = 1
        else:
            if object_id != 'AI:': raise self.zp.UPnPError(701, 'No such line-in container')
            start, count = int(args['StartingIndex'] or 0), int(args['RequestedCount'] or 0)
            page, total = nodes[start:start + min(count or 100, 100)], len(nodes)
        return {'Result': self.line_in_content.didl(page, state.config.udn),
                'NumberReturned': str(len(page)), 'TotalMatches': str(total),
                'UpdateID': str(self.line_in_version)}

    def play_line_in(self, udn, uri):
        # The backend routes to its current real coordinator. The virtual group
        # map is published in batches and can still describe the previous group.
        destination = self.by_udn[udn].state.backend
        if destination is None: raise BackendError('This room has no line-in playback backend')
        destination.require('line_in')
        for source in destination.backend.line_in_sources():
            if uri in (source['uri'], self.line_in_uri(destination.backend, source['id'])):
                destination.play_line_in(source['id'])
                return
        raise BackendError('Unknown or disconnected line-in source')

    def sync_groups(self):
        """Take the backends' groups for their rooms. A group's coordinator is its first
        member that is also a room here (backends list the coordinator first)."""
        changed = False
        with self.lock:
            for ref, player in self.by_ref.items():
                members = [(ref[0], m) for m in player.state.backend.group_members()
                           if (ref[0], m) in self.by_ref] or [ref]
                coordinator = self.by_ref[members[0]].config.udn
                if self.coordinator[player.config.udn] != coordinator:
                    self.coordinator[player.config.udn] = coordinator; changed = True
        if changed: self.log({'event': 'groups', 'coordinators': {self.by_udn[u].config.room: self.by_udn[c].config.room
                                                                  for u, c in self.coordinator.items()}})
        if changed and self.smart_devices: self.smart_devices.hub_changed()
        return changed

    def members(self, udn, source_ip=None):
        with self.lock:
            coordinator = self.coordinator.get(udn, udn)
            return [self.by_udn[u] for u, c in self.coordinator.items() if c == coordinator
                    and self.udn_allowed(u, source_ip)]

    def group_volume(self, udn, source_ip=None):
        backends = [p.state.backend for p in self.members(udn, source_ip) if p.state.backend]
        if not backends: return 0, 0
        return (round(sum(b.volume for b in backends) / len(backends)),
                int(all(b.mute for b in backends)))

    def group_action(self, udn, action, args, source_ip=None):
        backends = [p.state.backend for p in self.members(udn, source_ip) if p.state.backend]
        if action in ('SetGroupVolume', 'SetRelativeGroupVolume', 'SetGroupMute'):
            for backend in backends: backend.require('mute' if action == 'SetGroupMute' else 'volume')
        if action in ('SetGroupVolume', 'SetRelativeGroupVolume'):
            volume, _ = self.group_volume(udn, source_ip)
            delta = int(args['Adjustment']) if 'Adjustment' in args else int(args['DesiredVolume']) - volume
            for backend in backends: backend.set_volume(backend.volume + delta)
        elif action == 'SetGroupMute':
            for backend in backends: backend.set_mute(args['DesiredMute'] in ('1', 'true', 'yes'))

    # --- topology ---------------------------------------------------------------
    def view_coordinator(self, udn, source_ip=None):
        members = self.members(udn, source_ip)
        coordinator = self.coordinator.get(udn, udn)
        return coordinator if any(p.config.udn == coordinator for p in members) else members[0].config.udn if members else udn

    def group_id(self, udn, source_ip=None):
        return self.view_coordinator(udn, source_ip) + ':1'

    def check_component_action(self, udn, source_ip, service, action, args):
        """Reject cached routes to hidden peers before any virtual/backend mutation."""
        if not self.udn_allowed(udn, source_ip): raise self.zp.UPnPError(701, 'No such component')
        if source_ip is None: return
        from urllib.parse import unquote
        for name, value in args.items():
            if name not in ('CoordinatorID', 'MemberID', 'DeviceUUID', 'NewCoordinator', 'CurrentCoordinator') \
                    and 'URI' not in name: continue
            for token in re.findall(r'(?:x-rincon(?:-queue|-stream)?:)([^\s<"/?#]+)', unquote(str(value))):
                self.check_component_target(token, source_ip)
            if name in ('CoordinatorID', 'MemberID', 'DeviceUUID', 'NewCoordinator', 'CurrentCoordinator'):
                self.check_component_target(str(value), source_ip)

    def check_component_target(self, token, source_ip):
        player = self.component_target(token)
        if player is not None and not self.component_allowed(player, source_ip):
            raise self.zp.UPnPError(701, 'No such component')

    def component_target(self, token):
        if not token: return None
        for player in self.players:
            room = player.state.backend
            identities = {player.config.udn}
            if room: identities.update((room.key, room.binding.native_id))
            if (player.state.identity or {}).get('id'): identities.add(player.state.identity['id'])
            if token in identities: return player
        return None

    def mask_component_uris(self, owner, source_ip, value):
        """Keep cached transport/group readback within the controller's topology view."""
        if source_ip is None: return value
        def replace(match):
            player = self.component_target(match[2])
            return match[1] + owner if player is not None and not self.component_allowed(player, source_ip) else match[0]
        return re.sub(r'(x-rincon(?:-queue|-stream)?:)([^\s<"/?#&]+)', replace, value)

    def topology(self, source_ip=None):
        zp = self.zp
        profile = self.discovery_profile(source_ip)
        root = zp.ET.Element('ZoneGroups')
        with self.lock:
            for coordinator in [p.config.udn for p in self.players if self.coordinator[p.config.udn] == p.config.udn]:
                members = self.members(coordinator, source_ip)
                if not members: continue
                proxy = self.view_coordinator(coordinator, source_ip)
                group = zp.ET.SubElement(root, 'ZoneGroup', Coordinator=proxy, ID=proxy + ':1')
                for player in members:
                    config = player.config
                    zp.ET.SubElement(group, 'ZoneGroupMember', UUID=config.udn, Location=player.state.location,
                        ZoneName=config.room, Icon=room_icons.uri(player.icon), Configuration='1',
                        SoftwareVersion=profile['sw'], MinCompatibleVersion=profile['mincompat'],
                        LegacyCompatibleVersion=profile['legacycompat'], BootSeq=str(self.discovery_boot_sequence(player, source_ip)),
                        WirelessMode='0', ChannelFreq='2412', BehindWifiExtender='0', WifiEnabled='1')
            for player in self.about_players.values():
                if not self.component_allowed(player, source_ip): continue
                config = player.config
                group = zp.ET.SubElement(root, 'ZoneGroup', Coordinator=config.udn, ID=config.udn + ':1')
                fallback = player.state.is_bridge and self.bridge_as_zone(source_ip)
                zp.ET.SubElement(group, 'ZoneGroupMember', UUID=config.udn, Location=player.state.location,
                    ZoneName=config.room, Icon='x-rincon-roomicon:zoneextender', Configuration='1',
                    Invisible='0' if fallback else '1', IsZoneBridge='1' if player.state.is_bridge and not fallback else '0',
                    SoftwareVersion=profile['sw'], MinCompatibleVersion=profile['mincompat'],
                    LegacyCompatibleVersion=profile['legacycompat'], BootSeq=str(self.discovery_boot_sequence(player, source_ip)),
                    WirelessMode='0', ChannelFreq='2412', BehindWifiExtender='0', WifiEnabled='1')
        return zp.ET.tostring(root, encoding='unicode')

    def join(self, udn, target):
        player, other = self.by_udn.get(udn), self.by_udn.get(target)
        if player and player.state.backend:
            if not (other and other.state.backend) or target == udn: return False
            coordinator = self.by_udn[self.coordinator[target]].state.backend
            if not coordinator.can_group_with(player.state.backend): return False
            # The backend reports the new group; flush() then publishes the topology.
            coordinator.join(player.state.backend)
            self.log({'event': 'join', 'player': udn, 'coordinator': coordinator.key})
            return False
        if other and other.state.backend: return False
        with self.lock:
            if target not in self.coordinator or target == udn: return False
            coordinator = self.coordinator[target]
            # A coordinator that joins another group brings no members along; they go standalone.
            for member, current in self.coordinator.items():
                if current == udn and member != udn: self.coordinator[member] = member
            self.coordinator[udn] = coordinator
        self.log({'event': 'join', 'player': udn, 'coordinator': coordinator})
        return True

    def leave(self, udn):
        player = self.by_udn.get(udn)
        if player is None: return False
        if player and player.state.backend:
            player.state.backend.require('group')
            if self.coordinator.get(udn) != udn: player.state.backend.unjoin()
            return False
        with self.lock:
            if self.coordinator.get(udn) == udn: return False
            self.coordinator[udn] = udn
        self.log({'event': 'leave', 'player': udn})
        return True

    # --- SSDP -------------------------------------------------------------------
    def discovery_views(self):
        """Restrict unsolicited announcements to each controller when any view differs.

        A multicast advertisement would expose hidden components to every CR100.
        Known leases and request sources instead receive normal SSDP NOTIFY packets
        over unicast. Without policies retain the original multicast discovery.
        """
        profiles = getattr(self, 'controller_config', {}).get('controllers') or {}
        scoped = any(visible_components(self.controller_config, mac) is not None for mac in profiles)
        if getattr(self, 'updates', None) and self.updates.generations: scoped = True
        if not scoped: return {self.zp.SSDP_GROUP: self.devices()}
        addresses = set(self.controller_addresses()) | self.discovery_sources
        return {address: self.devices(address) for address in addresses}

    def ssdp_loop(self, listen_ip, stop, iface=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if iface: listener.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        listener.bind(('', self.ssdp_port))
        listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(self.zp.SSDP_GROUP) + socket.inet_aton(listen_ip))
        listener.settimeout(1)
        senders, announced = {}, {}
        recent, next_alive, generation = {}, 0.0, -1

        def announce(player, address, alive):
            for target in player.ssdp_targets():
                with player.request_context(address):
                    packet = player.ssdp_packet(target, alive=True)
                if not alive: packet = packet.replace(b'NTS: ssdp:alive', b'NTS: ssdp:byebye')
                sender = senders.get(player.config.udn)
                if sender is None: continue
                try: sender.sendto(packet, (address, SSDP_PORT))
                except OSError as error:
                    self.log({'event': 'ssdp-send-error', 'udn': player.config.udn, 'error': repr(error)})

        try:
            while not stop.is_set():
                try:
                    now = time.monotonic()
                    players = self.devices()
                    current = {p.config.udn: p for p in players}
                    for udn, player in current.items():
                        if udn in senders: continue
                        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                        try:
                            sender.bind((player.config.advertise_ip, 0))
                            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(player.config.advertise_ip))
                            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                        except Exception:
                            sender.close()
                            raise
                        senders[udn] = sender
                    if generation != self.discovery_generation:
                        generation, next_alive = self.discovery_generation, 0
                    desired = {(address, player.config.udn): player for address, visible in self.discovery_views().items()
                               for player in visible if player.config.udn in senders}
                    for address, udn in announced.keys() - desired.keys():
                        announce(announced[(address, udn)], address, False)
                    for (address, udn), player in desired.items():
                        if (address, udn) not in announced or now >= next_alive: announce(player, address, True)
                    announced = desired
                    for udn in set(senders) - set(current): senders.pop(udn).close()
                    if now >= next_alive: next_alive = now + ALIVE_INTERVAL
                    try:
                        packet, source = listener.recvfrom(8192)
                    except socket.timeout:
                        continue
                    if source[0] in {p.config.advertise_ip for p in players}: continue
                    now = time.monotonic()
                    key = (source, packet)
                    if now - recent.get(key, -1e9) < DUPLICATE_WINDOW: continue
                    recent = {k: t for k, t in recent.items() if now - t < DUPLICATE_WINDOW}
                    recent[key] = now
                    answered = 0
                    for player in self.devices(source[0]):
                        with player.request_context(source[0]):
                            responses = player.search_responses(packet)
                        sender = senders.get(player.config.udn)
                        if sender is None: continue  # A newly advertised component gets a sender next pass.
                        for response in responses:
                            try: sender.sendto(response, source)
                            except OSError as error:
                                self.log({'event': 'ssdp-send-error', 'udn': player.config.udn, 'error': repr(error)})
                            else: answered += 1
                    if answered:
                        self.discovery_sources.add(source[0])
                        self.log({'event': 'm-search', 'from': source[0], 'responses': answered})
                except Exception as error:
                    self.log({'event': 'ssdp-loop-error', 'error': repr(error)})
                    stop.wait(.2)
        finally:
            listener.close()
            for (address, udn), player in announced.items(): announce(player, address, False)
            for sender in senders.values(): sender.close()

    def start(self, listen_ip, ssdp=True, iface=None):
        if self.updates: self.updates.start()
        if self.radio_service: self.radio_service.start()
        if self.radio_playback: self.radio_playback.start()
        if self.alarms: self.alarms.start()
        for player in self.devices(): player.start(ssdp=False, netstart=False)
        self.started = True
        self.identity_thread = threading.Thread(target=self.identity_loop, name='about-identities', daemon=True)
        self.identity_thread.start()
        if ssdp:
            self.ssdp_thread = threading.Thread(target=self.ssdp_loop, args=(listen_ip, self.stop, iface), daemon=True)
            self.ssdp_thread.start()

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
            if self.flush_timer: self.flush_timer.cancel()
        self.stop.set()
        if self.smart_devices: self.smart_devices.close()
        if self.updates: self.updates.close()
        for source in (self.alarms, self.radio_playback, self.radio_service, self.radio_transcoder):
            if source is not None: source.close()
        for policy in self.playback_policies:
            try:
                policy.close()
            except Exception as error:
                self.log({'event': 'playback-policy-close-failed', 'error_type': type(error).__name__})
        self.controller_notices.close()
        if self.ssdp_thread: self.ssdp_thread.join(2)
        if self.identity_thread: self.identity_thread.join(3)
        for player in self.devices():
            if player.http_thread: player.close()
            else: player.http.server_close()


def load_rooms(config_path, log):
    """Rooms from the config file. Returns (names, refs, slots, {backend name: Backend}).

    {"home_assistant": {"url": "http://homeassistant.local:8123", "token_file": "ha-token"},
     "rooms": [{"entity": "media_player.kitchen", "slot": 0}, {"name": "Den", "entity": "media_player.den", "icon": "den"},
               {"name": "Spare"}],
     "music_menu": {"library_entity": "media_player.kitchen"}}
    A room names its backend and id there ({"backend": "ha", "id": "media_player.den"}; "entity"
    is shorthand for Home Assistant, see backends.room_ref) and only the backends rooms use are
    connected. A room without "name" takes the backend's name for it; one without a backend is
    simulated. If a backend has not discovered the room yet, use its last known name
    or configured id; the room stays bound to that backend for recovery.
    "slot" fixes the room's player address and UUID, so hiding or reordering other rooms
    (the web interface does both) never changes which player the CR100 knows it as; a room
    without one takes its position, as before slots existed. "icon" is a room_icons name
    (configured_icons); without one the icon is guessed from the room's name.
    """
    config = json.loads(Path(config_path).read_text())
    rooms = config['rooms']
    refs = [backends.room_ref(room) for room in rooms]
    sources = {ref[0] for ref in refs if ref}
    automatic = config.get('backend') == 'sonos' and (config.get('sonos') or {}).get('follow_network', True)
    if automatic: sources.add('sonos')   # discover even while no speakers are advertised
    connected = backends.load(config, Path(config_path).resolve().parent,
                              sorted(sources), log)
    if automatic:
        config = reconcile_sonos_selection(config_path, connected['sonos'], log)
        rooms = config['rooms']
        refs = [backends.room_ref(room) for room in rooms]
    names = []
    for room, ref in zip(rooms, refs):
        name = room.get('name') or (connected[ref[0]].display_name(ref[1]) if ref else None)
        if not name and ref:
            name = room.get('last_known_name') or ref[1]
            log({'event': 'room-name-unavailable', 'backend': ref[0], 'id': ref[1],
                 'fallback': name})
        if not name: raise ValueError('Room needs a name or a backend: %r' % room)
        names.append(name)
    if len(set(names)) != len(names): raise ValueError('Room names must be unique: %s' % names)
    slots = [int(room.get('slot', index)) for index, room in enumerate(rooms)]
    if len(set(slots)) != len(slots): raise ValueError('Room slots must be unique: %s' % slots)
    if any(not 0 <= slot < MAX_SLOTS for slot in slots): raise ValueError('Room slots run 0-%d' % (MAX_SLOTS - 1))
    return names, refs, slots, connected


def reconcile_sonos_selection(config_path, backend, log):
    """Share the settings writer's lock so discovery cannot overwrite a user's edits."""
    from bridge_config import reconcile_sonos_rooms, update_config_file
    available, pending = backend.selection_snapshot()
    return update_config_file(config_path, lambda config: reconcile_sonos_rooms(config, available, pending))


def sonos_selection_changed(config, backend, names, refs, slots):
    rooms = config.get('rooms', [])
    wanted_refs = [backends.room_ref(room) for room in rooms]
    wanted_names = [room.get('name') or (backend.display_name(ref[1]) if ref else None)
                    or room.get('last_known_name') or (ref[1] if ref else '')
                    for room, ref in zip(rooms, wanted_refs)]
    wanted_slots = [int(room.get('slot', index)) for index, room in enumerate(rooms)]
    return (wanted_names, wanted_refs, wanted_slots) != (names, refs, slots)


def publish_player_status(path, house, slots):
    """Record the selection actually serving HTTP, separate from the radio's status."""
    rooms = []
    for player, slot in zip(house.players, slots):
        ref = player.state.backend.ref if player.state.backend else None
        key = (ref[1] if ref[0] == 'ha' else '%s:%s' % ref) if ref else 'sim:' + player.config.room
        rooms.append({'key': key, 'name': player.config.room, 'slot': slot})
    temporary = path.with_name(path.name + '.%d.tmp' % os.getpid())
    components = [{'key': player.component_key, 'name': player.config.room,
                   'kind': 'bridge' if player.state.is_bridge else 'about' if player.state.informational else 'room',
                   'parent_keys': sorted(player.component_parents)} for player in house.devices()]
    status = {'pid': os.getpid(), 'rooms': rooms, 'components': components}
    if house.smart_devices is not None: status['smart_devices'] = house.smart_devices.status()
    temporary.write_text(json.dumps(status) + '\n')
    temporary.replace(path)


def player_status_revision(house):
    """Changes that require a fresh status snapshot from this player process."""
    return (house.discovery_generation,
            house.smart_devices.revision if house.smart_devices is not None else None)


def configured_icons(config_path):
    """Each room's configured icon (rooms[].icon) in config order, None where unset."""
    if config_path is None: return None
    return [room.get('icon') for room in json.loads(Path(config_path).read_text()).get('rooms', [])]


def library_ref(config_path):
    """The (backend name, room id) whose Music menu every room browses, or None: music_menu.library
    as a room ({"backend": ..., "id": ...}), or music_menu.library_entity for Home Assistant."""
    if config_path is None: return None
    menu = json.loads(Path(config_path).read_text()).get('music_menu') or {}
    return backends.room_ref(menu.get('library') or {'entity': menu.get('library_entity')})


def music_service_controllers(service):
    """Providers with per-controller policies, without making other services Spotify."""
    if service is None:
        return ()
    return getattr(service, 'controller_services',
                   (service,) if hasattr(service, 'controller_resolver') else ())


def music_service_playback_policies(service):
    """Unique optional policies; ordinary catalogue providers need no hooks."""
    found = []
    for provider in getattr(service, 'services', (service,)) if service is not None else ():
        policy = getattr(provider, 'playback_policy', None)
        if policy is not None and not any(policy is existing for existing in found):
            found.append(policy)
    return found


def load_music_service(config_path, listen_ip, log, rooms=None):
    """Construct registered providers while retaining existing configuration."""
    if config_path is None: return None
    from music_service_registry import load_services
    config_path = Path(config_path)
    return load_services(json.loads(config_path.read_text()), config_path, listen_ip, log, rooms)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--state', default='bridge-state.json', help='household comes from the pairing')
    parser.add_argument('--config', type=Path, help='rooms and backend settings (JSON); overrides --rooms')
    parser.add_argument('--rooms', nargs='+', default=['Living Room'], help='one simulated ZonePlayer per room')
    parser.add_argument('--iface', default='sonos0')
    parser.add_argument('--leases-file', type=Path, default=Path('/run/cr100-bridge/leases'),
                        help='dnsmasq leases used for controller visibility and Spotify menus')
    parser.add_argument('--listen-ip', default='192.168.69.1', help="the TAP's own address (SSDP membership)")
    parser.add_argument('--base-ip', default='192.168.69.11', help='first player address; one per room upward')
    parser.add_argument('--prefix', type=int, default=24)
    parser.add_argument('--profile', default='7.1')
    parser.add_argument('--boot-sequence', type=int, default=int(time.time()),
                        help='player restart generation, shared by SSDP and topology')
    parser.add_argument('--peer-dir', type=Path, default=DEFAULT_PEER_DIR)
    parser.add_argument('--no-addresses', action='store_true', help='player addresses already exist')
    args = parser.parse_args()
    status_path = args.leases_file.with_name('players-status.json')
    # SIGHUP (the web interface after saving the config) restarts this process in place: same
    # PID for bridge_run.sh, rooms and music service re-read, the radio link untouched. Handle
    # it before the slow startup below: its default action would end the players, and a second
    # save can arrive while an earlier reload is still starting. One caught here reloads again
    # once startup finishes, so the config it announced is always read.
    reload = threading.Event()
    signal.signal(signal.SIGHUP, lambda *_: reload.set())

    zp = load_peer_module(args.peer_dir)
    configure_profile(zp, args.profile)
    household = json.loads(Path(args.state).read_text())['household']
    log = lambda record: print(json.dumps({'time': round(time.time(), 3), **record}), flush=True)
    try: status_path.unlink(missing_ok=True)
    except OSError as error: log({'event': 'player-status-unavailable', 'error': str(error)})
    if args.config:
        try: rooms, refs, slots, connected = load_rooms(args.config, log)
        except (ValueError, KeyError, OSError) as error: parser.error(str(error))
    else:
        rooms, refs, connected = args.rooms, None, {}
        slots = list(range(len(rooms)))
        if len(set(rooms)) != len(rooms): parser.error('Room names must be unique')
    first = ipaddress.IPv4Address(args.base_ip)
    addresses = [str(first + slot) for slot in slots]
    if not args.no_addresses:
        for address in addresses:
            subprocess.run(['ip', 'addr', 'replace', f'{address}/{args.prefix}', 'dev', args.iface], check=True)
        # A room hidden since the last start leaves its address behind; drop it.
        shown = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'dev', args.iface],
                               capture_output=True, text=True).stdout
        for address in re.findall(r'inet (\d+\.\d+\.\d+\.\d+)/', shown):
            if first <= ipaddress.IPv4Address(address) < first + MAX_SLOTS and address not in addresses:
                subprocess.run(['ip', 'addr', 'del', f'{address}/{args.prefix}', 'dev', args.iface])

    stub = load_music_service(args.config, args.listen_ip, log, rooms=lambda: house.settings_rooms())
    scoped_services = music_service_controllers(stub)
    from spotify_controllers import ControllerRegistry
    registry = ControllerRegistry(args.state, args.leases_file, args.iface)
    for service in scoped_services: service.controller_resolver = registry
    controller_config = json.loads(args.config.read_text()) if args.config else {}
    from ha_hub import current as current_ha_hub
    house = Household(zp, household, rooms, addresses, log, backends=connected.values(), refs=refs,
                      music_service=stub, boot_sequence=args.boot_sequence, slots=slots,
                      library=library_ref(args.config), icons=configured_icons(args.config),
                      controller_config=controller_config, controller_resolver=registry, listen_ip=args.listen_ip,
                      config_path=args.config, devices_hub=current_ha_hub(), devices_config=controller_config)
    from radio_runtime import configure_sources
    configure_sources(house, args.config or Path(args.state).parent / 'bridge-config.json', args.listen_ip)
    from update_service import configure_updates
    configure_updates(house, args.config or Path(args.state).parent / 'bridge-config.json', args.listen_ip)
    from bridge_runtime import configure as configure_bridge_playback
    try:
        bridge_playback = configure_bridge_playback(house, connected.values(),
                                                    args.config or Path(args.state).parent / 'bridge-config.json',
                                                    args.listen_ip)
    except (ValueError, OSError) as error:
        bridge_playback = None
        log({'event': 'bridge-playback-unavailable', 'error': str(error)})
    if stub: stub.start()
    house.start(args.listen_ip, iface=args.iface)
    for player in house.players:
        log({'event': 'player', 'room': player.config.room, 'uuid': player.config.udn, 'icon': player.icon,
             'location': player.state.location,
             'backend': player.state.backend.ref if player.state.backend else None})
    try: publish_player_status(status_path, house, slots)
    except OSError as error: log({'event': 'player-status-unavailable', 'error': str(error)})
    status_generation = player_status_revision(house)
    try:
        while not reload.wait(1):
            current_status_generation = player_status_revision(house)
            if status_generation != current_status_generation:
                try:
                    publish_player_status(status_path, house, slots)
                    status_generation = current_status_generation
                except OSError as error: log({'event': 'player-status-unavailable', 'error': str(error)})
            if args.config and 'sonos' in connected:
                try:
                    config = reconcile_sonos_selection(args.config, connected['sonos'], log)
                    automatic = config.get('backend') == 'sonos' and (config.get('sonos') or {}).get('follow_network', True)
                    if automatic and sonos_selection_changed(config, connected['sonos'], rooms, refs, slots):
                        log({'event': 'network-speakers-changed', 'rooms': [r.get('last_known_name') or r.get('name')
                                                                          for r in config['rooms']]})
                        reload.set()
                except (OSError, ValueError, KeyError) as error:
                    log({'event': 'network-speakers-refresh-failed', 'error': str(error)})
    except KeyboardInterrupt:
        pass
    finally:
        try: status_path.unlink(missing_ok=True)
        except OSError: pass
        house.close()
        if bridge_playback: bridge_playback.close()
        if stub: stub.close()
        for backend in connected.values(): backend.close()
    if reload.is_set():
        log({'event': 'reload'})
        argv = list(sys.argv)   # drop any --boot-sequence: the restarted players are a new generation
        for i, arg in enumerate(argv):
            if arg == '--boot-sequence': del argv[i:i + 2]; break
            if arg.startswith('--boot-sequence='): del argv[i]; break
        # exec resets a handled signal to its default (terminate) but keeps an ignored one.
        # Ignore SIGHUP until the new image installs its handler, before it reads the config.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        os.execv(sys.executable, [sys.executable] + argv)


if __name__ == '__main__':
    main()
