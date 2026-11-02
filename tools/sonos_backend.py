"""Sonos backend for virtual ZonePlayers: one real Sonos player per room, over its local UPnP.

Rooms are player UUIDs (RINCON_...). The backend reads the household's topology from any
of its players, subscribes to every visible player's AVTransport, RenderingControl and
ContentDirectory events and to the topology, and reads state itself: right after each
action (events come seconds late; see sonos_upnp.py), every RESYNC_INTERVAL, and every
POLL_INTERVAL while a player's events are not arriving. Transport goes to the room's group
coordinator; volume to the room itself. The Music menu is the players' own
ContentDirectory (`sonos_content.py`), and album art comes from the players.

Music services (the bridge's Spotify): Sonos must have the account linked. Items are
queued as share links, as SoCo's ShareLinkPlugin does: the encoded URI with the
service's SA_RINCON token in the metadata, trying the EU then the US service number.

Config: {"sonos": {"hosts": ["10.0.0.130"], "event_port": 0}}, rooms {"backend": "sonos",
"id": "RINCON_000E5820ACB601400"}. Without hosts the players are found by SSDP.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from urllib.parse import urljoin

import sonos_upnp
from backends import MODE_SETTINGS, PLAY_MODES, ArtStore, Backend, BackendError, Room, hms, parse_hms
from content import PLAIN_SCHEMES
from sonos_content import SonosContent
from sonos_upnp import UPnPFault, didl_entry, parse_didl, soap

STARTUP_WAIT = 10        # s to wait for the first topology before naming rooms
RESYNC_INTERVAL = 30     # s between full reads of every player, events or not
POLL_INTERVAL = 3        # s between reads of a player whose events are not arriving
TOPOLOGY_INTERVAL = 60   # s between topology reads (also on its events)
RETRY_TOPOLOGY = 5       # s between topology attempts until one succeeds
POSITION_TTL = 5         # s a read position is interpolated before it is read again
REFRESH_AFTER = (0.3, 1.5, 4.0)   # s after an action that its player is read again
READ_TIMEOUT = 3
ART_LIMIT = 8 * 1024 * 1024
QUEUE_PREFIX = 'x-rincon-queue:'
SHARE_SERVICES = (2311, 3079)    # Spotify's service numbers: EU/global, then US
SHARE_KINDS = {   # spotify kind -> (enqueue URI prefix, item id key, class), as SoCo's ShareLinkPlugin
    'track': ('', '00032020', 'object.item.audioItem.musicTrack'),
    'album': ('x-rincon-cpcontainer:1004206c', '00040000', 'object.container.album.musicAlbum'),
    'playlist': ('x-rincon-cpcontainer:1006206c', '1006206c', 'object.container.playlistContainer'),
}
IGNORED_STREAM = re.compile(r'^(ZPSTR_\w+|)$')   # buffering/connecting placeholders, not titles


def number(text, default=0):
    try: return int(text)
    except (TypeError, ValueError): return default


def seconds(text):
    try: return parse_hms(text) if text and text != 'NOT_IMPLEMENTED' else 0
    except ValueError: return 0


class Speaker:
    """One real player: its last known state and when to read it again."""

    def __init__(self, uuid, base, name):
        self.uuid, self.base, self.name = uuid, base, name
        self.avt, self.volume, self.mute = {}, None, False
        self.queue_update = 0
        self.position, self.position_at = 0.0, 0.0     # seconds, monotonic time read
        self.read_at, self.refresh_at = 0.0, []         # last full read; requested re-reads
        self.subscriptions = []

    @property
    def healthy(self):
        return bool(self.subscriptions) and all(s.healthy for s in self.subscriptions)


class SonosBackend(Backend):
    """One Sonos household, reached through hosts (addresses or base URLs) or discovery."""
    name = 'sonos'

    def __init__(self, hosts=(), log=print, event_port=0, listener=None):
        super().__init__(log)
        self.hosts = list(hosts)
        self.lock = threading.RLock()
        self.speakers = {}                     # uuid -> Speaker
        self.groups = []                       # [(coordinator, [members])]
        self.topology_base, self.topology_at = None, 0.0
        self.topology_subscription = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.listener = listener or sonos_upnp.EventListener(log, port=event_port)
        self.art = ArtStore(self, log)
        self.content = SonosContent(self, self.art, log)

    # --- lifecycle --------------------------------------------------------------------
    def start(self):
        self.listener.start()
        threading.Thread(target=self.run, name='sonos-reads', daemon=True).start()

    def close(self):
        self.stop.set(); self.wake.set()
        self.listener.close()

    def run(self):
        """The one thread that reads players, so a slow player never runs two reads at once."""
        while not self.stop.is_set():
            try:
                if time.monotonic() - self.topology_at > (TOPOLOGY_INTERVAL if self.speakers else RETRY_TOPOLOGY):
                    self.read_topology()
                for speaker in self.due():
                    if self.stop.is_set(): return
                    self.read(speaker)
            except Exception as error:
                self.log({'event': 'sonos-read-loop-error', 'error': repr(error)})
            self.wake.wait(0.1); self.wake.clear()

    def due(self):
        now = time.monotonic()
        with self.lock:
            found = []
            for speaker in self.speakers.values():
                requested = [t for t in speaker.refresh_at if t <= now]
                interval = RESYNC_INTERVAL if speaker.healthy else POLL_INTERVAL
                if requested or now - speaker.read_at > interval:
                    speaker.refresh_at = [t for t in speaker.refresh_at if t > now]
                    found.append(speaker)
            return found

    def refresh_soon(self, uuid):
        """Read this player again shortly: what an action changed, before its event arrives."""
        with self.lock:
            speaker = self.speakers.get(uuid)
            if speaker: speaker.refresh_at.extend(time.monotonic() + delay for delay in REFRESH_AFTER)
        self.wake.set()

    # --- topology -----------------------------------------------------------------------
    def read_topology(self):
        self.topology_at = time.monotonic()
        try:
            base, groups, zones = sonos_upnp.household(self.hosts + ([self.topology_base] if self.topology_base else []),
                                                       READ_TIMEOUT)
        except UPnPFault as error:
            self.log({'event': 'sonos-topology-failed', 'error': str(error)})
            return
        self.apply_topology(groups, zones)
        if base != self.topology_base or self.topology_subscription is None:
            if self.topology_subscription: self.listener.unsubscribe(self.topology_subscription)
            self.topology_base = base
            self.topology_subscription = self.listener.subscribe(base, 'ZoneGroupTopology', self.on_topology)
        self.ready.set()

    def on_topology(self, values):
        if 'ZoneGroupState' not in values: return   # some S2 events leave it out: read it then
        if not values['ZoneGroupState']:
            self.topology_at = 0; self.wake.set(); return
        self.apply_topology(*sonos_upnp.parse_topology(values['ZoneGroupState']))

    def apply_topology(self, groups, zones):
        added = []
        with self.lock:
            changed = groups != self.groups
            self.groups = groups
            for uuid, zone in zones.items():
                speaker = self.speakers.get(uuid)
                if speaker is None:
                    speaker = self.speakers[uuid] = Speaker(uuid, zone['base'], zone['name'])
                    added.append(speaker)
                elif (speaker.base, speaker.name) != (zone['base'], zone['name']):
                    if speaker.base != zone['base']: added.append(speaker)   # moved: subscribe there
                    speaker.base, speaker.name = zone['base'], zone['name']; changed = True
        for speaker in added: self.watch(speaker)
        if changed:
            self.log({'event': 'sonos-groups', 'groups': [[self.zone_name(u) for u in members] for _, members in groups]})
            for uuid in list(self.speakers): self.changed(uuid)

    def watch(self, speaker):
        for subscription in speaker.subscriptions: self.listener.unsubscribe(subscription)
        uuid = speaker.uuid
        speaker.subscriptions = [
            self.listener.subscribe(speaker.base, 'AVTransport', lambda v: self.on_avt(uuid, v)),
            self.listener.subscribe(speaker.base, 'RenderingControl', lambda v: self.on_rendering(uuid, v)),
            self.listener.subscribe(speaker.base, 'ContentDirectory', lambda v: self.on_content(uuid, v)),
        ]
        speaker.read_at = 0   # read it now as well
        self.wake.set()

    def zone_name(self, uuid):
        speaker = self.speakers.get(uuid)
        return speaker.name if speaker else uuid

    def coordinator_of(self, uuid):
        with self.lock:
            for coordinator, members in self.groups:
                if uuid in members: return coordinator
        return uuid

    def members_of(self, uuid):
        with self.lock:
            for _, members in self.groups:
                if uuid in members: return list(members)
        return [uuid]

    def speaker(self, uuid):
        with self.lock: return self.speakers.get(uuid)

    def any_speaker(self):
        with self.lock: return next(iter(self.speakers.values()), None)

    # --- state ------------------------------------------------------------------------------
    def read(self, speaker):
        """Everything a room shows, read from the player itself."""
        base = speaker.base
        try:
            transport = soap(base, 'AVTransport', 'GetTransportInfo', [('InstanceID', 0)], READ_TIMEOUT)
            position = soap(base, 'AVTransport', 'GetPositionInfo', [('InstanceID', 0)], READ_TIMEOUT)
            media = soap(base, 'AVTransport', 'GetMediaInfo', [('InstanceID', 0)], READ_TIMEOUT)
            settings = soap(base, 'AVTransport', 'GetTransportSettings', [('InstanceID', 0)], READ_TIMEOUT)
            actions = soap(base, 'AVTransport', 'GetCurrentTransportActions', [('InstanceID', 0)], READ_TIMEOUT)
            volume = soap(base, 'RenderingControl', 'GetVolume', [('InstanceID', 0), ('Channel', 'Master')], READ_TIMEOUT)
            mute = soap(base, 'RenderingControl', 'GetMute', [('InstanceID', 0), ('Channel', 'Master')], READ_TIMEOUT)
        except UPnPFault as error:
            speaker.read_at = time.monotonic()
            self.log({'event': 'sonos-read-failed', 'speaker': speaker.name, 'error': str(error)})
            return
        avt = {'TransportState': transport.get('CurrentTransportState', ''),
               'CurrentTrack': position.get('Track', ''), 'CurrentTrackDuration': position.get('TrackDuration', ''),
               'CurrentTrackMetaData': position.get('TrackMetaData', ''), 'CurrentTrackURI': position.get('TrackURI', ''),
               'NumberOfTracks': media.get('NrTracks', ''), 'AVTransportURI': media.get('CurrentURI', ''),
               'AVTransportURIMetaData': media.get('CurrentURIMetaData', ''),
               'CurrentPlayMode': settings.get('PlayMode', ''), 'CurrentTransportActions': actions.get('Actions', '')}
        with self.lock:
            speaker.read_at = time.monotonic()
            changed = any(speaker.avt.get(k) != v for k, v in avt.items())
            speaker.avt.update(avt)
            speaker.position, speaker.position_at = seconds(position.get('RelTime')), time.monotonic()
            level, muted = number(volume.get('CurrentVolume'), None), mute.get('CurrentMute') == '1'
            changed_rendering = (level, muted) != (speaker.volume, speaker.mute)
            speaker.volume, speaker.mute = level, muted
        if changed: self.transport_changed(speaker.uuid)
        elif changed_rendering: self.changed(speaker.uuid)

    def read_position(self, speaker):
        try:
            position = soap(speaker.base, 'AVTransport', 'GetPositionInfo', [('InstanceID', 0)], READ_TIMEOUT)
        except UPnPFault:
            return
        with self.lock:
            speaker.position, speaker.position_at = seconds(position.get('RelTime')), time.monotonic()

    def transport_changed(self, uuid):
        """A coordinator's transport is every member's."""
        for member in self.members_of(uuid): self.changed(member)

    def on_avt(self, uuid, values):
        update = sonos_upnp.last_change(values.get('LastChange'))
        speaker = self.speaker(uuid)
        if not speaker or not update: return
        with self.lock:
            speaker.avt.update({k: v for k, v in update.items() if not k.startswith('Next')})
            if 'TransportState' in update or 'CurrentTrackURI' in update: speaker.position_at = 0   # read again
        self.transport_changed(uuid)

    def on_rendering(self, uuid, values):
        update = sonos_upnp.last_change(values.get('LastChange'))
        speaker = self.speaker(uuid)
        if not speaker or not ('Volume' in update or 'Mute' in update): return
        with self.lock:
            if 'Volume' in update: speaker.volume = number(update['Volume'], speaker.volume)
            if 'Mute' in update: speaker.mute = update['Mute'] == '1'
        self.changed(uuid)

    def on_content(self, uuid, values):
        updates = sonos_upnp.container_updates(values.get('ContainerUpdateIDs'))
        speaker = self.speaker(uuid)
        if speaker and 'Q:0' in updates:
            with self.lock: speaker.queue_update = updates['Q:0']
            self.content.queue_changed(uuid)
            self.transport_changed(uuid)

    # --- Backend --------------------------------------------------------------------------
    def room(self, uuid):
        return SonosRoom(self, uuid, self.log)

    def display_name(self, uuid):
        speaker = self.speaker(uuid)
        return speaker.name if speaker else None

    def picture_url(self, base, art):
        """An absolute art URL from a player's (often relative /getaa?...) albumArtURI."""
        return urljoin(base + '/', art) if art else ''

    def fetch(self, picture):
        with urllib.request.urlopen(picture, timeout=10) as response:
            return response.read(ART_LIMIT)



def load(settings, config_dir, log):
    hosts = settings.get('hosts') or []
    if isinstance(hosts, str): hosts = [hosts]
    backend = SonosBackend(hosts, log, event_port=int(settings.get('event_port', 0)))
    backend.start()
    if not backend.ready.wait(STARTUP_WAIT):
        log({'event': 'sonos-not-connected', 'detail': 'starting anyway; rooms update when a player answers'})
    return backend


class SonosRoom(Room):
    """One virtual room backed by one Sonos player."""

    @property
    def speaker(self):
        return self.backend.speaker(self.key)

    @property
    def coordinator(self):
        """The player that owns this room's transport and queue (itself when alone)."""
        return self.backend.speaker(self.backend.coordinator_of(self.key)) or self.speaker

    @property
    def avt(self):
        coordinator = self.coordinator
        if not coordinator: return {}
        with self.backend.lock: return dict(coordinator.avt)

    # --- state ------------------------------------------------------------------------
    @property
    def reported_volume(self):
        speaker = self.speaker
        return speaker.volume if speaker and speaker.volume is not None else 0

    @property
    def reported_mute(self):
        speaker = self.speaker
        return bool(speaker and speaker.mute)

    @property
    def transport(self):
        state = self.avt.get('TransportState')
        return state if state in ('PLAYING', 'PAUSED_PLAYBACK', 'TRANSITIONING') else 'STOPPED'

    def shuffle_repeat(self):
        return MODE_SETTINGS.get(self.avt.get('CurrentPlayMode'), (False, 'off'))

    def group_members(self):
        return self.backend.members_of(self.key)

    def playing_queue(self, avt=None):
        return (avt if avt is not None else self.avt).get('AVTransportURI', '').startswith(QUEUE_PREFIX)

    def track(self):
        avt, coordinator = self.avt, self.coordinator
        uri = avt.get('AVTransportURI', '')
        if not coordinator or not uri or avt.get('TransportState') in (None, '', 'NO_MEDIA_PRESENT'): return None
        entry = next(iter(parse_didl(avt.get('CurrentTrackMetaData'))), {})
        station = next(iter(parse_didl(avt.get('AVTransportURIMetaData'))), {}).get('title', '') \
            if not self.playing_queue(avt) else ''
        stream = entry.get('stream_content', '')
        if IGNORED_STREAM.match(stream): stream = ''
        if stream or (station and not self.playing_queue(avt)):
            artist, _, title = stream.partition(' - ') if ' - ' in stream else ('', '', stream)
            title, album = title or station or entry.get('title', ''), station if stream else ''
        else:
            title, artist, album = entry.get('title', ''), entry.get('artist', ''), entry.get('album', '')
        if not title: return None
        duration = seconds(avt.get('CurrentTrackDuration'))
        art = self.backend.picture_url(coordinator.base, entry.get('art', ''))
        return title, artist, album, self.backend.art.path_for(art), duration or None

    def position(self):
        coordinator = self.coordinator
        if coordinator is None: return 0
        if time.monotonic() - coordinator.position_at > POSITION_TTL: self.backend.read_position(coordinator)
        elapsed = time.monotonic() - coordinator.position_at if self.transport == 'PLAYING' else 0
        duration = seconds(self.avt.get('CurrentTrackDuration'))
        position = coordinator.position + elapsed
        return min(position, duration) if duration else position

    def queue_numbers(self):
        avt = self.avt
        if self.track() is None: return 0, 0
        if self.playing_queue(avt):
            current, total = number(avt.get('CurrentTrack')), number(avt.get('NumberOfTracks'))
            if current and total: return current, total
        return 1, 1

    def queue_token(self):
        coordinator = self.coordinator
        return (coordinator.queue_update, self.avt.get('NumberOfTracks')) if coordinator else None

    def capabilities(self):
        actions = {a.strip().lower() for a in self.avt.get('CurrentTransportActions', '').split(',')}
        return frozenset(actions & {'seek', 'next', 'previous'})

    # --- calls --------------------------------------------------------------------------
    def call(self, speaker, service, action, args=(), ignore=()):
        if speaker is None: raise BackendError('%s is not reachable' % self.key)
        try:
            return soap(speaker.base, service, action, [('InstanceID', 0)] + list(args))
        except UPnPFault as error:
            if error.code in ignore: return {}
            self.log({'event': 'sonos-call-failed', 'speaker': speaker.name, 'action': action, 'error': str(error)})
            raise BackendError(str(error)) from error
        finally:
            self.backend.refresh_soon(speaker.uuid)

    def transport_call(self, action, args=(), ignore=()):
        return self.call(self.coordinator, 'AVTransport', action, args, ignore)

    def queue_uri(self):
        return '%s%s#0' % (QUEUE_PREFIX, self.backend.coordinator_of(self.key))

    # --- actions ------------------------------------------------------------------------
    def play(self): self.transport_call('Play', [('Speed', 1)])
    def pause(self):
        if self.transport in ('STOPPED', 'PAUSED_PLAYBACK'): return   # the player refuses (701); nothing to do
        # A radio stream offers Stop but not Pause (CurrentTransportActions "Set, Stop, Play").
        actions = {a.strip() for a in self.avt.get('CurrentTransportActions', '').split(',')}
        self.transport_call('Stop' if 'Stop' in actions and 'Pause' not in actions else 'Pause')
    def stop(self): self.transport_call('Stop')
    def next(self): self.transport_call('Next')
    def previous(self): self.transport_call('Previous')

    def seek(self, seconds):
        self.transport_call('Seek', [('Unit', 'REL_TIME'), ('Target', hms(seconds))])

    def use_queue(self):
        if not self.playing_queue():
            self.transport_call('SetAVTransportURI', [('CurrentURI', self.queue_uri()), ('CurrentURIMetaData', '')])

    def play_track(self, number):
        self.use_queue()
        self.transport_call('Seek', [('Unit', 'TRACK_NR'), ('Target', number)])
        self.transport_call('Play', [('Speed', 1)])

    def remove_track(self, number):
        self.transport_call('RemoveTrackFromQueue', [('ObjectID', 'Q:0/%d' % number), ('UpdateID', 0)])

    def clear_queue(self):
        self.transport_call('RemoveAllTracksFromQueue', ignore=(804,))   # 804: already empty

    def set_play_mode(self, shuffle, repeat):
        self.transport_call('SetPlayMode', [('NewPlayMode', PLAY_MODES[(shuffle, repeat)])])

    def add_to_queue(self, uri, metadata, as_next=False):
        current = number(self.avt.get('CurrentTrack')) if self.playing_queue() else 0
        result = self.transport_call('AddURIToQueue', [
            ('EnqueuedURI', uri), ('EnqueuedURIMetaData', metadata),
            ('DesiredFirstTrackNumberEnqueued', current + 1 if as_next and current else 0),
            ('EnqueueAsNext', int(bool(as_next)))])
        self.queue_edited()
        return {key: result[key] for key in ('FirstTrackNumberEnqueued', 'NumTracksAdded', 'NewQueueLength')
                if key in result}

    def replace_queue(self, uri, metadata):
        self.clear_queue()
        self.add_to_queue(uri, metadata)
        self.play_track(1)

    def item_uri(self, node):
        """(URI, metadata) a player accepts for a content node."""
        res, res_md = getattr(node, 'res', ''), getattr(node, 'res_md', '')
        if node.container:
            uri = res or 'x-rincon-playlist:%s#%s' % (self.backend.coordinator_of(self.key), node.ref)
            return uri, didl_entry(node.ref, node.parent_id, node.title, node.upnp_class, container=True)
        uri = res or node.ref
        return uri, res_md or didl_entry(node.ref if not node.ref.startswith(PLAIN_SCHEMES) else '-1',
                                         node.parent_id or '-1', node.title, node.upnp_class, res=uri)

    def direct(self, node, uri):
        """Whether the player plays it as its transport (a station, a favourite stream)
        rather than from the queue."""
        return not node.container and not uri.startswith(PLAIN_SCHEMES) and not uri.startswith('x-rincon-cpcontainer:')

    def play_item(self, node):
        uri, metadata = self.item_uri(node)
        if self.direct(node, uri):
            self.transport_call('SetAVTransportURI', [('CurrentURI', uri), ('CurrentURIMetaData', metadata)])
            self.play()
        else:
            self.replace_queue(uri, metadata)

    def enqueue_item(self, node, as_next=False):
        uri, metadata = self.item_uri(node)
        if self.direct(node, uri):   # a station plays at once, as with Home Assistant
            self.play_item(node)
            return {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1',
                    'NewQueueLength': self.avt.get('NumberOfTracks') or '1'}
        return self.add_to_queue(uri, metadata, as_next)

    def share(self, uri, as_next=False):
        """Queue a spotify:<kind>:<id> URI as a share link; the AddURIToQueue results."""
        match = re.fullmatch(r'spotify:(track|album|playlist):(\w+)', uri)
        if not match: raise BackendError('Not a Spotify item: %s' % uri)
        prefix, key, upnp_class = SHARE_KINDS[match.group(1)]
        encoded = uri.replace(':', '%3a')
        error = None
        for service in SHARE_SERVICES:
            metadata = (sonos_upnp.DIDL_OPEN + '<item id="%s%s" restricted="true"><upnp:class>%s</upnp:class>'
                        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
                        'SA_RINCON%d_X_#Svc%d-0-Token</desc></item></DIDL-Lite>') % (key, encoded, upnp_class,
                                                                                   service, service)
            try:
                return self.add_to_queue(prefix + encoded, metadata, as_next)
            except BackendError as failure:
                error = failure
        raise error

    def play_provider(self, uri, kind):
        self.clear_queue()
        self.share(uri)
        self.play_track(1)

    def enqueue_provider(self, uri, kind, as_next=False):
        return self.share(uri, as_next)

    def send_volume(self, value):
        self.call(self.speaker, 'RenderingControl', 'SetVolume', [('Channel', 'Master'), ('DesiredVolume', value)])

    def send_mute(self, value):
        self.call(self.speaker, 'RenderingControl', 'SetMute', [('Channel', 'Master'), ('DesiredMute', int(value))])

    def join(self, member):
        coordinator = self.backend.coordinator_of(self.key)
        self.call(member.speaker, 'AVTransport', 'SetAVTransportURI',
                  [('CurrentURI', 'x-rincon:%s' % coordinator), ('CurrentURIMetaData', '')])
        self.backend.topology_at = 0   # read the new groups now rather than wait for the event

    def unjoin(self):
        self.call(self.speaker, 'AVTransport', 'BecomeCoordinatorOfStandaloneGroup')
        self.backend.topology_at = 0
