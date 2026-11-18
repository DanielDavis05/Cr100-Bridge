"""Provider-neutral media sources for bridge-owned playback (bridge_player.py).

A media source turns what the CR100 selected (a provider URI and kind, as the
music-service resolvers return them) into queue entries, and can open the audio
of one entry for the relay route. Routes that let the speaker fetch content
itself (Spotify Connect, a plain URL) need no audio from the source.

Register another provider by adding a MediaSource; bridge_player and the HA
backend only talk to this registry.
"""
from __future__ import annotations

import dataclasses
import subprocess
from typing import Iterable, Protocol

from backends import BackendError
from bridge_queue import QueueEntry


@dataclasses.dataclass
class AudioInput:
    """Audio for ffmpeg: either a URL it may open, or a process whose stdout carries it."""
    url: str | None = None
    process: subprocess.Popen | None = None
    format: str | None = None          # ffmpeg -f hint for piped input ('ogg', ...)

    def close(self):
        process = self.process
        if process is None: return
        try:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=1)
        except (OSError, ProcessLookupError):
            pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe: pipe.close()
            except (OSError, ValueError):
                pass


class MediaSource(Protocol):
    name: str

    def owns(self, uri: str) -> bool:
        """Whether this source expands and plays a provider URI."""
        ...

    def available(self) -> bool:
        """Local preflight: configured and signed in; no network I/O."""
        ...

    def expand(self, uri: str, kind: str) -> list[QueueEntry]:
        """Playable entries in order (an album's tracks, ...). Raises BackendError."""
        ...

    def open_audio(self, entry: QueueEntry) -> AudioInput:
        """Start this entry's audio from its beginning for the relay route."""
        ...


class MediaSources:
    """Name -> MediaSource, with URI ownership lookup."""

    def __init__(self, sources: Iterable[MediaSource] = ()):
        self.sources: dict[str, MediaSource] = {}
        for source in sources: self.register(source)

    def register(self, source):
        if not getattr(source, 'name', None) or source.name in self.sources:
            raise ValueError('Media sources need a unique name')
        self.sources[source.name] = source

    def __bool__(self):
        return bool(self.sources)

    def get(self, name):
        return self.sources.get(name)

    def for_uri(self, uri):
        return next((source for source in self.sources.values() if source.owns(uri)), None)

    def available(self):
        return any(self._available(source) for source in self.sources.values())

    @staticmethod
    def _available(source):
        try: return bool(source.available())
        except Exception: return False

    def expand(self, uri, kind):
        source = self.for_uri(uri)
        if source is None: raise BackendError('No bridge media source plays this item')
        if not self._available(source):
            raise BackendError('%s playback is not set up on the bridge' % source.name.title())
        entries = source.expand(uri, kind)
        if not entries: raise BackendError('This selection has no playable tracks')
        return entries

    def open_audio(self, entry):
        source = self.get(entry.source)
        if source is None: raise BackendError('No bridge media source plays this item')
        return source.open_audio(entry)
