"""A bridge-owned play queue for rooms whose speaker has no native Sonos queue.

The CR100 drives a Sonos queue (Q:0): add, remove, clear, jump to a track,
next/previous and play modes. A non-Sonos speaker plays one stream at a time, so
the bridge keeps that queue itself. Entries are provider-neutral: each names the
media source that can expand or play it (``spotify``, later others) and carries
display metadata. Nothing here performs I/O; routes in bridge_player.py play it.
"""
from __future__ import annotations

import dataclasses
import random
import threading

MAX_ENTRIES = 1000


@dataclasses.dataclass(frozen=True)
class QueueEntry:
    source: str              # media source name, e.g. 'spotify'
    ref: str                 # that source's playable identity, e.g. 'spotify:track:<id>'
    title: str
    artist: str = ''
    album: str = ''
    art: str = ''            # picture URL for the room's ArtStore
    duration: float | None = None


class BridgeQueue:
    """Ordered entries, a cursor and Sonos-style play modes.

    Numbers exposed to the CR100 are 1-based positions in the visible order. With
    shuffle on, playback follows a permutation; the visible order stays put, as a
    Sonos S1 queue does while shuffling.
    """

    def __init__(self, rng=None):
        self.lock = threading.RLock()
        self.entries: list[QueueEntry] = []
        self.cursor: int | None = None        # index into entries
        self.shuffle, self.repeat = False, 'off'
        self.order: list[int] = []            # playback order when shuffling
        self.version = 0
        self.rng = rng or random.Random()

    # --- inspection ------------------------------------------------------------
    def __len__(self):
        with self.lock: return len(self.entries)

    def snapshot(self):
        with self.lock: return list(self.entries)

    @property
    def current(self) -> QueueEntry | None:
        with self.lock:
            return self.entries[self.cursor] if self.cursor is not None and self.cursor < len(self.entries) else None

    def numbers(self):
        """(current track, number of tracks), 1-based; (0, 0) when empty."""
        with self.lock:
            if not self.entries: return 0, 0
            return (self.cursor + 1 if self.cursor is not None else 1), len(self.entries)

    # --- edits ------------------------------------------------------------------
    def _changed(self):
        self.version += 1
        if self.shuffle: self._reshuffle()

    def add(self, entries, as_next=False):
        """Append (or insert after the current entry); AddURIToQueue's results."""
        entries = list(entries)
        with self.lock:
            if not entries: raise ValueError('Nothing to add to the queue')
            if len(self.entries) + len(entries) > MAX_ENTRIES:
                raise ValueError('The queue is limited to %d tracks' % MAX_ENTRIES)
            at = self.cursor + 1 if as_next and self.cursor is not None else len(self.entries)
            self.entries[at:at] = entries
            if self.cursor is not None and at <= self.cursor: self.cursor += len(entries)
            self._changed()
            return {'FirstTrackNumberEnqueued': str(at + 1), 'NumTracksAdded': str(len(entries)),
                    'NewQueueLength': str(len(self.entries))}

    # --- persistence ---------------------------------------------------------------
    def export(self):
        with self.lock:
            return {'entries': [dataclasses.asdict(entry) for entry in self.entries], 'cursor': self.cursor,
                    'shuffle': self.shuffle, 'repeat': self.repeat}

    def restore(self, data):
        """Load an export(); invalid data leaves the queue empty."""
        fields = {field.name for field in dataclasses.fields(QueueEntry)}
        try:
            entries = []
            rows = data.get('entries') or []
            if not isinstance(rows, list): raise ValueError
            for row in rows[:MAX_ENTRIES]:
                if not isinstance(row, dict) or set(row) - fields: raise ValueError
                if not all(isinstance(row.get(name), str) and row[name] for name in ('source', 'ref', 'title')): raise ValueError
                entries.append(QueueEntry(**row))
            cursor = data.get('cursor')
            if cursor is not None and (type(cursor) is not int or not 0 <= cursor < len(entries)): raise ValueError
            repeat = data.get('repeat', 'off')
            if repeat not in ('off', 'all', 'one'): raise ValueError
        except (AttributeError, TypeError, ValueError):
            return False
        with self.lock:
            self.entries, self.cursor = entries[:MAX_ENTRIES], cursor
            self.shuffle, self.repeat = bool(data.get('shuffle')), repeat
            self._changed()
        return True

    def replace(self, entries):
        with self.lock:
            self.entries, self.cursor = [], None
            result = self.add(entries)
            self.cursor = 0
            return result

    def remove(self, number):
        with self.lock:
            index = self._index(number)
            del self.entries[index]
            if self.cursor is not None:
                if index < self.cursor: self.cursor -= 1
                elif index == self.cursor and self.cursor >= len(self.entries):
                    self.cursor = len(self.entries) - 1 if self.entries else None
            self._changed()

    def clear(self):
        with self.lock:
            self.entries, self.cursor, self.order = [], None, []
            self._changed()

    def select(self, number):
        """Make this 1-based track current."""
        with self.lock:
            self.cursor = self._index(number)
            return self.entries[self.cursor]

    def _index(self, number):
        if not isinstance(number, int) or not 1 <= number <= len(self.entries):
            raise ValueError('No such queue track: %s' % number)
        return number - 1

    # --- play modes ----------------------------------------------------------------
    def set_mode(self, shuffle, repeat):
        if repeat not in ('off', 'all', 'one'): raise ValueError('Invalid repeat mode')
        with self.lock:
            changed = bool(shuffle) != self.shuffle
            self.shuffle, self.repeat = bool(shuffle), repeat
            if changed and self.shuffle: self._reshuffle()

    def _reshuffle(self):
        rest = [i for i in range(len(self.entries)) if i != self.cursor]
        self.rng.shuffle(rest)
        self.order = ([self.cursor] if self.cursor is not None and self.cursor < len(self.entries) else []) + rest

    def _sequence(self):
        return self.order if self.shuffle and len(self.order) == len(self.entries) else list(range(len(self.entries)))

    # --- movement ---------------------------------------------------------------------
    def advance(self, finished=False):
        """Move to the next entry and return it, or None at the end of the queue.

        finished: the current track ended by itself, so repeat-one replays it
        (an explicit Next still moves on, as on Sonos).
        """
        with self.lock:
            if not self.entries: return None
            if self.cursor is None:
                self.cursor = self._sequence()[0]
                return self.entries[self.cursor]
            if finished and self.repeat == 'one': return self.entries[self.cursor]
            sequence = self._sequence()
            position = sequence.index(self.cursor) if self.cursor in sequence else -1
            if position + 1 < len(sequence):
                self.cursor = sequence[position + 1]
            elif self.repeat in ('all', 'one') and sequence:
                if self.shuffle: self._reshuffle_from_start()
                self.cursor = self._sequence()[0]
            else:
                return None
            return self.entries[self.cursor]

    def _reshuffle_from_start(self):
        order = list(range(len(self.entries)))
        self.rng.shuffle(order)
        self.order = order

    def back(self):
        """Previous entry (stays on the first one), as the Previous key does."""
        with self.lock:
            if not self.entries: return None
            if self.cursor is None: self.cursor = 0
            sequence = self._sequence()
            position = sequence.index(self.cursor) if self.cursor in sequence else 0
            if position > 0: self.cursor = sequence[position - 1]
            elif self.repeat == 'all': self.cursor = sequence[-1]
            return self.entries[self.cursor]

    def has_next(self):
        with self.lock:
            if not self.entries: return False
            if self.repeat != 'off' or self.cursor is None: return True
            sequence = self._sequence()
            return self.cursor not in sequence or sequence.index(self.cursor) + 1 < len(sequence)

    def has_previous(self):
        with self.lock:
            return bool(self.entries)

    def locate(self, title, artist=''):
        """Point the cursor at the entry a speaker reports playing (Connect routes).

        Searches forward from the cursor first, so repeated titles resolve to the
        occurrence the speaker most plausibly reached. Returns True when found.
        """
        with self.lock:
            if not self.entries or not title: return False
            start = self.cursor or 0
            for index in list(range(start, len(self.entries))) + list(range(0, start)):
                if matches(self.entries[index], title, artist):
                    self.cursor = index
                    return True
            return False


def _norm(value):
    return ' '.join(str(value or '').casefold().split())


def matches(entry, title, artist=''):
    """Whether a speaker's reported title/artist describe this entry.

    Speakers differ: some report the title alone, others "Artist - Title"
    (Bose SoundTouch through Home Assistant does).
    """
    title, artist, own = _norm(title), _norm(artist), _norm(entry.title)
    if not title or not own: return False
    if title != own and not title.endswith(' - ' + own): return False
    theirs = _norm(entry.artist)
    return not artist or not theirs or theirs in artist or artist in theirs
