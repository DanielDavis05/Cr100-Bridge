"""Sonos content for the CR100's Music menu (a content.ContentSource): the players' own
ContentDirectory, browsed in the CR100's own object IDs.

A real player already answers the IDs the CR100 browses (FV:2, A:..., SQ:, S:, R:0/0, Q:0),
and pages natively, so a Browse goes to a player page by page; only Line-In (AI:) is the
bridge's. Each entry is rebuilt as the CR100-safe DIDL every backend sends (content.py),
never passed through: modern players add URIs and metadata a 2005 controller never saw.
A Node's ref is the player's object ID; SonosNode keeps the player's own res and r:resMD
so the backend can queue or play it exactly as a Sonos controller would. The queue (Q:)
comes from the room's group coordinator; everything else from the room's own player.
"""
from __future__ import annotations

import dataclasses
import html
import re
from urllib.parse import quote, unquote

from content import (BROADCAST_CLASS, PAGE_LIMIT, PLAIN_SCHEMES, RADIO_PREFIX, TRACK_CLASS, ContentError,
                     ContentSource, Node)
from sonos_upnp import UPnPFault, parse_didl, soap

QUEUE_TTL = 2.0           # the CR100 asks for Q:0 metadata many times in a row
BROWSE_TTL = 10.0         # other pages, while the CR100 scrolls back and forth
BROWSE_TIMEOUT = 15
# CR100 object ID -> (title, parent): the menu's top entries, whatever a player calls them
TOP = {'FV:2': ('Sonos Favorites', 'FV:'), 'A:': ('Music Library', '0'), 'SQ:': ('Sonos Playlists', '0'),
       'S:': ('Folders', '0'), 'R:0/0': ('Radio Stations', 'R:0'), 'AI:': ('Line-In', '0'), 'Q:0': ('Queue', 'Q:')}
LIBRARY_PREFIXES = ('A:', 'SQ:', 'S:')


@dataclasses.dataclass
class SonosNode(Node):
    res: str = ''            # the player's own URI for it
    res_md: str = ''         # its r:resMD (favourites): the metadata to play it with


class SonosContent(ContentSource):
    def __init__(self, backend, art=None, log=print):
        super().__init__(art, log)
        self.backend = backend

    def player(self, key, object_id):
        uuid = self.backend.coordinator_of(key) if object_id.startswith('Q:') else key
        speaker = self.backend.speaker(uuid) or self.backend.any_speaker()
        if speaker is None: raise ContentError(720, 'Cannot process the request: no Sonos player is reachable')
        return speaker

    def request(self, speaker, object_id, flag, start=0, count=1):
        """(entries, TotalMatches) from one Browse, cached briefly."""
        cache_key = (speaker.uuid, object_id, flag, start, count)
        found = self.cached(cache_key, QUEUE_TTL if object_id.startswith('Q:') else BROWSE_TTL)
        if found is not None: return found
        try:
            result = soap(speaker.base, 'ContentDirectory', 'Browse', [
                ('ObjectID', object_id), ('BrowseFlag', flag), ('Filter', '*'), ('StartingIndex', start),
                ('RequestedCount', count), ('SortCriteria', '')], BROWSE_TIMEOUT)
        except UPnPFault as error:
            if error.code == 701: raise ContentError(701, 'No such object: %s' % object_id) from error
            self.log({'event': 'browse-failed', 'speaker': speaker.name, 'id': object_id, 'error': str(error)})
            raise ContentError(720, 'Cannot process the request: %s' % error) from error
        found = (parse_didl(result.get('Result')), int(result.get('TotalMatches') or 0))
        self.store(cache_key, found)
        return found

    def queue_changed(self, key):
        with self.lock:
            for cache_key in [k for k in self.cache if isinstance(k, tuple) and str(k[1]).startswith('Q:')]:
                del self.cache[cache_key]

    def node_from(self, entry, speaker):
        object_id, res = entry['id'], entry['res']
        favourite = object_id.startswith('FV:2/') or entry['parent'] == 'FV:2'
        queued = object_id.startswith('Q:0/')
        # Favourites play at once, whatever they are, as with Home Assistant.
        container = entry['container'] and not favourite
        if container:
            upnp_class = entry['class'] if entry['class'].startswith('object.container') else 'object.container'
        elif queued or (not favourite and (entry['class'].startswith(TRACK_CLASS) or res.startswith(PLAIN_SCHEMES))):
            upnp_class = TRACK_CLASS
        else:
            upnp_class = BROADCAST_CLASS
        node = SonosNode(object_id, entry['parent'], entry['title'], object_id,
                         'favourite' if favourite else 'container' if container else 'item', container, upnp_class,
                         playable=bool(res) or object_id.startswith(LIBRARY_PREFIXES),
                         artist=entry['artist'], album=entry['album'],
                         thumbnail=self.backend.picture_url(speaker.base, entry['art']),
                         child_count=entry['child_count'], res=res, res_md=entry['res_md'])
        if not container:
            node.uri = res if upnp_class == TRACK_CLASS and res.startswith(PLAIN_SCHEMES) \
                else RADIO_PREFIX + quote(object_id, safe='')
        if object_id in TOP: node.title, node.parent_id = TOP[object_id]
        if not queued: self.register(node)   # queue IDs repeat in every room
        return node

    def top(self, key, object_id):
        """A top entry a player does not answer for itself (Line-In, an empty radio list)."""
        title, parent = TOP[object_id]
        count = 0 if object_id == 'AI:' else self.page(key, object_id, 0, 1)[1]
        return SonosNode(object_id, parent, title, None, None, True, 'object.container', child_count=count)

    def page(self, key, object_id, start, count):
        """(Nodes, TotalMatches) for children start.. of a container."""
        if object_id == 'AI:': return [], 0
        speaker = self.player(key, object_id)
        entries, total = self.request(speaker, object_id, 'BrowseDirectChildren', start,
                                      min(count or PAGE_LIMIT, PAGE_LIMIT))
        return [self.node_from(entry, speaker) for entry in entries], total

    # --- ContentSource --------------------------------------------------------------------
    def children(self, key, object_id):
        nodes, total = self.page(key, object_id, 0, PAGE_LIMIT)
        while len(nodes) < total:
            more, _ = self.page(key, object_id, len(nodes), PAGE_LIMIT)
            if not more: break
            nodes += more
        return nodes

    def metadata(self, key, object_id):
        if object_id == 'AI:': return self.top(key, object_id)
        speaker = self.player(key, object_id)
        try:
            entries, _ = self.request(speaker, object_id, 'BrowseMetadata')
        except ContentError as error:
            if error.code == 701 and object_id in TOP: return self.top(key, object_id)
            raise
        if not entries: raise ContentError(701, 'No such object: %s' % object_id)
        return self.node_from(entries[0], speaker)

    def browse(self, key, udn, object_id, flag, start=0, count=0):
        """(DIDL-Lite, NumberReturned, TotalMatches), paged by the player itself."""
        if flag == 'BrowseMetadata':
            return self.didl([self.metadata(key, object_id)], udn), 1, 1
        nodes, total = self.page(key, object_id, start, count)
        return self.didl(nodes, udn), len(nodes), total

    def resolve(self, uri, metadata=''):
        candidates = []
        match = re.search(r'<(?:item|container)\b[^>]*?\bid="([^"]*)"', metadata or '')
        if match: candidates.append(html.unescape(match.group(1)))
        if uri.startswith('x-rincon-playlist:') and '#' in uri: candidates.append(uri.split('#', 1)[1])
        if uri.startswith(RADIO_PREFIX): candidates.append(unquote(uri[len(RADIO_PREFIX):]))
        with self.lock:
            if uri in self.by_uri: candidates.append(self.by_uri[uri])
            for object_id in candidates:
                if object_id in self.nodes: return self.nodes[object_id]
        # Not in the registry (bridge restarted since the CR100 browsed it): a track's own URI
        # plays as it is; anything else the player can still describe.
        if uri.startswith(PLAIN_SCHEMES):
            return SonosNode(uri, '', '', uri, 'item', False, TRACK_CLASS, uri=uri, res=uri)
        speaker = self.backend.any_speaker()
        for object_id in candidates:
            if not speaker or not object_id.startswith(LIBRARY_PREFIXES + ('FV:2/',)): continue
            try:
                entries, _ = self.request(speaker, object_id, 'BrowseMetadata')
            except ContentError:
                continue
            if entries: return self.node_from(entries[0], speaker)
        return None
