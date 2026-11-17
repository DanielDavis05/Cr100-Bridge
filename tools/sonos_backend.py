"""Sonos backend for virtual ZonePlayers: one real Sonos player per room, over its local UPnP.

Rooms are player UUIDs (RINCON_...). The backend reads the household's topology from any
of its players, subscribes to every visible player's AVTransport, RenderingControl and
ContentDirectory events and to the topology, and reads state itself: right after each
action (events come seconds late; see sonos_upnp.py), every RESYNC_INTERVAL, and every
POLL_INTERVAL while a player's events are not arriving. Transport goes to the room's group
coordinator; volume to the room itself. The Music menu is the players' own
ContentDirectory (`sonos_content.py`), and album art comes from the players.

Music services: Sonos must have the account linked. Provider playback adapters
prepare native queue URIs and metadata; the room executes the prepared plan.

Config: {"sonos": {"hosts": ["192.168.1.50"], "event_port": 0}}, rooms {"backend": "sonos",
"id": "RINCON_000E5800000A01400"}. Without hosts the players are found by SSDP.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

import sonos_upnp
import sonos_settings
from backends import MODE_SETTINGS, PLAY_MODES, ArtStore, Backend, BackendError, NativeSettingsRoom, hms, parse_hms, track_revision
from content import PLAIN_SCHEMES
from provider_playback import default_sonos_playback
from room_capabilities import RoomBinding
from sonos_content import SonosContent
from sonos_identity import IdentityInventory, MAX_REQUESTS, NETWORK_TIMEOUT, physical_uuid
from sonos_queue_art import _device_base
from sonos_upnp import UPnPFault, didl_entry, parse_didl, soap

STARTUP_WAIT = 10        # s to wait for the first topology before naming rooms
RESYNC_INTERVAL = 30     # s between full reads of every player, events or not
POLL_INTERVAL = 3        # s between reads of a player whose events are not arriving
# Slow S1 reads can fail transiently; withdraw only after both limits are met.
READ_FAILURE_LIMIT = 3
READ_FAILURE_GRACE = 20
TOPOLOGY_INTERVAL = 60   # s between topology reads (also on its events)
RETRY_TOPOLOGY = 5       # s between topology attempts until one succeeds
POSITION_TTL = 5         # s a read position is interpolated before it is read again
REFRESH_AFTER = (0.3, 1.5, 4.0)   # s after an action that its player is read again
# S1 ZP80 playback getters can take five seconds even while the player is
# reachable. A shorter timeout withdraws its room and repeatedly reloads players.
READ_TIMEOUT = 8
ART_LIMIT = 8 * 1024 * 1024
QUEUE_PREFIX = 'x-rincon-queue:'
LINE_IN_PREFIX = 'x-rincon-stream:'
TV_PREFIX = 'x-sonos-htastream:'   # a home-theater player's TV input: x-sonos-htastream:RINCON_...:spdif
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
        self.available = None                       # cached reachability; About metadata is independent
        self.failures, self.failed_since = 0, None
        self.subscriptions = []
        self.audio_in = {'connected': False, 'name': 'Line-In', 'icon': ''}
        self.audio_subscription = None
        self.audio_checked = False

    @property
    def healthy(self):
        return bool(self.subscriptions) and all(s.healthy for s in self.subscriptions)


class SonosBackend(Backend):
    """One Sonos household, reached through hosts (addresses or base URLs) or discovery."""
    name = 'sonos'

    def __init__(self, hosts=(), log=print, event_port=0, listener=None, identity_inventory=None,
                 settings=None, playback_adapters=None):
        super().__init__(log)
        self.hosts = list(hosts)
        self.lock = threading.RLock()
        self.speakers = {}                     # uuid -> Speaker
        self.groups = []                       # [(coordinator, [members])]
        self.topology_base, self.topology_at = None, 0.0
        self.topology_subscription = None
        self.line_in_snapshot = ()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.listener = listener or sonos_upnp.EventListener(log, port=event_port)
        self.art = ArtStore(self, log)
        self.content = SonosContent(self, self.art, log)
        self.identity_inventory = identity_inventory or IdentityInventory(log)
        self.identity_thread = None
        self.settings = settings if settings is not None else sonos_settings.SettingsProvider(
            self.settings_binding, self.settings_changed, coordinator_reader=self.settings_coordinator, log=log)
        self.playback_adapters = playback_adapters if playback_adapters is not None else default_sonos_playback()

    # --- lifecycle --------------------------------------------------------------------
    def start(self):
        self.settings.start()
        self.listener.start()
        threading.Thread(target=self.run, name='sonos-reads', daemon=True).start()
        self.identity_thread = threading.Thread(target=self.run_identities, name='sonos-identities', daemon=True)
        self.identity_thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        self.settings.close()
        self.listener.close()
        if self.identity_thread: self.identity_thread.join(NETWORK_TIMEOUT * MAX_REQUESTS + 1)

    def run_identities(self):
        # A household with many slow/offline products must not delay playback reads.
        while not self.stop.is_set():
            try:
                with self.lock:
                    seeds = {uuid: {'uuid': uuid, 'base': speaker.base, 'room_name': speaker.name}
                             for uuid, speaker in self.speakers.items()}
                changed = self.identity_inventory.refresh(seeds)
                if self.stop.is_set(): return
                for uuid in changed: self.changed(uuid)
            except Exception:
                self.log({'event': 'sonos-identities-unavailable'})
            self.stop.wait(1)

    def run(self):
        """The one thread that reads players, so a slow player never runs two reads at once."""
        while not self.stop.is_set():
            try:
                with self.lock:
                    healthy = self.speakers and all(s.available is not False for s in self.speakers.values())
                if time.monotonic() - self.topology_at > (TOPOLOGY_INTERVAL if healthy else RETRY_TOPOLOGY):
                    self.read_topology()
                for speaker in self.due():
                    if self.stop.is_set(): return
                    self.read(speaker)
                self.publish_line_in_changes()
            except Exception as error:
                self.log({'event': 'sonos-read-loop-error', 'error': repr(error)})
            self.wake.wait(0.1); self.wake.clear()

    def due(self):
        now = time.monotonic()
        with self.lock:
            found = []
            for speaker in self.speakers.values():
                requested = [t for t in speaker.refresh_at if t <= now]
                interval = RESYNC_INTERVAL if speaker.healthy and speaker.available is not False and not speaker.failures else POLL_INTERVAL
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
                                                       READ_TIMEOUT, discover_fallback=True)
        except UPnPFault as error:
            self.log({'event': 'sonos-topology-failed', 'error': str(error)})
            return
        self.apply_topology(groups, zones)
        with self.lock:
            for speaker in self.speakers.values():
                if speaker.base == base: speaker.failures, speaker.failed_since = 0, None
        if base != self.topology_base or self.topology_subscription is None:
            if self.topology_subscription: self.listener.unsubscribe(self.topology_subscription)
            self.topology_base = base
            self.topology_subscription = self.listener.subscribe(base, 'ZoneGroupTopology', self.on_topology)
        self.ready.set()

    def selection_snapshot(self):
        """Reachable rooms and newly discovered rooms still awaiting their first read."""
        with self.lock:
            available = {'sonos:' + s.uuid: s.name for s in self.speakers.values() if s.available is True}
            pending = {'sonos:' + s.uuid: s.name for s in self.speakers.values() if s.available is None}
        return available, pending

    def on_topology(self, values):
        with self.lock:
            for speaker in self.speakers.values():
                if speaker.base == self.topology_base: speaker.failures, speaker.failed_since = 0, None
        if 'ZoneGroupState' not in values: return   # some S2 events leave it out: read it then
        if not values['ZoneGroupState']:
            self.topology_at = 0; self.wake.set(); return
        self.apply_topology(*sonos_upnp.parse_topology(values['ZoneGroupState']))

    def apply_topology(self, groups, zones):
        added = []
        removed = []
        with self.lock:
            changed = groups != self.groups
            self.groups = groups
            for uuid in set(self.speakers) - set(zones):
                removed.append(self.speakers.pop(uuid))
                changed = True
            for uuid, zone in zones.items():
                speaker = self.speakers.get(uuid)
                if speaker is None:
                    speaker = self.speakers[uuid] = Speaker(uuid, zone['base'], zone['name'])
                    added.append(speaker)
                elif (speaker.base, speaker.name) != (zone['base'], zone['name']):
                    if speaker.base != zone['base']: added.append(speaker)   # moved: subscribe there
                    speaker.base, speaker.name = zone['base'], zone['name']; changed = True
        for speaker in removed:
            for subscription in speaker.subscriptions: self.listener.unsubscribe(subscription)
            if speaker.audio_subscription: self.listener.unsubscribe(speaker.audio_subscription)
            self.changed(speaker.uuid)
        for speaker in added: self.watch(speaker)
        if changed:
            self.log({'event': 'sonos-groups', 'groups': [[self.zone_name(u) for u in members] for _, members in groups]})
            for uuid in list(self.speakers): self.changed(uuid)

    def watch(self, speaker):
        for subscription in speaker.subscriptions: self.listener.unsubscribe(subscription)
        if speaker.audio_subscription:
            self.listener.unsubscribe(speaker.audio_subscription)
            speaker.audio_subscription = None
        speaker.audio_checked = False
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
        self.read_audio_in(speaker)
        try:
            transport = soap(base, 'AVTransport', 'GetTransportInfo', [('InstanceID', 0)], READ_TIMEOUT)
            position = soap(base, 'AVTransport', 'GetPositionInfo', [('InstanceID', 0)], READ_TIMEOUT)
            media = soap(base, 'AVTransport', 'GetMediaInfo', [('InstanceID', 0)], READ_TIMEOUT)
            settings = soap(base, 'AVTransport', 'GetTransportSettings', [('InstanceID', 0)], READ_TIMEOUT)
            actions = soap(base, 'AVTransport', 'GetCurrentTransportActions', [('InstanceID', 0)], READ_TIMEOUT)
            volume = soap(base, 'RenderingControl', 'GetVolume', [('InstanceID', 0), ('Channel', 'Master')], READ_TIMEOUT)
            mute = soap(base, 'RenderingControl', 'GetMute', [('InstanceID', 0), ('Channel', 'Master')], READ_TIMEOUT)
        except UPnPFault as error:
            with self.lock:
                was_available = speaker.available
                speaker.read_at = time.monotonic()
                speaker.failures += 1
                if speaker.failed_since is None: speaker.failed_since = speaker.read_at
                if speaker.failures >= READ_FAILURE_LIMIT and speaker.read_at - speaker.failed_since >= READ_FAILURE_GRACE:
                    speaker.available = False
            self.log({'event': 'sonos-read-failed', 'speaker': speaker.name, 'error': str(error)})
            if was_available is not speaker.available: self.transport_changed(speaker.uuid)
            return
        avt = {'TransportState': transport.get('CurrentTransportState', ''),
               'CurrentTrack': position.get('Track', ''), 'CurrentTrackDuration': position.get('TrackDuration', ''),
               'CurrentTrackMetaData': position.get('TrackMetaData', ''), 'CurrentTrackURI': position.get('TrackURI', ''),
               'NumberOfTracks': media.get('NrTracks', ''), 'AVTransportURI': media.get('CurrentURI', ''),
               'AVTransportURIMetaData': media.get('CurrentURIMetaData', ''),
               'CurrentPlayMode': settings.get('PlayMode', ''), 'CurrentTransportActions': actions.get('Actions', '')}
        with self.lock:
            was_available = speaker.available
            speaker.available = True
            speaker.failures, speaker.failed_since = 0, None
            speaker.read_at = time.monotonic()
            changed = any(speaker.avt.get(k) != v for k, v in avt.items())
            speaker.avt.update(avt)
            speaker.position, speaker.position_at = seconds(position.get('RelTime')), time.monotonic()
            level, muted = number(volume.get('CurrentVolume'), None), mute.get('CurrentMute') == '1'
            changed_rendering = (level, muted) != (speaker.volume, speaker.mute)
            speaker.volume, speaker.mute = level, muted
        if changed or was_available is not True: self.transport_changed(speaker.uuid)
        elif changed_rendering: self.changed(speaker.uuid)

    def read_audio_in(self, speaker):
        """Probe input support once, then keep its connected/name state through GENA.

        AudioIn is absent on speakers without an input. Its subscription is separate
        from transport health so an unsupported input cannot force fast AVT polling.
        Network failures are retried on the next read; a genuine UPnP unsupported
        action is remembered until the player changes address.
        """
        if speaker.audio_checked: return
        try:
            attributes = sonos_upnp.read_line_in(speaker.base, READ_TIMEOUT)
        except UPnPFault as error:
            # Unsupported service/action: do not subscribe forever to a non-input player.
            if error.code in (401, 501) or 'HTTP 404' in str(error): speaker.audio_checked = True
            return
        with self.lock:
            speaker.audio_in.update(attributes)
            speaker.audio_checked = True
            speaker.failures, speaker.failed_since = 0, None
        speaker.audio_subscription = self.listener.subscribe(
            speaker.base, 'AudioIn', lambda values: self.on_audio_in(speaker.uuid, values))

    def on_audio_in(self, uuid, values):
        speaker = self.speaker(uuid)
        if speaker is None: return
        with self.lock: speaker.failures, speaker.failed_since = 0, None
        cached = self.settings.values(uuid)
        if any(name in values and str(values[name]) != str(cached.get(name))
               for name in ('AudioInputName', 'Icon', 'LeftLineInLevel', 'RightLineInLevel')):
            self.settings.request_refresh(uuid)
        update = {}
        if 'LineInConnected' in values: update['connected'] = values['LineInConnected'].lower() in ('1', 'true', 'yes')
        if 'AudioInputName' in values: update['name'] = values['AudioInputName'] or 'Line-In'
        if 'Icon' in values: update['icon'] = values['Icon']
        if not update: return
        with self.lock:
            changed = any(speaker.audio_in.get(k) != v for k, v in update.items())
            speaker.audio_in.update(update)
        if changed:
            # Source attributes are also shown on other rooms playing this input.
            for member in list(self.speakers): self.changed(member)

    def read_position(self, speaker):
        try:
            position = soap(speaker.base, 'AVTransport', 'GetPositionInfo', [('InstanceID', 0)], READ_TIMEOUT)
        except UPnPFault:
            return
        track = {target: position[source] for source, target in (
            ('Track', 'CurrentTrack'), ('TrackDuration', 'CurrentTrackDuration'),
            ('TrackMetaData', 'CurrentTrackMetaData'), ('TrackURI', 'CurrentTrackURI'))
            if source in position}
        with self.lock:
            changed = any(speaker.avt.get(key) != value for key, value in track.items())
            speaker.failures, speaker.failed_since = 0, None
            speaker.avt.update(track)
            speaker.position, speaker.position_at = seconds(position.get('RelTime')), time.monotonic()
        # GetPositionInfo is also our recovery path when Connect changes songs
        # without a complete event. Publish only track changes, after recording
        # position_at so a listener reading position cannot start another read.
        if changed: self.transport_changed(speaker.uuid)

    def transport_changed(self, uuid):
        """A coordinator's transport is every member's."""
        for member in self.members_of(uuid): self.changed(member)

    def on_avt(self, uuid, values):
        update = sonos_upnp.last_change(values.get('LastChange'))
        speaker = self.speaker(uuid)
        if not speaker or not update: return
        with self.lock:
            transitioned = any(key in update and speaker.avt.get(key) != update[key]
                               for key in ('AVTransportURI', 'CurrentTrackURI', 'CurrentTrack', 'TransportState'))
            incomplete = not parse_didl(update.get('CurrentTrackMetaData'))
            speaker.available = True
            speaker.failures, speaker.failed_since = 0, None
            speaker.avt.update({k: v for k, v in update.items() if not k.startswith('Next')})
            if 'TransportState' in update or 'CurrentTrackURI' in update: speaker.position_at = 0   # read again
            if transitioned and incomplete:
                # A source/track-only event can leave the previous song's
                # metadata cached. Coalesce a burst into one prompt state read.
                due = time.monotonic() + REFRESH_AFTER[0]
                if not any(deadline <= due for deadline in speaker.refresh_at):
                    speaker.refresh_at.append(due)
        if transitioned and incomplete: self.wake.set()
        self.transport_changed(uuid)

    def on_rendering(self, uuid, values):
        speaker = self.speaker(uuid)
        raw = values.get('LastChange')
        if not speaker or not raw: return
        try:
            update = sonos_upnp.last_change(raw)
            document = ET.fromstring(raw)
        except ET.ParseError:
            return
        with self.lock: speaker.failures, speaker.failed_since = 0, None
        cached = self.settings.values(uuid)
        needs_refresh = any(name in update and update[name] != cached.get(name)
                            for name in ('Bass', 'Treble', 'Loudness', 'OutputFixed'))
        # last_change deliberately returns only Master volume/mute. Balance
        # changes arrive as LF/RF events and need their own native readback.
        for instance in document:
            if instance.get('val') != '0': continue
            for variable in instance:
                name, channel = sonos_upnp.local(variable.tag), variable.get('channel')
                if name in ('Volume', 'Mute') and channel in ('LF', 'RF'):
                    if variable.get('val', '') != cached.get(name + 'Channels', {}).get(channel):
                        needs_refresh = True
        if needs_refresh:
            self.settings.request_refresh(uuid, output_fixed=update.get('OutputFixed'))
        if not ('Volume' in update or 'Mute' in update): return
        with self.lock:
            was_available = speaker.available
            speaker.available = True
            if 'Volume' in update: speaker.volume = number(update['Volume'], speaker.volume)
            if 'Mute' in update: speaker.mute = update['Mute'] == '1'
        if was_available is False: self.transport_changed(uuid)
        else: self.changed(uuid)

    def on_content(self, uuid, values):
        updates = sonos_upnp.container_updates(values.get('ContainerUpdateIDs'))
        speaker = self.speaker(uuid)
        if speaker:
            with self.lock: speaker.failures, speaker.failed_since = 0, None
        if speaker and 'Q:0' in updates:
            with self.lock: speaker.queue_update = updates['Q:0']
            self.content.queue_changed(uuid)
            self.transport_changed(uuid)

    # --- Backend --------------------------------------------------------------------------
    def room(self, uuid):
        self.settings.register(uuid)
        return SonosRoom(self, uuid, self.log)

    def settings_binding(self, uuid):
        """Verified cached route only while this room's playback domain is available."""
        speaker = self.speaker(uuid)
        coordinator = self.speaker(self.coordinator_of(uuid)) or speaker
        if not speaker or not coordinator or speaker.available is False or coordinator.available is False:
            return RoomBinding()
        return self.binding(uuid)

    def settings_coordinator(self, uuid):
        # A timer belongs to a known real coordinator, never an assumed member.
        if not self.settings_binding(uuid).native_sonos: return None
        with self.lock:
            coordinator = next((key for key, members in self.groups if uuid in members), None)
        return coordinator if coordinator and self.settings_binding(coordinator).native_sonos else None

    def settings_changed(self, uuid):
        # Native input edits must refresh the existing source menu's cache too.
        values = self.settings.values(uuid)
        update = {target: values[source] for source, target in
                  (('AudioInputName', 'name'), ('Icon', 'icon')) if source in values}
        with self.lock:
            speaker = self.speakers.get(uuid)
            if speaker and update: speaker.audio_in.update(update)
            keys = list(self.speakers) if update else [uuid]
        for key in keys: self.changed(key)

    def display_name(self, uuid):
        speaker = self.speaker(uuid)
        return speaker.name if speaker else None

    def binding(self, uuid):
        """Cached control binding, never inferred from descriptive About fields.

        Discovery selects one topology for this backend. Only actual playback
        members in that topology can share its grouping key; cached bonded
        identities do not acquire playback or grouping capabilities.
        """
        if not physical_uuid(uuid): return RoomBinding()
        with self.lock:
            speaker = self.speakers.get(uuid)
            base = _device_base(speaker.base) if speaker else None
            # Fake/local fixtures use arbitrary loopback HTTP ports. Their
            # trusted topology is the source just as for production speakers.
            if speaker and not base:
                try:
                    parsed = urlsplit(speaker.base)
                    if (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1')
                            and parsed.port and not parsed.username and not parsed.password
                            and not parsed.path and not parsed.query and not parsed.fragment):
                        base = speaker.base
                except ValueError:
                    pass
            in_topology = speaker and any(uuid in members for _, members in self.groups)
            group = ('sonos', str(id(self))) if base and in_topology else None
            return RoomBinding(platform='sonos', device_id=uuid,
                               native_id=uuid if base else '', native_url=base or '', group_key=group)

    def identity(self, uuid):
        cached = self.identity_inventory.identity(uuid)
        if cached is not None:
            if not cached.get('model'): cached['model'] = 'Sonos (details unavailable)'
            return cached
        if not physical_uuid(uuid): return None
        speaker = self.speaker(uuid)
        return IdentityInventory.metadata({'id': uuid, 'model': 'Sonos (details unavailable)',
            'name': speaker.name if speaker else '',
            'ip': (urlsplit(speaker.base).hostname or '') if speaker else ''})

    def bonded_devices(self, uuid):
        return self.identity_inventory.bonded_devices(uuid)

    def line_in_sources(self):
        with self.lock:
            return [{'id': speaker.uuid, 'name': speaker.audio_in['name'], 'room_name': speaker.name,
                     'icon': speaker.audio_in['icon'], 'uri': LINE_IN_PREFIX + speaker.uuid}
                    for speaker in self.speakers.values() if speaker.audio_in['connected']
                    and speaker.audio_subscription and speaker.audio_subscription.healthy]

    def publish_line_in_changes(self):
        """Renewal/expiry can change availability even when no input event arrives."""
        snapshot = tuple(tuple(sorted(source.items())) for source in self.line_in_sources())
        if snapshot != self.line_in_snapshot:
            self.line_in_snapshot = snapshot
            for uuid in list(self.speakers): self.changed(uuid)

    def picture_url(self, base, art):
        """An absolute art URL from a player's (often relative /getaa?...) albumArtURI."""
        return urljoin(base + '/', art) if art else ''

    def fetch(self, picture):
        parsed = urlsplit(picture)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Album art URL must use HTTP or HTTPS and include a hostname')
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


class SonosRoom(NativeSettingsRoom):
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

    @property
    def available(self):
        speaker, coordinator = self.speaker, self.coordinator
        return bool(speaker and coordinator and speaker.available is not False
                    and coordinator.available is not False and self.backend.binding(self.key).native_sonos)

    def operation_capabilities(self):
        """Implemented routes and current-source controls, from cached state only."""
        if not self.available: return frozenset()
        operations = {'volume', 'mute', 'browse', 'play_item', 'radio_play', 'queue_browse', 'queue_play',
                      'queue_add', 'queue_use', 'queue_remove', 'queue_clear',
                      'provider_play', 'provider_enqueue'}
        if self.backend.binding(self.key).group_key: operations.add('group')
        if self.backend.line_in_sources(): operations.add('line_in')
        avt = self.avt
        actions = {value.strip().lower() for value in (avt.get('CurrentTransportActions') or '').split(',')}
        # S1 and S2 players advertise time seeking as the DLNA extension
        # (e.g. "Set, Stop, Pause, Play, X_DLNA_SeekTime, Next, X_DLNA_SeekTrackNr"),
        # never as a plain "Seek".
        if 'x_dlna_seektime' in actions: actions.add('seek')
        operations.update(actions & {'play', 'pause', 'stop'})
        # A paused/stopped source can still be resumed; these are supported
        # native actions even when a device omits them from an empty state.
        if not avt.get('AVTransportURI') or self.transport in ('STOPPED', 'PAUSED_PLAYBACK'):
            operations.add('play')
        if self.line_in_source():
            # The CR100's play/pause key needs Pause advertised to stop a live
            # input. pause() translates that action to the speaker's Stop;
            # resuming keeps the selected input and leaves the queue intact.
            operations.update(('play', 'stop', 'pause'))
        else:
            operations.update(actions & {'seek', 'next', 'previous'})
            if self.playing_queue() or not avt.get('AVTransportURI'):
                operations.update(('shuffle', 'repeat'))
        return self.with_settings_capabilities(operations)

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

    def line_in_source(self):
        avt = self.avt
        uri = avt.get('AVTransportURI') or avt.get('CurrentTrackURI', '')
        return uri[len(LINE_IN_PREFIX):].split('?', 1)[0] if uri.startswith(LINE_IN_PREFIX) else None

    def tv_source(self):
        """The home-theater player whose TV input is playing, or None.

        The player reports its own UUID as the track title, which must not
        reach the controller as if it were a song name.
        """
        avt = self.avt
        uri = avt.get('AVTransportURI') or avt.get('CurrentTrackURI', '')
        if not uri.startswith(TV_PREFIX): return None
        return uri[len(TV_PREFIX):].split(':', 1)[0] or None

    def track_reference(self):
        coordinator, avt = self.coordinator, self.avt
        return '%s:%s' % (coordinator.uuid if coordinator else self.key,
                          avt.get('CurrentTrackURI') or avt.get('AVTransportURI', ''))

    def track(self):
        avt, coordinator = self.avt, self.coordinator
        uri = avt.get('AVTransportURI', '')
        if not coordinator or not uri or avt.get('TransportState') in (None, '', 'NO_MEDIA_PRESENT'): return None
        source_id = self.line_in_source()
        if source_id:
            source = self.backend.speaker(source_id)
            title = source.audio_in['name'] if source else 'Line-In'
            return title, '', source.name if source else '', '', None
        tv_id = self.tv_source()
        if tv_id:
            source = self.backend.speaker(tv_id)
            return 'TV', '', source.name if source else '', '', None
        entry = next(iter(parse_didl(avt.get('CurrentTrackMetaData'))), {})
        context = next(iter(parse_didl(avt.get('AVTransportURIMetaData'))), {}) \
            if not self.playing_queue(avt) else {}
        station = context.get('title', '')
        stream = entry.get('stream_content', '')
        if IGNORED_STREAM.match(stream): stream = ''
        music_track = entry.get('class', '').startswith('object.item.audioItem.musicTrack')
        broadcast = context.get('class', '').startswith('object.item.audioItem.audioBroadcast')
        if entry.get('title') and music_track and not (stream and broadcast):
            # Spotify Connect exposes a non-queue x-sonos-vli transport whose
            # context title is only "Spotify". GetPositionInfo still supplies
            # a complete musicTrack; that song must win over the context label.
            title, artist, album = entry['title'], entry.get('artist', ''), entry.get('album', '')
        elif stream or (station and not self.playing_queue(avt)):
            artist, _, title = stream.partition(' - ') if ' - ' in stream else ('', '', stream)
            title, album = title or station or entry.get('title', ''), station if stream else ''
        else:
            title, artist, album = entry.get('title', ''), entry.get('artist', ''), entry.get('album', '')
        if not title: return None
        duration = seconds(avt.get('CurrentTrackDuration'))
        art = self.backend.picture_url(coordinator.base, entry.get('art', ''))
        revision = track_revision(self.track_reference(), title, artist, album, duration or None)
        return title, artist, album, self.backend.art.path_for(art, revision=revision), duration or None

    def position(self):
        if self.line_in_source() or self.tv_source(): return 0
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
        return self.operation_capabilities() & {'seek', 'next', 'previous'}

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
    def play_line_in(self, source_id):
        if not any(source['id'] == source_id for source in self.backend.line_in_sources()):
            raise BackendError('Line-In source is not connected: %s' % source_id)
        self.transport_call('SetAVTransportURI', [('CurrentURI', LINE_IN_PREFIX + source_id),
                                                ('CurrentURIMetaData', '')])
        self.play()
    def pause(self):
        if self.transport in ('STOPPED', 'PAUSED_PLAYBACK'): return   # the player refuses (701); nothing to do
        # A radio stream offers Stop but not Pause (CurrentTransportActions "Set, Stop, Play").
        actions = {a.strip() for a in self.avt.get('CurrentTransportActions', '').split(',')}
        self.transport_call('Stop' if self.line_in_source() or ('Stop' in actions and 'Pause' not in actions) else 'Pause')
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

    def play_radio(self, uri, metadata=''):
        self.transport_call('SetAVTransportURI', [('CurrentURI', uri), ('CurrentURIMetaData', metadata)])
        self.transport_call('Play', [('Speed', 1)])

    def radio_formats(self):
        return ('mp3', 'aac', 'wma')

    def enqueue_item(self, node, as_next=False):
        uri, metadata = self.item_uri(node)
        if self.direct(node, uri):   # a station plays at once, as with Home Assistant
            self.play_item(node)
            return {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1',
                    'NewQueueLength': self.avt.get('NumberOfTracks') or '1'}
        return self.add_to_queue(uri, metadata, as_next)

    def _enqueue_provider_plan(self, items, as_next=False):
        """Execute a validated plan, trying the next native encoding on failure."""
        error = None
        for item in items:
            try:
                return self.add_to_queue(item.uri, item.metadata, as_next)
            except BackendError as failure:
                error = failure
        raise error

    def share(self, uri, as_next=False):
        """Compatibility entry point for queueing a resolved provider share URI."""
        return self._enqueue_provider_plan(self.backend.playback_adapters.prepare(uri), as_next)

    def play_provider(self, uri, kind):
        items = self.backend.playback_adapters.prepare(uri, kind)
        self.clear_queue()
        self._enqueue_provider_plan(items)
        self.play_track(1)

    def enqueue_provider(self, uri, kind, as_next=False):
        return self._enqueue_provider_plan(self.backend.playback_adapters.prepare(uri, kind), as_next)

    def send_volume(self, value):
        self.require('volume')
        self.call(self.speaker, 'RenderingControl', 'SetVolume', [('Channel', 'Master'), ('DesiredVolume', value)])

    def send_mute(self, value):
        self.require('mute')
        self.call(self.speaker, 'RenderingControl', 'SetMute', [('Channel', 'Master'), ('DesiredMute', int(value))])

    def join(self, member):
        coordinator = self.backend.coordinator_of(self.key)
        self.call(member.speaker, 'AVTransport', 'SetAVTransportURI',
                  [('CurrentURI', 'x-rincon:%s' % coordinator), ('CurrentURIMetaData', '')])
        self.backend.topology_at = 0   # read the new groups now rather than wait for the event

    def unjoin(self):
        self.call(self.speaker, 'AVTransport', 'BecomeCoordinatorOfStandaloneGroup')
        self.backend.topology_at = 0
