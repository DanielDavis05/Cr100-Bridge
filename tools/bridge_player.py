"""Bridge-owned playback for rooms whose speaker is not a Sonos.

A Sonos keeps its own queue and fetches music-service audio itself. Other
speakers (a Bose, a Chromecast, a DLNA renderer behind Home Assistant) do not,
so for those rooms the bridge keeps the CR100's queue (bridge_queue.py) and
plays it through the best *route* the speaker offers:

* ConnectRoute: the speaker is a Spotify Connect receiver. The bridge sends the
  queue's tracks to it; Spotify streams and advances on the speaker itself.
* RelayRoute: the speaker can play a URL. The bridge fetches each entry's audio
  from its media source and serves one continuous MP3 stream (relay_stream.py).

Routes are chosen per play request, Connect first. Media sources
(media_sources.py) are provider-neutral, so another provider only needs a
source (and, optionally, its own native route).

The speaker is reached through a small adapter (see Speaker) so this module
does not depend on Home Assistant.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Protocol

from backends import BackendError
from bridge_queue import BridgeQueue, matches
from relay_stream import Segment

CONNECT_CACHE = 600          # s a discovered Connect endpoint is reused
DISCOVERY_RETRY = 30         # s between discovery attempts that found nothing
CONNECT_CONFIRM = 60         # s to wait for the speaker to report a queued track after a Connect start
LOST_GRACE = 30              # s a confirmed Connect session survives the speaker reporting something else
COMMAND_HOLD = 20            # s a commanded play/pause state wins over the speaker's older report
CONNECT_LATENCY = 2.0        # s from Spotify accepting a command to the speaker playing (SoundTouch, measured)
REPORT_LAG = 15              # s a speaker's new-track report may trail the change (HA polling)
# A Sonos reports TRANSITIONING after Play, Seek or a skip, then PLAYING once audio resumes.
# The CR100 restarts its progress bar from that second event (it does not poll
# position otherwise), so the bridge reports the same pair.
TRANSITION = CONNECT_LATENCY
RELAY_START = 12             # s for a speaker to begin fetching the relay URL
MAX_FAILURES = 3


class Speaker(Protocol):
    """What bridge playback needs from the room's real speaker."""
    name: str

    def snapshot(self) -> dict:
        """Cached state: available, state (playing/paused/idle/off), source, content_id,
        title, artist, album, picture, duration, position."""
        ...

    def can_play_url(self) -> bool: ...
    def play_url(self, url: str) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def wake(self) -> None:
        """Turn the speaker on if it is off (no-op when unsupported)."""
        ...


# --- routes -----------------------------------------------------------------------
class ConnectRoute:
    """Spotify Connect: the speaker streams the bridge queue's tracks itself."""
    name = 'connect'

    def __init__(self, player, connect, url):
        self.player, self.connect, self.url = player, connect, url
        self.receiver = None
        self.started_at = 0.0
        # Home Assistant integrations poll some speakers every 10-30 s (SoundTouch), and a
        # speaker waking from standby reports "unknown" or its old source meanwhile. The
        # session is confirmed only once the speaker reports a queued track; until then,
        # and for a grace period after it reports something else, it stays ours.
        self.source = None               # the speaker's source label once confirmed
        self.lost_since = None
        # Some speakers (SoundTouch via HA) report no position: keep a track clock.
        self.clock_entry, self.clock_base, self.clock_since = None, 0.0, None
        self.clock_reset_at = -REPORT_LAG    # when the clock last started a new track
        self.expected, self.expected_at = None, 0.0     # 'playing' / 'paused' just commanded

    @staticmethod
    def eligible(entries):
        return bool(entries) and all(entry.source == 'spotify' for entry in entries)

    def _expect(self, state):
        self.expected, self.expected_at = state, time.monotonic()

    def _reported_state(self, snapshot):
        """The speaker's state, unless it has not yet caught up with a recent command."""
        state = snapshot.get('state')
        if self.expected is not None:
            if state == self.expected or time.monotonic() - self.expected_at >= COMMAND_HOLD:
                self.expected = None
            else:
                return self.expected
        return state

    @property
    def confirmed(self):
        return self.source is not None

    def _clock(self, position, playing=True, latency=CONNECT_LATENCY):
        entry = self.player.queue.current
        self.clock_entry = entry.ref if entry else None   # sync() refines this to what the speaker reports
        self.clock_base, self.clock_since = float(position), time.monotonic() + latency if playing else None
        self.clock_reset_at = time.monotonic()

    def _elapsed(self):
        running = max(0.0, time.monotonic() - self.clock_since) if self.clock_since is not None else 0.0
        return self.clock_base + running

    def start(self, index, position=0.0):
        queue = self.player.queue
        self.player.speaker.wake()
        result = self.connect.play_list(self.url, [entry.ref for entry in queue.snapshot()], index,
                                        int(position * 1000), shuffle=queue.shuffle, repeat=queue.repeat,
                                        autoplay=self.player.service.autoplay_enabled())
        self.receiver, self.started_at = result['device_id'], time.monotonic()
        self.source, self.lost_since = None, None
        self._clock(position)
        self._expect('playing')

    def _command(self, endpoint, value=None):
        if self.receiver is None: raise BackendError('Spotify playback on this speaker has ended')
        self.connect.command(self.receiver, endpoint, value)

    def pause(self):
        self._command('pause')
        self._freeze()
        self._expect('paused')

    def resume(self):
        self._command('resume')
        if self.clock_since is None: self.clock_since = time.monotonic() + CONNECT_LATENCY
        self._expect('playing')

    def stop(self): self.pause()
    def release(self): self.receiver = None

    def next(self):
        self._command('skip_next')
        self.player.queue.advance()
        self._clock(0)

    def previous(self):
        self._command('skip_prev')
        self.player.queue.back()
        self._clock(0)

    def seek(self, seconds):
        self._command('seek_to', int(max(0, seconds) * 1000))
        self._clock(max(0, seconds), self.clock_since is not None)

    def set_mode(self, shuffle, repeat):
        self._command('set_shuffling_context', bool(shuffle))
        self._command('set_repeating_context', repeat == 'all')
        self._command('set_repeating_track', repeat == 'one')

    def _freeze(self):
        if self.clock_since is not None:
            self.clock_base, self.clock_since = self._elapsed(), None

    # --- following the speaker --------------------------------------------------------
    def _reports_queue(self, snapshot):
        title = snapshot.get('title') or ''
        return bool(title) and any(matches(entry, title, snapshot.get('artist', ''))
                                   for entry in self.player.queue.snapshot())

    def active(self, snapshot):
        if self.receiver is None: return False
        now = time.monotonic()
        state, source = snapshot.get('state'), snapshot.get('source') or ''
        live = snapshot.get('available') and state in ('playing', 'paused', 'buffering')
        if not self.confirmed:
            if live and state == 'playing' and source and self._reports_queue(snapshot):
                self.source = source
                self.player.log({'event': 'bridge-route-confirmed', 'room': self.player.key, 'source': source,
                                 'seconds': round(now - self.started_at, 1)})
                return True
            return now - self.started_at < CONNECT_CONFIRM
        if live and source == self.source:
            self.lost_since = None
            return True
        if self.lost_since is None: self.lost_since = now
        return now - self.lost_since < LOST_GRACE

    def _track_key(self, snapshot):
        """What the speaker is playing: a queue entry, or (past the queue, Autoplay) its title."""
        entry, title = self.player.queue.current, snapshot.get('title') or ''
        if self.confirmed and title and (entry is None or not matches(entry, title, snapshot.get('artist', ''))):
            return 'speaker:' + title
        return entry.ref if entry else None

    def sync(self, snapshot):
        title = snapshot.get('title') or ''
        if self.confirmed and title: self.player.queue.locate(title, snapshot.get('artist', ''))
        key = self._track_key(snapshot)
        if key is not None and key != self.clock_entry:             # the speaker moved to another track
            # A late report of a change the clock already made (Next, Seek, a track that
            # ran out) keeps the clock; any other change starts the track from zero now.
            if time.monotonic() - self.clock_reset_at >= REPORT_LAG:
                self._clock(0, snapshot.get('state') == 'playing', latency=0)
            self.clock_entry = key
            self.player.service.save()
        if self.confirmed:
            state = self._reported_state(snapshot)
            if state == 'playing':
                if self.clock_since is None: self.clock_since = time.monotonic()
            elif state == 'paused':
                self._freeze()

    def transport(self, snapshot):
        # Spotify acknowledged the command; until the speaker's report catches up, show
        # what was commanded so the controller's progress bar keeps moving.
        if not self.confirmed or self.lost_since is not None: return 'PLAYING' if self.clock_since else 'PAUSED_PLAYBACK'
        state = self._reported_state(snapshot)
        if state == 'playing': return 'PLAYING'
        if state == 'paused': return 'PAUSED_PLAYBACK'
        return 'TRANSITIONING'

    def position(self, snapshot):
        if self.confirmed and snapshot.get('position'): return snapshot['position']
        elapsed = self._elapsed()
        entry = self.player.queue.current
        duration = (snapshot.get('duration') if self.confirmed else None) or (entry.duration if entry else None)
        if duration and self.clock_since is not None and elapsed > duration + 1:
            # The track ran out before the speaker's report: the next one is playing.
            self.clock_base, self.clock_since = elapsed - duration, time.monotonic()
            self.clock_reset_at = time.monotonic()
            return self.clock_base
        return min(elapsed, duration) if duration else elapsed

    def track(self, snapshot):
        """Queue metadata when it matches what the speaker reports, else the speaker's own."""
        entry, title = self.player.queue.current, snapshot.get('title') or ''
        if entry is not None and (not self.confirmed or not title or matches(entry, title, snapshot.get('artist', ''))):
            duration = (snapshot.get('duration') if self.confirmed and title else None) or entry.duration
            return entry.title, entry.artist, entry.album, entry.art or snapshot.get('picture', ''), duration
        if title:
            artist = snapshot.get('artist', '')
            if artist and title.casefold().startswith(artist.casefold() + ' - '):
                title = title[len(artist) + 3:]                    # SoundTouch's "Artist - Title"
            return (title, artist, snapshot.get('album', ''), snapshot.get('picture', ''), snapshot.get('duration'))
        return None

    # --- surviving a bridge restart --------------------------------------------------
    def export(self):
        if self.receiver is None: return None
        wall = time.time() - (time.monotonic() - self.clock_since) if self.clock_since is not None else None
        return {'name': self.name, 'url': self.url, 'receiver': self.receiver, 'source': self.source,
                'clock_entry': self.clock_entry, 'clock_base': self.clock_base, 'clock_wall': wall}

    def restore(self, data):
        """Resume following a session the previous bridge process started."""
        now = time.monotonic()
        self.receiver, self.source = data['receiver'], data.get('source')
        self.started_at = now - CONNECT_CONFIRM if self.source else now
        self.clock_entry, self.clock_base = data.get('clock_entry'), float(data.get('clock_base') or 0)
        wall = data.get('clock_wall')
        self.clock_since = now - (time.time() - wall) if isinstance(wall, (int, float)) else None
        self.clock_reset_at = -REPORT_LAG

    def beyond_queue(self, snapshot):
        """The speaker plays something past the queue (Spotify Autoplay)."""
        return (self._track_key(snapshot) or '').startswith('speaker:')


class RelayRoute:
    """The bridge streams the queue as one MP3 URL the speaker plays."""
    name = 'relay'

    def __init__(self, player, streamer, sources):
        self.player, self.streamer, self.sources = player, streamer, sources
        self.lock = threading.Condition()
        self.generation = 0
        self.offset = 0.0               # position of the current entry, seconds
        self.state = 'STOPPED'
        self.failures = 0
        self.requested_at = 0.0
        self.token = streamer.register(self)
        self.streaming = False          # a speaker connection is consuming the feed

    def eligible(self, entries):
        if not entries or not self.streamer.available or not self.player.speaker.can_play_url(): return False
        for name in {entry.source for entry in entries}:
            source = self.sources.get(name)
            check = getattr(source, 'audio_available', None) if source else None
            if source is None or not hasattr(source, 'open_audio') or (callable(check) and not check()):
                return False
        return True

    @property
    def url(self):
        return self.streamer.url(self.token)

    def _bump(self, position=0.0, state='PLAYING'):
        with self.lock:
            self.generation += 1
            self.offset, self.state = float(position), state
            self.lock.notify_all()

    def start(self, index, position=0.0):
        self.player.queue.select(index + 1)
        self._bump(position)
        self.failures = 0
        snapshot = self.player.speaker.snapshot()
        if not (self.streaming and snapshot.get('state') == 'playing'):
            self.requested_at = time.monotonic()
            self.player.speaker.wake()
            self.player.speaker.play_url(self.url)

    def pause(self):
        with self.lock: self.state = 'PAUSED_PLAYBACK'
        self.player.speaker.pause()

    def resume(self):
        with self.lock:
            self.state = 'PLAYING'
            self.lock.notify_all()
        snapshot = self.player.speaker.snapshot()
        if snapshot.get('state') == 'paused' and self.streaming:
            self.player.speaker.resume()
        else:   # the speaker dropped the connection: reconnect from the saved position
            self._bump(self.offset)
            self.requested_at = time.monotonic()
            self.player.speaker.play_url(self.url)

    def stop(self):
        self._bump(self.offset, 'STOPPED')
        try: self.player.speaker.stop()
        except BackendError: pass

    def release(self):
        """Another source took over the speaker; end the stream without touching it."""
        self._bump(self.offset, 'STOPPED')

    def next(self):
        entry = self.player.queue.advance()
        if entry is None: raise BackendError('There is no next track in the queue')
        self._bump(0.0, self.state if self.state != 'STOPPED' else 'PLAYING')

    def previous(self):
        if self.offset > 3:
            self._bump(0.0, self.state); return
        self.player.queue.back()
        self._bump(0.0, self.state if self.state != 'STOPPED' else 'PLAYING')

    def seek(self, seconds): self._bump(max(0.0, seconds), self.state)
    def set_mode(self, shuffle, repeat): pass       # the bridge queue already applies it

    def active(self, snapshot):
        if self.state == 'STOPPED' and not self.streaming: return False
        if not snapshot.get('available'): return False
        if time.monotonic() - self.requested_at < RELAY_START: return True
        content = snapshot.get('content_id') or ''
        return self.streaming or (self.token in content and snapshot.get('state') in ('playing', 'paused', 'buffering'))

    def sync(self, snapshot): pass

    def transport(self, snapshot):
        if self.state == 'PLAYING' and not self.streaming: return 'TRANSITIONING'
        return self.state

    def position(self, snapshot):
        return self.offset

    def track(self, snapshot):
        entry = self.player.queue.current
        return (entry.title, entry.artist, entry.album, entry.art, entry.duration) if entry else None

    # --- feed for relay_stream ----------------------------------------------------
    def segment(self):
        with self.lock:
            while self.state == 'PAUSED_PLAYBACK':
                self.lock.wait(1)
            if self.state == 'STOPPED': return None
            generation, offset = self.generation, self.offset
        entry = self.player.queue.current
        if entry is None: return None
        self.streaming = True
        self.player.changed()
        try:
            audio = self.sources.open_audio(entry)
        except BackendError as error:
            self.failed(generation, str(error))
            return self.segment() if self.state == 'PLAYING' else None
        return Segment(generation, audio, offset)

    def progress(self, generation, seconds):
        with self.lock:
            if generation == self.generation: self.offset = seconds

    def finished(self, generation):
        with self.lock:
            if generation != self.generation: return
        self.failures = 0
        entry = self.player.queue.advance(finished=True)
        if entry is None:
            self._bump(0.0, 'STOPPED')
            self.streaming = False
        else:
            self._bump(0.0, 'PLAYING')
        self.player.changed()

    def failed(self, generation, message):
        self.player.log({'event': 'bridge-relay-entry-failed', 'room': self.player.key, 'error': message})
        with self.lock:
            if generation != self.generation: return
        self.failures += 1
        if self.failures >= MAX_FAILURES or self.player.queue.advance(finished=False) is None:
            self._bump(0.0, 'STOPPED')
            self.streaming = False
        else:
            self._bump(0.0, 'PLAYING')
        self.player.changed()

    def disconnected(self):
        self.streaming = False

    def close(self):
        self.release()
        self.streamer.unregister(self.token)


# --- the player -----------------------------------------------------------------------
class BridgePlayer:
    """One room's bridge queue and the route currently playing it."""

    def __init__(self, service, key, speaker, on_change=None):
        self.service, self.key, self.speaker = service, key, speaker
        self.queue = BridgeQueue()
        self.lock = threading.RLock()
        self.route = None
        self.relay = None
        self.queue_selected = False     # the CR100 chose the queue as what Play plays
        self.transition_until = 0.0
        self.on_change = on_change
        self.log = service.log

    def changed(self):
        self.service.save()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def export(self):
        with self.lock:
            route = self.route.export() if isinstance(self.route, ConnectRoute) else None
            return dict(self.queue.export(), queue_selected=self.queue_selected, route=route)

    def restore(self, data):
        if not isinstance(data, dict) or not self.queue.restore(data): return
        self.queue_selected = bool(data.get('queue_selected'))
        route = data.get('route')
        if isinstance(route, dict) and route.get('name') == 'connect' and isinstance(route.get('url'), str) \
                and isinstance(route.get('receiver'), str) and self.service.connect is not None:
            restored = ConnectRoute(self, self.service.connect, route['url'])
            try:
                restored.restore(route)
            except (KeyError, TypeError, ValueError):
                return
            self.route = restored
            self.log({'event': 'bridge-session-restored', 'room': self.key, 'tracks': len(self.queue)})

    # --- state ------------------------------------------------------------------------
    @property
    def active(self):
        route = self.route
        if route is None: return False
        snapshot = self.speaker.snapshot()
        if route.active(snapshot):
            route.sync(snapshot)       # follow what the speaker reports before any read
            return True
        # Decide again under the lock: a Play that is still starting this route (Spotify
        # sign-in can take seconds) holds it, and the route is valid once it returns.
        with self.lock:
            if self.route is not route: return self.route is not None and self.active
            snapshot = self.speaker.snapshot()
            if route.active(snapshot):
                route.sync(snapshot)
                return True
            self.log({'event': 'bridge-route-ended', 'room': self.key, 'route': route.name,
                      'state': snapshot.get('state'), 'source': snapshot.get('source'),
                      'available': snapshot.get('available')})
            route.release(); self.route = None
        self.changed()
        return False

    def snapshot(self):
        snapshot = self.speaker.snapshot()
        if self.route is not None: self.route.sync(snapshot)
        return snapshot

    def transport(self):
        if not self.active: return 'STOPPED'
        state = self.route.transport(self.snapshot())
        if state == 'PLAYING' and time.monotonic() < self.transition_until: return 'TRANSITIONING'
        return state

    def _transition(self):
        """Report TRANSITIONING now and PLAYING (with a change event) once audio resumes."""
        self.transition_until = time.monotonic() + TRANSITION
        timer = threading.Timer(TRANSITION + 0.2, self.changed)
        timer.daemon = True
        timer.start()

    def position(self):
        return self.route.position(self.snapshot()) if self.active else 0

    def track(self):
        if self.active: return self.route.track(self.snapshot())
        return None

    def autoplay_status(self):
        """'active' past the queue on Spotify Autoplay, 'armed' on a Spotify Connect queue, else None."""
        if not self.active or self.route.name != 'connect': return None
        if self.route.beyond_queue(self.snapshot()): return 'active'
        entry = self.queue.current
        return 'armed' if entry is not None and entry.source == 'spotify' else None

    def capabilities(self):
        if not self.active: return frozenset(('play',)) if len(self.queue) else frozenset()
        operations = {'play', 'pause', 'stop', 'seek', 'shuffle', 'repeat'}
        if self.queue.has_next() or self.route.name == 'connect': operations.add('next')
        if self.queue.has_previous(): operations.add('previous')
        return frozenset(operations)

    def can_play(self):
        """A route could exist for this room's music (before anything is queued)."""
        return self.service.connect_available() or self.service.relay_available(self.speaker)

    # --- queue edits -----------------------------------------------------------------
    def replace(self, uri, kind):
        entries = self.service.sources.expand(uri, kind)
        with self.lock:
            self.release()
            self.queue.replace(entries)
            self.play()
        self.changed()

    def enqueue(self, uri, kind, as_next=False):
        entries = self.service.sources.expand(uri, kind)
        with self.lock:
            was_active = self.active
            result = self.queue.add(entries, as_next)
            if was_active and self.route.name == 'connect' and not self.route.beyond_queue(self.speaker.snapshot()):
                # Connect plays a fixed list; restart it in place with the new rows. Past
                # the queue (Autoplay), adding does not interrupt what is playing.
                self._restart_in_place()
        self.changed()
        return result

    def remove(self, number):
        with self.lock:
            current = self.queue.numbers()[0]
            self.queue.remove(number)
            if self.active:
                beyond = self.route.name == 'connect' and self.route.beyond_queue(self.speaker.snapshot())
                if not len(self.queue): self.stop()
                elif not beyond and (self.route.name == 'connect' or number == current):
                    self._restart_in_place(number == current)
        self.changed()

    def clear(self):
        with self.lock:
            if self.active: self.stop()
            self.queue.clear()
            self.release()
        self.changed()

    def select(self, number):
        """Seek TRACK_NR: make it current, playing at once if the queue is playing."""
        with self.lock:
            self.queue.select(number)
            self.queue_selected = True
            if self.active and self.transport() in ('PLAYING', 'TRANSITIONING'):
                self.route.start(number - 1)
                self._transition()
        self.changed()

    def use_queue(self):
        with self.lock: self.queue_selected = True

    def _restart_in_place(self, from_start=False):
        snapshot = self.snapshot()            # follow the speaker before choosing the restart point
        index = (self.queue.numbers()[0] or 1) - 1
        position = 0.0 if from_start else self.route.position(snapshot)
        if self.transport() in ('PLAYING', 'TRANSITIONING'):
            self.route.start(index, position)
            self._transition()

    # --- transport ------------------------------------------------------------------
    def choose_route(self):
        entries = self.queue.snapshot()
        connect = self.service.connect_route(self, entries)
        if connect is not None: return connect
        if self.relay is None: self.relay = self.service.relay_route(self)
        if self.relay is not None and self.relay.eligible(entries): return self.relay
        raise BackendError(self.service.unavailable_reason(self.speaker, entries))

    def play(self):
        with self.lock:
            if not len(self.queue): raise BackendError('The queue is empty')
            state = self.transport() if self.active else None
            if state == 'PAUSED_PLAYBACK':
                self.route.resume()
                self._transition()
            elif state in ('PLAYING', 'TRANSITIONING'):
                pass        # already playing (Play Now's Seek TRACK_NR started the chosen track)
            else:
                route = self.choose_route()
                if self.route is not None and self.route is not route:
                    self.route.release(); self.route = None
                self.log({'event': 'bridge-play', 'room': self.key, 'route': route.name,
                          'tracks': len(self.queue)})
                # Publish the route only once the speaker has accepted it, so a reader
                # never judges a route that has no receiver yet.
                route.start((self.queue.numbers()[0] or 1) - 1)
                self.route = route
                self._transition()
            self.queue_selected = True
        self.changed()

    def _require_active(self):
        if not self.active: raise BackendError('The bridge queue is not playing in this room')

    def pause(self):
        with self.lock: self._require_active(); self.route.pause()
        self.changed()

    def stop(self):
        with self.lock:
            if self.route is not None: self.route.stop()
        self.changed()

    def next(self):
        with self.lock: self._require_active(); self.route.next(); self._transition()
        self.changed()

    def previous(self):
        with self.lock: self._require_active(); self.route.previous(); self._transition()
        self.changed()

    def seek(self, seconds):
        with self.lock: self._require_active(); self.route.seek(seconds); self._transition()
        self.changed()

    def set_mode(self, shuffle, repeat):
        with self.lock:
            self.queue.set_mode(shuffle, repeat)
            if self.active: self.route.set_mode(shuffle, repeat)
        self.changed()

    def release(self):
        """Another selection (radio, line-in, a station) replaces the bridge queue's playback."""
        with self.lock:
            if self.route is not None: self.route.release()
            self.route = None
            self.queue_selected = False

    def close(self):
        self.release()
        if self.relay is not None: self.relay.close()


# --- household service ------------------------------------------------------------------
class BridgePlayback:
    """Media sources, Spotify Connect control and the relay streamer, shared by rooms."""

    def __init__(self, sources, *, connect=None, streamer=None, connect_urls=None, discover=None,
                 relay=True, autoplay=None, state_file=None, log=print, clock=time.monotonic):
        self.sources, self.connect, self.streamer = sources, connect, streamer
        self.connect_urls = dict(connect_urls or {})      # room key -> ZeroConf URL, or False to disable
        self.discover = discover
        self.relay_enabled = relay
        self.autoplay = autoplay          # callable: the Spotify Autoplay setting, read live
        self.log, self.clock = log, clock
        self.players = {}
        self.lock = threading.RLock()
        self.discovered, self.discovered_at, self.discovery_failed_at = {}, 0.0, -DISCOVERY_RETRY
        # Queues and Connect sessions survive a bridge restart or config reload.
        self.state_file = Path(state_file) if state_file else None
        self.saved, self.loading = self._load(), False
        self.dirty, self.writer, self.closing = threading.Event(), None, False

    def player(self, key, speaker, on_change=None):
        with self.lock:
            player = self.players.get(key)
            if player is None:
                player = self.players[key] = BridgePlayer(self, key, speaker, on_change)
                saved = self.saved.pop(key, None)
                if saved is not None:
                    self.loading = True
                    try: player.restore(saved)
                    finally: self.loading = False
            else:
                player.speaker, player.on_change = speaker, on_change or player.on_change
            return player

    # --- persistence -------------------------------------------------------------------
    def _load(self):
        if self.state_file is None: return {}
        try:
            data = json.loads(self.state_file.read_text())
            rooms = data.get('rooms') if data.get('schema') == 1 else None
            return {key: value for key, value in rooms.items() if isinstance(key, str)} if isinstance(rooms, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def save(self):
        """Schedule a write. Callers may hold a player's lock, so never write here."""
        if self.state_file is None or self.loading or self.closing: return
        self.dirty.set()
        if self.writer is None:
            with self.lock:
                if self.writer is None:
                    self.writer = threading.Thread(target=self._write_loop, name='bridge-state', daemon=True)
                    self.writer.start()

    def _write_loop(self):
        while not self.closing:
            if not self.dirty.wait(1): continue
            time.sleep(.5)                      # coalesce bursts (a Play Now is several actions)
            self.dirty.clear()
            self.write()

    def write(self):
        if self.state_file is None: return
        with self.lock:
            rooms, players = dict(self.saved), list(self.players.items())   # unopened rooms keep their state
        for key, player in players:
            rooms[key] = player.export()                # takes each player's lock on its own
        body = json.dumps({'schema': 1, 'rooms': rooms}, ensure_ascii=False, separators=(',', ':'))
        with self.lock:
            try:
                self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix='.' + self.state_file.name + '.', dir=self.state_file.parent)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as target:
                        os.fchmod(target.fileno(), 0o600)
                        target.write(body)
                    os.replace(temporary, self.state_file)
                finally:
                    try: os.unlink(temporary)
                    except FileNotFoundError: pass
            except OSError as error:
                self.log({'event': 'bridge-state-save-failed', 'error': str(error)})

    # --- routes ------------------------------------------------------------------------
    def autoplay_enabled(self):
        try: return bool(self.autoplay()) if callable(self.autoplay) else False
        except Exception: return False

    def connect_available(self):
        """Local preflight, cached briefly: capability reads happen on every CR100 event."""
        now = self.clock()
        cached = getattr(self, '_connect_ok', None)
        if cached is not None and now - cached[0] < 30: return cached[1]
        try: ok = self.connect is not None and bool(self.connect.available())
        except Exception: ok = False
        self._connect_ok = (now, ok)
        return ok

    def relay_available(self, speaker):
        return bool(self.relay_enabled and self.streamer is not None and self.streamer.available
                    and speaker.can_play_url())

    def connect_url(self, key, name):
        configured = self.connect_urls.get(key)
        if configured is False: return None
        if configured: return configured
        if self.discover is None or not name: return None
        now = self.clock()
        with self.lock:
            fresh = now - self.discovered_at < CONNECT_CACHE
            wanted = ' '.join(name.casefold().split())
            if fresh and wanted in self.discovered: return self.discovered[wanted]
            if now - self.discovery_failed_at < DISCOVERY_RETRY and not fresh: return None
        try:
            found = self.discover()
        except Exception as error:
            self.log({'event': 'connect-discovery-failed', 'error': str(error)})
            found = {}
        with self.lock:
            if found:
                self.discovered, self.discovered_at = {' '.join(k.casefold().split()): v for k, v in found.items()}, now
            else:
                self.discovery_failed_at = now
            return self.discovered.get(wanted)

    def connect_route(self, player, entries):
        if not self.connect_available() or not ConnectRoute.eligible(entries): return None
        url = self.connect_url(player.key, getattr(player.speaker, 'name', ''))
        if not url: return None
        current = player.route
        if isinstance(current, ConnectRoute) and current.url == url: return current
        return ConnectRoute(player, self.connect, url)

    def relay_route(self, player):
        if not self.relay_enabled or self.streamer is None: return None
        return RelayRoute(player, self.streamer, self.sources)

    def unavailable_reason(self, speaker, entries):
        if not self.relay_enabled or self.streamer is None:
            return 'This speaker cannot play bridge music: it is not a Spotify Connect speaker and relay playback is off'
        if not speaker.can_play_url():
            return 'This speaker cannot play bridge music: it is not a Spotify Connect speaker and cannot play a URL'
        if self.streamer is None or not self.streamer.available:
            return 'Relay playback needs FFmpeg on the bridge'
        names = sorted({entry.source for entry in entries})
        return 'Relay playback for %s needs the bridge playback sign-in' % ', '.join(n.title() for n in names)

    def close(self):
        with self.lock: players = list(self.players.values())
        if self.dirty.is_set(): self.write()
        self.closing = True
        for player in players: player.close()
        if self.streamer is not None: self.streamer.close()


def spotify_connect_discovery(browse, connect, timeout=2.0):
    """name -> ZeroConf URL for every Spotify Connect receiver answering on the LAN."""
    def discover():
        found = {}
        for service in browse('_spotify-connect._tcp.local', timeout):
            path = service['txt'].get('CPath') or '/'
            url = 'http://%s:%d%s' % (service['address'], service['port'], path if path.startswith('/') else '/' + path)
            try:
                info = connect.receiver_info(url)
            except Exception:
                continue
            for label in {info.get('remoteName'), service['instance']}:
                if isinstance(label, str) and label: found.setdefault(label, url)
        return found
    return discover
