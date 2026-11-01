"""Home Assistant backend for a virtual ZonePlayer: one media_player entity per room.

The CR100 talks UPnP to the virtual player; this module answers transport, volume
and now-playing from the entity's state and turns the CR100's actions into Home
Assistant service calls. Everything is presented as a plain queue track so the
CR100's now-playing screen, progress bar and next/previous behave the same for
Spotify Connect, AirPlay, radio or the Sonos queue.

Album art: the entity's picture is fetched from Home Assistant, shrunk to a
baseline JPEG the CR100's libjpeg decodes, and served by the player itself under
a relative /art/ path (the CR100 resolves relative art URIs against the player).
"""
from __future__ import annotations

import collections
import datetime
import hashlib
import html
import io
import threading
import time

ART_SIZE = 200                 # px, longest side; the CR100 screen is 240 x 320
ART_CACHE = 48
ART_SOURCES = 2048             # picture keys remembered: browse lists name many, fetched as shown
VOLUME_HOLD = 2.0              # s the CR100's own volume/mute wins over Home Assistant's reports
TRACK_URI = 'http://bridge.invalid/track.mp3'   # neutral: the CR100 needs no real stream URI

# media_player supported_features bits
PAUSE, SEEK, VOLUME_SET, VOLUME_MUTE, PREVIOUS, NEXT, PLAY_MEDIA = 1, 2, 4, 8, 16, 32, 512
STOP, CLEAR_PLAYLIST, PLAY, SHUFFLE_SET, REPEAT_SET, GROUPING = 4096, 8192, 16384, 32768, 262144, 524288

TRANSPORT = {'playing': 'PLAYING', 'paused': 'PAUSED_PLAYBACK', 'buffering': 'TRANSITIONING'}
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
    """entity_picture paths <-> short /art/ keys; fetched, shrunk and cached on demand."""

    def __init__(self, ha, log=print, size=ART_SIZE):
        self.ha, self.log, self.size = ha, log, size
        self.sources = {}                          # key -> HA picture path
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
            image = self.shrink(self.ha.fetch(source))
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


class HARoom:
    """One virtual room backed by one Home Assistant media_player entity."""

    def __init__(self, ha, entity_id, art, log=print, content=None):
        self.ha, self.entity_id, self.art, self.log = ha, entity_id, art, log
        self.content = content    # ha_content.Content: what the CR100's play actions refer to
        self.lock = threading.Lock()
        self.held = {}            # 'volume'/'mute' -> (value, monotonic time set by the CR100)
        self.sending = set()      # attributes with a sender thread running
        self.queue_updates, self.seen_queue_size = 0, None

    # --- state ------------------------------------------------------------------
    @property
    def state(self):
        return self.ha.state(self.entity_id)

    @property
    def attributes(self):
        return self.state.get('attributes') or {}

    @property
    def available(self):
        return self.state.get('state') not in (None, 'unavailable', 'unknown')

    def features(self):
        return int(self.attributes.get('supported_features') or 0)

    def held_value(self, name, current):
        with self.lock:
            value, since = self.held.get(name, (None, 0))
            if name in self.sending or time.monotonic() - since < VOLUME_HOLD: return value
        return current

    @property
    def volume(self):
        level = self.attributes.get('volume_level')
        return self.held_value('volume', int(round(level * 100)) if level is not None else 0)

    @property
    def mute(self):
        return int(bool(self.held_value('mute', bool(self.attributes.get('is_volume_muted')))))

    @property
    def transport(self):
        return TRANSPORT.get(self.state.get('state'), 'STOPPED')

    @property
    def play_mode(self):
        attributes = self.attributes
        return PLAY_MODES.get((bool(attributes.get('shuffle')), attributes.get('repeat') or 'off'), 'NORMAL')

    def group_members(self):
        return list(self.attributes.get('group_members') or [self.entity_id])

    def position(self):
        attributes = self.attributes
        position = attributes.get('media_position')
        if position is None: return 0
        updated = attributes.get('media_position_updated_at')
        if self.state.get('state') == 'playing' and updated:
            try:
                then = datetime.datetime.fromisoformat(str(updated).replace('Z', '+00:00'))
                position += (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
            except ValueError:
                pass
        duration = attributes.get('media_duration')
        return min(position, duration) if duration else position

    def track(self):
        """(title, artist, album, art path, duration) for the current item, or None."""
        attributes = self.attributes
        if not self.available or self.state.get('state') in ('off', 'idle', 'standby'): return None
        channel = attributes.get('media_channel') or ''
        source = attributes.get('source') or attributes.get('app_name') or ''
        title = attributes.get('media_title') or channel or source
        if not title: return None
        artist = attributes.get('media_artist') or attributes.get('media_album_artist') or \
            (channel if channel != title else '')
        return (title, artist, attributes.get('media_album_name') or '',
                self.art.path_for(attributes.get('entity_picture')), attributes.get('media_duration'))

    def queue_numbers(self):
        attributes = self.attributes
        if self.track() is None: return 0, 0
        position, size = attributes.get('queue_position'), attributes.get('queue_size')
        if position and size: return int(position), int(size)
        return 1, 1

    def queue_version(self):
        """Grows whenever the queue may have changed, so the CR100 browses Q:0 again
        (ContainerUpdateIDs and the Queue service's UpdateID carry it)."""
        size = self.attributes.get('queue_size')
        with self.lock:
            changed = size != self.seen_queue_size
            if changed: self.seen_queue_size = size; self.queue_updates += 1
            version = self.queue_updates
        if changed and self.content: self.content.queue_changed(self.entity_id)
        return version

    def queue_edited(self):
        with self.lock: self.queue_updates += 1
        if self.content: self.content.queue_changed(self.entity_id)

    def transport_actions(self):
        features = self.features()
        actions = ['Play', 'Stop', 'Pause']
        if features & SEEK: actions.append('Seek')
        if features & NEXT: actions.append('Next')
        if features & PREVIOUS: actions.append('Previous')
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

    # --- actions ----------------------------------------------------------------
    def ha_call(self, service, data=None, feature=None):
        if feature and not self.features() & feature: raise BackendError('%s not supported' % service)
        try:
            self.ha.call_service('media_player', service, self.entity_id, data)
        except Exception as error:
            self.log({'event': 'ha-call-failed', 'entity': self.entity_id, 'service': service, 'error': str(error)})
            raise BackendError(str(error)) from error

    def hold(self, name, value, service, data):
        """Answer at once and send in the background; only the latest value is sent."""
        with self.lock:
            self.held[name] = (value, time.monotonic())
            if name in self.sending: return
            self.sending.add(name)
        threading.Thread(target=self.send_held, args=(name, service, data), daemon=True).start()

    def send_held(self, name, service, data):
        sent = None
        try:
            while True:
                with self.lock:
                    value = self.held[name][0]
                    if value == sent:
                        self.sending.discard(name)
                        self.held[name] = (value, time.monotonic()); return
                try:
                    self.ha_call(service, data(value))
                except BackendError:
                    pass
                sent = value
        except BaseException:
            with self.lock: self.sending.discard(name)
            raise

    def set_volume(self, value):
        self.hold('volume', max(0, min(100, int(value))), 'volume_set', lambda v: {'volume_level': v / 100})

    def set_mute(self, value):
        self.hold('mute', bool(value), 'volume_mute', lambda v: {'is_volume_muted': v})

    # --- content ------------------------------------------------------------------
    def sonos_call(self, service, data=None, response=False):
        try:
            return self.ha.call_service('sonos', service, self.entity_id, data, response=response)
        except Exception as error:
            self.log({'event': 'ha-call-failed', 'entity': self.entity_id, 'service': 'sonos.' + service,
                      'error': str(error)})
            raise BackendError(str(error)) from error

    def queue_length(self):
        response = self.sonos_call('get_queue', response=True) or {}
        return len(response.get(self.entity_id) or next(iter(response.values()), None) or [])

    def item(self, uri, metadata):
        node = self.content.resolve(uri, metadata) if self.content else None
        if node is None or node.ha_id is None: raise BackendError('Unknown item: %s' % uri)
        return node

    def play_media(self, node, enqueue=None):
        self.play_uri(node.ha_id, node.ha_type, enqueue)

    def play_uri(self, uri, media_type, enqueue=None):
        """Hand a resolved provider URI to Sonos; Sonos owns the audio and queue."""
        data = {'media_content_id': uri, 'media_content_type': media_type}
        if enqueue: data['enqueue'] = enqueue
        self.ha_call('play_media', data, feature=PLAY_MEDIA)

    def enqueue_uri(self, uri, media_type, as_next=False):
        """Append or Play Next in the native Sonos queue without starting playback."""
        before = self.queue_length()
        current = int(self.attributes.get('queue_position') or 0)
        self.play_uri(uri, media_type, 'next' if as_next else 'add')
        self.queue_edited()
        after = self.queue_length()
        # Sonos expands albums/playlists, so use the actual resulting queue length.
        return {'FirstTrackNumberEnqueued': str(current + 1 if as_next and current else before + 1),
                'NumTracksAdded': str(max(0, after - before)), 'NewQueueLength': str(after)}

    def enqueue(self, args):
        """AddURIToQueue: Home Assistant adds it (after the current track for 'as next')."""
        node = self.item(args['EnqueuedURI'], args.get('EnqueuedURIMetaData', ''))
        as_next = args.get('EnqueueAsNext') in ('1', 'true')
        before, current = self.queue_length(), int(self.attributes.get('queue_position') or 0)
        if node.ha_type == 'favorite_item_id':   # Home Assistant always plays these at once
            self.play_media(node); first = 1
        else:
            self.play_media(node, 'next' if as_next else 'add')
            first = current + 1 if as_next and current else before + 1
        self.queue_edited()
        after = self.queue_length()
        return {'FirstTrackNumberEnqueued': str(first), 'NumTracksAdded': str(max(0, after - before) or 1),
                'NewQueueLength': str(after)}

    def action(self, service, action, args):
        """Carry out a CR100 action. Returns None when not handled here, else a dict of
        results beyond values() (usually empty)."""
        if service == 'AVTransport':
            if action == 'AddURIToQueue': return self.enqueue(args)
            if action == 'SetAVTransportURI':
                uri = args.get('CurrentURI', '')
                # Its own queue: what the Sonos plays already; a station or favourite: play it.
                if uri.startswith('x-rincon-queue:'): return {}
                self.play_media(self.item(uri, args.get('CurrentURIMetaData', '')))
                return {}
            if action == 'Seek' and args.get('Unit') == 'TRACK_NR':
                self.sonos_call('play_queue', {'queue_position': max(0, int(args['Target']) - 1)})
                return {}
            if action == 'RemoveTrackFromQueue':
                number = args.get('ObjectID', '').rpartition('/')[2]
                if not number.isdigit(): raise BackendError('Not a queue track: %s' % args.get('ObjectID'))
                self.sonos_call('remove_from_queue', {'queue_position': int(number) - 1})
                self.queue_edited()
                return {}
            if action == 'Play': self.ha_call('media_play')
            elif action == 'Pause': self.ha_call('media_pause')
            elif action == 'Stop': self.ha_call('media_stop' if self.features() & STOP else 'media_pause')
            elif action == 'Next': self.ha_call('media_next_track', feature=NEXT)
            elif action == 'Previous': self.ha_call('media_previous_track', feature=PREVIOUS)
            elif action == 'Seek':
                if args.get('Unit') not in ('REL_TIME', 'TIME_DELTA'): raise BackendError('Unsupported seek unit')
                self.ha_call('media_seek', {'seek_position': parse_hms(args['Target'])}, feature=SEEK)
            elif action == 'SetPlayMode':
                shuffle, repeat = MODE_SETTINGS.get(args['NewPlayMode'], (False, 'off'))
                if self.features() & SHUFFLE_SET: self.ha_call('shuffle_set', {'shuffle': shuffle})
                if self.features() & REPEAT_SET: self.ha_call('repeat_set', {'repeat': repeat})
            elif action == 'RemoveAllTracksFromQueue':
                self.ha_call('clear_playlist', feature=CLEAR_PLAYLIST); self.queue_edited()
            else: return None
            return {}
        if service == 'RenderingControl':
            if action in ('SetVolume', 'RampToVolume'): self.set_volume(int(args['DesiredVolume']))
            elif action == 'SetRelativeVolume': self.set_volume(self.volume + int(args['Adjustment']))
            elif action == 'SetMute': self.set_mute(args['DesiredMute'] in ('1', 'true', 'yes'))
            else: return None
            return {}
        return None
