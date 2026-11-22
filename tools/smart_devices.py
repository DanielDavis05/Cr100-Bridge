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


DOMAINS = frozenset(('light', 'switch', 'input_boolean', 'fan', 'scene', 'script', 'climate'))
ONE_SHOT = frozenset(('scene', 'script'))
UNAVAILABLE = frozenset(('unavailable', 'unknown'))
HVAC_MODES = ('off', 'heat', 'cool', 'heat_cool', 'auto', 'fan_only', 'dry')
HVAC_MODE_LABELS = {
    'off': 'Off',
    'heat': 'Heat',
    'cool': 'Cool',
    'heat_cool': 'Heat/Cool',
    'auto': 'Auto',
    'fan_only': 'Fan Only',
    'dry': 'Dry',
}
HVAC_ACTION_LABELS = {
    'heating': 'Heating',
    'cooling': 'Cooling',
    'idle': 'Idle',
    'off': 'Off',
    'drying': 'Drying',
    'fan': 'Fan',
}
CLIMATE_TARGET_TEMP = 1
CLIMATE_TARGET_RANGE = 2
CLIMATE_FAN_MODE = 8
CLIMATE_TURN_OFF = 128
CLIMATE_TURN_ON = 256
TITLE_WIDTH = 24
_NATIVE_ID = re.compile(r'RINCON_[A-Za-z0-9_]{1,100}\Z')
_ENTITY_ID = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+\Z')
_FAN_MODE = re.compile(r'[a-z0-9_-]{1,24}\Z')
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


def _signed_round(value):
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


def snap_tenths(tenths, min_tenths, max_tenths, step_tenths):
    """Round a tenths-of-a-degree value to the nearest step and clamp to [min, max]."""
    steps = _signed_round((tenths - min_tenths) / step_tenths)
    snapped = min_tenths + steps * step_tenths
    return max(min_tenths, min(max_tenths, snapped))


def format_tenths(tenths):
    if tenths is None: return ''
    whole, frac = divmod(abs(int(tenths)), 10)
    sign = '-' if tenths < 0 and (whole or frac) else ''
    return f'{sign}{whole}.{frac}°' if frac else f'{sign}{whole}°'


def fan_label(mode):
    if not mode: return ''
    return ' '.join(part.capitalize() for part in str(mode).replace('-', '_').split('_') if part)


def _climate_mode_token(hvac_mode):
    if hvac_mode == 'heat': return 'sdHeat'
    if hvac_mode == 'cool': return 'sdCool'
    if hvac_mode in ('heat_cool', 'auto'): return 'sdHeatCool'
    if hvac_mode == 'fan_only': return 'sdFan'
    if hvac_mode == 'dry': return 'sdIdle'
    return 'sdClimateOff'


def _climate_state_token(hvac_action, hvac_mode):
    if hvac_action == 'heating': return 'sdHeat'
    if hvac_action == 'cooling': return 'sdCool'
    if hvac_action in ('idle', 'drying'): return 'sdIdle'
    if hvac_action == 'fan': return 'sdFan'
    if hvac_action == 'off': return 'sdClimateOff'
    return _climate_mode_token(hvac_mode)


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
    state_token: str = ''
    level_spec: str = ''
    structure: tuple = ()

    @property
    def upnp_class(self):
        if self.folder: return 'object.container'
        if not self.available: return 'object.item.sdUnavailable.inert.none'
        if self.domain in ONE_SHOT: return 'object.item.sdActivate.activate.none'
        if self.domain == 'climate':
            state = self.state_token or ('sdOn' if self.on else 'sdClimateOff')
            kind = self.level_kind or 'climate'
            level = self.level_spec or 'none'
            return f'object.item.{state}.{kind}.{level}'
        kind = {'brightness': 'light', 'speed': 'fan'}.get(self.level_kind, 'power')
        level = str(self.level if self.level is not None else 0) if self.level_kind else 'none'
        return 'object.item.' + ('sdOn' if self.on else 'sdOff') + '.' + kind + '.' + level

    @property
    def signature(self):
        """Structure only: authoritative state refreshes must retain the view."""
        base = (self.row_id, self.name, self.targets, self.domain, self.level_kind,
                self.folder, self.members, self.member_closure)
        return base + self.structure if self.structure else base


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
            old = previous.entities.get(entity_id) if previous is not None else None
            # HA integrations may clear attributes while unavailable. Preserve
            # known identity/capability fields, while explicit values still win.
            if state.get('state', 'unknown') in UNAVAILABLE and old is not None:
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
            climate_meta = None
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
            elif domain == 'climate':
                climate_meta = self._parse_climate(state, attributes, old)
                if climate_meta is None:
                    continue
                level_kind = 'climate'
            record = {'entity_id': entity_id, 'area_id': area_id,
                'domain': domain, 'name': name, 'members': members,
                'level_kind': level_kind, 'registry': entry, 'state': state}
            if climate_meta is not None:
                record.update(climate_meta)
            self.entities[entity_id] = record
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

    def has_entities(self, *, climate=True):
        if climate: return bool(self.entities)
        return any(entity['domain'] != 'climate' for entity in self.entities.values())

    @staticmethod
    def _parse_climate(state, attributes, old=None):
        raw_state = state.get('state', 'unknown')
        if raw_state not in (*HVAC_MODES, *UNAVAILABLE):
            return None
        available = raw_state not in UNAVAILABLE
        raw_modes = attributes.get('hvac_modes')
        modes = []
        if isinstance(raw_modes, (list, tuple)):
            for mode in raw_modes:
                if isinstance(mode, str) and mode in HVAC_MODES and mode not in modes:
                    modes.append(mode)
        if available and raw_state in HVAC_MODES and raw_state not in modes:
            modes.append(raw_state)
        if not modes and old is not None:
            modes = list(old.get('hvac_modes', ()))
        if not modes:
            return None
        hvac_mode = raw_state if available else (old.get('hvac_mode', modes[0]) if old else modes[0])
        if hvac_mode not in modes:
            hvac_mode = modes[0]
        features = attributes.get('supported_features', 0)
        features = features if isinstance(features, int) else 0
        raw_unit = str(attributes.get('temperature_unit') or attributes.get('unit_of_measurement') or '')
        if 'c' in raw_unit.lower():
            unit = 'C'
        elif 'f' in raw_unit.lower():
            unit = 'F'
        elif _number(attributes.get('max_temp')) and float(attributes['max_temp']) <= 45:
            unit = 'C'
        elif _number(attributes.get('target_temp_step')) and float(attributes['target_temp_step']) == 0.5:
            unit = 'C'
        else:
            unit = 'F'
        raw_step = attributes.get('target_temp_step')
        if _number(raw_step) and 0 < float(raw_step) <= 10:
            step_tenths = max(1, _signed_round(float(raw_step) * 10))
        else:
            step_tenths = 5 if unit == 'C' else 10
        raw_min = attributes.get('min_temp')
        raw_max = attributes.get('max_temp')
        min_tenths = _signed_round(float(raw_min) * 10) if _number(raw_min) else (70 if unit == 'C' else 450)
        max_tenths = _signed_round(float(raw_max) * 10) if _number(raw_max) else (350 if unit == 'C' else 950)
        if min_tenths >= max_tenths:
            min_tenths, max_tenths = (70, 350) if unit == 'C' else (450, 950)
        current_temp = _signed_round(float(attributes['current_temperature']) * 10) \
            if _number(attributes.get('current_temperature')) else None
        current_humidity = _percentage(attributes.get('current_humidity'))
        raw_action = attributes.get('hvac_action')
        hvac_action = raw_action if isinstance(raw_action, str) and raw_action in HVAC_ACTION_LABELS else None
        target_temp = snap_tenths(_signed_round(float(attributes['temperature']) * 10),
                                  min_tenths, max_tenths, step_tenths) \
            if _number(attributes.get('temperature')) else None
        target_low = snap_tenths(_signed_round(float(attributes['target_temp_low']) * 10),
                                 min_tenths, max_tenths, step_tenths) \
            if _number(attributes.get('target_temp_low')) else None
        target_high = snap_tenths(_signed_round(float(attributes['target_temp_high']) * 10),
                                  min_tenths, max_tenths, step_tenths) \
            if _number(attributes.get('target_temp_high')) else None
        fan_modes = []
        if features & CLIMATE_FAN_MODE and isinstance(attributes.get('fan_modes'), (list, tuple)):
            for fmode in attributes['fan_modes']:
                if isinstance(fmode, str) and _FAN_MODE.fullmatch(fmode) and fmode not in fan_modes:
                    fan_modes.append(fmode)
        raw_fan = attributes.get('fan_mode')
        fan_mode = raw_fan if isinstance(raw_fan, str) and raw_fan in fan_modes else (fan_modes[0] if fan_modes else '')
        raw_gap = next((attributes.get(k) for k in ('target_temp_gap', 'min_temp_gap', 'deadband', 'min_deadband')
                        if _number(attributes.get(k)) and float(attributes.get(k)) > 0), None)
        min_gap = max(step_tenths, _signed_round(float(raw_gap) * 10)) if raw_gap is not None else None
        return {
            'available': available,
            'hvac_mode': hvac_mode,
            'hvac_modes': tuple(modes),
            'hvac_action': hvac_action,
            'current_temp': current_temp,
            'current_humidity': current_humidity,
            'target_temp': target_temp,
            'target_low': target_low,
            'target_high': target_high,
            'min_tenths': min_tenths,
            'max_tenths': max_tenths,
            'step_tenths': step_tenths,
            'unit': unit,
            'features': features,
            'fan_modes': tuple(fan_modes),
            'fan_mode': fan_mode,
            'min_gap': min_gap,
        }

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

    def _climate_row(self, entity_id):
        entity = self.entities[entity_id]
        available = entity['available']
        hvac_mode = entity['hvac_mode']
        hvac_action = entity['hvac_action']
        cur = entity['current_temp']
        tgt = entity['target_temp']
        low = entity['target_low']
        high = entity['target_high']
        if not available:
            text = 'Unavailable'
            state_token = 'sdUnavailable'
        elif hvac_mode == 'off':
            text = f'{format_tenths(cur)} · Off' if cur is not None else 'Off'
            state_token = _climate_state_token(hvac_action, hvac_mode)
        elif hvac_mode in ('heat', 'cool'):
            state_token = _climate_state_token(hvac_action, hvac_mode)
            if cur is not None and tgt is not None:
                text = f'{format_tenths(cur)} -> {format_tenths(tgt)}'
            elif tgt is not None:
                text = f'{HVAC_MODE_LABELS[hvac_mode]} {format_tenths(tgt)}'
            elif cur is not None:
                text = f'{format_tenths(cur)} · {HVAC_MODE_LABELS[hvac_mode]}'
            else:
                text = HVAC_MODE_LABELS[hvac_mode]
        elif hvac_mode in ('heat_cool', 'auto'):
            state_token = _climate_state_token(hvac_action, hvac_mode)
            if low is not None and high is not None:
                span = f'{format_tenths(low)}-{format_tenths(high)}'
                text = f'{format_tenths(cur)} -> {span}' if cur is not None else span
            elif tgt is not None:
                text = f'{format_tenths(cur)} -> {format_tenths(tgt)}' if cur is not None else format_tenths(tgt)
            elif cur is not None:
                text = f'{format_tenths(cur)} · {HVAC_MODE_LABELS[hvac_mode]}'
            else:
                text = HVAC_MODE_LABELS[hvac_mode]
        else:
            state_token = _climate_state_token(hvac_action, hvac_mode)
            label = HVAC_MODE_LABELS.get(hvac_mode, 'Idle')
            text = f'{format_tenths(cur)} · {label}' if cur is not None else label
        return DeviceRow('SD:t:' + entity_id, entity['name'], self._title(entity['name'], text),
                         (entity_id,), 'climate', available, available and hvac_mode != 'off',
                         'climate', state_token=state_token)

    def _climate_folder_rows(self, entity_id):
        entity = self.entities[entity_id]
        available = entity['available']
        hvac_mode = entity['hvac_mode']
        hvac_modes = entity['hvac_modes']
        min_t, max_t, step_t, unit = entity['min_tenths'], entity['max_tenths'], entity['step_tenths'], entity['unit']
        spec_tail = f'{min_t}:{max_t}:{step_t}:{unit}'
        setpoint_struct = (min_t, max_t, step_t, unit)
        default_temp = snap_tenths(200 if unit == 'C' else 680, min_t, max_t, step_t)
        mode_text = 'Unavailable' if not available else HVAC_MODE_LABELS.get(hvac_mode, 'Off')
        mode_token = _climate_mode_token(hvac_mode)
        rows = [
            DeviceRow(f'SD:t:{entity_id}:mode', 'Mode', f'Mode: {mode_text}', (entity_id,),
                      'climate', available, hvac_mode != 'off', 'hvacMode',
                      state_token=mode_token, level_spec=f'{hvac_mode}:{",".join(hvac_modes)}',
                      structure=(hvac_mode, hvac_modes))
        ]
        if hvac_mode in ('heat', 'cool'):
            label = 'Heat to' if hvac_mode == 'heat' else 'Cool to'
            st_token = 'sdHeat' if hvac_mode == 'heat' else 'sdCool'
            val = entity['target_temp']
            if val is None:
                val = (entity['target_low'] if hvac_mode == 'heat' else entity['target_high']) or default_temp
            val_text = 'Unavailable' if not available else format_tenths(val)
            rows.append(DeviceRow(f'SD:t:{entity_id}:single', label, f'{label}: {val_text}',
                                  (entity_id,), 'climate', available, True, 'setpoint', val,
                                  state_token=st_token, level_spec=f'{val}:{spec_tail}',
                                  structure=setpoint_struct))
        elif hvac_mode == 'heat_cool' or (hvac_mode == 'auto' and (
                entity['features'] & CLIMATE_TARGET_RANGE or (entity['target_low'] is not None and entity['target_high'] is not None))):
            low = entity['target_low'] if entity['target_low'] is not None else default_temp
            high = entity['target_high'] if entity['target_high'] is not None else snap_tenths(low + (20 if unit == 'F' else 10), min_t, max_t, step_t)
            low_text = 'Unavailable' if not available else format_tenths(low)
            high_text = 'Unavailable' if not available else format_tenths(high)
            rows.append(DeviceRow(f'SD:t:{entity_id}:low', 'Heat to', f'Heat to: {low_text}',
                                  (entity_id,), 'climate', available, True, 'setpoint', low,
                                  state_token='sdHeat', level_spec=f'{low}:{spec_tail}',
                                  structure=setpoint_struct))
            rows.append(DeviceRow(f'SD:t:{entity_id}:high', 'Cool to', f'Cool to: {high_text}',
                                  (entity_id,), 'climate', available, True, 'setpoint', high,
                                  state_token='sdCool', level_spec=f'{high}:{spec_tail}',
                                  structure=setpoint_struct))
        elif hvac_mode == 'auto' and (entity['features'] & CLIMATE_TARGET_TEMP or entity['target_temp'] is not None):
            val = entity['target_temp'] if entity['target_temp'] is not None else default_temp
            val_text = 'Unavailable' if not available else format_tenths(val)
            rows.append(DeviceRow(f'SD:t:{entity_id}:single', 'Heat to', f'Heat to: {val_text}',
                                  (entity_id,), 'climate', available, True, 'setpoint', val,
                                  state_token='sdHeatCool', level_spec=f'{val}:{spec_tail}',
                                  structure=setpoint_struct))
        if entity['fan_modes']:
            fmode = entity['fan_mode']
            fan_text = 'Unavailable' if not available else fan_label(fmode)
            rows.append(DeviceRow(f'SD:t:{entity_id}:fan', 'Fan', f'Fan: {fan_text}',
                                  (entity_id,), 'climate', available, True, 'fanMode',
                                  state_token='sdFan', level_spec=f'{fmode}:{",".join(entity["fan_modes"])}',
                                  structure=(entity['fan_modes'],)))
        if not available:
            now_text = 'Unavailable'
        else:
            parts = []
            if entity['current_temp'] is not None:
                parts.append(format_tenths(entity['current_temp']))
            parts.append(HVAC_ACTION_LABELS.get(entity['hvac_action'])
                         or HVAC_MODE_LABELS.get(hvac_mode, 'Idle'))
            if entity['current_humidity'] is not None:
                parts.append(f'{entity["current_humidity"]}%')
            now_text = ' · '.join(parts)
        rows.append(DeviceRow(f'SD:t:{entity_id}:now', 'Now', f'Now: {now_text}',
                              (entity_id,), 'climate', available, hvac_mode != 'off', 'info',
                              state_token='sdIdle'))
        return tuple(rows)

    def _entity_row(self, entity_id):
        entity = self.entities[entity_id]
        if entity['domain'] == 'climate':
            return self._climate_row(entity_id)
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

    def _area_rows(self, area_id, *, climate=True):
        if area_id not in self.areas: raise KeyError(area_id)
        ids = self.area_entities.get(area_id, ())
        lights = self._individual_lights({area_id})
        rows = []
        if len(lights) >= 2:
            rows.append(self._all_lights('SD:all:' + quote(area_id, safe=''), lights))
        groups, singles, others, scenes = [], [], [], []
        for entity_id in ids:
            entity = self.entities[entity_id]
            if entity['domain'] == 'climate' and not climate:
                continue
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

    def _areas(self, climate=True):
        if climate: return self.catalogue_areas
        return tuple(area for area in self.catalogue_areas
                     if any(self.entities[eid]['domain'] != 'climate' for eid in self.area_entities.get(area, ())))

    def _other_areas(self, rooms, *, climate=True):
        excluded = self._room_areas(rooms)
        return tuple(area for area in self._areas(climate) if area not in excluded)

    def _multiple_floors(self, *, climate=True):
        return len({self._area_floor(area) for area in self._areas(climate)
                    if self._area_floor(area) is not None}) > 1

    def _area_folders(self, areas):
        return tuple(self._folder(area_container(area), self.areas[area].get('name') or area)
                     for area in sorted(areas, key=self._area_sort))

    def rows(self, container_id, rooms=(), *, climate=True):
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
                rows = list(self._area_rows(area, climate=climate)) if area is not None else []
            rows.append(self._folder('SD:other', 'Other Rooms'))
            return tuple(rows)
        if container_id.startswith('SD:r:'):
            room = next((room for room in rooms if room_container(room) == container_id), None)
            if room is None: raise KeyError(container_id)
            area = self.resolve_area(room)
            return self._area_rows(area, climate=climate) if area is not None else (self._folder('SD:other', 'Other Rooms'),)
        if container_id == 'SD:other':
            areas = self._other_areas(rooms, climate=climate)
            if not self._multiple_floors(climate=climate): return self._area_folders(areas)
            floors = {self._area_floor(area) for area in areas}
            return tuple(self._folder('SD:floor:' + quote(floor or '', safe=''),
                         self.floors.get(floor, {}).get('name') or 'No Floor')
                         for floor in sorted(floors, key=self._floor_sort))
        if container_id.startswith('SD:floor:'):
            floor = unquote(container_id[len('SD:floor:'):]) or None
            if not self._multiple_floors(climate=climate) or floor is not None and floor not in self.floors:
                raise KeyError(container_id)
            return self._area_folders(area for area in self._other_areas(rooms, climate=climate)
                                      if self._area_floor(area) == floor)
        if container_id.startswith('SD:a:'):
            segment = container_id[len('SD:a:'):]
            area_id, separator, kind = segment.partition(':')
            area_id = unquote(area_id)
            if area_id not in self.areas: raise KeyError(container_id)
            if not separator: return self._area_rows(area_id, climate=climate)
            if kind == 'scenes':
                ids = [entity_id for entity_id in self.area_entities.get(area_id, ())
                       if self.entities[entity_id]['domain'] in ONE_SHOT]
            elif kind == 'lights':
                ids = [entity_id for entity_id in self._individual_lights({area_id})
                       if entity_id in self.grouped_lights]
            else: raise KeyError(container_id)
            return tuple(sorted((self._entity_row(entity_id) for entity_id in ids),
                                key=lambda row: (row.name.casefold(), row.row_id)))
        if container_id.startswith('SD:t:'):
            if not climate:
                raise KeyError(container_id)
            entity_id = container_id[len('SD:t:'):]
            entity = self.entities.get(entity_id)
            if entity is None or entity['domain'] != 'climate':
                raise KeyError(container_id)
            return self._climate_folder_rows(entity_id)
        raise KeyError(container_id)

    def containers(self, rooms=(), *, climate=True):
        ids = {'SD:', 'SD:other'}
        ids.update(room_container(room) for room in rooms if len(rooms) > 1)
        for area in self._areas(climate):
            ids.add(area_container(area))
            for row in self._area_rows(area, climate=climate):
                if row.folder or (climate and row.level_kind == 'climate'):
                    ids.add(row.row_id)
        if self._multiple_floors(climate=climate):
            ids.update('SD:floor:' + quote(floor or '', safe='')
                for floor in {self._area_floor(area) for area in self._other_areas(rooms, climate=climate)})
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
