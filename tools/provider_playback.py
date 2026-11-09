"""Prepare music-service items for a direct Sonos queue before changing playback.

Adapters translate a provider URI/type into ordered QueueItem candidates. A
registry materializes and validates the whole plan first; the room only owns
queue execution and retrying candidates. Home Assistant continues to pass its
provider URI/type to the integration, which owns that translation itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Protocol

from backends import BackendError
from sonos_upnp import DIDL_OPEN


@dataclass(frozen=True)
class QueueItem:
    uri: str
    metadata: str


class SonosPlaybackAdapter(Protocol):
    def prepare(self, uri: str, kind: str | None) -> Iterable[QueueItem]:
        """Validate an item and return native encodings in retry order, without I/O."""
        ...


class SonosPlaybackRegistry:
    """URI scheme -> adapter with prepare(uri, kind) -> QueueItem candidates.

    Register another provider on a backend's playback_adapters without changing
    SonosRoom. prepare() must validate the item and return candidates in retry
    order; it must not mutate a player. kind is None for the legacy share() call.
    """
    def __init__(self, adapters=None):
        self.adapters: dict[str, SonosPlaybackAdapter] = {}
        for scheme, adapter in (adapters or {}).items():
            self.register(scheme, adapter)

    def register(self, scheme, adapter):
        if not isinstance(scheme, str) or not re.fullmatch(r'[a-z][a-z0-9+.-]*', scheme):
            raise ValueError('Expected a lowercase provider URI scheme')
        if not callable(getattr(adapter, 'prepare', None)):
            raise TypeError('A playback adapter must implement prepare(uri, kind)')
        self.adapters[scheme] = adapter

    def prepare(self, uri, kind=None):
        if not isinstance(uri, str) or ':' not in uri:
            raise BackendError('Expected a music-service URI')
        scheme = uri.partition(':')[0].lower()
        adapter = self.adapters.get(scheme)
        if adapter is None:
            raise BackendError('Music service %r is not supported for direct Sonos playback' % scheme)
        # Materialize generators too: validation must finish before clear_queue.
        items = tuple(adapter.prepare(uri, kind))
        if not items or any(not isinstance(item, QueueItem) or not isinstance(item.uri, str)
                            or not item.uri or not isinstance(item.metadata, str) for item in items):
            raise BackendError('Music service %r returned an invalid Sonos playback plan' % scheme)
        return items


class SpotifyPlaybackAdapter:
    """Spotify share links, trying the global/EU account then the US account."""
    services = (2311, 3079)
    kinds = {
        # Spotify URI kind -> (HA type, enqueue prefix, item key, UPnP class).
        'track': ('music', '', '00032020', 'object.item.audioItem.musicTrack'),
        'album': ('album', 'x-rincon-cpcontainer:1004206c', '00040000',
                  'object.container.album.musicAlbum'),
        'playlist': ('playlist', 'x-rincon-cpcontainer:1006206c', '1006206c',
                     'object.container.playlistContainer'),
    }

    def prepare(self, uri, kind=None):
        match = re.fullmatch(r'spotify:(track|album|playlist):(\w+)', uri)
        if not match:
            raise BackendError('Not a supported Spotify item: %s' % uri)
        media_type, prefix, key, upnp_class = self.kinds[match.group(1)]
        if kind is not None and kind != media_type:
            raise BackendError('Spotify item type %r does not match %r' % (kind, media_type))
        encoded = uri.replace(':', '%3a')
        items = []
        for service in self.services:
            metadata = (DIDL_OPEN + '<item id="%s%s" restricted="true"><upnp:class>%s</upnp:class>'
                        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
                        'SA_RINCON%d_X_#Svc%d-0-Token</desc></item></DIDL-Lite>') % (
                            key, encoded, upnp_class, service, service)
            items.append(QueueItem(prefix + encoded, metadata))
        return items


def default_sonos_playback():
    """An independent registry so adding a provider affects only this backend."""
    return SonosPlaybackRegistry({'spotify': SpotifyPlaybackAdapter()})
