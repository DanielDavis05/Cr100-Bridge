"""Direct Radio playback and generation-scoped native metadata/artwork.

Directory requests never run on the metadata/event read path. A room's broadcast
session is independent of its saved queue and belongs to its current coordinator.
"""
from __future__ import annotations

import dataclasses
import collections
import hashlib
import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from backends import ArtStore, BackendError, didl, hms, track_revision
from content import BROADCAST_CLASS, ContentSource, Node
from radiotime import RadioTimeError
from radio_transcode import RadioTranscodeError

RADIO_URI = 'x-rincon-mp3radio://bridge-radio/'
USER_AGENT = 'CR100Bridge/1.0 (Radio metadata and artwork)'


def item_value(item, key, default=''):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def item_data(item):
    data = dict(item_value(item, 'metadata', {}) or {})
    for key in ('title', 'artist', 'album', 'image', 'duration', 'kind', 'id'):
        value = item_value(item, key, None)
        if value is not None: data.setdefault(key, value)
    return data


def radio_uri(item_id):
    return RADIO_URI + urllib.parse.quote(item_id, safe='')


def radio_id(uri):
    uri = str(uri or '')
    if uri.startswith(RADIO_URI):
        return urllib.parse.unquote(uri[len(RADIO_URI):].partition('?')[0])
    if uri.startswith('x-cr100-radio:'): return urllib.parse.unquote(uri[len('x-cr100-radio:'):])
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme in ('x-sonosapi-stream', 'x-sonosapi-radio', 'x-sonos-http', 'x-sonosapi-show', 'x-sonosapi-rtrecent') \
            and urllib.parse.parse_qs(parsed.query).get('sid') == ['254']:
        identity = urllib.parse.unquote(parsed.path)
        if parsed.scheme == 'x-sonos-http': identity = identity.removesuffix('.mp3')
        if re.fullmatch(r'[spt]\d+', identity):
            return {'s': 'station', 'p': 'show', 't': 'episode'}[identity[0]] + ':' + identity
        if re.fullmatch(r'(?:station:s\d+|show:p\d+|episode:t\d+|custom:[\w-]+)', identity): return identity
    return None


class RadioImages:
    def __init__(self):
        self.prepare = lambda data: data
        self.lock = threading.Lock()
        self.prepared_sources = collections.OrderedDict()

    def backend_picture(self, store, path):
        key = path.rsplit('/', 1)[-1].removesuffix('.jpg')
        identity = 'radio-backend:' + hashlib.sha256(('%s:%s' % (id(store), key)).encode()).hexdigest()[:24]
        with self.lock:
            self.prepared_sources[identity] = lambda: store.get(key)
            self.prepared_sources.move_to_end(identity)
            while len(self.prepared_sources) > 2048: self.prepared_sources.popitem(last=False)
        return identity

    def fetch(self, url):
        candidates = json.loads(url[len('radio-image:'):]) if url.startswith('radio-image:') else [url]
        candidates = list(dict.fromkeys([*candidates, 'radio:fallback']))
        for candidate in candidates:
            try:
                with self.lock: prepared = self.prepared_sources.get(candidate)
                if prepared is not None:
                    data = prepared()
                    if not data: raise ValueError('Backend artwork is unavailable')
                    return data  # The backend's ArtStore already made baseline JPEG.
                if candidate == 'radio:fallback':
                    data = (Path(__file__).parent / 'webui' / 'room-icons' / 'generic.png').read_bytes()
                else:
                    if not str(candidate).startswith(('http://', 'https://')):
                        raise ValueError('Unsupported artwork URL')
                    request = urllib.request.Request(candidate, headers={'User-Agent': USER_AGENT})
                    with urllib.request.urlopen(request, timeout=8) as response:
                        data = response.read(5 * 1024 * 1024 + 1)
                    if len(data) > 5 * 1024 * 1024: raise ValueError('Artwork exceeds size limit')
                # Decode/resize once here so invalid image bodies can try the
                # station logo before falling back to the local Radio image.
                return self.prepare(data)
            except Exception:
                continue
        raise ValueError('Radio artwork and its local fallback are unavailable')


class RadioArtStore(ArtStore):
    def __init__(self, log=print):
        super().__init__(RadioImages(), log)
        self.backend.prepare = lambda data: ArtStore.shrink(self, data)

    def shrink(self, data):
        return data  # RadioImages already decoded and resized the chosen image.

    def path_for(self, picture, prefetch=True, *, revision=None, fallback=None):
        if picture and fallback and picture != fallback:
            # Include the station fallback in the cache identity: two rooms
            # sharing a missing album cover must not receive each other's logo.
            picture = 'radio-image:' + json.dumps([picture, fallback], separators=(',', ':'))
        return super().path_for(picture, prefetch, revision=revision)


class CoverLookup:
    """Conservative, cached public recording/release cover lookup; one MB call/s."""
    def __init__(self, fetch=None, clock=time.monotonic):
        self.fetch = fetch or self._fetch
        self.clock, self.lock, self.cache = clock, threading.Lock(), {}
        self.rate_lock, self.last_request = threading.Lock(), 0

    def _fetch(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=6) as response:
            return json.loads(response.read(2 * 1024 * 1024))

    @staticmethod
    def normal(value):
        return ' '.join(''.join(c.casefold() if c.isalnum() else ' ' for c in str(value)).split())

    def lookup(self, artist, title, album=''):
        key = (self.normal(artist), self.normal(title), self.normal(album))
        if not all(key[:2]): return ''
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() - cached[0] < (86400 if cached[1] else 3600): return cached[1]
        cover = ''
        try:
            # Serialize all rooms through the service's public rate limit.
            with self.rate_lock:
                delay = 1.05 - (self.clock() - self.last_request)
                if delay > 0: time.sleep(delay)
                quote = lambda text: str(text).replace('\\', '\\\\').replace('"', '\\"')
                query = 'recording:"%s" AND artist:"%s"' % (quote(title), quote(artist))
                url = 'https://musicbrainz.org/ws/2/recording/?' + urllib.parse.urlencode(
                    {'query': query, 'fmt': 'json', 'limit': 5})
                self.last_request = self.clock()
                data = self.fetch(url)
            matches = []
            for record in data.get('recordings', []):
                credits = record.get('artist-credit', [])
                names = [part.get('name') or part.get('artist', {}).get('name', '')
                         for part in credits if isinstance(part, dict)]
                if self.normal(record.get('title')) != key[1] or key[0] not in map(self.normal, names): continue
                for release in record.get('releases', []):
                    if key[2] and self.normal(release.get('title')) != key[2]: continue
                    if release.get('id'): matches.append(release['id'])
            # Without album identity, multiple releases are ambiguous.
            unique = list(dict.fromkeys(matches))
            if len(unique) == 1:
                candidate = 'https://coverartarchive.org/release/%s' % unique[0]
                art = self.fetch(candidate)
                front = next((x for x in art.get('images', []) if x.get('front') and x.get('approved', True)), None)
                if front: cover = front.get('thumbnails', {}).get('250') or front.get('image', '')
        except Exception:
            pass  # A metadata lookup must never fail playback.
        with self.lock:
            if len(self.cache) > 2048: self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (self.clock(), cover)
        return cover


@dataclasses.dataclass
class RadioSession:
    room: str
    item_id: str
    item: object
    stream: str
    generation: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    started: float = dataclasses.field(default_factory=time.monotonic)
    state: str = 'PLAYING'
    title: str = ''
    artist: str = ''
    album: str = ''
    program: str = ''
    art: str = ''
    song_identity: tuple = ()
    backend_uri: str = ''
    retry_at: float = 0
    retries: int = 0
    duration: float | None = None
    finite: bool = False
    cover_identity: tuple = ()
    cover_art: str = ''
    stream_index: int = 0
    last_position: float = 0
    bookmark_at: float = 0
    profile_at: float = 0
    profile_pending: bool = False


class RadioPlayback:
    def __init__(self, household, catalog, store, *, log=print, cover_lookup=None, clock=time.monotonic,
                 transcoder=None):
        self.household, self.catalog, self.store = household, catalog, store
        self.log, self.clock, self.lock = log, clock, threading.RLock()
        self.sessions, self.stop_event = {}, threading.Event()
        self.play_requests, self.transport_locks = {}, {}
        self.art = RadioArtStore(log)
        self.covers = cover_lookup or CoverLookup()
        self.transcoder = transcoder
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name='radio-metadata', daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.play_requests.clear()
        for session in sessions:
            self.save_bookmark(session)
            if self.transcoder is not None: self.transcoder.stop(session.generation)
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(2)

    def coordinator(self, udn):
        return self.household.coordinator.get(udn, udn)

    def player(self, udn):
        return self.household.by_udn[self.coordinator(udn)]

    def room(self, udn):
        return self.player(udn).state.backend

    @staticmethod
    def owns(uri, metadata=''):
        return radio_id(uri) is not None

    def session(self, udn):
        with self.lock: return self.sessions.get(self.coordinator(udn))

    def is_current(self, session):
        return self.sessions.get(session.room) is session

    def station_metadata(self, item, uri, art='', *, stream_format=None):
        data = item_data(item)
        node = Node(item_value(item, 'id'), '0', item_value(item, 'title') or data.get('name', 'Radio'),
                    item_value(item, 'id'), 'radio', False,
                    'object.item.audioItem.musicTrack' if item_value(item, 'kind') == 'episode' else BROADCAST_CLASS, uri=uri,
                    artist=data.get('host', ''), album=data.get('current_track', ''), thumbnail=art)
        result = ContentSource().didl([node], '')
        parsed = urllib.parse.urlsplit(uri)
        if parsed.scheme in ('http', 'https'):
            actual = stream_format or parsed.path.rsplit('.', 1)[-1].lower()
            mime = {'mp3': 'audio/mpeg', 'aac': 'audio/aac', 'aacp': 'audio/aacp',
                    'wma': 'audio/x-ms-wma', 'hls': 'application/vnd.apple.mpegurl',
                    'm3u8': 'application/vnd.apple.mpegurl'}.get(actual, '*')
            result = result.replace('protocolInfo="%s:*:*:*"' % parsed.scheme,
                                    'protocolInfo="http-get:*:%s:*"' % mime, 1)
        return result

    def _request_current(self, room, generation):
        return (not self.stop_event.is_set() and self.play_requests.get(room) == generation
                and self.coordinator(room) == room)

    def _episode_node(self, player, item, url):
        node = Node(item_value(item, 'id'), '0', item_value(item, 'title'), url, 'track', False,
                    'object.item.audioItem.musicTrack', uri=url,
                    artist=item_value(item, 'artist'), album=item_value(item, 'album'))
        image = item_value(item, 'image')
        art = self.art.path_for(image or 'radio:fallback', prefetch=False)
        service = getattr(self.household, 'radio_service', None)
        base = getattr(service, 'art_base_url', '')
        if not base and hasattr(player.config, 'advertise_ip') and hasattr(player.config, 'port'):
            base = 'http://%s:%d' % (player.config.advertise_ip, player.config.port)
        node.res = url
        node.res_md = didl(node.title, node.artist, node.album, base + art if base and art else '',
                           item_value(item, 'duration', None), uri=url, item_id=node.object_id)
        return node

    def play(self, udn, item_id, *, start_index=0, retry_count=0):
        player = self.player(udn)
        room = player.state.backend
        if room is not None: room.require('radio_play')
        generation = uuid.uuid4().hex
        with self.lock:
            if self.stop_event.is_set(): raise BackendError('Radio playback is closed')
            self.play_requests[player.config.udn] = generation
            transport_lock = self.transport_locks.setdefault(player.config.udn, threading.Lock())
        item = self.catalog.metadata(item_id)
        if item_value(item, 'kind') in ('folder', 'show', 'action', 'message'):
            raise BackendError('Choose a station or an episode to play')
        formats = getattr(room, 'radio_formats', lambda: ('mp3', 'aac', 'hls', 'wma', 'ogg'))() if room else ('mp3', 'aac', 'hls', 'wma', 'ogg')
        try: streams = self.catalog.resolve(item_id, formats=formats)
        except RadioTimeError as error:
            if error.code not in ('unavailable', 'format'): raise
            streams = []
        fallback = False
        if not streams and self.transcoder is not None and self.transcoder.available:
            # HLS is a container, even when Tune labels its codec "mp3".
            # Older Sonos renderers cannot play MP3-in-HLS; expose supported
            # audio through the same continuous MP3 route as legacy WMA.
            streams = self.catalog.resolve(item_id, formats=('wma', 'hls'))
            fallback = bool(streams)
        if not streams: raise BackendError('No compatible stream is available')
        failure = None
        ordered = list(enumerate(streams))
        ordered = ordered[start_index % len(ordered):] + ordered[:start_index % len(ordered)]
        for stream_index, stream in ordered:
            url = item_value(stream, 'url')
            if not url or not url.startswith(('http://', 'https://', 'mms://', 'mmsh://')): continue
            stream_format = item_value(stream, 'format') or urllib.parse.urlsplit(url).path.rsplit('.', 1)[-1].lower()
            convert = fallback and stream_format not in formats
            try:
                with transport_lock:
                    with self.lock:
                        if not self._request_current(player.config.udn, generation):
                            raise BackendError('Radio selection was superseded')
                    # prepare() revokes a previous source of the same identity;
                    # prepare only the alternative being attempted right now.
                    if convert: url = self.transcoder.prepare(url, identity=generation)
                    md = self.station_metadata(item, url, stream_format='mp3' if convert else stream_format)
                    policy_args = {'InstanceID': '0', 'CurrentURI': radio_uri(item_id),
                                   'CurrentURIMetaData': md}
                    before = getattr(self.household, 'before_playback_action', None)
                    if room is not None and callable(before):
                        intercepted, _ = before(room, 'AVTransport', 'SetAVTransportURI', policy_args)
                        if intercepted is not None:
                            raise BackendError('Another playback policy intercepted Radio selection')
                    if room is not None:
                        if item_value(item, 'kind') == 'episode':
                            room.play_item(self._episode_node(player, item, url))
                        else: room.play_radio(url, md)
                    else:
                        player.state.transport = 'PLAYING'
                    session = RadioSession(player.config.udn, item_id, item, url,
                                           generation=generation, started=self.clock(),
                                           title=item_value(item, 'title'), duration=item_value(item, 'duration', None),
                                           finite=item_value(item, 'kind') == 'episode')
                    session.stream_index, session.retries = stream_index, retry_count
                    data = item_data(item)
                    session.program = data.get('current_track', '')
                    fallback = data.get('logo') or data.get('image') or item_value(item, 'image') or 'radio:fallback'
                    session.art = self.art.path_for(fallback, revision='station:' + item_id)
                    session.backend_uri = url
                    session.profile_pending = True
                    with self.lock:
                        if not self._request_current(player.config.udn, generation):
                            raise BackendError('Radio selection was superseded')
                        previous = self.sessions.get(player.config.udn)
                        self.sessions[player.config.udn] = session
                    after = getattr(self.household, 'after_playback_action', None)
                    if room is not None and callable(after):
                        after(room, 'AVTransport', 'SetAVTransportURI', policy_args, result={})
                if previous:
                    self.save_bookmark(previous)
                    if self.transcoder is not None: self.transcoder.stop(previous.generation)
                try: self.store.record_history(item.as_dict() if hasattr(item, 'as_dict') else data)
                except (AttributeError, TypeError): pass
                self.changed(player.config.udn)
                threading.Thread(target=self.refresh_profile, args=(session,), daemon=True).start()
                if session.finite and self.store.settings().get('episode_resume', True):
                    bookmark = getattr(self.store, 'bookmark', lambda _: None)(item_id)
                    if bookmark and bookmark.get('position', 0) > 0:
                        threading.Thread(target=self.resume_episode,
                                         args=(session, bookmark['position']), daemon=True).start()
                return session.generation
            except (BackendError, ValueError, RadioTranscodeError) as error:
                failure = error
                if convert: self.transcoder.stop(generation)
                with self.lock:
                    if not self._request_current(player.config.udn, generation): break
        raise BackendError('Station playback failed: %s' % (failure or 'no supported stream'))

    def refresh_profile(self, session):
        try:
            with self.lock:
                if not self.is_current(session): return
            item = self.catalog.metadata(session.item_id)
            with self.lock:
                if self.is_current(session): session.item = item; session.profile_at = self.clock()
            self.refresh(session)
        except Exception as error:
            self.log({'event': 'radio-metadata-error', 'error': str(error)})
        finally:
            with self.lock: session.profile_pending = False

    @staticmethod
    def backend_uri(room):
        if room is None: return ''
        avt = getattr(room, 'avt', None)
        if isinstance(avt, dict): return avt.get('AVTransportURI', '')
        attrs = getattr(room, 'attributes', None)
        if isinstance(attrs, dict): return str(attrs.get('media_content_id') or '')
        return ''

    def changed(self, udn):
        player = self.household.by_udn.get(udn)
        if player and getattr(player, 'http_thread', None): player.publish_changed('AVTransport')

    def cancel(self, udn, *, expected=None):
        key = expected.room if expected is not None else self.coordinator(udn)
        with self.lock:
            if expected is not None and self.sessions.get(key) is not expected: return
            self.play_requests[key] = uuid.uuid4().hex
            session = self.sessions.pop(key, None)
        if session: self.save_bookmark(session)
        if session and self.transcoder is not None: self.transcoder.stop(session.generation)

    def save_bookmark(self, session):
        if not session.finite or not self.store.settings().get('episode_resume', True): return
        duration = session.duration if session.duration and session.duration > 0 else None
        if duration is not None and session.last_position >= max(0, duration - 2):
            clear = getattr(self.store, 'clear_bookmark', None)
            if clear: clear(session.item_id)
            session.bookmark_at = self.clock()
            return
        writer = getattr(self.store, 'set_bookmark', None)
        if writer and session.last_position > 0:
            writer(session.item_id, session.last_position, duration)
            session.bookmark_at = self.clock()

    def resume_episode(self, session, position):
        for _ in range(4):
            if self.stop_event.wait(0.5): return
            with self.lock:
                if not self.is_current(session): return
            room = self.room(session.room)
            if room is None: return
            if not room.supports('seek'): continue
            try:
                room.seek(position)
                return
            except BackendError:
                continue

    def transport_action(self, state, service, action, args):
        udn = state.config.udn
        if service != 'AVTransport': return None
        uri = args.get('CurrentURI') or args.get('EnqueuedURI', '')
        if action in ('SetAVTransportURI', 'AddURIToQueue') and self.owns(uri):
            item_id = radio_id(uri)
            if action == 'AddURIToQueue':
                item = self.catalog.metadata(item_id)
                if item_value(item, 'kind') == 'episode':
                    room = self.room(udn)
                    if room is None: raise BackendError('No queue playback backend')
                    streams = self.catalog.resolve(item_id, formats=('mp3', 'aac'))
                    if not streams: raise BackendError('No compatible episode stream is available')
                    stream = streams[0]
                    node = self._episode_node(self.player(udn), item, item_value(stream, 'url'))
                    return room.enqueue_item(node, args.get('EnqueueAsNext') in ('1', 'true'))
            self.play(udn, item_id)
            return {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1', 'NewQueueLength': '1'} \
                if action == 'AddURIToQueue' else {}
        if action == 'SetAVTransportURI':
            return None  # Release the old source only after the ordinary route succeeds.
        session = self.session(udn)
        if session is None:
            if action in ('Stop', 'Pause'): self.cancel(udn)
            return None
        if action in ('Stop', 'Pause'):
            with self.lock: self.play_requests[session.room] = uuid.uuid4().hex
            room = self.room(udn)
            if room is not None and session.finite:
                session.last_position = room.position()
                self.save_bookmark(session)
            if room is not None:
                if session.finite and action == 'Pause' and room.supports('pause'): room.pause()
                else: room.stop()
            with self.lock:
                session.state = 'PAUSED_PLAYBACK' if session.finite and action == 'Pause' else 'STOPPED'
            if self.transcoder is not None and (action == 'Stop' or not session.finite):
                self.transcoder.stop(session.generation)
            self.changed(session.room)
            return {}
        if action == 'Play':
            room = self.room(udn)
            if session.finite and room is not None: room.play(); session.state = 'PLAYING'
            elif session.state != 'PLAYING': self.play(udn, session.item_id)
            return {}
        if action in ('Seek', 'Next', 'Previous') and not session.finite:
            raise BackendError('Live radio does not support seeking or skipping')
        return None

    def after_transport_action(self, udn, service, action, args):
        """Release an overlay after a successful ordinary source/track selection.

        udn is the coordinator captured before dispatch; a group operation may
        have changed the household mapping by the time its backend acknowledges.
        Failed or denied selections never reach this hook.
        """
        if service != 'AVTransport': return
        with self.lock: session = self.sessions.get(udn)
        replacement = action == 'SetAVTransportURI' and not self.owns(args.get('CurrentURI', ''))
        skipped = session is not None and session.finite and (
            action in ('Next', 'Previous') or action == 'Seek' and args.get('Unit') == 'TRACK_NR')
        if not (replacement or skipped): return
        if session is not None: self.cancel(udn, expected=session)
        else:
            with self.lock: self.play_requests[udn] = uuid.uuid4().hex

    def refresh(self, session):
        with self.lock:
            if not self.is_current(session): return
        room = self.room(session.room)
        if room is None: return
        actual = self.backend_uri(room)
        if actual and actual not in (session.stream, session.backend_uri) and not session.finite:
            if self.clock() - session.started > 8: self.cancel(session.room, expected=session)
            return  # Never display metadata belonging to the previous source.
        if session.finite:
            avt = getattr(room, 'avt', {})
            attributes = getattr(room, 'attributes', {})
            track_uri = avt.get('CurrentTrackURI', '') if isinstance(avt, dict) else ''
            if not track_uri and isinstance(attributes, dict): track_uri = attributes.get('media_content_id', '')
            if track_uri and track_uri != session.stream:
                if self.clock() - session.started > 2: self.cancel(session.room, expected=session)
                return  # A following queued song must not overwrite this episode's bookmark.
        track = room.track()
        if session.finite and track and len(track) > 4 and track[4] and track[4] > 0:
            session.duration = track[4]
        reported_state = getattr(room, 'transport', None)
        avt = getattr(room, 'avt', {})
        failed = isinstance(avt, dict) and avt.get('TransportStatus') in ('ERROR', 'ERROR_OCCURRED')
        if reported_state in ('PLAYING', 'STOPPED', 'PAUSED_PLAYBACK', 'TRANSITIONING') \
                and self.clock() - session.started > 2 and not failed:
            state = 'STOPPED' if not session.finite and reported_state == 'PAUSED_PLAYBACK' else reported_state
            if state != session.state:
                session.state = state
                self.changed(session.room)
        if session.finite:
            session.last_position = room.position()
            if self.clock() - session.bookmark_at > 20: self.save_bookmark(session)
        data = item_data(session.item)
        station = item_value(session.item, 'title')
        title = artist = album = art = ''
        if track and track[0] not in (station, session.stream, 'Internet Radio', 'Radio'):
            title, artist, album, art = track[:4]
            if not artist and ' - ' in title: artist, title = title.split(' - ', 1)
        if not title:
            title, artist, album = data.get('current_song', ''), data.get('current_artist', ''), data.get('current_album', '')
            if not title and data.get('playing') and ' - ' in data['playing']:
                artist, title = data['playing'].split(' - ', 1)
            art = data.get('current_album_art') or data.get('playing_image', '')
        elif title and artist:
            supplied_title, supplied_artist = data.get('current_song', ''), data.get('current_artist', '')
            if not supplied_title and ' - ' in data.get('playing', ''):
                supplied_artist, supplied_title = data['playing'].split(' - ', 1)
            if (CoverLookup.normal(supplied_title), CoverLookup.normal(supplied_artist)) == \
                    (CoverLookup.normal(title), CoverLookup.normal(artist)):
                art = data.get('current_album_art') or data.get('playing_image') or art
        identity = (title or '', artist or '', album or '')
        fallback = data.get('logo') or item_value(session.item, 'image') or 'radio:fallback'
        if not art and session.cover_identity == identity and session.cover_art: image = session.cover_art
        elif art and art.startswith('/art/'):
            backend_art = getattr(getattr(room, 'backend', None), 'art', None)
            if backend_art is not None and isinstance(self.art, RadioArtStore):
                source = self.art.backend.backend_picture(backend_art, art)
                image = self.art.path_for(source, revision=repr(identity), fallback=fallback)
            else: image = art
        else: image = self.art.path_for(art or fallback, revision=repr(identity) if title else session.item_id,
                                       fallback=fallback if art else None)
        changed = False
        with self.lock:
            if not self.is_current(session): return
            if session.song_identity != identity or session.art != image:
                session.song_identity = identity
                session.title, session.artist, session.album = title or station, artist or '', album or ''
                session.art = image; changed = True
        if changed:
            self.changed(session.room)
            settings = self.store.settings()
            if title and artist and not art and settings.get('cover_lookup', True):
                threading.Thread(target=self.lookup_cover, args=(session, identity), daemon=True).start()

    def lookup_cover(self, session, identity):
        title, artist, album = identity
        url = self.covers.lookup(artist, title, album)
        if not url: return
        fallback = item_data(session.item).get('logo') or item_value(session.item, 'image') or 'radio:fallback'
        image = self.art.path_for(url, revision=repr(identity), fallback=fallback)
        with self.lock:
            if not self.is_current(session) or session.song_identity != identity: return
            session.art = image
            session.cover_identity, session.cover_art = identity, image
        self.changed(session.room)

    def run(self):
        while not self.stop_event.wait(2):
            with self.lock: sessions = list(self.sessions.values())
            for session in sessions:
                try:
                    self.refresh(session)
                    with self.lock:
                        if not self.is_current(session): continue
                    if self.clock() - session.profile_at > 60 and not session.profile_pending:
                        # The client's cache/TTL gates all actual upstream requests.
                        with self.lock:
                            session.profile_pending, session.profile_at = True, self.clock()
                        threading.Thread(target=self.refresh_profile, args=(session,), daemon=True).start()
                    room = self.room(session.room)
                    avt = getattr(room, 'avt', {}) if room else {}
                    attrs = getattr(room, 'attributes', {}) if room else {}
                    failed = avt.get('TransportStatus') in ('ERROR_OCCURRED', 'ERROR') or bool(attrs.get('media_error'))
                    if failed and session.state == 'PLAYING' and self.store.settings().get('automatic_reconnect', True):
                        if not session.retry_at: session.retry_at = self.clock() + 2 ** session.retries
                        if self.clock() >= session.retry_at:
                            if session.retries >= 3:
                                session.state = 'STOPPED'; self.changed(session.room)
                            elif self._request_current(session.room, session.generation):
                                self.play(session.room, session.item_id, start_index=session.stream_index + 1,
                                          retry_count=session.retries + 1)
                except Exception as error: self.log({'event': 'radio-refresh-error', 'error': str(error)})

    def values(self, udn, values, base_url=''):
        session = self.session(udn)
        if not session: return values
        with self.lock:
            if not self.is_current(session): return values
            uri = radio_uri(session.item_id)
            art = base_url + session.art if session.art.startswith('/') else session.art
            revision = track_revision(session.item_id, session.title, session.artist, session.album, session.duration)
            current_uri = uri + '?track=' + revision
            md = didl(session.title, session.artist, session.album, art, session.duration,
                      uri=current_uri, item_id='radio:' + revision)
            if session.artist:
                md = md.replace('</item>', '<r:streamContent>%s</r:streamContent></item>' % html.escape(
                    session.artist + ' - ' + session.title))
            if session.program:
                md = md.replace('</item>', '<r:radioShowMd>%s</r:radioShowMd></item>' % html.escape(session.program))
            station_md = self.station_metadata(session.item, uri)
            # ContentSource omits art without an ArtStore; insert the already-proxied URI.
            if art: station_md = station_md.replace('</item>', '<upnp:albumArtURI>%s</upnp:albumArtURI></item>' % html.escape(art))
            duration = (hms(session.duration) if session.duration and session.duration > 0 else
                        values.get('CurrentTrackDuration') or values.get('TrackDuration') or 'NOT_IMPLEMENTED') \
                if session.finite else 'NOT_IMPLEMENTED'
            room = self.room(udn)
            if session.finite and room is not None: actions = room.transport_actions()
            else:
                actions = 'Play' + (',Stop' if room is None or room.supports('stop') else '')
            updated = dict(values)
            updated.update(TransportState=session.state, CurrentTransportState=session.state,
                           AVTransportURI=uri, CurrentURI=uri,
                           AVTransportURIMetaData=station_md, CurrentURIMetaData=station_md,
                           CurrentTrackURI=current_uri, TrackURI=current_uri,
                           CurrentTrackMetaData=md, TrackMetaData=md,
                           CurrentTrackDuration=duration, TrackDuration=duration,
                           CurrentMediaDuration='0:00:00', MediaDuration='0:00:00',
                           NumberOfTracks='1', NrTracks='1', CurrentTrack='1', Track='1',
                           CurrentPlayMode='NORMAL', PlayMode='NORMAL',
                           CurrentTransportActions=actions, Actions=actions)
            if session.finite:
                for key in ('NumberOfTracks', 'NrTracks', 'CurrentTrack', 'Track', 'CurrentPlayMode', 'PlayMode'):
                    if key in values: updated[key] = values[key]
            if not session.finite: updated.update(RelTime='NOT_IMPLEMENTED', AbsTime='NOT_IMPLEMENTED')
            return updated
