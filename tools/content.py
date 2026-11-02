"""The CR100's Music menu, whatever backend supplies it: object registry, paging, DIDL.

The CR100 browses its own object IDs (4.3.2 firmware strings, seen in captures):
FV:2 Sonos Favorites, A:... the Music Library, SQ: Sonos Playlists, R:0/0 radio
stations, Q:0 the room's queue, S: folders, AI: line-in. A backend's ContentSource
answers children() and metadata() for those IDs as Nodes and resolve()s what the CR100
hands back to play; anything without a Sonos ID of its own gets a short ID derived under
its parent (derived_id), kept in the registry so a later Browse or play finds it again.

Paging: backends usually return whole lists. A source caches them briefly and gives the
CR100 at most PAGE_LIMIT entries per Browse, with the true TotalMatches, so it asks for
the rest as it scrolls.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import html
import threading
import time

PAGE_LIMIT = 100          # entries per Browse answer, whatever the CR100 asks for (it asks 500)
CACHE_TTL = 30.0          # s a backend's list is reused while the CR100 pages through it
NODE_LIMIT = 20000        # registry entries kept (object ID -> what the backend calls it)

DIDL_OPEN = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
             'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">')
TRACK_CLASS = 'object.item.audioItem.musicTrack'
BROADCAST_CLASS = 'object.item.audioItem.audioBroadcast'
# Playing: every playable entry carries a URI the bridge recognises when the CR100 hands it
# back (AddURIToQueue, SetAVTransportURI): x-rincon-playlist:<udn>#<id> for containers, as the
# ZP80 does; a track's own URI when it is one of PLAIN_SCHEMES; RADIO_PREFIX + <id> for
# anything else (favourites, stations), which the CR100 plays directly rather than queueing.
RADIO_PREFIX = 'x-rincon-mp3radio://bridge/'
PLAIN_SCHEMES = ('x-file-cifs:', 'http:', 'https:')


class ContentError(Exception):
    """Reported to the CR100 as this UPnP error code (701: no such object)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass
class Node:
    object_id: str
    parent_id: str
    title: str
    ref: str | None          # what the backend calls it (to browse or play); None: nothing to play
    kind: str | None         # the backend's type for ref
    container: bool
    upnp_class: str
    playable: bool = True
    uri: str = ''            # items only; containers get x-rincon-playlist per player
    artist: str = ''
    album: str = ''
    thumbnail: str = ''      # a picture reference the backend's ArtStore fetches
    child_count: int | None = None


def derived_id(parent, ref, kind):
    return '%s/%s' % (parent, hashlib.sha1(('%s|%s' % (kind, ref)).encode()).hexdigest()[:10])


def text(value):
    return html.escape(str(value), quote=False)


class ContentSource:
    """A backend's Music menu. Subclasses implement children(), metadata() and resolve();
    key is the room whose view is browsed (its own queue, its speaker's library)."""

    def __init__(self, art=None, log=print):
        self.art, self.log = art, log
        self.lock = threading.Lock()
        self.nodes = collections.OrderedDict()   # object ID -> Node (LRU)
        self.by_uri = {}                         # item URI -> object ID
        self.cache = {}                          # cache key -> (monotonic time, [Node])

    def children(self, key, object_id):
        """The container's Nodes, in order."""
        raise NotImplementedError

    def metadata(self, key, object_id):
        """The Node for one object ID; ContentError 701 when unknown."""
        raise NotImplementedError

    def resolve(self, uri, metadata=''):
        """The Node the CR100 means by a URI (and its DIDL) it was given, or None."""
        raise NotImplementedError

    def decorate_queue(self, key, nodes, start):
        """Queue Nodes about to be returned, starting at queue index start; may add art."""
        return nodes

    # --- registry and cache ------------------------------------------------------
    def register(self, node):
        with self.lock:
            self.nodes[node.object_id] = node
            self.nodes.move_to_end(node.object_id)
            if node.uri: self.by_uri[node.uri] = node.object_id
            while len(self.nodes) > NODE_LIMIT:
                _, old = self.nodes.popitem(last=False)
                if old.uri and self.by_uri.get(old.uri) == old.object_id: del self.by_uri[old.uri]

    def cached(self, key, ttl):
        with self.lock:
            hit = self.cache.get(key)
            return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def store(self, key, nodes):
        with self.lock:
            self.cache[key] = (time.monotonic(), nodes)
            stale = [k for k, (t, _) in self.cache.items() if time.monotonic() - t > CACHE_TTL]
            for k in stale: del self.cache[k]

    def queue_changed(self, key):
        with self.lock: self.cache.pop((key, 'Q:0'), None)

    # --- CR100 side -------------------------------------------------------------------
    def browse(self, key, udn, object_id, flag, start=0, count=0):
        """(DIDL-Lite, NumberReturned, TotalMatches) for a CR100 Browse."""
        if flag == 'BrowseMetadata':
            node = self.metadata(key, object_id)
            if object_id.startswith('Q:0/'):
                node = self.decorate_queue(key, [node], int(object_id[len('Q:0/'):]) - 1)[0]
            return self.didl([node], udn), 1, 1
        nodes = self.children(key, object_id)
        page = nodes[start:start + min(count or PAGE_LIMIT, PAGE_LIMIT)]
        if object_id == 'Q:0': page = self.decorate_queue(key, page, start)
        return self.didl(page, udn), len(page), len(nodes)

    def didl(self, nodes, udn):
        return DIDL_OPEN + ''.join(self.entry(node, udn) for node in nodes) + '</DIDL-Lite>'

    def entry(self, node, udn):
        tag = 'container' if node.container else 'item'
        attributes = ' id="%s" parentID="%s" restricted="true"' % (html.escape(node.object_id), html.escape(node.parent_id))
        if node.child_count is not None: attributes += ' childCount="%d"' % node.child_count
        fields = ['<dc:title>%s</dc:title>' % text(node.title), '<upnp:class>%s</upnp:class>' % node.upnp_class]
        if node.artist: fields.append('<dc:creator>%s</dc:creator>' % text(node.artist))
        if node.album: fields.append('<upnp:album>%s</upnp:album>' % text(node.album))
        if node.thumbnail and self.art:
            fields.append('<upnp:albumArtURI>%s</upnp:albumArtURI>' % self.art.path_for(node.thumbnail, prefetch=False))
        if node.container and node.playable and node.ref is not None:
            uri = 'x-rincon-playlist:%s#%s' % (udn, node.object_id)
            fields.append('<res protocolInfo="x-rincon-playlist:*:*:*">%s</res>' % text(uri))
        elif node.uri:
            fields.append('<res protocolInfo="%s:*:*:*">%s</res>' % (node.uri.split(':', 1)[0], text(node.uri)))
        return '<%s%s>%s</%s>' % (tag, attributes, ''.join(fields), tag)
