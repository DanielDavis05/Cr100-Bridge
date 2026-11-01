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

Rooms can be backed by Home Assistant media_player entities (`ha_backend.py`):
transport, volume, mute, grouping and now-playing with album art then follow the
real speakers. Rooms without an entity stay simulated. The Music menu (favourites,
library, Sonos playlists, radio, each room's queue) comes from Home Assistant's media
browser (`ha_content.py`).

Usage: sudo python3 zoneplayers.py --state bridge-state.json --rooms "Living Room" Kitchen
       sudo python3 zoneplayers.py --state bridge-state.json --config bridge-config.json
"""
from __future__ import annotations

import argparse
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

import room_icons
from ha_backend import PLAY_MODES, ArtStore, BackendError, HARoom
from ha_content import Content, ContentError

_HERE = Path(__file__).resolve().parent
# The player implementation and protocol fixtures ship with this distribution.
DEFAULT_PEER_DIR = _HERE / 'zp80'
SSDP_PORT = 1900
ALIVE_INTERVAL = 600   # the CR100 finds players by M-SEARCH; each alive round is a burst to it
DUPLICATE_WINDOW = 0.5   # the CR100 sends each M-SEARCH to multicast and broadcast
HA_STARTUP_WAIT = 15   # s to wait for Home Assistant before naming rooms
PUBLISH_DELAY = 0.2      # s; Home Assistant often reports one change as several state updates
MAX_SLOTS = 38           # player addresses .11-.48, below the DHCP range (.50-.99)


def load_peer_module(peer_dir):
    sys.path.insert(0, str(peer_dir))
    import zp80_peer
    return zp80_peer


def player_mac(slot):
    """Locally administered, stable per room slot (see load_rooms)."""
    return '02:69:00:00:00:%02x' % (slot + 1)


QUEUE_ACTIONS = ('AddURIToQueue', 'RemoveTrackFromQueue', 'RemoveAllTracksFromQueue')
AVT_EVENT_KEYS = ('TransportState', 'CurrentPlayMode', 'NumberOfTracks', 'CurrentTrack', 'CurrentTrackDuration',
                  'CurrentMediaDuration', 'AVTransportURI', 'AVTransportURIMetaData', 'CurrentTrackURI',
                  'CurrentTrackMetaData', 'CurrentTransportActions')


def build_classes(zp):
    class HouseholdState(zp.PeerState):
        def __init__(self, config, household, backend=None):
            super().__init__(config)
            self.household = household
            self.backend = backend   # an ha_backend.HARoom, or None for a simulated room

        def topology(self):
            return self.household.topology()

        def last_change(self, rendering=False):
            if not self.backend: return super().last_change(rendering)
            ns = ('urn:schemas-upnp-org:metadata-1-0/RCS/' if rendering
                  else 'urn:schemas-upnp-org:metadata-1-0/AVT/')
            root = zp.ET.Element('Event', xmlns=ns)
            instance = zp.ET.SubElement(root, 'InstanceID', val='0')
            if rendering:
                for channel in ('Master', 'LF', 'RF'):
                    zp.ET.SubElement(instance, 'Volume', channel=channel, val=str(self.backend.volume))
                    zp.ET.SubElement(instance, 'Mute', channel=channel, val=str(self.backend.mute))
                    zp.ET.SubElement(instance, 'Loudness', channel=channel, val=str(self.loudness))
                zp.ET.SubElement(instance, 'Bass', val=str(self.bass))
                zp.ET.SubElement(instance, 'Treble', val=str(self.treble))
                zp.ET.SubElement(instance, 'OutputFixed', val='0')
            else:
                values = self.backend.values(self.config.udn)
                zp.ET.SubElement(instance, 'TransportStatus', val='OK')
                for key in AVT_EVENT_KEYS: zp.ET.SubElement(instance, key, val=values[key])
                for key, value in (('CurrentCrossfadeMode', '0'), ('NextAVTransportURI', ''),
                                   ('NextAVTransportURIMetaData', ''), ('TransportPlaySpeed', '1'),
                                   ('CurrentValidPlayModes', ','.join(PLAY_MODES.values()))):
                    zp.ET.SubElement(instance, key, val=value)
            return zp.ET.tostring(root, encoding='unicode')

        def event_values(self, service):
            values = super().event_values(service)
            if self.backend and service == 'GroupRenderingControl':
                volume, mute = self.household.group_volume(self.config.udn)
                values.update(GroupVolume=str(volume), GroupMute=str(mute))
            elif self.backend and service == 'ContentDirectory':
                values['ContainerUpdateIDs'] = 'Q:0,%d' % self.backend.queue_version()
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
            return values

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
                coordinator = self.household.by_udn[self.household.coordinator[self.config.udn]]
                backend = coordinator.state.backend
                if backend is None:
                    raise zp.UPnPError(501, 'Action Failed: this room has no Sonos playback backend')
                try:
                    provider_uri, media_type = resolver(uri, metadata)
                    if action == 'AddURIToQueue':
                        return backend.enqueue_uri(provider_uri, media_type,
                                                   args.get('EnqueueAsNext') in ('1', 'true'))
                    backend.play_uri(provider_uri, media_type, 'replace')
                    backend.queue_edited()
                    return {}
                except (ValueError, BackendError) as error:
                    raise zp.UPnPError(501, 'Action Failed: %s' % error) from error
            return None

        def browse(self, args):
            """ContentDirectory Browse from Home Assistant; a simulated room shows the
            household's content but has no queue of its own. The Music menu comes from the
            configured library speaker when there is one (a non-Sonos room has no Sonos
            library of its own); each room's queue always comes from its own speaker."""
            content, object_id = self.household.content, args['ObjectID']
            own = self.backend.entity_id if self.backend else None
            if object_id.startswith('Q:'): entity = own
            else: entity = self.household.library_entity or own or self.household.default_entity
            if not content or not entity: return None
            try:
                result, returned, total = content.browse(entity, self.config.udn, object_id, args['BrowseFlag'],
                                                         int(args['StartingIndex'] or 0), int(args['RequestedCount'] or 0))
            except ContentError as error:
                raise zp.UPnPError(error.code, str(error))
            update = self.backend.queue_version() if self.backend and object_id.startswith('Q:') else 1
            return {'Result': result, 'NumberReturned': str(returned), 'TotalMatches': str(total),
                    'UpdateID': str(update)}

        def action(self, service, action, args):
            values, changed = super().action(service, action, args)
            udn = self.config.udn
            extra = self.music_service_action(service, action, args) if self.household.music_service else None
            try:
                if extra is not None or service == 'MusicServices':
                    if extra is not None and service == 'AVTransport':
                        changed = list(changed) + ['ContentDirectory', 'Queue']
                elif service == 'ContentDirectory' and action == 'Browse':
                    extra = self.browse(args)
                elif service == 'AVTransport' and action == 'SetAVTransportURI' \
                        and args.get('CurrentURI', '').startswith('x-rincon:'):
                    if self.household.join(udn, args['CurrentURI'][len('x-rincon:'):]):
                        changed = list(changed) + ['ZoneGroupTopology']
                elif service == 'AVTransport' and action == 'BecomeCoordinatorOfStandaloneGroup':
                    if self.household.leave(udn):
                        changed = list(changed) + ['ZoneGroupTopology']
                elif self.backend and service == 'GroupRenderingControl':
                    self.household.group_action(udn, action, args)
                elif self.backend:
                    extra = self.backend.action(service, action, args)
                    if service == 'AVTransport' and action in QUEUE_ACTIONS:
                        changed = list(changed) + ['ContentDirectory', 'Queue']
            except BackendError as error:
                raise zp.UPnPError(501, 'Action Failed: %s' % error)
            if extra: values.update({key: value for key, value in extra.items() if key in values})
            if self.backend:
                current = self.backend.values(udn)
                if service == 'GroupRenderingControl':
                    volume, mute = self.household.group_volume(udn)
                    current.update(CurrentVolume=str(volume), CurrentMute=str(mute), NewVolume=str(volume))
                elif service == 'RenderingControl':
                    current['NewVolume'] = current['CurrentVolume']
                values.update({key: value for key, value in current.items() if key in values})
            return values, changed

        @property
        def group_id(self):
            return self.household.group_id(self.config.udn)

    class ArtHandler(zp.PeerHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            art = self.peer.household.art
            if not (path.startswith('/art/') and path.endswith('.jpg') and art): return super().do_GET()
            image = art.get(path[len('/art/'):-len('.jpg')])
            if image: self.reply(200, image, 'image/jpeg', headers={'Cache-Control': 'max-age=86400'})
            else: self.reply(404, b'Not found', 'text/plain')

    class VirtualPlayer(zp.ZP80Peer):
        def __init__(self, config, household, backend=None):
            super().__init__(config)
            self.state = HouseholdState(self.config, household, backend)
            self.household = household
            self.http.RequestHandlerClass = ArtHandler
            self.published = {}

        def publish(self, service):
            if service == 'ZoneGroupTopology':
                for player in self.household.players: player.publish_changed(service)
            else:
                self.publish_changed(service)

        def publish_changed(self, service):
            """NOTIFY subscribers only when the evented values differ from the last NOTIFY."""
            body = self.state.propertyset(service)
            with self.state.lock:
                if self.published.get(service) == body: return
                self.published[service] = body
            zp.ZP80Peer.publish(self, service)

    return HouseholdState, VirtualPlayer


class Household:
    def __init__(self, zp, household, rooms, addresses, log=print, port=1400, ssdp_port=SSDP_PORT,
                 entities=None, ha=None, music_service=None, boot_sequence=1, slots=None, library_entity=None,
                 icons=None):
        """rooms: names; entities: a Home Assistant media_player per room, or None for a simulated room;
        music_service: a smapi_stub.SmapiStub to advertise, or None; slots: each room's fixed
        player number (its UUID), default its position; library_entity: the speaker the Music
        menu browses, default each room's own; icons: each room's room_icons name, default
        guessed from its name."""
        self.zp, self.log, self.ssdp_port, self.ha = zp, log, ssdp_port, ha
        self.music_service = music_service
        self.music_service_accounts = None
        if music_service:
            from music_service_events import encode_media_servers
            # Keep one encoded value per household so unchanged topology events stay equal.
            self.music_service_accounts = encode_media_servers(music_service.media_servers(), household)
        self.lock = threading.RLock()
        self.coordinator = {}   # player UDN -> its group coordinator's UDN
        entities = entities or [None] * len(rooms)
        if any(entities) and ha is None: raise ValueError('Rooms with entities need Home Assistant')
        self.art = ArtStore(ha, log) if ha else None
        self.content = Content(ha, self.art, log) if ha else None
        self.default_entity = next((e for e in entities if e), None)   # browses for simulated rooms
        self.library_entity = library_entity
        slots = list(range(len(rooms))) if slots is None else slots
        icons = icons or [None] * len(rooms)
        _, VirtualPlayer = build_classes(zp)
        self.players = []
        if not len(rooms) == len(addresses) == len(entities) == len(slots): raise ValueError('Need one address per room')
        if len(set(slots)) != len(slots): raise ValueError('Room slots must be unique')
        for room, ip, entity, slot, icon in zip(rooms, addresses, entities, slots, icons):
            config = zp.Config(bind=ip, advertise_ip=ip, port=port, household=household,
                               mac=player_mac(slot), room=room, boot_sequence=boot_sequence)
            backend = HARoom(ha, entity, self.art, log, self.content) if entity else None
            player = VirtualPlayer(config, self, backend)
            player.icon = room_icons.effective(room, icon)
            self.players.append(player)
            self.coordinator[config.udn] = config.udn
        self.by_udn = {p.config.udn: p for p in self.players}
        self.by_entity = {p.state.backend.entity_id: p for p in self.players if p.state.backend}
        self.dirty, self.flush_timer = set(), None
        if ha:
            ha.listeners.append(self.on_ha_state)
            self.sync_groups()

    # --- Home Assistant ---------------------------------------------------------
    def on_ha_state(self, entity_id, state):
        """Runs on the client thread: collect changes, publish them together shortly after."""
        player = self.by_entity.get(entity_id)
        if player is None: return
        with self.lock:
            self.dirty.add(player.config.udn)
            if self.flush_timer is None:
                self.flush_timer = threading.Timer(PUBLISH_DELAY, self.flush)
                self.flush_timer.daemon = True
                self.flush_timer.start()

    def flush(self):
        with self.lock:
            dirty, self.dirty, self.flush_timer = self.dirty, set(), None
        if self.sync_groups(): self.players[0].publish('ZoneGroupTopology')
        coordinators = {self.coordinator[udn] for udn in dirty}
        for udn in dirty:
            player = self.by_udn[udn]
            # ContentDirectory and Queue go out only when the queue version moved (publish_changed).
            for service in ('AVTransport', 'RenderingControl', 'ContentDirectory', 'Queue'): player.publish(service)
        for udn in dirty | coordinators: self.by_udn[udn].publish('GroupRenderingControl')

    def sync_groups(self):
        """Take Home Assistant's groups for HA rooms. A group's coordinator is its first
        member that is also a room here (Home Assistant lists the coordinator first)."""
        changed = False
        with self.lock:
            for entity, player in self.by_entity.items():
                members = [m for m in player.state.backend.group_members() if m in self.by_entity] or [entity]
                coordinator = self.by_entity[members[0]].config.udn
                if self.coordinator[player.config.udn] != coordinator:
                    self.coordinator[player.config.udn] = coordinator; changed = True
        if changed: self.log({'event': 'groups', 'coordinators': {self.by_udn[u].config.room: self.by_udn[c].config.room
                                                                  for u, c in self.coordinator.items()}})
        return changed

    def members(self, udn):
        with self.lock:
            coordinator = self.coordinator.get(udn, udn)
            return [self.by_udn[u] for u, c in self.coordinator.items() if c == coordinator]

    def group_volume(self, udn):
        backends = [p.state.backend for p in self.members(udn) if p.state.backend]
        if not backends: return 0, 0
        return (round(sum(b.volume for b in backends) / len(backends)),
                int(all(b.mute for b in backends)))

    def group_action(self, udn, action, args):
        backends = [p.state.backend for p in self.members(udn) if p.state.backend]
        if action in ('SetGroupVolume', 'SetRelativeGroupVolume'):
            volume, _ = self.group_volume(udn)
            delta = int(args['Adjustment']) if 'Adjustment' in args else int(args['DesiredVolume']) - volume
            for backend in backends: backend.set_volume(backend.volume + delta)
        elif action == 'SetGroupMute':
            for backend in backends: backend.set_mute(args['DesiredMute'] in ('1', 'true', 'yes'))

    # --- topology ---------------------------------------------------------------
    def group_id(self, udn):
        return self.coordinator.get(udn, udn) + ':1'

    def topology(self):
        zp = self.zp
        root = zp.ET.Element('ZoneGroups')
        with self.lock:
            for coordinator in [p.config.udn for p in self.players if self.coordinator[p.config.udn] == p.config.udn]:
                group = zp.ET.SubElement(root, 'ZoneGroup', Coordinator=coordinator, ID=coordinator + ':1')
                for player in self.players:
                    if self.coordinator[player.config.udn] != coordinator: continue
                    config = player.config
                    zp.ET.SubElement(group, 'ZoneGroupMember', UUID=config.udn, Location=player.state.location,
                        ZoneName=config.room, Icon=room_icons.uri(player.icon), Configuration='1',
                        SoftwareVersion=zp.PROFILE['sw'], MinCompatibleVersion=zp.PROFILE['mincompat'],
                        LegacyCompatibleVersion=zp.PROFILE['legacycompat'], BootSeq=str(config.boot_sequence),
                        WirelessMode='0', ChannelFreq='2412', BehindWifiExtender='0', WifiEnabled='1')
        return zp.ET.tostring(root, encoding='unicode')

    def join(self, udn, target):
        player, other = self.by_udn.get(udn), self.by_udn.get(target)
        if player and player.state.backend:
            if not (other and other.state.backend) or target == udn: return False
            coordinator = self.by_udn[self.coordinator[target]].state.backend
            # Home Assistant reports the new group; flush() then publishes the topology.
            coordinator.ha_call('join', {'group_members': [player.state.backend.entity_id]})
            self.log({'event': 'join', 'player': udn, 'coordinator': coordinator.entity_id})
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
        if player and player.state.backend:
            if self.coordinator.get(udn) != udn: player.state.backend.ha_call('unjoin')
            return False
        with self.lock:
            if self.coordinator.get(udn) == udn: return False
            self.coordinator[udn] = udn
        self.log({'event': 'leave', 'player': udn})
        return True

    # --- SSDP -------------------------------------------------------------------
    def ssdp_loop(self, listen_ip, stop, iface=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Only the SonosNet TAP: a wildcard bind would also answer searches from the LAN.
        if iface: listener.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        listener.bind(('', self.ssdp_port))
        listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(self.zp.SSDP_GROUP) + socket.inet_aton(listen_ip))
        listener.settimeout(1)
        senders = {}
        for player in self.players:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sender.bind((player.config.advertise_ip, 0))
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(player.config.advertise_ip))
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            senders[player.config.udn] = sender
        recent, next_alive = {}, 0.0
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_alive:
                    for player in self.players:
                        for target in player.ssdp_targets():
                            senders[player.config.udn].sendto(player.ssdp_packet(target, alive=True),
                                                              (self.zp.SSDP_GROUP, SSDP_PORT))
                    next_alive = now + ALIVE_INTERVAL
                try:
                    packet, source = listener.recvfrom(8192)
                except socket.timeout:
                    continue
                if source[0] in senders or source[0] in {p.config.advertise_ip for p in self.players}: continue
                key = (source, packet)
                if now - recent.get(key, -1e9) < DUPLICATE_WINDOW: continue
                recent = {k: t for k, t in recent.items() if now - t < DUPLICATE_WINDOW}
                recent[key] = now
                answered = 0
                for player in self.players:
                    for response in player.search_responses(packet):
                        senders[player.config.udn].sendto(response, source)
                        answered += 1
                if answered: self.log({'event': 'm-search', 'from': source[0], 'responses': answered})
        finally:
            listener.close()
            for sender in senders.values(): sender.close()

    def start(self, listen_ip, ssdp=True, iface=None):
        self.stop = threading.Event()
        for player in self.players: player.start(ssdp=False, netstart=False)
        if ssdp:
            threading.Thread(target=self.ssdp_loop, args=(listen_ip, self.stop, iface), daemon=True).start()

    def close(self):
        self.stop.set()
        for player in self.players: player.close()


def load_rooms(config_path, log):
    """Rooms from the config file. Returns (names, entities, slots, Home Assistant client or None).

    {"home_assistant": {"url": "http://192.168.1.148:8123", "token_file": "/home/kali/ha-token"},
     "rooms": [{"entity": "media_player.kitchen", "slot": 0}, {"name": "Den", "entity": "media_player.den", "icon": "den"},
               {"name": "Spare"}],
     "music_menu": {"library_entity": "media_player.kitchen"}}
    A room without "name" takes the entity's friendly name; one without "entity" is simulated.
    "slot" fixes the room's player address and UUID, so hiding or reordering other rooms
    (the web interface does both) never changes which player the CR100 knows it as; a room
    without one takes its position, as before slots existed. "icon" is a room_icons name
    (configured_icons); without one the icon is guessed from the room's name.
    """
    config = json.loads(Path(config_path).read_text())
    rooms = config['rooms']
    ha = None
    if any(room.get('entity') for room in rooms):
        from ha_client import HomeAssistant
        settings = config['home_assistant']
        token_file = Path(settings['token_file']).expanduser()
        if not token_file.is_absolute(): token_file = Path(config_path).resolve().parent / token_file
        ha = HomeAssistant(settings['url'], token_file.read_text().strip(), log)
        ha.start()
        if not ha.connected.wait(HA_STARTUP_WAIT):
            log({'event': 'ha-not-connected', 'detail': 'starting anyway; rooms update when it connects'})
    names = []
    for room in rooms:
        entity = room.get('entity')
        name = room.get('name') or (ha.state(entity).get('attributes', {}).get('friendly_name') if entity else None) \
            or (entity.split('.', 1)[-1].replace('_', ' ').title() if entity else None)
        if not name: raise ValueError('Room needs a name or an entity: %r' % room)
        names.append(name)
    if len(set(names)) != len(names): raise ValueError('Room names must be unique: %s' % names)
    slots = [int(room.get('slot', index)) for index, room in enumerate(rooms)]
    if len(set(slots)) != len(slots): raise ValueError('Room slots must be unique: %s' % slots)
    if any(not 0 <= slot < MAX_SLOTS for slot in slots): raise ValueError('Room slots run 0-%d' % (MAX_SLOTS - 1))
    return names, [room.get('entity') for room in rooms], slots, ha


def configured_icons(config_path):
    """Each room's configured icon (rooms[].icon) in config order, None where unset."""
    if config_path is None: return None
    return [room.get('icon') for room in json.loads(Path(config_path).read_text()).get('rooms', [])]


def library_entity(config_path):
    """The speaker the Music menu browses (music_menu.library_entity), or None."""
    if config_path is None: return None
    return (json.loads(Path(config_path).read_text()).get('music_menu') or {}).get('library_entity') or None


def load_music_service(config_path, listen_ip, log):
    """Prefer the Spotify catalogue when configured; retain the diagnostic stub."""
    if config_path is None: return None
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    settings = config.get('spotify_music_service')
    if settings and settings.get('enabled', True):
        from spotify_client import SpotifyClient
        from spotify_service import SpotifyService
        token_file = Path(settings.get('token_file', 'spotify-token.json')).expanduser()
        if not token_file.is_absolute(): token_file = config_path.resolve().parent / token_file
        client = SpotifyClient(settings.get('client_id', ''), token_file, log=log)
        return SpotifyService(settings.get('host', listen_ip), settings.get('port', 8400), client,
                              service_id=settings.get('id', 240), name=settings.get('name', 'Spotify'), log=log)
    settings = config.get('music_service_stub')
    if settings and settings.get('enabled', True):
        from smapi_stub import SmapiStub
        return SmapiStub(settings.get('host', listen_ip), settings.get('port', 8400),
                         settings.get('id', 240), settings.get('name', 'Bridge Test'), log,
                         str(settings.get('capabilities', '1')))
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--state', default='bridge-state.json', help='household comes from the pairing')
    parser.add_argument('--config', type=Path, help='rooms and Home Assistant settings (JSON); overrides --rooms')
    parser.add_argument('--rooms', nargs='+', default=['Living Room'], help='one simulated ZonePlayer per room')
    parser.add_argument('--iface', default='sonos0')
    parser.add_argument('--listen-ip', default='192.168.69.1', help="the TAP's own address (SSDP membership)")
    parser.add_argument('--base-ip', default='192.168.69.11', help='first player address; one per room upward')
    parser.add_argument('--prefix', type=int, default=24)
    parser.add_argument('--profile', default='7.1')
    parser.add_argument('--boot-sequence', type=int, default=int(time.time()),
                        help='player restart generation, shared by SSDP and topology')
    parser.add_argument('--peer-dir', type=Path, default=DEFAULT_PEER_DIR)
    parser.add_argument('--no-addresses', action='store_true', help='player addresses already exist')
    args = parser.parse_args()

    zp = load_peer_module(args.peer_dir)
    zp.PROFILE = dict(zp.PROFILES[args.profile])
    zp.PeerHandler.server_version = zp.PROFILE['server']
    household = json.loads(Path(args.state).read_text())['household']
    log = lambda record: print(json.dumps({'time': round(time.time(), 3), **record}), flush=True)
    if args.config:
        try: rooms, entities, slots, ha = load_rooms(args.config, log)
        except (ValueError, KeyError, OSError) as error: parser.error(str(error))
    else:
        rooms, entities, ha = args.rooms, None, None
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

    stub = load_music_service(args.config, args.listen_ip, log)
    if stub: stub.start()
    house = Household(zp, household, rooms, addresses, log, entities=entities, ha=ha, music_service=stub,
                      boot_sequence=args.boot_sequence, slots=slots, library_entity=library_entity(args.config),
                      icons=configured_icons(args.config))
    house.start(args.listen_ip, iface=args.iface)
    for player in house.players:
        log({'event': 'player', 'room': player.config.room, 'uuid': player.config.udn, 'icon': player.icon,
             'location': player.state.location,
             'entity': player.state.backend.entity_id if player.state.backend else None})
    # SIGHUP (the web interface after saving the config) restarts this process in place: same
    # PID for bridge_run.sh, rooms and music service re-read, the radio link untouched.
    reload = threading.Event()
    signal.signal(signal.SIGHUP, lambda *_: reload.set())
    try:
        while not reload.wait(1): pass
    except KeyboardInterrupt:
        pass
    finally:
        house.close()
        if stub: stub.close()
        if ha: ha.close()
    if reload.is_set():
        log({'event': 'reload'})
        argv = list(sys.argv)   # drop any --boot-sequence: the restarted players are a new generation
        for i, arg in enumerate(argv):
            if arg == '--boot-sequence': del argv[i:i + 2]; break
            if arg.startswith('--boot-sequence='): del argv[i]; break
        os.execv(sys.executable, [sys.executable] + argv)


if __name__ == '__main__':
    main()
