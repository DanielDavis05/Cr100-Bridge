"""Household Radio state. Directory caches and temporary stream URLs never live here."""
from __future__ import annotations

import copy
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid


DEFAULT_SETTINGS = {
    'location': None, 'station_order': 'popular', 'preferred_language': '',
    'language_filter': False, 'quality': 'auto', 'automatic_reconnect': True,
    'history': True, 'episode_resume': True, 'show_podcasts': True,
    'show_language': False, 'cover_lookup': True,
}
_BOOLS = {key for key, value in DEFAULT_SETTINGS.items() if type(value) is bool}


def _http_url(value, allow_empty=False):
    value = str(value or '').strip()
    if not value and allow_empty:
        return ''
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or any(c in value for c in '\r\n'):
        raise ValueError('A valid HTTP or HTTPS URL is required')
    return value


def _settings(patch, previous=None):
    if not isinstance(patch, dict):
        raise ValueError('Radio settings must be an object')
    unknown = set(patch) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError('Unknown Radio setting: %s' % ', '.join(sorted(unknown)))
    result = copy.deepcopy(previous or DEFAULT_SETTINGS)
    for key, value in patch.items():
        if key in _BOOLS and type(value) is not bool:
            raise ValueError('%s must be true or false' % key)
        if key == 'station_order' and value not in ('popular', 'name', 'provider'):
            raise ValueError('Station order must be popular, name, or provider')
        if key == 'quality' and value not in ('auto', 'best', 'low', 'best_available', 'low_bandwidth'):
            raise ValueError('Stream quality must be auto, best, or low')
        if key == 'preferred_language' and (not isinstance(value, str) or len(value) > 80):
            raise ValueError('Preferred language must be a short language code')
        if key == 'location' and value is not None:
            if not isinstance(value, dict):
                raise ValueError('Location must be an object or null')
            value = copy.deepcopy(value)
            if not isinstance(value.get('label', value.get('name', '')), str):
                raise ValueError('Location label must be text')
            coordinates = value.get('latlon')
            if coordinates:
                try:
                    lat, lon = [float(x) for x in (coordinates.split(',') if isinstance(coordinates, str) else coordinates)]
                except (ValueError, TypeError):
                    raise ValueError('Location coordinates must contain latitude and longitude') from None
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError('Location coordinates are out of range')
                value['latlon'] = '%s,%s' % (lat, lon)
            region = value.get('region_id', value.get('id', ''))
            if region and not re.fullmatch(r'r\d+', str(region)):
                raise ValueError('Location region ID is invalid')
            if value and not coordinates and not region:
                raise ValueError('Choose a city from the directory or resolve a ZIP code')
        result[key] = value
    return result


def _item(value):
    if hasattr(value, 'as_dict'):
        value = value.as_dict()
    if not isinstance(value, dict):
        raise ValueError('Radio item must be an object')
    result = copy.deepcopy(value)
    identity = result.get('id') or result.get('guide_id')
    kind = result.get('kind', result.get('type', ''))
    if identity and re.fullmatch(r'[spt]\d+', str(identity)):
        kind = {'s': 'station', 'p': 'show', 't': 'episode'}[identity[0]]
        identity = kind + ':' + identity
    if not isinstance(identity, str) or not re.fullmatch(r'(?:station:s\d+|show:p\d+|episode:t\d+|custom:[\w-]+)', identity):
        raise ValueError('A stable station, show, episode, or custom-station ID is required')
    expected = identity.split(':', 1)[0]
    if kind and kind != expected:
        raise ValueError('Radio item kind does not match its ID')
    result.update(id=identity, kind=expected)
    title = result.get('title', result.get('name', ''))
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise ValueError('A Radio item name is required')
    result['title'] = title.strip()
    # Persistence is a data boundary; reject non-JSON values before touching disk.
    json.dumps(result, allow_nan=False)
    return result


def _empty():
    return dict(version=1, revision=0, radio_favorites_update_id=0, radio_location_update_id=0,
                radio_directory_update_id=0,
                settings=copy.deepcopy(DEFAULT_SETTINGS), favorites=[], custom_stations=[],
                history=[], searches=[], bookmarks={})


class RadioStore:
    """Serialized atomic state updates, shared by HTTP and native controller actions.

    ``path=None`` is deliberately in-memory for fixtures. Production passes a path
    beneath the installation's persistent state directory. ``on_change`` receives
    an event dictionary after an update is safely committed.
    """
    def __init__(self, path=None, on_change=None, clock=time.time):
        self.path = Path(path) if path else None
        self.on_change, self.clock = on_change, clock
        self.lock = threading.RLock()
        self._state = _empty()
        if self.path and self.path.exists():
            try:
                self._state = self._validated(json.loads(self.path.read_text()))
            except (ValueError, TypeError, OSError) as error:
                raise ValueError('Cannot read Radio state %s: %s' % (self.path, error)) from error

    @contextlib.contextmanager
    def _file_guard(self, exclusive=False):
        if not self.path:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a stable sidecar inode: replacing the data file must not replace
        # the lock used by the separate web and player processes.
        lock_path = self.path.with_name(self.path.name + '.lock')
        with open(lock_path, 'a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                if self.path.exists():
                    self._state = self._validated(json.loads(self.path.read_text()))
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _validated(data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported Radio data format (expected version 1)')
        state = _empty()
        state['settings'] = _settings(data.get('settings', {}))
        for key in ('revision', 'radio_favorites_update_id', 'radio_location_update_id', 'radio_directory_update_id'):
            value = data.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError('%s must be a nonnegative integer' % key)
            state[key] = value
        for key in ('favorites', 'custom_stations', 'history'):
            rows = data.get(key, [])
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError('%s must be a bounded list' % key)
            seen = set()
            for row in rows:
                item = _item(row)
                if item['id'] in seen:
                    raise ValueError('Duplicate Radio item: %s' % item['id'])
                seen.add(item['id'])
                if key == 'custom_stations':
                    if item['kind'] != 'custom':
                        raise ValueError('Custom station has an invalid identity')
                    item['url'] = _http_url(item.get('url'))
                    item['image'] = _http_url(item.get('image', ''), True)
                if key == 'favorites' and item['kind'] == 'episode':
                    raise ValueError('Save a show as a favorite rather than an individual episode')
                state[key].append(item)
        custom_ids = {row['id'] for row in state['custom_stations']}
        if any(row['kind'] == 'custom' and row['id'] not in custom_ids for row in state['favorites']):
            raise ValueError('A custom-station favorite has no corresponding station')
        searches = data.get('searches', [])
        if not isinstance(searches, list) or any(not isinstance(q, str) or len(q) > 500 for q in searches):
            raise ValueError('Invalid recent Radio searches')
        state['searches'] = list(dict.fromkeys(searches))[:10]
        state['history'] = state['history'][:30]
        bookmarks = data.get('bookmarks', {})
        if not isinstance(bookmarks, dict):
            raise ValueError('Bookmarks must be an object')
        for identity, mark in bookmarks.items():
            if not re.fullmatch(r'episode:t\d+', identity) or not isinstance(mark, dict):
                raise ValueError('Bookmark must refer to an episode')
            position = mark.get('position')
            duration = mark.get('duration')
            if type(position) not in (int, float) or not math.isfinite(position) or position < 0:
                raise ValueError('Bookmark position is invalid')
            if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0 or position > duration):
                raise ValueError('Bookmark duration is invalid')
            state['bookmarks'][identity] = copy.deepcopy(mark)
        json.dumps(state, allow_nan=False)
        return state

    def _persist(self, state):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.%s.' % self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _change(self, operation, event='radio-state-changed', favorites=False, location=False):
        with self.lock, self._file_guard(exclusive=True):
            candidate = copy.deepcopy(self._state)
            result = operation(candidate)
            if candidate == self._state:
                return copy.deepcopy(result)
            candidate['revision'] = self._state['revision'] + 1
            if favorites:
                candidate['radio_favorites_update_id'] += 1
            if location:
                candidate['radio_location_update_id'] += 1
            self._persist(candidate)
            self._state = candidate
            notification = dict(event=event, revision=candidate['revision'],
                                radio_favorites_update_id=candidate['radio_favorites_update_id'],
                                radio_location_update_id=candidate['radio_location_update_id'],
                                radio_directory_update_id=candidate['radio_directory_update_id'])
        if self.on_change:
            self.on_change(notification)
        return copy.deepcopy(result)

    def snapshot(self):
        with self.lock, self._file_guard():
            return copy.deepcopy(self._state)

    def settings(self):
        return self.snapshot()['settings']

    def update_settings(self, patch):
        # Validate independently, then validate again against the latest state in the transaction.
        _settings(patch)
        def change(state):
            updated = _settings(patch, state['settings'])
            if updated != state['settings']:
                state['radio_directory_update_id'] += 1
            state['settings'] = updated
            return state['settings']
        return self._change(change, 'radio-settings-changed', location='location' in patch)

    def reset_settings(self):
        return self.update_settings(copy.deepcopy(DEFAULT_SETTINGS))

    def refresh(self):
        def change(state):
            state['radio_directory_update_id'] += 1
            return state['radio_directory_update_id']
        return self._change(change, 'radio-directory-refresh')

    touch = refresh

    def set_location(self, location):
        return self.update_settings({'location': location})['location']

    def favorites(self, kind=None):
        rows = self.snapshot()['favorites']
        if kind:
            kind = {'stations': 'station', 'shows': 'show'}.get(kind, kind)
            rows = [row for row in rows if row['kind'] == kind or kind == 'station' and row['kind'] == 'custom']
        return rows

    def add_favorite(self, item):
        item = _item(item)
        if item['kind'] == 'episode':
            raise ValueError('Save the parent show as a favorite')
        def change(state):
            if item['kind'] == 'custom' and not any(c['id'] == item['id'] for c in state['custom_stations']):
                raise ValueError('Custom station does not exist')
            existing = next((row for row in state['favorites'] if row['id'] == item['id']), None)
            if existing:
                return existing
            state['favorites'].append(item)
            return item
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def remove_favorite(self, identity):
        def change(state):
            before = len(state['favorites'])
            state['favorites'] = [row for row in state['favorites'] if row['id'] != identity]
            return len(state['favorites']) != before
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def update_favorite(self, identity, title=None, index=None):
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 1000):
            raise ValueError('A favorite name is required')
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError('Favorite position must be a nonnegative integer')
        def change(state):
            rows = state['favorites']
            item = next((row for row in rows if row['id'] == identity), None)
            if item is None:
                raise KeyError(identity)
            if title is not None:
                item['title'] = title.strip()
            if index is not None:
                rows.remove(item)
                rows.insert(min(index, len(rows)), item)
            return item
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def reorder_favorites(self, identities):
        def change(state):
            rows = {row['id']: row for row in state['favorites']}
            if not isinstance(identities, list) or len(identities) != len(rows) or set(identities) != set(rows):
                raise ValueError('Favorite ordering must include every favorite exactly once')
            state['favorites'] = [rows[identity] for identity in identities]
            return state['favorites']
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def custom_stations(self):
        return self.snapshot()['custom_stations']

    def custom(self, identity):
        return next((row for row in self.custom_stations() if row['id'] == identity), None)

    def add_custom(self, name, url, logo='', **fields):
        item = _item(dict(id='custom:' + uuid.uuid4().hex, kind='custom', title=name,
                          url=_http_url(url), image=_http_url(logo, True), **fields))
        def change(state):
            state['custom_stations'].append(item)
            state['favorites'].append(copy.deepcopy(item))
            return item
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def update_custom(self, identity, patch=None, **fields):
        patch = dict(patch or {}, **fields)
        if set(patch) - {'title', 'name', 'url', 'image', 'logo', 'description', 'artist', 'album', 'metadata'}:
            raise ValueError('Unknown custom station field')
        if 'name' in patch:
            patch['title'] = patch.pop('name')
        if 'logo' in patch:
            patch['image'] = patch.pop('logo')
        if 'url' in patch:
            patch['url'] = _http_url(patch['url'])
        if 'image' in patch:
            patch['image'] = _http_url(patch['image'], True)
        def change(state):
            item = next((row for row in state['custom_stations'] if row['id'] == identity), None)
            if item is None:
                raise KeyError(identity)
            item.update(patch)
            item.update(_item(item))
            for favorite in state['favorites']:
                if favorite['id'] == identity:
                    favorite.update(item)
            return item
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def delete_custom(self, identity):
        def change(state):
            before = len(state['custom_stations'])
            state['custom_stations'] = [row for row in state['custom_stations'] if row['id'] != identity]
            state['favorites'] = [row for row in state['favorites'] if row['id'] != identity]
            state['history'] = [row for row in state['history'] if row['id'] != identity]
            return before != len(state['custom_stations'])
        return self._change(change, 'radio-favorites-changed', favorites=True)

    def add_history(self, item):
        item = _item(item)
        def change(state):
            if not state['settings']['history']:
                return []
            item['played_at'] = self.clock()
            state['history'] = [item] + [row for row in state['history'] if row['id'] != item['id']][:29]
            return state['history']
        return self._change(change)

    def history(self):
        state = self.snapshot()
        return state['history'] if state['settings']['history'] else []

    record_history = add_history

    def clear_history(self):
        def change(state):
            state['history'] = []
            state['searches'] = []
        return self._change(change)

    def add_search(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError('A search query of up to 500 characters is required')
        query = query.strip()
        def change(state):
            if state['settings']['history']:
                state['searches'] = [query] + [q for q in state['searches'] if q != query][:9]
            return state['searches'] if state['settings']['history'] else []
        return self._change(change)

    def searches(self):
        state = self.snapshot()
        return state['searches'] if state['settings']['history'] else []

    def clear_searches(self):
        def change(state):
            state['searches'] = []
        return self._change(change)

    def set_bookmark(self, identity, position, duration=None):
        mark = dict(position=position, duration=duration, updated_at=self.clock())
        self._validated(dict(version=1, bookmarks={identity: mark}))
        def change(state):
            if state['settings']['episode_resume']:
                state['bookmarks'][identity] = mark
            return mark
        return self._change(change)

    def bookmark(self, identity):
        state = self.snapshot()
        return state['bookmarks'].get(identity) if state['settings']['episode_resume'] else None

    get_bookmark = bookmark

    def clear_bookmark(self, identity):
        return self._change(lambda state: state['bookmarks'].pop(identity, None))

    def export_data(self):
        return self.snapshot()

    def import_data(self, data, merge=False):
        validated = self._validated(data)
        def change(state):
            if merge:
                if 'settings' in data:
                    state['settings'] = _settings(data['settings'], state['settings'])
                for key in ('custom_stations', 'favorites'):
                    identities = {row['id'] for row in state[key]}
                    state[key].extend(row for row in validated[key] if row['id'] not in identities)
                state['bookmarks'].update(validated['bookmarks'])
            else:
                for key in ('settings', 'favorites', 'custom_stations', 'history', 'searches', 'bookmarks'):
                    state[key] = validated[key]
            # Cross-reference validation also applies to merged imports.
            self._validated(state)
            state['radio_directory_update_id'] += 1
            return state
        self._change(change, 'radio-imported', favorites=True, location=True)
        return self.snapshot()
