"""Pure, read-only Home Assistant device catalogue for the CR100.

The UI service owns connections, view tokens and actions.  This module accepts
registry/state snapshots only, so building or paging a menu can never call HA.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import unicodedata
from urllib.parse import quote, unquote


DOMAINS = frozenset(('light', 'switch', 'input_boolean', 'fan', 'scene', 'script'))
ONE_SHOT = frozenset(('scene', 'script'))
UNAVAILABLE = frozenset(('unavailable', 'unknown'))
TITLE_WIDTH = 24
_NATIVE_ID = re.compile(r'RINCON_[A-Za-z0-9_]{1,100}\Z')
_ENTITY_ID = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+\Z')
_DIMMABLE_MODES = frozenset(('brightness', 'color_temp', 'hs', 'xy', 'rgb', 'rgbw', 'rgbww', 'white'))


def _records(value, key):
    values = value.values() if isinstance(value, dict) else value or ()
    return {entry[key]: dict(entry) for entry in values
            if isinstance(entry, dict) and isinstance(entry.get(key), str) and entry[key]}


def _line(value):
    return ' '.join(str(value or '').split())


def _width(value):
    return sum(0 if unicodedata.category(char) in ('Mn', 'Me', 'Cf') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in value)


def _fit(value, width):
    used = 0
    for index, char in enumerate(value):
        used += _width(char)
        if used > width: return value[:index]
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _rounded(value):
    # Device percentages are non-negative; avoid Python's tie-to-even rounding.
    return int(value + .5)


def _percentage(value):
    return min(100, max(0, _rounded(value))) if _number(value) else None


def _native_id(value):
    if isinstance(value, str):
        if value.startswith('uuid:'): value = value[5:]
        if _NATIVE_ID.fullmatch(value): return value
    return None


def area_container(area_id, kind=''):
    """ASCII container identity, with registry IDs escaped as opaque segments."""
    if kind not in ('', 'scenes', 'lights'): raise ValueError('Unknown device list kind')
    return 'SD:a:' + quote(str(area_id), safe='') + (':' + kind if kind else '')


def room_container(room):
    """An alias preserves one folder per grouped room, including shared areas."""
    key = '\0'.join(str(room.get(field) or '') for field in ('udn', 'backend', 'key', 'name'))
    return 'SD:r:' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


@dataclass(frozen=True)
class DeviceRow:
    row_id: str
    name: str
    title: str
    targets: tuple = ()
    domain: str = ''
    available: bool = True
    on: bool = False
    level_kind: str = ''
    level: int | None = None
    folder: bool = False
    members: tuple = ()
    member_closure: tuple = ()

    @property
    def upnp_class(self):
        if self.folder: return 'object.container'
        if not self.available: return 'object.item.sdUnavailable.inert.none'
        if self.domain in ONE_SHOT: return 'object.item.sdActivate.activate.none'
        kind = {'brightness': 'light', 'speed': 'fan'}.get(self.level_kind, 'power')
        level = str(self.level if self.level is not None else 0) if self.level_kind else 'none'
        return 'object.item.' + ('sdOn' if self.on else 'sdOff') + '.' + kind + '.' + level

    @property
    def signature(self):
        """Structure only: authoritative state refreshes must retain the view."""
        return (self.row_id, self.name, self.targets, self.domain, self.level_kind,
                self.folder, self.members, self.member_closure)


def unavailable_row():
    return DeviceRow('SD:unavailable', 'Home Assistant unavailable',
                     'Home Assistant unavailable', available=False)


class SmartCatalogue:
    def __init__(self, snapshot, states, previous=None):
        self.registry = snapshot or {}
        self.registry_entities = _records(self.registry.get('entities'), 'entity_id')
        self.devices = _records(self.registry.get('devices'), 'id')
        self.areas = _records(self.registry.get('areas'), 'area_id')
        self.floors = _records(self.registry.get('floors'), 'floor_id')
        self.states = states or {}
        if not isinstance(self.states, dict): self.states = _records(self.states, 'entity_id')
        self.entities = {}
        for entity_id, entry in self.registry_entities.items():
            domain = entity_id.partition('.')[0]
            state = self.states.get(entity_id)
            if (not _ENTITY_ID.fullmatch(entity_id) or domain not in DOMAINS
                    or entry.get('disabled_by') or entry.get('hidden_by')
                    or entry.get('entity_category') or not isinstance(state, dict)):
                continue
            area_id = self._entry_area(entry)
            if area_id is None: continue
            attributes = dict(state.get('attributes') or {})
            # HA integrations may clear attributes while unavailable. Preserve
            # known identity/capability fields, while explicit values still win.
            if state.get('state', 'unknown') in UNAVAILABLE and previous is not None:
                old = previous.entities.get(entity_id)
                if old:
                    attributes = {**old['state'].get('attributes', {}), **attributes}
            state = {**state, 'attributes': attributes}
            name = _line(entry.get('name') or attributes.get('friendly_name') or entity_id)
            area_name = _line(self.areas[area_id].get('name') or area_id)
            prefix = re.match(re.escape(area_name) + r'(?=$|[\s_:\-])', name, re.IGNORECASE)
            if prefix:
                # Match a leading name, not e.g. "Den" in "Denny's lamp".
                stripped = name[prefix.end():].lstrip(' -_:')
                if stripped: name = stripped
            raw_members = attributes.get('entity_id')
            members = tuple(sorted(set(member for member in raw_members if isinstance(member, str)))) \
                if domain == 'light' and isinstance(raw_members, list) and raw_members else ()
            level_kind = ''
            if domain == 'light':
                modes = attributes.get('supported_color_modes') or ()
                if not isinstance(modes, (list, tuple, set)): modes = ()
                features = attributes.get('supported_features', 0)
                if (set(modes) & _DIMMABLE_MODES or attributes.get('color_mode') in _DIMMABLE_MODES
                        or _number(attributes.get('brightness'))
                        or isinstance(features, int) and features & 1):
                    level_kind = 'brightness'
            elif domain == 'fan':
                features = attributes.get('supported_features', 0)
                if _number(attributes.get('percentage')) or isinstance(features, int) and features & 1:
                    level_kind = 'speed'
            self.entities[entity_id] = {'entity_id': entity_id, 'area_id': area_id,
                'domain': domain, 'name': name, 'members': members,
                'level_kind': level_kind, 'registry': entry, 'state': state}
        # A visible native group can refer to another group filtered out of the
        # catalogue. Its cached raw membership still changes the visible row's
        # actual target closure. Traverse only reachable light IDs, not all HA.
        self.member_graph, referenced = {}, set()
        pending = [entity_id for entity_id, entity in self.entities.items() if entity['members']]
        while pending:
            entity_id = pending.pop()
            if entity_id in self.member_graph: continue
            entity = self.entities.get(entity_id)
            if entity is not None:
                members = entity['members']
            else:
                state = self.states.get(entity_id)
                state = state if isinstance(state, dict) else {}
                attrs = state.get('attributes')
                attrs = attrs if isinstance(attrs, dict) else {}
                raw = attrs.get('entity_id')
                if 'entity_id' not in attrs and state.get('state') in UNAVAILABLE and previous is not None:
                    members = previous.member_graph.get(entity_id, ())
                else:
                    members = tuple(sorted(set(member for member in raw if isinstance(member, str)))) \
                        if isinstance(raw, list) else ()
            self.member_graph[entity_id] = members
            children = [member for member in members
                        if _ENTITY_ID.fullmatch(member) and member.startswith('light.')]
            referenced.update(children); pending.extend(children)
        self.referenced_entities = frozenset(referenced)
        area_entities = {}
        for entity_id, entity in self.entities.items():
            area_entities.setdefault(entity['area_id'], []).append(entity_id)
        self.area_entities = {area: tuple(sorted(ids)) for area, ids in area_entities.items()}
        self.catalogue_areas = tuple(sorted(self.area_entities, key=self._area_sort))
        # Hue/native and helper groups have identical membership semantics here.
        self.grouped_lights = frozenset(member for entity in self.entities.values()
            if entity['members'] and not self._collapsed_group(entity)
            for member in entity['members']
            if member in self.entities and self.entities[member]['domain'] == 'light')

    def _device_area(self, device_id):
        seen = set()
        while device_id and device_id not in seen:
            seen.add(device_id)
            device = self.devices.get(device_id, {})
            if device.get('area_id'):
                area = device['area_id']
                return area if area in self.areas else None
            device_id = device.get('parent_device_id')
        return None

    def _entry_area(self, entry):
        if entry.get('area_id'):
            area = entry['area_id']
            return area if area in self.areas else None
        return self._device_area(entry.get('device_id'))

    def resolve_area(self, room):
        """Resolve cached room identity; a URL alone never identifies a speaker."""
        if 'devices_area' in room:
            area = room['devices_area']
            return area if isinstance(area, str) and area in self.areas else None
        if 'area_id' in room:
            area = room['area_id']
            return area if area in self.areas else None
        backend = room.get('backend')
        key = room.get('key') or room.get('entity') or room.get('id')
        if backend == 'ha' or not backend and isinstance(key, str) and key.startswith('media_player.'):
            return self._entry_area(self.registry_entities.get(key, {}))
        if backend != 'sonos': return None
        identity = room.get('identity') or {}
        wanted = _native_id(room.get('native_id')) or _native_id(key) or _native_id(identity.get('id'))
        if wanted is None: return None
        for device in self.devices.values():
            for identifier in device.get('identifiers') or ():
                if (isinstance(identifier, (list, tuple)) and len(identifier) == 2
                        and identifier[0] == 'sonos' and _native_id(identifier[1]) == wanted):
                    return self._device_area(device['id'])
        # ha_linein also recognizes the Sonos media player's unique_id when
        # device identifiers are missing. Both joins require the exact RINCON.
        for entry in self.registry_entities.values():
            if (entry.get('platform') == 'sonos' and entry['entity_id'].startswith('media_player.')
                    and _native_id(entry.get('unique_id')) == wanted):
                return self._entry_area(entry)
        return None

    @staticmethod
    def structural_signature(rows):
        return tuple(row.signature for row in rows)

    def _floor_sort(self, floor_id):
        floor = self.floors.get(floor_id, {})
        level = floor.get('level')
        return (level if _number(level) else math.inf,
                _line(floor.get('name') or floor_id or 'No Floor').casefold(), floor_id or '')

    def _area_sort(self, area_id):
        area = self.areas[area_id]
        return (*self._floor_sort(self._area_floor(area_id)),
                _line(area.get('name') or area_id).casefold(), area_id)

    def _area_floor(self, area_id):
        floor = self.areas[area_id].get('floor_id')
        return floor if floor in self.floors else None

    def _room_areas(self, rooms):
        return frozenset(area for room in rooms if (area := self.resolve_area(room)) is not None)

    def _individual_lights(self, areas):
        return tuple(sorted(entity_id for entity_id, entity in self.entities.items()
            if entity['area_id'] in areas and entity['domain'] == 'light' and not entity['members']))

    def _collapsed_group(self, entity):
        lights = self._individual_lights({entity['area_id']})
        return len(lights) >= 2 and set(entity['members']) == set(lights)

    @staticmethod
    def _folder(row_id, name):
        name = _line(name)
        return DeviceRow(row_id, name, _fit(name, TITLE_WIDTH), folder=True)

    @staticmethod
    def _title(name, state):
        suffix = ': ' + state if state else ''
        return _fit(name, max(1, TITLE_WIDTH - _width(suffix))).rstrip() + suffix

    def _member_closure(self, entity_id):
        """Include intermediate groups and leaves, excluding a cyclic root."""
        visited, members = {entity_id}, set()
        pending = list(self.member_graph.get(entity_id, ()))
        while pending:
            member = pending.pop()
            if member in visited: continue
            visited.add(member); members.add(member)
            pending.extend(self.member_graph.get(member, ()))
        return tuple(sorted(members))

    def _entity_row(self, entity_id):
        entity = self.entities[entity_id]
        state, domain, kind = entity['state'], entity['domain'], entity['level_kind']
        attrs = state.get('attributes') or {}
        available = state.get('state', 'unknown') not in UNAVAILABLE
        on = state.get('state') == 'on'
        level = None
        if kind == 'brightness' and _number(attrs.get('brightness')):
            level = _percentage(attrs['brightness'] / 255 * 100)
            if on: level = max(1, level)
        elif kind == 'speed': level = _percentage(attrs.get('percentage'))
        if domain in ONE_SHOT: text = ''
        elif not available: text = 'Unavailable'
        elif not on: text = 'Off'
        else:
            text = 'On'
            if level is not None: text += ' · %d%%' % level
        return DeviceRow(entity_id, entity['name'], self._title(entity['name'], text),
            (entity_id,), domain, available, on, kind, level, members=entity['members'],
            member_closure=self._member_closure(entity_id))

    def _all_lights(self, row_id, lights):
        entries = [self._entity_row(entity_id) for entity_id in lights]
        available = any(row.available for row in entries)
        on = any(row.on for row in entries)
        reporting = [self.entities[row.row_id]['state']['attributes']['brightness'] for row in entries
            if row.on and _number(self.entities[row.row_id]['state']['attributes'].get('brightness'))]
        level = max(1, min(100, _rounded(sum(reporting) / len(reporting) / 255 * 100))) if reporting else 0
        kind = 'brightness' if any(row.level_kind == 'brightness' for row in entries) else ''
        text = 'Unavailable' if not available else 'Off' if not on else 'On'
        if available and on and reporting: text += ' · %d%%' % level
        return DeviceRow(row_id, 'All Lights', self._title('All Lights', text), tuple(lights),
                         'light', available, on, kind, level if kind else None,
                         members=tuple(lights), member_closure=tuple(lights))

    def _area_rows(self, area_id):
        if area_id not in self.areas: raise KeyError(area_id)
        ids = self.area_entities.get(area_id, ())
        lights = self._individual_lights({area_id})
        rows = []
        if len(lights) >= 2:
            rows.append(self._all_lights('SD:all:' + quote(area_id, safe=''), lights))
        groups, singles, others, scenes = [], [], [], []
        for entity_id in ids:
            entity = self.entities[entity_id]
            if entity['domain'] in ONE_SHOT:
                scenes.append(entity_id)
            elif entity['members']:
                if self._collapsed_group(entity): continue
                groups.append(self._entity_row(entity_id))
            elif entity['domain'] == 'light':
                if entity_id not in self.grouped_lights: singles.append(self._entity_row(entity_id))
            else: others.append(self._entity_row(entity_id))
        by_name = lambda row: (row.name.casefold(), row.row_id)
        rows += sorted(groups, key=by_name) + sorted(singles, key=by_name) + sorted(others, key=by_name)
        if scenes: rows.append(self._folder(area_container(area_id, 'scenes'), 'Scenes'))
        if any(entity_id in self.grouped_lights for entity_id in lights):
            rows.append(self._folder(area_container(area_id, 'lights'), 'Individual Lights'))
        return tuple(rows)

    def _other_areas(self, rooms):
        excluded = self._room_areas(rooms)
        return tuple(area for area in self.catalogue_areas if area not in excluded)

    def _multiple_floors(self):
        return len({self._area_floor(area) for area in self.catalogue_areas
                    if self._area_floor(area) is not None}) > 1

    def _area_folders(self, areas):
        return tuple(self._folder(area_container(area), self.areas[area].get('name') or area)
                     for area in sorted(areas, key=self._area_sort))

    def rows(self, container_id, rooms=()):
        rooms = tuple(rooms)
        if container_id == 'SD:':
            if len(rooms) > 1:
                rows = []
                lights = self._individual_lights(self._room_areas(rooms))
                if lights: rows.append(self._all_lights('SD:all:group', lights))
                rows += [self._folder(room_container(room), room.get('name') or 'Room')
                         for room in sorted(rooms, key=lambda room: (_line(room.get('name')).casefold(), room_container(room)))]
            else:
                area = self.resolve_area(rooms[0]) if rooms else None
                rows = list(self._area_rows(area)) if area is not None else []
            rows.append(self._folder('SD:other', 'Other Rooms'))
            return tuple(rows)
        if container_id.startswith('SD:r:'):
            room = next((room for room in rooms if room_container(room) == container_id), None)
            if room is None: raise KeyError(container_id)
            area = self.resolve_area(room)
            return self._area_rows(area) if area is not None else (self._folder('SD:other', 'Other Rooms'),)
        if container_id == 'SD:other':
            areas = self._other_areas(rooms)
            if not self._multiple_floors(): return self._area_folders(areas)
            floors = {self._area_floor(area) for area in areas}
            return tuple(self._folder('SD:floor:' + quote(floor or '', safe=''),
                         self.floors.get(floor, {}).get('name') or 'No Floor')
                         for floor in sorted(floors, key=self._floor_sort))
        if container_id.startswith('SD:floor:'):
            floor = unquote(container_id[len('SD:floor:'):]) or None
            if not self._multiple_floors() or floor is not None and floor not in self.floors:
                raise KeyError(container_id)
            return self._area_folders(area for area in self._other_areas(rooms)
                                      if self._area_floor(area) == floor)
        if container_id.startswith('SD:a:'):
            segment = container_id[len('SD:a:'):]
            area_id, separator, kind = segment.partition(':')
            area_id = unquote(area_id)
            if area_id not in self.areas: raise KeyError(container_id)
            if not separator: return self._area_rows(area_id)
            if kind == 'scenes':
                ids = [entity_id for entity_id in self.area_entities.get(area_id, ())
                       if self.entities[entity_id]['domain'] in ONE_SHOT]
            elif kind == 'lights':
                ids = [entity_id for entity_id in self._individual_lights({area_id})
                       if entity_id in self.grouped_lights]
            else: raise KeyError(container_id)
            return tuple(sorted((self._entity_row(entity_id) for entity_id in ids),
                                key=lambda row: (row.name.casefold(), row.row_id)))
        raise KeyError(container_id)

    def containers(self, rooms=()):
        ids = {'SD:', 'SD:other'}
        ids.update(room_container(room) for room in rooms if len(rooms) > 1)
        for area in self.catalogue_areas:
            ids.add(area_container(area))
            for row in self._area_rows(area):
                if row.folder: ids.add(row.row_id)
        if self._multiple_floors():
            ids.update('SD:floor:' + quote(floor or '', safe='')
                for floor in {self._area_floor(area) for area in self._other_areas(rooms)})
        return tuple(sorted(ids))

    def dependencies(self, container_id, rooms=()):
        """Entity lookup index for state events; navigation folders have no state."""
        rows = self.rows(container_id, rooms)
        dependencies = {target for row in rows for target in (*row.targets, *row.member_closure)}
        if container_id == 'SD:' and len(rooms) <= 1:
            area = self.resolve_area(rooms[0]) if rooms else None
        elif container_id.startswith('SD:r:'):
            room = next((room for room in rooms if room_container(room) == container_id), {})
            area = self.resolve_area(room)
        elif container_id.startswith('SD:a:') and ':' not in container_id[len('SD:a:'):]:
            area = unquote(container_id[len('SD:a:'):])
        else: area = None
        if area is not None: dependencies.update(self.area_entities.get(area, ()))
        return frozenset(dependencies)
