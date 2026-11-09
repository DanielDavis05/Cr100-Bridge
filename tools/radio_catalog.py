"""Typed native Radio catalog shared by both renderers and the management UI.

Each folder is one finite directory page. Continuations are ordinary folders;
opening one fetches exactly one upstream page, never an entire catalog crawl.
"""
from __future__ import annotations

import base64
import collections
import copy
import dataclasses
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json
import re
import threading
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from radio_store import RadioStore, DEFAULT_SETTINGS
from radiotime import (RadioTimeClient, RadioTimeError, RadioStream, merge_metadata,
                       outlines, unavailable_url)


@dataclasses.dataclass
class RadioItem:
    id: str
    title: str
    kind: str
    url: str = ''
    image: str = ''
    description: str = ''
    artist: str = ''
    album: str = ''
    duration: int = 0
    metadata: dict = dataclasses.field(default_factory=dict)

    @property
    def container(self):
        return self.kind in ('folder', 'show')

    @property
    def playable(self):
        return self.kind in ('station', 'custom', 'episode') and not self.metadata.get('unavailable')

    @property
    def uri(self):
        return 'x-cr100-radio:' + quote(self.id, safe=':') if self.playable else ''

    @property
    def guide_id(self):
        return self.metadata.get('guide_id') or (self.id.split(':', 1)[1] if ':' in self.id else '')

    def as_dict(self):
        return dataclasses.asdict(self)

    def to_node(self, parent='0'):
        from content import Node, BROADCAST_CLASS, TRACK_CLASS
        return Node(self.id, parent, self.title, self.id, self.kind, self.container,
                    'object.container' if self.container else TRACK_CLASS if self.kind == 'episode' else BROADCAST_CLASS,
                    playable=self.playable, uri=self.uri, artist=self.artist, album=self.album, thumbnail=self.image)


@dataclasses.dataclass
class CatalogPage:
    items: list
    total: int
    title: str = ''
    object_id: str = '0'
    start: int = 0

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def as_dict(self):
        return dict(items=[item.as_dict() for item in self.items], total=self.total,
                    title=self.title, object_id=self.object_id, start=self.start)


ROOT_TITLES = {'0': 'Radio', 'favorites': 'Favorites', 'favorites:stations': 'Radio Stations',
               'favorites:shows': 'Radio Shows', 'recent': 'Recently Played', 'search': 'Search',
               'local': 'Local Radio', 'music': 'Music', 'talk': 'Talk', 'sports': 'Sports',
               'location': 'Location', 'podcasts': 'Podcasts', 'language': 'By Language',
               'settings': 'Radio Settings', 'continue': 'Continue Listening'}
ROOT_TITLES['station-info'] = 'Station Information'
ROOT_REMOTE = {'music': {'c': 'music'}, 'talk': {'c': 'talk'}, 'sports': {'c': 'sports'},
               'location': {'id': 'r0'}, 'podcasts': {'c': 'podcast'}, 'language': {'c': 'lang'}}


def _encoded(payload):
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')


def _decoded(value):
    try:
        if len(value) > 16384:
            raise ValueError('Radio folder ID is too long')
        return json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
    except (ValueError, TypeError, UnicodeDecodeError) as error:
        raise RadioTimeError('Invalid Radio folder ID', 'id', 400) from error


def _guide(identity):
    return identity.split(':', 1)[1] if re.fullmatch(r'(?:station:s\d+|show:p\d+|episode:t\d+)', identity) else identity


def _clean_identity(identity):
    if not isinstance(identity, str):
        raise ValueError('Radio identity must be text')
    if identity.startswith('x-cr100-radio:'):
        from urllib.parse import unquote
        identity = unquote(identity[len('x-cr100-radio:'):])
    if re.fullmatch(r'[spt]\d+', identity):
        identity = {'s': 'station', 'p': 'show', 't': 'episode'}[identity[0]] + ':' + identity
    return identity


def _item_from_dict(data):
    fields = {field.name for field in dataclasses.fields(RadioItem)}
    return RadioItem(**{key: copy.deepcopy(value) for key, value in data.items() if key in fields})


class RadioCatalog:
    def __init__(self, client=None, store=None, log=None, zip_resolver=None, timezone='UTC'):
        self.client = client or RadioTimeClient()
        self.store = store or RadioStore()
        self.log = log or (lambda event: None)
        self.zip_resolver = zip_resolver or self.client.zip_location
        self.timezone = timezone
        self.lock = threading.RLock()
        self._items = collections.OrderedDict()
        self._views = collections.OrderedDict()
        self._cursor_chains = collections.OrderedDict()
        self._directory_revision = self.store.snapshot()['radio_directory_update_id']

    def _sync_revision(self, state=None):
        state = state or self.store.snapshot()
        revision = state['radio_directory_update_id']
        clear = False
        with self.lock:
            if revision != self._directory_revision:
                self._directory_revision = revision
                self._views.clear()
                self._cursor_chains.clear()
                clear = True
        if clear:
            self.client.clear_cache()
        return state

    def _register(self, item):
        with self.lock:
            old = self._items.get(item.id)
            if old:
                metadata = dict(old.metadata)
                metadata.update(item.metadata)
                item.metadata = metadata
            self._items[item.id] = copy.deepcopy(item)
            self._items.move_to_end(item.id)
            while len(self._items) > 20000:
                self._items.popitem(last=False)
        return item

    def root(self):
        settings = self.store.settings()
        location = settings.get('location') or {}
        local_title = 'Local Radio'
        label = location.get('label') or location.get('name') or location.get('city')
        if label:
            local_title += ' (%s)' % label
        ids = ['favorites', 'search', 'local', 'music', 'talk', 'sports', 'location']
        if settings.get('show_podcasts', True):
            ids.append('podcasts')
        if settings.get('show_language'):
            ids.append('language')
        ids.append('settings')
        return [self._register(RadioItem(identity, local_title if identity == 'local' else ROOT_TITLES[identity], 'folder'))
                for identity in ids]

    def _folder(self, url, title, metadata=None, section=None, chain=None, event=None):
        payload = {'url': self.client.url(url)}
        if section is not None:
            payload['section'] = section
        if chain is not None:
            payload['chain'] = chain
        if event is not None:
            payload['event'] = event
        return self._register(RadioItem('radio:' + _encoded(payload), title, 'folder',
                                       url=payload['url'], metadata=copy.deepcopy(metadata or {})))

    def _row(self, row, parent_url, index, section=(), chain=None):
        title = str(row.get('text') or row.get('name') or row.get('title') or '')
        target = row.get('URL') or row.get('url') or ''
        guide = str(row.get('guide_id') or '')
        if not guide:
            parsed_id = dict(parse_qsl(urlsplit(target).query)).get('id', '')
            if re.fullmatch(r'[spt]\d+', parsed_id):
                guide = parsed_id
        metadata = copy.deepcopy(row)
        if row.get('children') is not None:
            metadata.pop('children', None)
            return self._folder(parent_url, title, metadata, list(section) + [index])
        if row.get('type') == 'text':
            return self._register(RadioItem('message:' + _encoded({'url': parent_url, 'index': index, 'section': section}),
                                           title, 'message', description=str(row.get('subtext') or ''), metadata=metadata)) if title else None
        if re.fullmatch(r'[spt]\d+', guide):
            kind = {'s': 'station', 'p': 'show', 't': 'episode'}[guide[0]]
            metadata['guide_id'] = guide
            metadata['unavailable'] = row.get('key') == 'unavailable' or unavailable_url(target)
            image = row.get('playing_image') if kind == 'station' else ''
            image = image or row.get('image') or row.get('logo') or ''
            duration = row.get('topic_duration') or row.get('duration') or 0
            try:
                duration = int(float(duration))
            except (ValueError, TypeError):
                duration = 0
            return self._register(RadioItem(kind + ':' + guide, title, kind, url=target, image=image,
                                           description=str(row.get('subtext') or row.get('description') or ''),
                                           artist=str(row.get('current_artist') or ''),
                                           album=str(row.get('current_album') or ''), duration=duration, metadata=metadata))
        if row.get('type') == 'link' and target:
            if urlsplit(target).hostname != 'opml.radiotime.com':
                return None
            if row.get('key', '').startswith('next'):
                metadata['cursor'] = True
                target = self.client.url(target)
                with self.lock:
                    state = self._cursor_chains.get(chain or parent_url)
                    if target == parent_url or state and target in state['urls']:
                        self.log({'event': 'radio-cursor-loop', 'url': target})
                        return None
                return self._folder(target, title, metadata, chain=chain or parent_url)
            return self._folder(target, title, metadata)
        # Nonstation artist profiles still browse through real provider links.
        if row.get('type') == 'text' or not target:
            return self._register(RadioItem('message:' + _encoded({'url': parent_url, 'index': index, 'section': section}),
                                           title, 'message', description=str(row.get('subtext') or ''), metadata=metadata))
        return None

    def _convert(self, response, section=None, flatten_stations=True, chain=None):
        chain_id = chain or response.url + ('#section=' + json.dumps(section) if section else '')
        with self.lock:
            # A newly fetched first view starts a fresh chain. Section folders
            # have their own chain, so opening Shows cannot erase the station
            # IDs already seen before following More Stations.
            previous = self._cursor_chains.get(chain_id) if chain else None
            previous_ids = set(previous['ids']) if previous and chain else set()
            if previous is None:
                previous = self._cursor_chains[chain_id] = {'ids': set(), 'urls': set()}
            previous['urls'].add(response.url)
            self._cursor_chains.move_to_end(chain_id)
            while len(self._cursor_chains) > 512:
                self._cursor_chains.popitem(last=False)
        rows = response.body
        path = []
        for index in section or []:
            if not isinstance(index, int) or index < 0 or index >= len(rows):
                raise RadioTimeError('This Radio section is no longer available', 'section', 404)
            path.append(index)
            rows = rows[index].get('children') or []
        result, seen = [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if flatten_stations and row.get('key') == 'stations' and row.get('children') is not None:
                for child_index, child in enumerate(row['children']):
                    item = self._row(child, response.url, child_index, tuple(path + [index]), chain_id)
                    if item and item.id not in seen and item.id not in previous_ids:
                        seen.add(item.id)
                        result.append(item)
                continue
            item = self._row(row, response.url, index, tuple(path), chain_id)
            if item and item.id not in seen and item.id not in previous_ids:
                seen.add(item.id)
                result.append(item)
        with self.lock:
            previous['ids'].update(item.id for item in result if item.kind in ('station', 'show', 'episode'))
        return result

    def _popular_response(self, response):
        """Replace only station ordering, preserving show and genre navigation."""
        popular = next((row for row in outlines(response.body)
                        if row.get('key') == 'popular' and row.get('URL')), None)
        station_section = next((row for row in response.body if row.get('key') == 'stations' and 'children' in row), None)
        if not popular or station_section is None:
            return response
        ordered = self.client.request(popular['URL'])
        replacement = []
        for row in ordered.body:
            if row.get('key') == 'stations' and 'children' in row:
                replacement.extend(row['children'])
            elif row.get('type') == 'audio' or row.get('key', '').startswith('next'):
                replacement.append(row)
        replacement.extend(row for row in station_section['children']
                           if row.get('key') in ('popular', 'pivotLocation', 'pivotName'))
        response = copy.deepcopy(response)
        next(row for row in response.body if row.get('key') == 'stations')['children'] = replacement
        return response

    def _browse_params(self):
        settings = self.store.settings()
        params = {}
        language = settings.get('preferred_language')
        if language:
            if settings.get('language_filter'):
                self.validate_language(language)
                params['filter'] = language
            elif not language.startswith('l'):
                params['locale'] = language
        return params

    def language_choices(self):
        response = self.client.browse(c='lang', locale='en-US')
        result, seen = [], set()
        for row in response.body:
            guide = row.get('guide_id') or dict(parse_qsl(urlsplit(row.get('URL', '')).query)).get('filter')
            if guide and re.fullmatch(r'l\d+', guide) and guide not in seen:
                seen.add(guide)
                result.append({'id': guide, 'label': row.get('text') or guide})
        return sorted(result, key=lambda choice: choice['label'].casefold())

    def validate_language(self, identity):
        if identity == '':
            return identity
        if not isinstance(identity, str) or identity not in {choice['id'] for choice in self.language_choices()}:
            raise ValueError('Choose a preferred language from the available language list')
        return identity

    def location_page(self, region_id='r0', ancestors=None):
        """One geographic level for an explicit city picker, without station rows."""
        if not isinstance(region_id, str) or not re.fullmatch(r'r\d+', region_id):
            raise ValueError('Choose a location from the directory')
        if ancestors is not None and (not isinstance(ancestors, list) or
                                     any(not isinstance(value, str) or not re.fullmatch(r'r\d+', value) for value in ancestors)):
            raise ValueError('Invalid location navigation history')
        ancestors = set(ancestors or [])
        response = self.client.browse(id=region_id)
        choices, seen = [], set()
        has_stations = False
        for row in outlines(response.body):
            if row.get('guide_id', '').startswith('s') or row.get('item') == 'station':
                has_stations = True
            if row.get('type') != 'link' or not row.get('URL'):
                continue
            query = dict(parse_qsl(urlsplit(row['URL']).query))
            identity = query.get('id', '')
            if (not re.fullmatch(r'r\d+', identity) or identity == region_id or identity in ancestors
                    or query.get('filter') or query.get('pivot') or query.get('offset')
                    or row.get('key') in ('localCountry', 'parent', 'pivotLocation')):
                continue
            if identity not in seen:
                seen.add(identity)
                choices.append({'id': identity, 'label': row.get('text') or identity})
        choices.sort(key=lambda choice: choice['label'].casefold())
        return {'id': region_id, 'title': response.head.get('title', 'Location'),
                'locations': choices, 'selectable': region_id != 'r0' and not choices and has_stations}

    def _remote(self, object_id):
        if object_id.startswith('station-info:'):
            identity = object_id[len('station-info:'):]
            item = self.metadata(identity)
            if item.kind == 'custom':
                return [self._register(RadioItem('message:' + _encoded({'custom': identity}),
                                                item.description or 'Custom station; no provider schedule', 'message'))]
            response = self.client.options(_guide(identity))
            return [self._folder(row['URL'], row.get('text', row['key']), row)
                    for row in outlines(response.body)
                    if row.get('key') in ('nowplaying', 'schedule', 'related') and row.get('URL')]
        if object_id in ROOT_REMOTE:
            response = self.client.browse(**dict(ROOT_REMOTE[object_id], **self._browse_params()))
            items = self._convert(response)
            if object_id in ('music', 'talk', 'sports'):
                # These are taxonomy links; station/editorial collection contents
                # retain their provider ordering when opened.
                items.sort(key=lambda item: item.title.casefold())
            return items
        if object_id == 'local':
            location = self.store.settings().get('location') or {}
            if location.get('latlon'):
                response = self.client.local(location['latlon'], **self._browse_params())
            elif location.get('region_id') or location.get('id'):
                response = self.client.browse(id=location.get('region_id') or location['id'], **self._browse_params())
            else:
                response = self.client.local(**self._browse_params())
            items = self._convert(response, flatten_stations=False)
            return items
        if object_id.startswith('show:'):
            item = self.metadata(object_id)
            url = item.url or self.client.url('Tune.ashx', dict(c='pbrowse', id=_guide(object_id)))
            response = self.client.request(url)
            return self._convert(response)
        if object_id.startswith('radio:'):
            payload = _decoded(object_id[6:])
            if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
                raise RadioTimeError('Invalid Radio navigation ID', 'id', 400)
            url = payload['url']
            # The API has historically emitted broken language hub IDs. This
            # repair preserves the provider's language token and classifier.
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query))
            language_hubs = {'c424724': 'music', 'c424725': 'talk', 'c424726': 'sports'}
            if query.get('id') in language_hubs and re.fullmatch(r'l\d+', query.get('filter', '')):
                query['c'] = language_hubs[query.pop('id')]
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
            settings = self.store.settings()
            response = self.client.request(url)
            if query.get('c') == 'schedule':
                return self._schedule_items(response)
            if (query.get('id', '').startswith(('g', 'c')) and not query.get('filter') and not query.get('pivot')
                    and not payload.get('section') and settings['station_order'] == 'popular'):
                response = self._popular_response(response)
            items = self._convert(response, payload.get('section'), chain=payload.get('chain'))
            # Local broadcast sections preserve frequency ordering; this works
            # on both '88.5 | Name' and older 'Name 88.5' labels.
            with self.lock:
                registered = self._items.get(object_id)
            if registered and registered.title in ('FM', 'AM'):
                def frequency(item):
                    match = re.search(r'\d+(?:\.\d+)?', item.title)
                    return float(match.group()) if match else float('inf')
                items.sort(key=frequency)
            # Explicit alternatives are ordinary folders, never settings writes.
            if (query.get('id', '').startswith(('g', 'c')) and not payload.get('section')
                    and not query.get('pivot') and not query.get('offset') and not payload.get('chain')):
                extras = []
                supplied = {item.metadata.get('key') for item in items}
                if settings['station_order'] == 'name' and not query.get('filter'):
                    response = self.client.request(url, dict(pivot='name'))
                    return self._convert(response)
                # Only expose axes the API actually advertised in this response.
                # Original raw section wrappers can contain those links.
                for row in outlines(response.body):
                    key = row.get('key', '')
                    if key in ('popular', 'pivotLocation', 'pivotName') and row.get('URL') and key not in supplied:
                        extras.append(self._folder(row['URL'], row.get('text', ''), row))
                        supplied.add(key)
                items += extras
            return items
        raise RadioTimeError('No such Radio folder', 'id', 404)

    def children(self, object_id='0'):
        state = self._sync_revision()
        object_id = _clean_identity(object_id)
        if object_id == '0':
            return self.root()
        if object_id == 'favorites':
            return [self._register(RadioItem(identity, ROOT_TITLES[identity], 'folder'))
                    for identity in ('favorites:stations', 'favorites:shows', 'recent', 'continue')]
        if object_id.startswith('favorites:'):
            kind = 'show' if object_id == 'favorites:shows' else 'station'
            return [self._register(_item_from_dict(item)) for item in self.store.favorites(kind)]
        if object_id == 'recent':
            return [self._register(_item_from_dict(item)) for item in self.store.history()]
        if object_id == 'continue':
            marks = self.store.snapshot()['bookmarks'] if self.store.settings()['episode_resume'] else {}
            history = {item['id']: item for item in self.store.history()}
            result = []
            for identity, mark in sorted(marks.items(), key=lambda pair: pair[1].get('updated_at', 0), reverse=True):
                if mark.get('position', 0) <= 0 or mark.get('duration') and mark['position'] >= mark['duration']:
                    continue
                try:
                    item = _item_from_dict(history[identity]) if identity in history else self.metadata(identity)
                    item.metadata['bookmark'] = mark
                    result.append(self._register(item))
                except RadioTimeError:
                    continue
            return result
        if object_id == 'search':
            return [self._register(RadioItem('search:' + kind, title, 'folder'))
                    for kind, title in (('stations', 'Stations'), ('shows', 'Shows'), ('hosts', 'Hosts'))]
        if object_id.startswith('search:'):
            # The native adapter owns the keyboard and submits query text through search().
            return [self._register(RadioItem('search-recent:' + _encoded(query), query, 'action',
                                            metadata={'action': 'repeat_search', 'query': query,
                                                      'search_type': object_id.split(':', 1)[1]}))
                    for query in self.store.searches()]
        if object_id == 'settings':
            return self.settings_items()
        if object_id == 'station-info':
            result, seen = [], set()
            for saved in self.store.favorites('stations') + self.store.history():
                if saved.get('kind') not in ('station', 'custom') or saved['id'] in seen:
                    continue
                seen.add(saved['id'])
                result.append(self._register(RadioItem('station-info:' + saved['id'], saved['title'], 'folder',
                                                      image=saved.get('image', ''), metadata={'station_id': saved['id']})))
            return result
        if object_id.startswith('settings:'):
            return self.settings_choices(object_id.split(':', 1)[1])
        signature = json.dumps((state['settings'], state['radio_directory_update_id']), sort_keys=True)
        cache_key = (object_id, signature)
        with self.lock:
            found = self._views.get(cache_key)
            if found is not None:
                self._views.move_to_end(cache_key)
                return copy.deepcopy(found)
        items = self._remote(object_id)
        with self.lock:
            self._views[cache_key] = copy.deepcopy(items)
            while len(self._views) > 512:
                self._views.popitem(last=False)
        return items

    def browse(self, object_id='0', start=0, count=100):
        if type(start) is not int or type(count) is not int or start < 0 or count < 0:
            raise ValueError('Invalid Radio page range')
        items = self.children(object_id)
        title = ROOT_TITLES.get(object_id, '')
        if not title:
            with self.lock:
                item = self._items.get(object_id)
            title = item.title if item else ''
        return CatalogPage(items[start:start + min(count or 100, 100)], len(items), title, object_id, start)

    def search(self, query, kind='stations', start=0, count=100):
        self._sync_revision()
        kind = {'station': 'stations', 'show': 'shows', 'host': 'hosts'}.get(kind, kind)
        if kind not in ('stations', 'shows', 'hosts'):
            raise ValueError('Unknown Radio search type')
        response = self.client.search(query, kind, **self._browse_params())
        items = self._convert(response)
        allowed = ('station',) if kind == 'stations' else ('show',)
        items = [item for item in items if item.kind in allowed]
        self.store.add_search(query)
        # Hosts are associated-program matches, not invented host-profile rows.
        if kind == 'hosts':
            for item in items:
                item.metadata.update(search_type='hosts', host_query=query)
        if type(start) is not int or type(count) is not int or start < 0 or count < 0:
            raise ValueError('Invalid Radio search page range')
        return CatalogPage(items[start:start + min(count or 100, 100)], len(items),
                           str(response.head.get('title', 'Search Results')), 'search:' + kind, start)

    def metadata(self, identity):
        self._sync_revision()
        identity = _clean_identity(identity)
        if identity in ROOT_TITLES:
            if identity == 'local':
                return next(item for item in self.root() if item.id == 'local')
            return RadioItem(identity, ROOT_TITLES[identity], 'folder')
        if identity.startswith('custom:'):
            item = self.store.custom(identity)
            if not item:
                raise RadioTimeError('Custom station no longer exists', 'id', 404)
            return self._register(_item_from_dict(item))
        with self.lock:
            existing = copy.deepcopy(self._items.get(identity))
        if re.fullmatch(r'(?:station:s\d+|show:p\d+|episode:t\d+)', identity):
            try:
                response = self.client.describe(_guide(identity))
            except RadioTimeError:
                # Offline directory access must not make saved favorites or
                # their native hover metadata disappear. Playback resolution
                # still reports its own network failure honestly.
                saved = next((row for row in self.store.favorites() + self.store.history() if row['id'] == identity), None)
                if existing or saved:
                    item = existing or _item_from_dict(saved)
                    item.metadata['profile_stale'] = True
                    return self._register(item)
                raise
            profile = next((row for row in response.body if row.get('element') in ('station', 'show', 'topic')), None)
            if profile is None:
                if existing:
                    return existing
                saved = next((row for row in self.store.favorites() + self.store.history() if row['id'] == identity), None)
                if saved:
                    return self._register(_item_from_dict(saved))
                raise RadioTimeError('Radio item is no longer available', 'id', 404)
            kind = identity.split(':', 1)[0]
            metadata = merge_metadata(existing.metadata if existing else {}, profile)
            metadata.pop('profile_stale', None)
            if 'is_available' in profile:
                metadata['unavailable'] = profile['is_available'] is False or str(profile['is_available']).lower() == 'false'
            if existing:
                item = existing
                item.metadata = metadata
            else:
                item = RadioItem(identity, '', kind, metadata=metadata)
            item.title = str(profile.get('name') or profile.get('title') or item.title or _guide(identity))
            item.description = str(profile.get('description') or item.description)
            item.image = str(metadata.get('artwork') or item.image)
            item.artist = str(metadata.get('song_artist') or item.artist)
            item.album = str(metadata.get('song_album') or item.album)
            if kind == 'show' and not item.url:
                item.url = self.client.url('Tune.ashx', dict(c='pbrowse', id=_guide(identity)))
            if kind == 'episode':
                try:
                    item.duration = int(float(profile.get('duration') or item.duration or 0))
                except (ValueError, TypeError):
                    pass
            return self._register(item)
        if existing:
            return existing
        if identity.startswith('radio:'):
            payload = _decoded(identity[6:])
            return self._register(RadioItem(identity, 'Radio', 'folder', url=self.client.url(payload['url'])))
        raise RadioTimeError('Unknown Radio item', 'id', 404)

    def resolve(self, identity, formats=None):
        identity = _clean_identity(identity)
        item = self.metadata(identity)
        if item.kind == 'custom':
            streams = self.client.resolve_url(item.url, formats)
        elif item.kind in ('station', 'episode'):
            params = dict(parse_qsl(urlsplit(item.url).query))
            sid = params.get('sid') or item.metadata.get('show_id') or item.metadata.get('program_id')
            streams = self.client.resolve_streams(_guide(identity), formats, sid=sid)
        else:
            raise RadioTimeError('Choose a station or episode from this folder', 'not_playable', 400)
        quality = self.store.settings()['quality']
        if quality in ('low', 'low_bandwidth'):
            streams.sort(key=lambda stream: (stream.bitrate or float('inf'), -stream.reliability))
        elif quality in ('best', 'best_available'):
            streams.sort(key=lambda stream: (-stream.bitrate, -stream.reliability))
        return streams

    def favorite(self, identity):
        return self.store.add_favorite(self.metadata(identity).as_dict())

    def remove_favorite(self, identity):
        return self.store.remove_favorite(_clean_identity(identity))

    def set_location(self, location):
        result = self.store.set_location(location)
        self.clear_cache()
        return result

    def set_zip(self, zipcode):
        return self.set_location(self.zip_resolver(str(zipcode)))

    def settings_items(self):
        settings = self.store.settings()
        values = [('station_order', 'Station Order'), ('preferred_language', 'Preferred Language'),
                  ('language_filter', 'Language Filter'), ('quality', 'Stream Quality'),
                  ('automatic_reconnect', 'Automatic Reconnect'), ('history', 'Radio History'),
                  ('episode_resume', 'Episode Resume'), ('show_podcasts', 'Podcasts'),
                  ('show_language', 'By Language'), ('cover_lookup', 'Album Cover Lookup')]
        result = [RadioItem('settings:location', 'Local Radio Location', 'folder', metadata={'setting': 'location'}),
                  RadioItem('station-info', 'Station Information', 'folder')]
        choices = {'station_order': ('popular', 'name', 'provider'), 'quality': ('auto', 'best', 'low')}
        for key, title in values:
            value = settings[key]
            result.append(RadioItem('settings:' + key, '%s: %s' % (title, 'On' if value is True else 'Off' if value is False else value or 'Controller language'),
                                    'folder', metadata={'setting': key, 'value': value,
                                                        'choices': list(choices.get(key, (False, True) if type(value) is bool else ())) }))
        for action, title in [('refresh', 'Refresh Radio'), ('clear_history', 'Clear History'),
                              ('clear_searches', 'Clear Recent Searches'), ('reset', 'Reset Radio Preferences')]:
            result.append(RadioItem('settings-action:' + action, title, 'action', metadata={'action': action}))
        return [self._register(item) for item in result]

    def apply_setting(self, name, value=None):
        if name in ('refresh', 'clear_cache'):
            self.store.refresh()
            self.clear_cache()
            return self.store.settings()
        if name == 'clear_history':
            self.store.clear_history()
        elif name == 'clear_searches':
            self.store.clear_searches()
        elif name == 'reset':
            self.store.reset_settings()
        elif name in DEFAULT_SETTINGS:
            if name == 'preferred_language':
                self.validate_language(value)
            if name == 'language_filter' and value is True:
                if not self.store.settings().get('preferred_language'):
                    raise ValueError('Choose a preferred language before enabling its filter')
                self.validate_language(self.store.settings()['preferred_language'])
            self.store.update_settings({name: value})
        else:
            raise ValueError('Unknown Radio setting')
        self.clear_cache()
        return self.store.settings()

    def settings_choices(self, name):
        """A read-only options list. Only apply_action/apply_setting writes state."""
        settings = self.store.settings()
        if name == 'location':
            return [self._register(RadioItem('settings-action:enter_zip', 'Enter ZIP Code', 'action',
                                            metadata={'action': 'enter_zip'})),
                    self._register(RadioItem('location', 'Pick a City', 'folder'))]
        if name not in DEFAULT_SETTINGS:
            raise ValueError('Unknown Radio setting')
        choices = {'station_order': [('Most Popular', 'popular'), ('By Name', 'name'), ('Provider Order', 'provider')],
                   'quality': [('Auto', 'auto'), ('Best Available', 'best'), ('Low Bandwidth', 'low')],
                   'preferred_language': [('Controller Language', '')]}.get(name)
        if choices is None and type(settings[name]) is bool:
            choices = [('On', True), ('Off', False)]
        result = []
        for title, value in choices or []:
            result.append(self._register(RadioItem('settings-apply:' + _encoded({'name': name, 'value': value}),
                                                  title, 'action', metadata={'action': 'apply_setting', 'setting': name,
                                                                            'value': value, 'selected': settings[name] == value})))
        if name == 'preferred_language':
            for choice in self.language_choices():
                guide = choice['id']
                result.append(self._register(RadioItem('settings-apply:' + _encoded({'name': name, 'value': guide}),
                                                      choice['label'], 'action',
                                                      metadata={'action': 'apply_setting', 'setting': name,
                                                                'value': guide, 'selected': settings[name] == guide})))
        return result

    def apply_action(self, identity, value=None):
        if identity.startswith('settings-apply:'):
            payload = _decoded(identity[len('settings-apply:'):])
            return self.apply_setting(payload['name'], payload['value'])
        if identity.startswith('settings-action:'):
            action = identity[len('settings-action:'):]
            if action == 'enter_zip':
                return self.set_zip(value)
            return self.apply_setting(action)
        raise ValueError('Unknown Radio action')

    def schedule(self, identity, **params):
        self._sync_revision()
        response = self.client.schedule(_guide(_clean_identity(identity)), **params)
        return self._schedule_items(response)

    def _schedule_items(self, response):
        """Local labels and unique airings; each folder opens real show options."""
        zone = self.timezone() if callable(self.timezone) else self.timezone
        zone = ZoneInfo(zone or 'UTC')
        result = []
        for index, row in enumerate(response.body):
            target = row.get('URL') or row.get('url')
            if not target:
                continue
            start_utc = row.get('start_utc')
            if not start_utc:
                # A schedule without a UTC identity cannot be safely relabeled
                # into the household timezone; retain the provider's label.
                title = row.get('text', '')
            else:
                try:
                    instant = datetime.fromisoformat(start_utc.replace('Z', '+00:00'))
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=datetime_timezone.utc)
                    local = instant.astimezone(zone)
                    title = '%s — %s' % (local.strftime('%a %H:%M %Z'), row.get('text', ''))
                except (ValueError, TypeError):
                    title = row.get('text', '')
            metadata = copy.deepcopy(row)
            metadata['program_id'] = row.get('guide_id', '')
            if start_utc:
                metadata['start_utc'] = start_utc
            result.append(self._folder(target, title, metadata,
                                       event={'start_utc': start_utc or row.get('start'), 'index': index}))
        return result

    def options(self, identity):
        self._sync_revision()
        response = self.client.options(_guide(_clean_identity(identity)))
        return self._convert(response)

    def clear_cache(self):
        with self.lock:
            self._views.clear()
            self._cursor_chains.clear()
        self.client.clear_cache()

    new_session = clear_cache
