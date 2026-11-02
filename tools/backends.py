"""Backends for virtual ZonePlayers: what plays behind each room, whatever it is.

A backend (Home Assistant today, see `ha_backend.py`) is one connection that provides
rooms and, optionally, the Music menu's content (`content.py`). A room reports its
player's state and carries out the CR100's actions as plain calls (play, seek, join, ...);
this module turns both into the CR100's UPnP, so every backend behaves the same on the
CR100's now-playing screen, progress bar, queue and volume.

Everything is presented as a plain queue track so the CR100 behaves the same for
Spotify Connect, AirPlay, radio or the Sonos queue.

Album art: a backend's pictures are fetched through it, shrunk to a baseline JPEG the
CR100's libjpeg decodes, and served by the player itself under a relative /art/ path
(the CR100 resolves relative art URIs against the player).

Config: a room names its backend and its id there ({"backend": "ha", "id": "media_player.den"});
{"entity": ...} is shorthand for a Home Assistant room. Each backend's settings live in its
own section (BACKENDS).
"""
from __future__ import annotations

import collections
import hashlib
import html
import importlib
import io
import threading
import time

ART_SIZE = 200                 # px, longest side; the CR100 screen is 240 x 320
ART_CACHE = 48
ART_SOURCES = 2048             # picture keys remembered: browse lists name many, fetched as shown
VOLUME_HOLD = 2.0              # s the CR100's own volume/mute wins over the backend's reports
TRACK_URI = 'http://bridge.invalid/track.mp3'   # neutral: the CR100 needs no real stream URI

# backend name -> (config section with its settings, module whose load() connects it)
BACKENDS = {'ha': ('home_assistant', 'ha_backend'), 'sonos': ('sonos', 'sonos_backend')}

PLAY_MODES = {(False, 'off'): 'NORMAL', (False, 'all'): 'REPEAT_ALL', (False, 'one'): 'REPEAT_ONE',
              (True, 'off'): 'SHUFFLE_NOREPEAT', (True, 'all'): 'SHUFFLE', (True, 'one'): 'SHUFFLE_REPEAT_ONE'}
MODE_SETTINGS = {mode: key for key, mode in PLAY_MODES.items()}


class BackendError(Exception):
    """The action could not be carried out; reported to the CR100 as UPnP error 501."""


def hms(seconds):
    total = int(max(0, seconds or 0))
    return '%d:%02d:%02d' % (total // 3600, total // 60 % 60, total % 60)


def parse_hms(text):
    parts = [float(p) for p in text.split(':')]
    if len(parts) != 3: raise ValueError('Expected H:MM:SS')
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def didl(title, artist='', album='', art='', duration=None):
    if not title: return ''
    fields = ['<dc:title>%s</dc:title>' % html.escape(title, quote=False),
              '<upnp:class>object.item.audioItem.musicTrack</upnp:class>']
    if artist: fields.append('<dc:creator>%s</dc:creator>' % html.escape(artist, quote=False))
    if album: fields.append('<upnp:album>%s</upnp:album>' % html.escape(album, quote=False))
    if art: fields.append('<upnp:albumArtURI>%s</upnp:albumArtURI>' % html.escape(art, quote=False))
    res = '<res protocolInfo="http-get:*:audio/mpeg:*"%s>%s</res>' % (
        ' duration="%s"' % hms(duration) if duration else '', TRACK_URI)
    return ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="-1" parentID="-1" restricted="true">' + res + ''.join(fields) + '</item></DIDL-Lite>')


class ArtStore:
    """A backend's picture references <-> short /art/ keys; fetched through the backend,
    shrunk and cached on demand."""

    def __init__(self, backend, log=print, size=ART_SIZE):
        self.backend, self.log, self.size = backend, log, size
        self.sources = {}                          # key -> picture reference
        self.images = collections.OrderedDict()    # key -> JPEG bytes (LRU)
        self.lock = threading.Lock()
        self.fetching = {}                         # key -> Event

    def path_for(self, picture, prefetch=True):
        if not picture: return ''
        key = hashlib.sha1(picture.encode()).hexdigest()[:16]
        with self.lock:
            new = key not in self.sources
            self.sources[key] = picture
            if len(self.sources) > ART_SOURCES:   # forget the oldest unreferenced sources
                for old in list(self.sources)[:len(self.sources) - ART_SOURCES]:
                    if old != key: self.sources.pop(old)
        if new and prefetch:  # warm the cache: the CR100 asks right after the track changes
            threading.Thread(target=self.get, args=(key,), daemon=True).start()
        return '/art/%s.jpg' % key

    def get(self, key):
        while True:
            with self.lock:
                if key in self.images:
                    self.images.move_to_end(key); return self.images[key]
                source = self.sources.get(key)
                if source is None: return None
                waiter = self.fetching.get(key)
                if waiter is None:
                    self.fetching[key] = threading.Event(); break
            if not waiter.wait(15): return None
        image = None
        try:
            image = self.shrink(self.backend.fetch(source))
        except Exception as error:
            self.log({'event': 'art-error', 'key': key, 'error': repr(error)})
        with self.lock:
            if image is not None:
                self.images[key] = image
                while len(self.images) > ART_CACHE: self.images.popitem(last=False)
            self.fetching.pop(key).set()
        return image

    def shrink(self, data):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as picture:
            picture = picture.convert('RGB')
            picture.thumbnail((self.size, self.size))
            out = io.BytesIO()
            # Baseline, no subsampling surprises, no EXIF: the CR100's 2002 libjpeg.
            picture.save(out, 'JPEG', quality=85, progressive=False, optimize=False)
        return out.getvalue()


class Backend:
    """One connection that plays rooms (one Home Assistant, one Sonos household, ...).

    Subclasses set name (its BACKENDS key) and, when they have them, art (an ArtStore) and
    content (a content.ContentSource for the Music menu), and call changed(room id) whenever
    a room's state may have changed."""
    name = None
    art = None
    content = None

    def __init__(self, log=print):
        self.log = log
        self.listeners = []       # fn(room id), on whatever thread noticed the change

    def room(self, room_id):
        """The Room for this backend's id (an entity, a speaker UUID, ...)."""
        raise NotImplementedError

    def display_name(self, room_id):
        """The room's own name, for config rooms without one; None when unknown."""
        return None

    def fetch(self, picture):
        """The bytes of a picture reference the backend handed to its ArtStore."""
        raise NotImplementedError

    def changed(self, room_id):
        for listener in list(self.listeners):
            try: listener(room_id)
            except Exception as error: self.log({'event': 'backend-listener-error', 'backend': self.name,
                                                 'error': repr(error)})

    def close(self):
        pass


class Room:
    """One virtual room backed by one real player.

    Subclasses report state (reported_volume, reported_mute, transport, shuffle_repeat,
    track, position, queue_numbers, queue_token, capabilities, group_members) and carry out
    the actions they support (play ... unjoin below; BackendError when one fails). This class
    answers the CR100: UPnP variables, action translation, and volume that follows the
    CR100's own changes at once."""

    def __init__(self, backend, key, log=print):
        self.backend, self.key, self.log = backend, key, log
        self.lock = threading.Lock()
        self.held = {}            # 'volume'/'mute' -> (value, monotonic time set by the CR100)
        self.sending = set()      # attributes with a sender thread running
        self.queue_updates, self.seen_queue = 0, None

    @property
    def ref(self):
        """(backend name, room id): how the household and the config name this room."""
        return self.backend.name, self.key

    @property
    def content(self):
        return self.backend.content

    # --- state, from the subclass -----------------------------------------------
    @property
    def reported_volume(self):
        """0-100, as the player last reported it."""
        raise NotImplementedError

    @property
    def reported_mute(self):
        raise NotImplementedError

    @property
    def transport(self):
        """PLAYING, PAUSED_PLAYBACK, TRANSITIONING or STOPPED."""
        raise NotImplementedError

    def shuffle_repeat(self):
        """(shuffle, 'off' | 'all' | 'one')."""
        return False, 'off'

    def track(self):
        """(title, artist, album, art path, duration) for the current item, or None."""
        raise NotImplementedError

    def position(self):
        """Seconds into the current item."""
        return 0

    def queue_numbers(self):
        """(current track, number of tracks), 1-based; (0, 0) with nothing to play."""
        return (1, 1) if self.track() is not None else (0, 0)

    def queue_token(self):
        """Any value that changes whenever the queue may have changed."""
        return None

    def capabilities(self):
        """Which of 'seek', 'next' and 'previous' the player can do now."""
        return frozenset()

    def group_members(self):
        """Room ids in this room's group, coordinator first."""
        return [self.key]

    # --- actions, by the subclass; what it leaves out is unsupported -----------------
    def unsupported(self, action):
        raise BackendError('%s not supported' % action)

    def play(self): self.unsupported('play')
    def pause(self): self.unsupported('pause')
    def stop(self): self.unsupported('stop')
    def next(self): self.unsupported('next')
    def previous(self): self.unsupported('previous')
    def seek(self, seconds): self.unsupported('seek')

    def play_track(self, number):
        """Play the queue from this track (1-based)."""
        self.unsupported('play_track')

    def remove_track(self, number):
        """Take this track (1-based) out of the queue."""
        self.unsupported('remove_track')

    def use_queue(self):
        """Make the queue what the player plays (the CR100 chose it); no-op where it always is."""

    def clear_queue(self): self.unsupported('clear_queue')
    def set_play_mode(self, shuffle, repeat): self.unsupported('set_play_mode')

    def play_item(self, node):
        """Play a content.Node the CR100 chose from the Music menu."""
        self.unsupported('play_item')

    def enqueue_item(self, node, as_next=False):
        """Add a content.Node to the queue; the AddURIToQueue results."""
        self.unsupported('enqueue_item')

    def play_provider(self, uri, kind):
        """Replace the queue with a music service's item (kind: music, album or playlist) and play."""
        self.unsupported('play_provider')

    def enqueue_provider(self, uri, kind, as_next=False):
        """Add a music service's item to the queue without starting playback; the
        AddURIToQueue results."""
        self.unsupported('enqueue_provider')

    def send_volume(self, value): self.unsupported('send_volume')
    def send_mute(self, value): self.unsupported('send_mute')

    def join(self, member):
        """Bring another Room of this backend into this room's group."""
        self.unsupported('join')

    def unjoin(self): self.unsupported('unjoin')

    # --- volume: the CR100's value wins until the player has caught up ---------------
    def held_value(self, name, current):
        with self.lock:
            value, since = self.held.get(name, (None, 0))
            if name in self.sending or time.monotonic() - since < VOLUME_HOLD: return value
        return current

    @property
    def volume(self):
        return self.held_value('volume', self.reported_volume)

    @property
    def mute(self):
        return int(bool(self.held_value('mute', self.reported_mute)))

    def hold(self, name, value, send):
        """Answer at once and send in the background; only the latest value is sent."""
        with self.lock:
            self.held[name] = (value, time.monotonic())
            if name in self.sending: return
            self.sending.add(name)
        threading.Thread(target=self.send_held, args=(name, send), daemon=True).start()

    def send_held(self, name, send):
        sent = None
        try:
            while True:
                with self.lock:
                    value = self.held[name][0]
                    if value == sent:
                        self.sending.discard(name)
                        self.held[name] = (value, time.monotonic()); return
                try:
                    send(value)
                except BackendError:
                    pass
                sent = value
        except BaseException:
            with self.lock: self.sending.discard(name)
            raise

    def set_volume(self, value):
        self.hold('volume', max(0, min(100, int(value))), self.send_volume)

    def set_mute(self, value):
        self.hold('mute', bool(value), self.send_mute)

    # --- UPnP ----------------------------------------------------------------------
    @property
    def play_mode(self):
        return PLAY_MODES.get(self.shuffle_repeat(), 'NORMAL')

    def queue_version(self):
        """Grows whenever the queue may have changed, so the CR100 browses Q:0 again
        (ContainerUpdateIDs and the Queue service's UpdateID carry it)."""
        token = self.queue_token()
        with self.lock:
            changed = token != self.seen_queue
            if changed: self.seen_queue = token; self.queue_updates += 1
            version = self.queue_updates
        if changed and self.content: self.content.queue_changed(self.key)
        return version

    def queue_edited(self):
        with self.lock: self.queue_updates += 1
        if self.content: self.content.queue_changed(self.key)

    def transport_actions(self):
        capabilities = self.capabilities()
        actions = ['Play', 'Stop', 'Pause']
        if 'seek' in capabilities: actions.append('Seek')
        if 'next' in capabilities: actions.append('Next')
        if 'previous' in capabilities: actions.append('Previous')
        return ','.join(actions)

    def values(self, udn):
        """Every UPnP variable this room overrides, by name (events and action results)."""
        track = self.track()
        metadata = didl(*track) if track else ''
        duration = hms(track[4]) if track else '0:00:00'
        current, total = self.queue_numbers()
        uri = 'x-rincon-queue:%s#0' % udn if track else ''
        return {
            'TransportState': self.transport, 'CurrentPlayMode': self.play_mode,
            'NumberOfTracks': str(total), 'CurrentTrack': str(current),
            'CurrentTrackDuration': duration, 'CurrentMediaDuration': '0:00:00',
            'AVTransportURI': uri, 'AVTransportURIMetaData': '',
            'CurrentTrackURI': TRACK_URI if track else '', 'CurrentTrackMetaData': metadata,
            'CurrentTransportActions': self.transport_actions(),
            # action results
            'CurrentTransportState': self.transport, 'NrTracks': str(total), 'MediaDuration': '0:00:00',
            'CurrentURI': uri, 'CurrentURIMetaData': '', 'Track': str(current), 'TrackDuration': duration,
            'TrackMetaData': metadata, 'TrackURI': TRACK_URI if track else '',
            'RelTime': hms(self.position()) if track else '0:00:00', 'AbsTime': 'NOT_IMPLEMENTED',
            'PlayMode': self.play_mode, 'Actions': self.transport_actions(),
            'CurrentVolume': str(self.volume), 'CurrentMute': str(self.mute),
        }

    def item(self, uri, metadata):
        node = self.content.resolve(uri, metadata) if self.content else None
        if node is None or node.ref is None: raise BackendError('Unknown item: %s' % uri)
        return node

    def action(self, service, action, args):
        """Carry out a CR100 action. Returns None when not handled here, else a dict of
        results beyond values() (usually empty)."""
        if service == 'AVTransport':
            if action == 'AddURIToQueue':
                node = self.item(args['EnqueuedURI'], args.get('EnqueuedURIMetaData', ''))
                return self.enqueue_item(node, args.get('EnqueueAsNext') in ('1', 'true'))
            if action == 'SetAVTransportURI':
                uri = args.get('CurrentURI', '')
                # Its own queue: what the player plays from now on; a station or favourite: play it.
                if uri.startswith('x-rincon-queue:'): self.use_queue(); return {}
                self.play_item(self.item(uri, args.get('CurrentURIMetaData', '')))
                return {}
            if action == 'Seek' and args.get('Unit') == 'TRACK_NR':
                self.play_track(int(args['Target']))
                return {}
            if action == 'RemoveTrackFromQueue':
                number = args.get('ObjectID', '').rpartition('/')[2]
                if not number.isdigit(): raise BackendError('Not a queue track: %s' % args.get('ObjectID'))
                self.remove_track(int(number))
                self.queue_edited()
                return {}
            if action == 'Play': self.play()
            elif action == 'Pause': self.pause()
            elif action == 'Stop': self.stop()
            elif action == 'Next': self.next()
            elif action == 'Previous': self.previous()
            elif action == 'Seek':
                if args.get('Unit') not in ('REL_TIME', 'TIME_DELTA'): raise BackendError('Unsupported seek unit')
                self.seek(parse_hms(args['Target']))
            elif action == 'SetPlayMode':
                self.set_play_mode(*MODE_SETTINGS.get(args['NewPlayMode'], (False, 'off')))
            elif action == 'RemoveAllTracksFromQueue':
                self.clear_queue(); self.queue_edited()
            else: return None
            return {}
        if service == 'RenderingControl':
            if action in ('SetVolume', 'RampToVolume'): self.set_volume(int(args['DesiredVolume']))
            elif action == 'SetRelativeVolume': self.set_volume(self.volume + int(args['Adjustment']))
            elif action == 'SetMute': self.set_mute(args['DesiredMute'] in ('1', 'true', 'yes'))
            else: return None
            return {}
        return None


# --- config ------------------------------------------------------------------------
def room_ref(entry):
    """(backend name, room id) for a config room or library entry, None for a simulated room."""
    if entry.get('backend'): return entry['backend'], entry['id']
    if entry.get('entity'): return 'ha', entry['entity']
    return None


def load(config, config_dir, names, log):
    """Connect the named backends: {name: Backend}. Each module's load(settings, config_dir,
    log) gets its own config section, {} when there is none (Sonos needs no settings);
    relative paths in it are relative to config_dir."""
    loaded = {}
    for name in names:
        if name not in BACKENDS: raise ValueError('Unknown backend: %s' % name)
        section, module = BACKENDS[name]
        loaded[name] = importlib.import_module(module).load(config.get(section) or {}, config_dir, log)
    return loaded
