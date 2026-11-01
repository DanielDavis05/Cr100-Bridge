"""Spotify catalogue exposed through the original CR100's SMAPI 1.1 interface.

Spotify supplies metadata only. Playback is routed by the virtual player to a
real Sonos player; this service neither downloads nor invents audio streams.
OAuth remains on the bridge, while the legacy controller sees an anonymous
service. Request logging deliberately excludes SOAP headers and credentials.
"""
from __future__ import annotations

import collections
from concurrent.futures import Future
import hashlib
import io
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from smapi_stub import SMAPI_NS, SOAP_NS, SmapiStub, collection, text
from spotify_client import SpotifyError
from spotify_playback import owns, resolve_playback


ID = re.compile(r'^spotify:(track|album|artist|playlist):([A-Za-z0-9]{22})$')
SEARCH = {'tracks': 'track', 'albums': 'album', 'artists': 'artist', 'playlists': 'playlist'}
PAGE_LIMIT = 50
SEARCH_LIMIT = 10
SEARCH_WINDOW = 50
SEARCH_SNAPSHOT_TTL = 300
SEARCH_SNAPSHOT_LIMIT = 64
CACHE_TTL = 30
POLL_INTERVAL = 300
CACHE_LIMIT = 128
NODE_LIMIT = 4000
ART_LIMIT = 128
ART_BYTES = 4 * 1024 * 1024
ART_PIXELS = 16_000_000
IMAGE_HOSTS = frozenset(('i.scdn.co', 'mosaic.scdn.co', 'image-cdn-ak.spotifycdn.com',
                         'image-cdn-fa.spotifycdn.com'))


class CatalogueError(Exception):
    """A safe message that may be displayed by the legacy controller."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Spotify image URLs are direct. Never allow a permitted image host to
        # redirect the bridge into a local address or another unchecked host.
        raise ValueError('Image redirects are not supported')


class SpotifyService(SmapiStub):
    def __init__(self, host, port, client, service_id=240, name='Spotify', log=print, capabilities='1'):
        self.client = client
        self.lock = threading.RLock()
        self.cache = collections.OrderedDict()
        self.search_snapshots = collections.OrderedDict()
        self.search_flights = {}
        self.catalog_version = 1
        self.images = collections.OrderedDict()
        self.image_sources = collections.OrderedDict()
        super().__init__(host, port, service_id, name, log, capabilities)
        self.nodes = collections.OrderedDict()
        self.root_nodes = [collection('library:playlists', 'My Playlists'),
                           collection('library:albums', 'Saved Albums'),
                           collection('library:tracks', 'Liked Songs')]
        self.search_nodes = [collection(key, key.title(), 'search') for key in SEARCH]
        self._remember(collection('root', name), collection('search', 'Search'),
                       *self.root_nodes, *self.search_nodes)

    def make_art(self):
        return None

    def descriptor(self):
        values = super().descriptor()
        values['AvailableServiceListVersion'] = '3'
        services = ET.fromstring(values['AvailableServiceDescriptorList'])
        services.find('./Service/Policy').set('PollInterval', str(POLL_INTERVAL))
        values['AvailableServiceDescriptorList'] = ET.tostring(services, encoding='unicode')
        return values

    def owns(self, uri, metadata=''):
        return owns(uri, metadata, service_id=self.service_id)

    def resolve_playback(self, uri, metadata=''):
        return resolve_playback(uri, metadata, service_id=self.service_id)

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log({'event': 'spotify-service', 'uri': self.uri, 'id': self.service_id,
                  'type': self.service_type})

    def _remember(self, *nodes):
        with self.lock:
            for node in nodes:
                self.nodes[node['id']] = node
                self.nodes.move_to_end(node['id'])
            while len(self.nodes) > NODE_LIMIT:
                self.nodes.popitem(last=False)

    def _get(self, path, params=None):
        key = (path, tuple(sorted((params or {}).items())))
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                self.cache.move_to_end(key)
                return hit[1]
        result = self.client.get(path, params=params)
        if not isinstance(result, dict):
            raise CatalogueError('Spotify returned an unreadable response. Try again.')
        with self.lock:
            self.cache[key] = (time.monotonic(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > CACHE_LIMIT:
                self.cache.popitem(last=False)
        return result

    @staticmethod
    def _paging(args, limit=PAGE_LIMIT):
        try:
            index, count = int(args.get('index') or 0), int(args.get('count') or limit)
        except (TypeError, ValueError):
            raise CatalogueError('Invalid browse page.') from None
        if index < 0 or count < 0 or index > 1_000_000:
            raise CatalogueError('Invalid browse page.')
        return index, min(count or limit, limit)

    def _page(self, nodes, index, total, wrapper):
        self._remember(*nodes)
        return ('<%s><index>%d</index><count>%d</count><total>%d</total>%s</%s>'
                % (wrapper, index, len(nodes), total, ''.join(self.entry(n) for n in nodes), wrapper))

    def _api_page(self, path, args, kind, wrapper='getMetadataResult', params=None, album=None):
        index, count = self._paging(args)
        data = self._get(path, {**(params or {}), 'offset': index, 'limit': count})
        return self._result(data, kind, index, wrapper, album=album)

    def _result(self, data, kind, index, wrapper, album=None):
        items, total = data.get('items'), data.get('total')
        if not isinstance(items, list) or not isinstance(total, int) or total < 0:
            raise CatalogueError('Spotify cannot provide this list. Check access in bridge setup.')
        nodes = []
        for raw in items:
            item = raw
            if kind == 'playlist_item':
                item = (raw.get('item') or raw.get('track')) if isinstance(raw, dict) else None
            elif kind in ('saved_track', 'saved_album'):
                item = raw.get(kind[6:]) if isinstance(raw, dict) else None
            expected = 'track' if kind in ('playlist_item', 'saved_track') else 'album' if kind == 'saved_album' else kind
            # Preserve truthful SMAPI counts and offsets. Silently dropping null,
            # local, or episode entries can make the CR100 repeat the same page.
            # A mixed/unavailable page therefore has an explicit error instead.
            nodes.append(self._node(item, expected, album=album))
        return self._page(nodes, index, total, wrapper)

    def _node(self, item, kind=None, album=None):
        if not isinstance(item, dict):
            raise CatalogueError('This Spotify list contains unavailable items.')
        kind = kind or item.get('type')
        item_id = item.get('id')
        if kind not in ('track', 'album', 'artist', 'playlist') or not isinstance(item_id, str) \
                or not re.fullmatch('[A-Za-z0-9]{22}', item_id) or item.get('is_local'):
            raise CatalogueError('This Spotify list contains an unsupported or unavailable item.')
        if item.get('type') and item['type'] != kind:
            raise CatalogueError('This Spotify list contains an unsupported item type.')
        uri = 'spotify:%s:%s' % (kind, item_id)
        title = item.get('name')
        if not isinstance(title, str):
            raise CatalogueError('Spotify returned an item without a title.')
        artists = item.get('artists') or []
        artist = ', '.join(a['name'] for a in artists if isinstance(a, dict) and isinstance(a.get('name'), str))
        if kind == 'track':
            album = item.get('album') or album or {}
            node = {'id': uri, 'itemType': 'track', 'title': title, 'mimeType': 'audio/mpeg',
                    'artist': artist, 'album': album.get('name') or '',
                    'albumId': 'spotify:album:' + album['id'] if re.fullmatch('[A-Za-z0-9]{22}', album.get('id') or '') else '',
                    'duration': max(0, int(item.get('duration_ms') or 0) // 1000),
                    'canPlay': item.get('is_playable', True) is not False,
                    'image': self._image_url(album.get('images') or [])}
        else:
            node = collection(uri, title, kind, can_play=kind in ('album', 'playlist'), artist=artist)
            node['image'] = self._image_url(item.get('images') or [])
        return node

    def node(self, item_id):
        with self.lock:
            node = self.nodes.get(item_id)
            if node:
                self.nodes.move_to_end(item_id)
                return node
        match = ID.fullmatch(item_id or '')
        if not match:
            raise LookupError('No such Spotify item.')
        kind, ident = match.groups()
        node = self._node(self._get('/%ss/%s' % (kind, ident)), kind)
        self._remember(node)
        return node

    def get_metadata(self, args):
        item_id = args.get('id') or ''
        index, count = self._paging(args)
        if item_id in ('root', 'search'):
            # Capabilities=1 makes the CR100 add Search itself to the root menu.
            nodes = self.root_nodes if item_id == 'root' else self.search_nodes
            return self._page(nodes[index:index + count], index, len(nodes), 'getMetadataResult')
        libraries = {'library:playlists': ('/me/playlists', 'playlist'),
                     'library:albums': ('/me/albums', 'saved_album'),
                     'library:tracks': ('/me/tracks', 'saved_track')}
        if item_id in libraries:
            path, kind = libraries[item_id]
            return self._api_page(path, args, kind)
        match = ID.fullmatch(item_id)
        if not match:
            raise LookupError('No such Spotify collection.')
        kind, ident = match.groups()
        if kind == 'album':
            # A cached search result already carries album title/art; no per-track
            # requests. Only direct IDs need the single album metadata lookup.
            album_node = self.node(item_id)
            album = {'id': ident, 'name': album_node['title'],
                     'images': [{'url': album_node['image']}] if album_node.get('image') else []}
            return self._api_page('/albums/%s/tracks' % ident, args, 'track', album=album)
        if kind == 'artist':
            return self._api_page('/artists/%s/albums' % ident, args, 'album',
                                  params={'include_groups': 'album,single'})
        if kind == 'playlist':
            return self._api_page('/playlists/%s/items' % ident, args, 'playlist_item')
        raise LookupError('A track is not a browsable collection.')

    def search(self, args):
        category = args.get('id') or ''
        kind = SEARCH.get(category)
        if not kind:
            raise LookupError('No such Spotify search category.')
        index, count = self._paging(args, SEARCH_WINDOW)
        term = (args.get('term') or '').strip()
        if not term:
            return self._page([], index, 0, 'searchResult')
        if len(term) > 500:
            raise CatalogueError('Search text is too long.')
        nodes = self._search_snapshot(category, term, refresh=index == 0)
        return self._page(nodes[index:index + count], index, len(nodes), 'searchResult')

    def _search_snapshot(self, category, term, refresh=True):
        """A stable first-50-slot search window, shared by concurrent requests.

        Spotify's search total can grow with every requested offset. Passing it
        to the CR100 makes its initial 500-item cache fill chase a moving end.
        Materialize this bounded window, remove unavailable/duplicate entries,
        then expose the exact size of that window as the SMAPI total. Users can
        refine their search to find items outside the first 50 Spotify slots.
        """
        key = (category, term)
        with self.lock:
            hit = self.search_snapshots.get(key)
            # A continuation page keeps the original snapshot even if the TTL
            # elapsed while the user was reading it. A new search at index 0
            # can refresh the window after five minutes.
            if hit and (not refresh or time.monotonic() - hit[0] < SEARCH_SNAPSHOT_TTL):
                self.search_snapshots.move_to_end(key)
                return hit[1]
            future = self.search_flights.get(key)
            owner = future is None
            if owner:
                future = Future()
                self.search_flights[key] = future
        if not owner:
            return future.result()
        try:
            nodes, seen, offset, pages, raw_count = [], set(), 0, 0, 0
            for _ in range(SEARCH_WINDOW // SEARCH_LIMIT):
                data = self._get('/search', {'q': term, 'type': SEARCH[category],
                                             'offset': offset, 'limit': SEARCH_LIMIT})
                page = data.get(category)
                items = page.get('items') if isinstance(page, dict) else None
                if not isinstance(items, list):
                    raise CatalogueError('Spotify could not return these search results.')
                pages += 1
                items = items[:min(SEARCH_LIMIT, SEARCH_WINDOW - raw_count)]
                raw_count += len(items)
                for item in items:
                    try:
                        node = self._node(item, SEARCH[category])
                    except (CatalogueError, AttributeError, TypeError, ValueError, OverflowError):
                        continue
                    if node['id'] not in seen:
                        seen.add(node['id'])
                        nodes.append(node)
                # Neither the provider's total nor next URL is trusted as a
                # global item count or a URL to fetch. Follow fixed API offsets
                # only while the provider says there is a next page.
                if not items or not page.get('next') or raw_count >= SEARCH_WINDOW:
                    break
                offset += len(items)
            with self.lock:
                self.search_snapshots[key] = (time.monotonic(), nodes)
                self.search_snapshots.move_to_end(key)
                while len(self.search_snapshots) > SEARCH_SNAPSHOT_LIMIT:
                    self.search_snapshots.popitem(last=False)
            self.log({'event': 'spotify-search-window', 'category': category,
                      'api_pages': pages, 'raw_slots': raw_count, 'results': len(nodes)})
            future.set_result(nodes)
            return nodes
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.search_flights.pop(key, None)

    def invalidate_catalog(self):
        """Explicitly invalidate controller caches after a catalogue mutation."""
        with self.lock:
            self.catalog_version += 1
            self.cache.clear()
            self.search_snapshots.clear()

    def dispatch(self, method, args):
        if method == 'getLastUpdate':
            # A clock-based version invalidates active native search caches
            # while they are being filled. Reads leave this sequence stable;
            # explicit catalogue mutations may call invalidate_catalog().
            with self.lock:
                version = str(self.catalog_version)
            return ('<getLastUpdateResult><catalog>%s</catalog><favorites>%s</favorites>'
                    '<pollInterval>%d</pollInterval></getLastUpdateResult>'
                    % (version, version, POLL_INTERVAL))
        if method == 'getMediaURI':
            raise CatalogueError('Play this item through a Sonos room linked to Spotify.')
        if method == 'getSessionId':
            raise CatalogueError('Connect Spotify in bridge setup.')
        return super().dispatch(method, args)

    @staticmethod
    def _image_url(images):
        candidates = [i for i in images if isinstance(i, dict) and isinstance(i.get('url'), str)]
        # Spotify normally returns largest first. Prefer a small original before
        # converting it to the CR100's 200-pixel baseline JPEG.
        candidates.sort(key=lambda i: i.get('width') or 10000)
        for candidate in candidates:
            try:
                uri = urlsplit(candidate['url'])
                if uri.scheme == 'https' and uri.hostname in IMAGE_HOSTS and uri.port in (None, 443) \
                        and not uri.username and not uri.password and not uri.fragment:
                    return candidate['url']
            except ValueError:
                continue
        return ''

    def _art_path(self, source):
        if not source:
            return ''
        key = hashlib.sha256(source.encode()).hexdigest()[:24]
        with self.lock:
            self.image_sources[key] = source
            self.image_sources.move_to_end(key)
            while len(self.image_sources) > ART_LIMIT:
                self.image_sources.popitem(last=False)
        return 'http://%s:%d/art/%s.jpg' % (self.host, self.port, key)

    def entry(self, node, wrap=True):
        art = self._art_path(node.get('image'))
        image_field = '<albumArtURI>%s</albumArtURI>' % text(art) if art else ''
        fields = '<id>%s</id><itemType>%s</itemType><title>%s</title>' % (
            text(node['id']), node['itemType'], text(node['title']))
        playable = 'true' if node.get('canPlay') else 'false'
        if node['itemType'] == 'track':
            fields += ('<mimeType>audio/mpeg</mimeType><trackMetadata><artist>%s</artist><album>%s</album>'
                       '<albumId>%s</albumId><duration>%d</duration>%s<canPlay>%s</canPlay>'
                       '<canSkip>true</canSkip><canAddToFavorites>false</canAddToFavorites></trackMetadata>'
                       % (text(node['artist']), text(node['album']), text(node['albumId']), node['duration'],
                          image_field, playable))
            tag = 'mediaMetadata'
        else:
            fields += ('<canPlay>%s</canPlay><canEnumerate>true</canEnumerate><canScroll>false</canScroll>'
                       '<canAddToFavorites>false</canAddToFavorites>%s' % (playable, image_field))
            tag = 'mediaCollection'
        return '<%s>%s</%s>' % (tag, fields, tag) if wrap else fields

    def _fetch_art(self, source):
        from PIL import Image
        if not self._image_url([{'url': source}]):
            raise ValueError('Unapproved image source')
        request = urllib.request.Request(source, headers={'User-Agent': 'CR100-Bridge/1.0'})
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if not response.headers.get('Content-Type', '').lower().startswith('image/'):
                raise ValueError('Not an image')
            if int(response.headers.get('Content-Length') or 0) > ART_BYTES:
                raise ValueError('Image too large')
            data = response.read(ART_BYTES + 1)
        if len(data) > ART_BYTES:
            raise ValueError('Image too large')
        try:
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > ART_PIXELS:
                    raise ValueError('Image dimensions too large')
                picture.thumbnail((200, 200))
                output = io.BytesIO()
                picture.convert('RGB').save(output, 'JPEG', quality=85, progressive=False)
        except Image.DecompressionBombError:
            raise ValueError('Image dimensions too large') from None
        return output.getvalue()

    def handle_get(self, request):
        match = re.fullmatch(r'/art/([a-f0-9]{24})\.jpg', urlsplit(request.path).path)
        if not match:
            self.reply(request, 404, b'Not found', 'text/plain')
            return
        key = match[1]
        with self.lock:
            image = self.images.get(key)
            source = self.image_sources.get(key)
        if image is None and source:
            try:
                image = self._fetch_art(source)
            except (OSError, ValueError, ImportError):
                image = None
            if image:
                with self.lock:
                    self.images[key] = image
                    self.images.move_to_end(key)
                    while len(self.images) > ART_LIMIT:
                        self.images.popitem(last=False)
        if image:
            self.reply(request, 200, image, 'image/jpeg')
        else:
            self.reply(request, 404, b'Not found', 'text/plain')

    def handle_post(self, request):
        method, args = '', {}
        try:
            length = int(request.headers.get('Content-Length') or 0)
            if not 0 < length <= 1 << 20:
                request.close_connection = True
                raise CatalogueError('Invalid request.')
            raw = request.rfile.read(length)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise CatalogueError('Invalid request.')
            envelope = ET.fromstring(raw)
            body = envelope.find('{%s}Body' % SOAP_NS)
            if envelope.tag != '{%s}Envelope' % SOAP_NS or body is None or len(body) != 1:
                raise CatalogueError('Invalid request.')
            call = body[0]
            if not call.tag.startswith('{%s}' % SMAPI_NS):
                raise CatalogueError('Invalid request.')
            method = call.tag.rpartition('}')[2]
            args = {child.tag.rpartition('}')[2]: child.text or '' for child in call}
            result = self.dispatch(method, args)
            status, xml = 200, self.envelope('<%sResponse xmlns="%s">%s</%sResponse>'
                                            % (method, SMAPI_NS, result, method))
        except SpotifyError as error:
            code = 'Server.ServiceUnavailable'
            if error.status in (401, 403):
                # Auth is Anonymous on the controller; LoginInvalid would send
                # it into its legacy account repair flow. OAuth is repaired on
                # the bridge, not through a CR100 username/password dialog.
                message = 'Check Spotify authorization and access in bridge setup.'
            elif error.status == 429:
                message = 'Spotify is busy. Wait a little and try again.'
            elif error.status == 404:
                code, message = 'Client.ItemNotFound', 'This Spotify item is unavailable.'
            else:
                message = 'Spotify is unavailable. Try again shortly.'
            status, xml = 500, self.fault(code, message)
        except (ET.ParseError, IndexError, AttributeError, TypeError, ValueError, OverflowError, CatalogueError) as error:
            message = str(error) if isinstance(error, CatalogueError) else 'Invalid request or Spotify response.'
            status, xml = 500, self.fault('Client.InvalidRequest', message)
        except LookupError:
            status, xml = 500, self.fault('Client.ItemNotFound', 'This Spotify item is unavailable.')
        except NotImplementedError:
            status, xml = 500, self.fault('Server.NotImplemented', 'This action is unavailable.')
        self.log({'event': 'spotify-smapi', 'method': method, 'status': status})
        self.reply(request, status, xml.encode())
