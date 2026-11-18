"""Bridge-owned Spotify continuation with an explicit, native Sonos queue.

Off declines provider playback, preserving existing adapters and APIs. On uses
finite Connect tracks; recommendations never enter Q:0. Only sessions owned by
this policy may advance. Account Autoplay preferences are never changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import threading
import time
from urllib.parse import unquote

from backends import BackendError
from sonos_upnp import soap, parse_didl

PREFETCH_TRACKS = 5
RECENT_TRACKS = 100
MAX_SKIPPED_BATCHES = 8
# Spotify on S1/S2 reports TRANSITIONING for several seconds after Play.
ADOPT_WINDOW = 15


def spotify_track(uri):
    value = unquote(uri or '')
    match = re.match(r'(?:(?:x-sonos-spotify|x-sonos-http):)?(spotify:track:[A-Za-z0-9]{22})(?:$|[?./])', value)
    return match[1] if match else None


def seconds(value):
    try:
        a, b, c = map(int, value.split(':'))
        return a * 3600 + b * 60 + c
    except (ValueError, AttributeError):
        return None


def target(room):
    if not room.available or not room.binding.native_sonos:
        raise BackendError('Spotify Autoplay requires a verified Sonos speaker.')
    members = room.group_members()
    coordinator = room.backend.room(members[0]) if members else room
    binding = coordinator.binding
    if not binding.native_sonos:
        raise BackendError('Spotify Autoplay coordinator is unavailable.')
    return binding.native_id, binding.native_url


class NativeTransport:
    """A verified room's native endpoint; no provider or account API here."""
    def __init__(self, room):
        self.room = room
        self.uuid, self.base = target(room)

    def call(self, action, args=()):
        return soap(self.base, 'AVTransport', action, [('InstanceID', 0)] + list(args), timeout=4)

    def snapshot(self):
        position = self.call('GetPositionInfo')
        media = self.call('GetMediaInfo')
        state = self.call('GetTransportInfo')
        entries = parse_didl(position.get('TrackMetaData', ''))
        item = entries[0] if entries else {}
        return {'state': state.get('CurrentTransportState'), 'source': media.get('CurrentURI', ''),
                'status': state.get('CurrentTransportStatus', 'OK'),
                'uri': position.get('TrackURI', ''), 'title': item.get('title', ''),
                'artist': item.get('artist', ''), 'duration': seconds(position.get('TrackDuration')),
                'position': seconds(position.get('RelTime')), 'index': int(position.get('Track') or 0)}

    def queue(self):
        result = soap(self.base, 'ContentDirectory', 'Browse', [('ObjectID', 'Q:0'),
            ('BrowseFlag', 'BrowseDirectChildren'), ('Filter', '*'), ('StartingIndex', 0),
            ('RequestedCount', 0), ('SortCriteria', '')], timeout=4)
        entries = parse_didl(result.get('Result', ''))
        if int(result.get('TotalMatches') or 0) != len(entries):
            raise BackendError('Cannot read the complete Sonos queue safely.')
        return [item['res'] for item in entries]

    def clear(self):
        if self.queue():
            self.call('RemoveAllTracksFromQueue')
            self.room.queue_edited()

    def sleep_remaining(self):
        try:
            result = self.call('GetRemainingSleepTimerDuration')
            return seconds(result.get('RemainingSleepTimerDuration'))
        except Exception:
            return None

    def play_queue(self, number):
        self.call('SetAVTransportURI', [('CurrentURI', 'x-rincon-queue:%s#0' % self.uuid), ('CurrentURIMetaData', '')])
        self.call('Seek', [('Unit', 'TRACK_NR'), ('Target', number)])
        self.call('Play', [('Speed', 1)])
        self.room.queue_edited()

    def move_next(self, first, count, before):
        self.call('ReorderTracksInQueue', [('StartingIndex', first), ('NumberOfTracks', count),
            ('InsertBefore', before), ('UpdateID', 0)])
        self.room.queue_edited()


@dataclass
class Run:
    room: object
    transport: object
    mode: str
    seed: str
    queue: list = field(default_factory=list)
    cursor: int = 0                 # played historical prefix; never replay it
    candidates: list = field(default_factory=list)
    recent: list = field(default_factory=list)
    played: set = field(default_factory=set)
    stream: object = None
    stream_seed: str = ''
    stream_generation: int = 0
    exhausted: bool = False
    expected: str = ''
    identity: tuple | None = None
    maximum: int = 0
    terminal: int = 0
    foreign: bool = False
    suspended: bool = False
    started: float = 0
    members: tuple = ()
    last_index: int = 0
    last_queue_length: int = 0
    last_position: int | None = None
    last_duration: int | None = None   # the current row's, kept across the end-of-queue rewind
    loading: bool = False
    prefetch_failed: bool = False
    deadline: float | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: object = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.played.update(self.recent)
        if spotify_track(self.seed) == self.seed:
            self.played.add(self.seed)


class SpotifyAutoplay:
    def __init__(self, client, folder_provider, enabled, log=print, rooms=None, *,
                 connect_factory=None, transport_factory=NativeTransport, clock=time.monotonic,
                 poll_interval=.75, submit=None):
        self.client, self.folder_provider, self.enabled, self.log = client, folder_provider, enabled, log
        self.rooms = rooms or (lambda: ())
        self.connect_factory, self.transport_factory = connect_factory, transport_factory
        self.clock, self.poll_interval = clock, poll_interval
        self.submit = submit or (lambda job: threading.Thread(target=job,
            name='spotify-related-metadata', daemon=True).start())
        self.runs = {}
        self.pending = {}   # key -> (room, deadline): Play issued, not yet PLAYING
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread, self.connect = None, None

    def is_enabled(self):
        try:
            return self.enabled() is True
        except Exception:
            return False

    def start(self):
        with self.lock:
            if self.thread is None and not self.stop.is_set():
                self.thread = threading.Thread(target=self._loop, name='spotify-continuation', daemon=True)
                self.thread.start()

    def _connection(self):
        with self.lock:
            if self.stop.is_set():
                raise BackendError('Spotify playback is closed.')
            if self.connect is None:
                factory = self.connect_factory
                if factory is None:
                    from spotify_connect import SpotifyConnect
                    factory = SpotifyConnect
                self.connect = factory(self.client, self.folder_provider, log=self.log)
            return self.connect

    def _key(self, room):
        return target(room)

    def _cancel(self, room):
        try:
            key = self._key(room)
        except BackendError:
            return
        with self.lock:
            self.pending.pop(key, None)
            run = self.runs.pop(key, None)
            if run:
                run.cancelled.set()

    def _register(self, run):
        run.members = tuple(run.room.group_members())
        reader = getattr(run.transport, 'sleep_remaining', None)
        remaining = reader() if callable(reader) else None
        if remaining:
            run.deadline = self.clock() + remaining
        with self.lock:
            key = (run.transport.uuid, run.transport.base)
            previous = self.runs.get(key)
            if previous:
                previous.cancelled.set()
            self.runs[key] = run
        self.start()

    def _track(self, uri):
        try:
            item = self.client.get('/tracks/' + uri.rpartition(':')[2])
        except Exception:
            raise BackendError('Spotify cannot load the selected track. Try again.') from None
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            raise BackendError('Spotify cannot identify the selected track.')
        return item['name']

    def play_provider(self, room, uri, kind):
        if not self.is_enabled():
            return False
        if not room.available or not room.binding.native_sonos:
            return False                              # bridge-owned queues play it themselves
        transport = self.transport_factory(room)       # validate route before any mutation
        connection = self._connection()
        connection.prepare()                         # authenticate before clearing old queue
        if kind != 'music':
            self._cancel(room)
            room.play_provider(uri, kind)             # finite album/playlist stays in Q:0
            self._register(Run(room, transport, 'queue', uri, queue=transport.queue(), started=self.clock()))
            return True
        if spotify_track(uri) != uri:
            raise BackendError('Unsupported Spotify Autoplay selection.')
        name = self._track(uri)
        self._cancel(room)
        transport.clear()
        connection.play_track(transport.base, uri)
        run = Run(room, transport, 'native', uri, expected=name, recent=[uri], started=self.clock())
        self._register(run)
        return True

    def queue_added(self, room, uri, result, as_next=False):
        """Provider Add retains its existing encoding/API, then journals the append."""
        if not self.is_enabled() and not self.runs:
            return
        try:
            key = self._key(room)
        except BackendError:
            return
        try:
            self._added(room, result, as_next)
        except Exception:
            # The enqueue already succeeded. Returning an error would cause
            # controller retries and duplicate explicit music.
            self._cancel(room)
            self.log({'event': 'spotify-autoplay-queue-changed', 'room': key[0],
                      'message': 'Music was queued; continuation was disarmed after a queue change.'})
            return
        if self.is_enabled():
            self._adopt_queue(room)

    def _added(self, room, result, as_next):
        try:
            run = self.runs.get(self._key(room))
        except BackendError:
            return
        if run is None or run.cancelled.is_set():
            return
        with run.lock:
            if as_next and run.mode == 'native' and result:
                first, count = int(result.get('FirstTrackNumberEnqueued') or 0), int(result.get('NumTracksAdded') or 0)
                if first and count and first > run.cursor + 1:
                    run.transport.move_next(first, count, run.cursor + 1)
            self._queue_sync(run)

    def claims_action(self, room, service, action, args):
        """Next on an armed run is ours even when the player omits it (last queue row)."""
        if service != 'AVTransport' or action != 'Next':
            return False
        try:
            run = self.runs.get(self._key(room))
        except BackendError:
            return False
        return run is not None and not run.cancelled.is_set() and (run.mode == 'native' or self.is_enabled())

    def before_action(self, room, service, action, args):
        if service != 'AVTransport':
            return None
        try:
            run = self.runs.get(self._key(room))
        except BackendError:
            return None
        if run is None:
            return None
        if action in ('Stop', 'Previous', 'SetAVTransportURI', 'BecomeCoordinatorOfStandaloneGroup'):
            self._cancel(room)
        elif action == 'Pause':
            run.suspended = True
        elif action == 'Seek':
            if args.get('Unit') == 'TRACK_NR':
                self._cancel(room)
            else:
                run.maximum, run.terminal = 0, 0
        elif action == 'Next':
            with run.lock:
                if run.mode == 'native':
                    self._queue_sync(run)
                    if run.cursor < len(run.queue):
                        self._play_pending(run)
                    elif self.is_enabled():
                        if not self._recommend(run):
                            if run.cancelled.is_set():
                                self._cancel(room)
                                return None
                            raise BackendError('Related music is loading. Try Next again shortly.')
                    else:
                        self._cancel(room)
                        return None
                    return {}
                snap = run.transport.snapshot()
                self._queue_sync(run)
                mode = getattr(room, 'shuffle_repeat', lambda: (False, 'off'))()
                if (mode == (False, 'off') and snap['index'] == len(run.queue)
                        and not run.foreign and self.is_enabled()):
                    if not self._recommend(run):
                        if run.cancelled.is_set():
                            self._cancel(room)
                            return None
                        raise BackendError('Related music is loading. Try Next again shortly.')
                    return {}
        return None

    def after_action(self, room, service, action, args, result=None):
        if service != 'AVTransport':
            return
        try:
            key = self._key(room)
        except BackendError:
            return
        run = self.runs.get(key)
        if action == 'AddURIToQueue':
            self._added(room, result, args.get('EnqueueAsNext') in ('1', 'true'))
        elif run and action == 'RemoveTrackFromQueue':
            with run.lock:
                index = int(args.get('ObjectID', '').rpartition('/')[2]) - 1
                if run.mode == 'queue' and index == run.cursor:
                    run.cancelled.set()            # explicit removal of the playing occurrence
                if index < run.cursor:
                    run.cursor -= 1
                if 0 <= index < len(run.queue):
                    del run.queue[index]
                self._queue_sync(run)
        elif run and action == 'RemoveAllTracksFromQueue':
            with run.lock:
                run.queue, run.cursor = [], 0
        elif run and action == 'Play':
            run.suspended, run.terminal, run.maximum = False, 0, 0
        elif run and action == 'ConfigureSleepTimer':
            interval = seconds(args.get('NewSleepTimerDuration', ''))
            run.deadline = self.clock() + interval if interval else None
        if self.is_enabled() and action in ('Play', 'Seek'):
            self._adopt_queue(room, retry=True)

    def _adopt_queue(self, room, retry=False):
        """Arm continuation for a Spotify row already PLAYING from the Sonos queue.

        The player usually reports TRANSITIONING right after Play, so with
        retry the service loop keeps checking for ADOPT_WINDOW seconds.
        """
        try:
            key = self._key(room)
        except BackendError:
            return False
        with self.lock:
            if key in self.runs:
                self.pending.pop(key, None)
                return True
        try:
            transport = self.transport_factory(room)
            snap = transport.snapshot()
            seed = spotify_track(snap['uri'])
            # Never adopt external autonomous Connect.
            if snap['state'] == 'PLAYING' and snap['source'].startswith('x-rincon-queue:') and seed:
                run = Run(room, transport, 'queue', seed, queue=transport.queue(), started=self.clock())
                with self.lock:
                    self.pending.pop(key, None)
                self._register(run)
                return True
        except Exception:
            pass
        if retry:
            with self.lock:
                self.pending[key] = (room, self.clock() + ADOPT_WINDOW)
        return False

    def _retry_adoptions(self):
        with self.lock:
            pending = list(self.pending.items())
        for key, (room, deadline) in pending:
            if self.clock() >= deadline or not self.is_enabled():
                with self.lock:
                    if self.pending.get(key, (None,))[0] is room:
                        del self.pending[key]
                continue
            self._adopt_queue(room)

    def _queue_sync(self, run):
        new = run.transport.queue()
        # Played rows persist. Unknown edits to history have ambiguous duplicate
        # occurrences; disarm instead of replaying old music.
        if new[:run.cursor] != run.queue[:run.cursor]:
            raise BackendError('Sonos queue history changed; start a new Spotify selection.')
        run.queue = new

    def _play_pending(self, run):
        if run.cancelled.is_set() or run.suspended or self.stop.is_set():
            return
        run.mode, run.identity, run.maximum, run.terminal = 'queue', None, 0, 0
        run.last_position, run.last_index, run.last_queue_length = None, 0, 0
        run.last_duration = None
        run.transport.play_queue(run.cursor + 1)
        run.started = self.clock()

    def _prefetch(self, run):
        if run.loading or len(run.candidates) >= PREFETCH_TRACKS or run.exhausted \
                or run.prefetch_failed or run.cancelled.is_set() \
                or self.stop.is_set() or not self.is_enabled() or run.foreign:
            return
        run.loading = True
        seed = run.seed
        generation = run.stream_generation
        def fetch():
            try:
                if run.cancelled.is_set() or self.stop.is_set() or not self.is_enabled():
                    return
                connection = self._connection()
                factory = getattr(connection, 'recommendation_stream', None)
                stream = run.stream
                if callable(factory) and (stream is None or run.stream_seed != seed):
                    stream = factory(seed, recent=run.recent[-RECENT_TRACKS:])
                    with run.lock:
                        if generation != run.stream_generation or run.cancelled.is_set() or self.stop.is_set():
                            return
                        run.stream, run.stream_seed = stream, seed
                for _ in range(MAX_SKIPPED_BATCHES):
                    if run.cancelled.is_set() or self.stop.is_set() or not self.is_enabled() \
                            or generation != run.stream_generation:
                        return
                    candidates = (stream.next_batch(recent=run.recent[-RECENT_TRACKS:]) if stream is not None
                                  else connection.recommendations(seed, run.recent[-RECENT_TRACKS:]))
                    with run.lock:
                        if run.cancelled.is_set() or self.stop.is_set() or generation != run.stream_generation:
                            return
                        seen = run.played | set(run.candidates)
                        fresh = []
                        for uri in candidates:
                            if spotify_track(uri) == uri and uri not in seen:
                                fresh.append(uri)
                                seen.add(uri)
                        run.candidates.extend(fresh)
                        if not candidates or getattr(stream, 'exhausted', False):
                            run.exhausted = True
                        if fresh or run.exhausted:
                            return
                        if stream is None:
                            run.exhausted = True   # legacy injection has no cursor to advance
                            return
                # A station yielding only previously played music must not spin
                # forever or silently relax the no-repeat rule.
                with run.lock:
                    if generation == run.stream_generation:
                        run.exhausted = True
            except Exception:
                with run.lock:
                    if generation == run.stream_generation:
                        run.prefetch_failed = True
            finally:
                with run.lock:
                    run.loading = False
        try:
            self.submit(fetch)
        except Exception:
            run.loading, run.prefetch_failed = False, True

    def _recommend(self, run):
        if run.cancelled.is_set() or self.stop.is_set() or not self.is_enabled() or run.foreign:
            run.cancelled.set()
            return False
        if run.suspended:
            return False
        connection = self._connection()
        # Recheck at dispatch as a queued song may have played since prefetch.
        run.candidates = [uri for uri in run.candidates if uri not in run.played]
        if not run.candidates:
            if run.prefetch_failed:
                raise BackendError('Spotify related music is unavailable. Start a new selection to retry.')
            if run.exhausted:
                run.cancelled.set()
                self.log({'event': 'spotify-autoplay-exhausted', 'room': run.transport.uuid})
                return False
            self._prefetch(run)
            return False
        uri = run.candidates[0]
        # Queue callers have reached the end (or pressed Next on its final
        # row). Native playback retains an already-consumed historical prefix.
        # A physical enqueue can complete while its callback waits on run.lock.
        pending_cursor = len(run.queue) if run.mode == 'queue' else run.cursor
        name = self._track(uri)
        if run.cancelled.is_set() or run.suspended or self.stop.is_set() or not self.is_enabled():
            return False
        self._queue_sync(run)
        if pending_cursor < len(run.queue):
            run.cursor = pending_cursor
            self._play_pending(run)
            return True
        run.candidates.remove(uri)
        connection.play_track(run.transport.base, uri)
        # Every existing row is now history; a later Next must not replay the
        # final queue row that Next-on-last-row just left.
        run.cursor = max(run.cursor, pending_cursor)
        run.mode, run.expected, run.identity = 'native', name, None
        run.maximum, run.terminal, run.started = 0, 0, self.clock()
        run.last_position, run.last_index, run.last_queue_length = None, 0, 0
        run.last_duration = None
        run.played.add(uri)
        run.recent.append(uri)
        run.recent = run.recent[-RECENT_TRACKS:]
        self.log({'event': 'spotify-autoplay-next', 'room': run.transport.uuid})
        return True

    def _tick(self, run):
        if run.cancelled.is_set() or run.suspended:
            return
        if self._key(run.room) != (run.transport.uuid, run.transport.base) \
                or (run.members and tuple(run.room.group_members()) != run.members) \
                or (run.deadline is not None and self.clock() >= run.deadline):
            run.cancelled.set()
            return
        snap = run.transport.snapshot()
        if snap.get('status', 'OK') not in ('OK', ''):
            run.cancelled.set()
            return
        expected_source = 'x-sonos-vli:' if run.mode == 'native' else 'x-rincon-queue:'
        if run.identity is None:
            match = snap['source'].startswith(expected_source) and snap['state'] == 'PLAYING' and snap['title']
            if run.mode == 'native':
                match = match and snap['title'] == run.expected
            if not match:
                if self.clock() - run.started > 20:
                    raise BackendError('Spotify Autoplay did not start the selected source.')
                return
            run.identity = (snap['source'], snap['title'], snap['artist'], snap['index'] if run.mode == 'queue' else 0)
        if not snap['source'].startswith(expected_source):
            run.cancelled.set()
            return
        identity = (snap['source'], snap['title'], snap['artist'], snap['index'] if run.mode == 'queue' else 0)
        if snap['state'] == 'PLAYING':
            if identity != run.identity:
                if run.mode == 'native':           # external takeover, even on same vli URI
                    run.cancelled.set()
                    return
                run.identity, run.maximum = identity, 0
                run.last_position = None
            if run.mode == 'queue':
                uri = spotify_track(snap['uri'])
                if uri:
                    run.played.add(uri)
                    if not run.recent or run.recent[-1] != uri:
                        run.recent.append(uri)
                        run.recent = run.recent[-RECENT_TRACKS:]
                    if run.seed != uri:
                        run.seed = uri
                        run.stream_generation += 1
                        run.stream, run.stream_seed, run.candidates = None, '', []
                        run.exhausted, run.prefetch_failed = False, False
                else:
                    run.foreign = True
                run.cursor = max(0, snap['index'] - 1)
                run.last_index = snap['index']
                self._queue_sync(run)
                run.last_queue_length = len(run.queue)
            if snap['position'] is not None:
                if run.last_position is not None and snap['position'] < run.last_position - 2:
                    run.maximum = 0                # external seek/repeat must not look finished
                run.last_position = snap['position']
            run.maximum = max(run.maximum, snap['position'] or 0)
            run.last_duration = snap['duration']
            run.terminal = 0
            # Metadata only: the current finite track is untouched while this loads.
            self._prefetch(run)
            return
        if snap['state'] not in ('PAUSED_PLAYBACK', 'STOPPED', 'NO_MEDIA_PRESENT'):
            return
        # At the end of a queue, S1 and S2 players stop and rewind to row 1, so a
        # finished multi-row queue reports the first row's title at 0:00.
        rewound = (run.mode == 'queue' and snap['state'] == 'STOPPED' and identity != run.identity
                   and snap['source'] == run.identity[0] and (snap['position'] or 0) <= 1
                   and run.last_duration and run.maximum >= run.last_duration - 2)
        if snap['title'] and identity != run.identity and not rewound:
            run.cancelled.set()                    # a different externally selected item
            return
        duration, position = snap['duration'], snap['position']
        if rewound:
            duration, position = run.last_duration, 0
        complete = duration and run.maximum >= duration - 2 and position is not None \
            and (position <= 1 or position >= duration)
        if not complete:
            run.suspended = True                   # manual/external pause, never exhaustion
            return
        run.terminal += 1
        if run.terminal < 2:
            return
        self._queue_sync(run)
        if run.mode == 'queue':
            # A shuffled queue can finish on any original row. All entries that
            # existed at the last playing observation are history at a natural
            # stop; entries appended after that observation remain pending.
            run.cursor = max(run.cursor, run.last_queue_length, run.last_index, snap['index'])
        if run.cursor < len(run.queue):
            self._play_pending(run)
        elif self.is_enabled() and not run.foreign:
            self._recommend(run)
        else:
            run.cancelled.set()                    # finish current, no stop/pause command

    def _loop(self):
        previous_enabled = False
        while not self.stop.wait(self.poll_interval):
            now_enabled = self.is_enabled()
            if now_enabled and not previous_enabled:
                try:
                    for _, _, room in self.rooms():
                        self._adopt_queue(room)
                except Exception:
                    pass
            previous_enabled = now_enabled
            try:
                self._retry_adoptions()
            except Exception:
                pass
            with self.lock:
                runs = list(self.runs.items())
            for key, run in runs:
                try:
                    with run.lock:
                        self._tick(run)
                except Exception:
                    run.cancelled.set()
                    self.log({'event': 'spotify-autoplay-stopped', 'room': run.transport.uuid,
                              'message': 'Playback changed or continuation is unavailable; start a new selection.'})
                if run.cancelled.is_set():
                    with self.lock:
                        if self.runs.get(key) is run:
                            del self.runs[key]
            # An Off installation has no native SDK session or keepalive once
            # its last already-playing finite track has finished.
            idle = None
            with self.lock:
                if not self.runs and not self.is_enabled() and self.connect is not None:
                    idle, self.connect = self.connect, None
            if idle is not None:
                idle.close()

    def close(self):
        self.stop.set()
        with self.lock:
            for run in self.runs.values():
                run.cancelled.set()
        if self.connect is not None:
            self.connect.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(2)
