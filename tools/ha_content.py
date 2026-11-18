"""Home Assistant content for the CR100's Music menu (a content.ContentSource): browse
through HA's media browser, play through HA.

Home Assistant's Sonos media browser uses the CR100's own Sonos IDs for favourites, the
library and playlists, so most object IDs are also Home Assistant media_content_ids (a
Node's ref; its kind is the media_content_type). Anything else (Radio Browser stations;
library tracks, whose HA ID is their URI) gets an ID derived under its parent.
Playing follows content.py's URI conventions; Home Assistant plays PLAIN_SCHEMES URIs as
they are.
"""
from __future__ import annotations

import dataclasses
import collections
import html
import re
from urllib.parse import quote, unquote

from content import (BROADCAST_CLASS, CACHE_TTL, NODE_LIMIT, PLAIN_SCHEMES, RADIO_PREFIX, TRACK_CLASS, ContentError,
                     ContentSource, Node, derived_id)
from sonos_queue_art import SonosQueueArt
from room_capabilities import RoomBinding

QUEUE_TTL = 2.0           # the CR100 asks for Q:0 metadata many times in a row
BROWSE_TIMEOUT = 30

# CR100 object ID -> (title, parent, Home Assistant media_content_id, media_content_type)
TOP = {
    'FV:2': ('Sonos Favorites', 'FV:', None, None),          # flattened from HA's folders
    'A:': ('Music Library', '0', 'library', 'library'),
    'A:ARTIST': ('Contributing Artists', 'A:', 'A:ARTIST', 'contributing_artist'),
    'A:ALBUMARTIST': ('Artists', 'A:', 'A:ALBUMARTIST', 'artist'),
    'A:ALBUM': ('Albums', 'A:', 'A:ALBUM', 'album'),
    'A:GENRE': ('Genres', 'A:', 'A:GENRE', 'genre'),
    'A:COMPOSER': ('Composers', 'A:', 'A:COMPOSER', 'composer'),
    'A:TRACKS': ('Tracks', 'A:', 'A:TRACKS', 'track'),
    'A:PLAYLISTS': ('Playlists', 'A:', 'A:PLAYLISTS', 'playlist'),
    'SQ:': ('Sonos Playlists', '0', 'SQ:', 'playlist'),
    'S:': ('Folders', '0', 'S:', 'directory'),
    'R:0/0': ('Radio Stations', 'R:0', 'media-source://radio_browser', 'music'),
    'AI:': ('Line-In', '0', None, None),
    'Q:0': ('Queue', 'Q:', None, None),
}
# Library levels below each category, for IDs the registry no longer has (bridge restart).
LIBRARY_LEVELS = {'ALBUM': ['album'], 'ALBUMARTIST': ['artist', 'album'], 'ARTIST': ['contributing_artist', 'album'],
                  'GENRE': ['genre', 'artist', 'album'], 'COMPOSER': ['composer', 'album'],
                  'TRACKS': ['track'], 'PLAYLISTS': ['playlist']}
NATIVE_PREFIXES = ('A:', 'SQ:', 'S:', 'FV:')
CONTAINER_CLASS = {'album': 'object.container.album.musicAlbum', 'artist': 'object.container.person.musicArtist',
                   'contributing_artist': 'object.container.person.musicArtist',
                   'composer': 'object.container.person.composer', 'genre': 'object.container.genre.musicGenre',
                   'playlist': 'object.container.playlistContainer'}


def guess(object_id):
    """(media_content_id, media_content_type) from the shape of a Sonos ID alone."""
    if object_id.startswith('FV:2/'): return object_id, 'favorite_item_id'
    if object_id.startswith('SQ:'): return object_id, 'playlist'
    if object_id.startswith('A:'):
        category, _, rest = object_id[2:].partition('/')
        levels = LIBRARY_LEVELS.get(category)
        if levels and rest: return object_id, levels[min(rest.count('/'), len(levels) - 1)]
    return None


class Content(ContentSource):
    """The household's content, browsed through any Sonos room's media_player entity
    (the room key is its entity_id)."""

    def __init__(self, ha, art=None, log=print, binding=None):
        super().__init__(art, log)
        self.ha = ha
        self.binding = binding or (lambda entity: RoomBinding())
        self.generic_owners = collections.OrderedDict()
        self.queue_art = SonosQueueArt(ha, log) if art else None

    # --- Home Assistant -------------------------------------------------------------
    def browse_ha(self, entity, ha_id=None, ha_type=None):
        try:
            attributes = (self.ha.state(entity).get('attributes') or {})
            if not int(attributes.get('supported_features') or 0) & 131072:
                raise ContentError(720, 'Media browsing is unavailable for this Home Assistant player')
            request = {'type': 'media_player/browse_media', 'entity_id': entity}
            if ha_id is not None:
                request.update(media_content_id=ha_id, media_content_type=ha_type)
            return self.ha.call(request, BROWSE_TIMEOUT)
        except Exception as error:
            self.log({'event': 'browse-failed', 'entity': entity, 'id': ha_id, 'error': str(error)})
            raise ContentError(720, 'Cannot process the request: %s' % error) from error

    def queue(self, entity):
        """The room's queue as Q:0/<n> items (1-based, as the CR100 numbers tracks)."""
        provider = getattr(self, 'queue_provider', None)
        entries = provider(entity) if callable(provider) else None
        if entries is not None:   # a bridge-owned queue (non-Sonos player)
            return [Node('Q:0/%d' % number, 'Q:0', entry.title, entry.ref, 'track', False, TRACK_CLASS,
                         uri=entry.ref, artist=entry.artist, album=entry.album, thumbnail=entry.art)
                    for number, entry in enumerate(entries, 1)]
        if not self.binding(entity).is_sonos:
            raise ContentError(720, 'Native queue browsing is unavailable for this Home Assistant player')
        nodes = self.cached((entity, 'Q:0'), QUEUE_TTL)
        if nodes is not None: return nodes
        try:
            response = self.ha.call_service('sonos', 'get_queue', entity, response=True, timeout=BROWSE_TIMEOUT) or {}
        except Exception as error:
            self.log({'event': 'queue-failed', 'entity': entity, 'error': str(error)})
            raise ContentError(720, 'Cannot process the request: %s' % error) from error
        tracks = response.get(entity) or next(iter(response.values()), None) or []
        nodes = [Node('Q:0/%d' % number, 'Q:0', track.get('media_title') or '', track.get('media_content_id'), 'track',
                      False, TRACK_CLASS, uri=track.get('media_content_id') or '',
                      artist=track.get('media_artist') or '', album=track.get('media_album_name') or '',
                      thumbnail=next((track[k] for k in ('thumbnail', 'media_image_url', 'album_art_uri')
                                      if isinstance(track.get(k), str) and track[k]), ''))
                 for number, track in enumerate(tracks, 1)]
        self.store((entity, 'Q:0'), nodes)
        return nodes

    def decorate_queue(self, entity, nodes, start):
        """Recover artwork only for rows being returned, including hover metadata."""
        if not self.queue_art or not nodes or all(node.thumbnail for node in nodes): return nodes
        if not self.binding(entity).is_sonos: return nodes   # bridge queue rows carry their own art
        # Current-track proxy art changes as playback advances. Keep it out of
        # the cached queue nodes so a later hover cannot inherit another track's
        # image. Native page results have their own URI-validated helper cache.
        nodes = [dataclasses.replace(node) for node in nodes]
        pictures = self.queue_art.lookup(entity, [node.uri for node in nodes], start)
        for index, picture in pictures.items():
            if not nodes[index].thumbnail: nodes[index].thumbnail = picture
        # A current-track HA proxy is useful when the native endpoint is absent,
        # but never put the playing track's image on other queue entries.
        attributes = (self.ha.state(entity).get('attributes') or {})
        position = attributes.get('queue_position')
        if type(position) is not int or not start < position <= start + len(nodes): return nodes
        node = nodes[position - start - 1]
        current_uri = attributes.get('media_content_id')
        if current_uri != node.uri or not current_uri: return nodes
        picture = attributes.get('entity_picture')
        if not node.thumbnail and isinstance(picture, str): node.thumbnail = picture
        return nodes

    def node_from(self, parent, child, favourite=False, scope=None):
        ha_id, ha_type = child['media_content_id'], child['media_content_type']
        # A favourite keeps an FV:2 ID even when it is a playlist (SQ:2): the same playlist
        # under Sonos Playlists is a container and must not be confused with it.
        native = not scope and (ha_id.startswith('FV:2/') if favourite else ha_id.startswith(NATIVE_PREFIXES))
        object_id = ha_id if native else derived_id(parent + ('@' + scope if scope else ''), ha_id, ha_type)
        media_class = child.get('media_class') or ''
        # Favourites play at once, whatever they are (Home Assistant replaces the queue for them).
        container = bool(child.get('can_expand')) and not favourite
        node = Node(object_id, parent, child.get('title') or '', ha_id, ha_type, container,
                    CONTAINER_CLASS.get(media_class, 'object.container') if container
                    else TRACK_CLASS if media_class == 'track' and not favourite else BROADCAST_CLASS,
                    playable=bool(child.get('can_play')), thumbnail=child.get('thumbnail') or '')
        if not container:
            node.uri = ha_id if node.upnp_class == TRACK_CLASS and ha_id.startswith(PLAIN_SCHEMES) \
                else RADIO_PREFIX + quote(object_id, safe='')
        return node

    def favourites(self, entity):
        root = self.browse_ha(entity, '', 'favorites')
        found = []
        for folder in root.get('children') or []:
            if folder.get('can_expand'):
                found += self.browse_ha(entity, folder['media_content_id'], folder['media_content_type']).get('children') or []
            else:
                found.append(folder)
        nodes, seen = [], set()
        for child in found:
            node = self.node_from('FV:2', child, favourite=True)
            if node.object_id not in seen: seen.add(node.object_id); nodes.append(node)
        return nodes

    def lookup(self, object_id):
        """(media_content_id, media_content_type) to browse for a container ID."""
        if object_id in TOP and TOP[object_id][2] is not None: return TOP[object_id][2:]
        with self.lock: node = self.nodes.get(object_id)
        if node and node.container: return node.ref, node.kind
        found = guess(object_id)
        if found and found[1] != 'favorite_item_id': return found
        raise ContentError(701, 'No such object: %s' % object_id)

    def children(self, entity, object_id):
        if object_id == 'Q:0': return self.queue(entity)
        if not self.binding(entity).is_sonos:
            return self.generic_children(entity, object_id)
        nodes = self.cached(object_id, CACHE_TTL)
        if nodes is not None: return nodes
        if object_id == 'AI:': nodes = []
        elif object_id == 'FV:2': nodes = self.favourites(entity)
        else:
            ha_id, ha_type = self.lookup(object_id)
            result = self.browse_ha(entity, ha_id, ha_type)
            nodes = [self.node_from(object_id, child) for child in result.get('children') or []]
        for node in nodes: self.register(node)
        self.store(object_id, nodes)
        return nodes

    def generic_children(self, entity, object_id):
        """HA's opaque browse tree, without inventing Sonos catalog categories."""
        if object_id == 'AI:': return []
        with self.lock:
            node = self.nodes.get(object_id)
            owner = self.generic_owners.get(object_id)
        if object_id != 'A:' and (owner != entity or not node or not node.container):
            raise ContentError(701, 'This player has no such media collection')
        key = ('ha-browser', entity, object_id)
        nodes = self.cached(key, CACHE_TTL)
        if nodes is not None: return nodes
        result = self.browse_ha(entity) if object_id == 'A:' else self.browse_ha(entity, node.ref, node.kind)
        nodes = [self.node_from(object_id, child, scope=entity) for child in result.get('children') or []]
        for child in nodes:
            self.register(child)
            with self.lock:
                self.generic_owners[child.object_id] = entity
                self.generic_owners.move_to_end(child.object_id)
                while len(self.generic_owners) > NODE_LIMIT: self.generic_owners.popitem(last=False)
        self.store(key, nodes)
        return nodes

    # --- CR100 side -------------------------------------------------------------------
    def metadata(self, entity, object_id):
        if not self.binding(entity).is_sonos and not object_id.startswith('Q:'):
            if object_id == 'AI:':
                return Node('AI:', '0', 'Line-In', None, None, True, 'object.container', child_count=0)
            if object_id == 'A:':
                return Node('A:', '0', 'Home Assistant Media', None, None, True, 'object.container',
                            child_count=len(self.generic_children(entity, object_id)))
            with self.lock:
                node = self.nodes.get(object_id)
                owner = self.generic_owners.get(object_id)
            if node and owner == entity: return node
            raise ContentError(701, 'This player has no such media collection')
        if object_id in TOP:
            title, parent, _, _ = TOP[object_id]
            count = len(self.children(entity, object_id))
            return Node(object_id, parent, title, None, None, True, 'object.container', child_count=count)
        if object_id.startswith('Q:0/'):
            for node in self.queue(entity):
                if node.object_id == object_id: return node
        with self.lock: node = self.nodes.get(object_id)
        if node: return node
        raise ContentError(701, 'No such object: %s' % object_id)

    def resolve(self, uri, metadata=''):
        """The Node the CR100 means by a URI (and its DIDL) it was given, or None."""
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
        # plays as it is; a Sonos ID's shape still says what it is.
        if uri.startswith(PLAIN_SCHEMES): return Node(uri, '', '', uri, 'music', False, TRACK_CLASS, uri=uri)
        for object_id in candidates:
            found = guess(object_id)
            if found: return Node(object_id, '', '', found[0], found[1], found[1] != 'favorite_item_id', '')
        return None
