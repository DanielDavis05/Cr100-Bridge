"""Bridge configuration persistence and speaker-selection rules.

This module is shared by the web interface and virtual player process. It owns
configuration file locking, room identities, stable player slots and Sonos
network reconciliation without importing either runtime.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile

import room_icons

MAX_SLOTS = 38                 # player addresses .11-.48
MAX_ROOM_NAME = 30
LIBRARY_KEYS = ('library', 'library_entity')


class ConfigError(Exception):
    """A configuration validation failure with a caller-readable status/message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def write_json(path, value, mode=None):
    """Atomically replace a JSON file, keeping its mode (or the given one)."""
    path = Path(path)
    mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            os.fchmod(target.fileno(), mode)
            json.dump(value, target, indent=1)
            target.write('\n')
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def update_config_file(path, change):
    """Serialize settings writes with the player process's network reconciliation."""
    path = Path(path)
    descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(path.read_text())
        before = json.dumps(config, sort_keys=True)
        change(config)
        if json.dumps(config, sort_keys=True) != before: write_json(path, config)
        return config


def room_key(room):
    """The stable selection key for a config room: its entity, sonos:<UUID>, or sim:<name>."""
    if room.get('entity'): return room['entity']
    if room.get('backend'): return '%s:%s' % (room['backend'], room.get('id', ''))
    return 'sim:' + room.get('name', '')


def room_key_ref(room):
    """Whether a config room plays through a source (not simulated)."""
    return bool(room.get('entity') or room.get('backend'))


def room_for(key):
    """The config room for a source speaker's key."""
    if key.startswith('sonos:'): return {'backend': 'sonos', 'id': key[len('sonos:'):]}
    return {'entity': key}


def apply_rooms(config, wanted, library, source, listing, platforms, automatic=False):
    """Make the config's rooms the wanted ones ({'key', 'name', 'icon'}), in order, and the
    Music menu read from library (a shown speaker's key, or None)."""
    prefix = 'sonos:' if source == 'sonos' else 'media_player.'
    rooms = config.get('rooms', [])
    existing = {room_key(r): r for r in rooms}
    slots = dict(config.get('player_slots') or {})
    for index, room in enumerate(rooms):   # rooms from before slots keep their position
        slots.setdefault(room_key(room), int(room.get('slot', index)))
    new_rooms, names, keys = [], set(), set()
    for item in wanted:
        key = item.get('key') if isinstance(item, dict) else None
        if not isinstance(key, str) or key in keys: raise ConfigError(400, 'Each speaker can appear once.')
        keys.add(key)
        if key.startswith(prefix):
            if key not in listing and key not in existing:
                raise ConfigError(400, '%s is not a %s.' % (key, 'Sonos speaker on this network' if source == 'sonos'
                                                          else 'Home Assistant media player'))
            ref = key
        elif key in existing and not room_key_ref(existing[key]):
            ref = None   # a simulated room from the config file
        else:
            raise ConfigError(400, 'Unknown speaker %s.' % key)
        custom = (item.get('name') or '').strip()
        if len(custom) > MAX_ROOM_NAME:
            raise ConfigError(400, 'Keep room names to %d characters.' % MAX_ROOM_NAME)
        default = listing.get(ref) if ref else existing[key].get('name')
        if not default and ref in existing:
            default = existing[ref].get('name') or existing[ref].get('last_known_name')
        if custom == default: custom = ''
        name = custom or default or (ref.split('.', 1)[-1].replace('_', ' ').title() if source == 'ha' else ref)
        if name.lower() in names:
            if not automatic: raise ConfigError(400, 'Two rooms are both named %s.' % name)
            stem, suffix = name, 2
            while name.lower() in names:
                name = '%s (%d)' % (stem[:MAX_ROOM_NAME - len(str(suffix)) - 3], suffix)
                suffix += 1
            custom = name
        names.add(name.lower())
        room = room_for(ref) if ref else {}
        previous = existing.get(key, {})
        if source == 'sonos' and not previous:
            previous = ((config.get('sonos') or {}).get('room_preferences') or {}).get(key, {})
        # An explicit empty area means no area; truthiness would lose that choice.
        if 'devices_area' in previous: room['devices_area'] = previous['devices_area']
        if custom or not ref or (source != 'sonos' and not listing.get(ref)): room['name'] = name
        if source == 'sonos' and ref:
            room['last_known_name'] = listing.get(ref) or existing.get(key, {}).get('last_known_name') or name
        icon = item.get('icon') or ''
        if icon and icon not in room_icons.ICONS: raise ConfigError(400, 'Unknown room icon %s.' % icon)
        icon = room_icons.picture(icon)
        if icon and icon != room_icons.guess(name): room['icon'] = icon   # else follow the name
        new_rooms.append((key, room))
    # A speaker keeps the slot it had (its player identity on the controller); a new one
    # takes a slot no speaker has used, or failing that one no shown speaker uses.
    assigned = {}
    for key, _ in new_rooms:
        if key in slots and slots[key] not in assigned.values(): assigned[key] = slots[key]
    for key, _ in new_rooms:
        if key in assigned: continue
        free = [s for s in range(MAX_SLOTS) if s not in assigned.values() and s not in slots.values()] \
            or [s for s in range(MAX_SLOTS) if s not in assigned.values()]
        assigned[key] = slots[key] = free[0]
    for key, room in new_rooms: room['slot'] = assigned[key]
    config['rooms'] = [room for _, room in new_rooms]
    # Icons of speakers hidden now are kept, so showing one again restores it.
    kept = dict(config.get('room_icons') or {})
    for room in rooms:
        if room_key_ref(room) and room.get('icon'): kept[room_key(room)] = room['icon']
    for key, room in new_rooms:
        if room_key_ref(room): kept.pop(key, None)
    if kept: config['room_icons'] = kept
    else: config.pop('room_icons', None)
    config['player_slots'] = {k: v for k, v in slots.items() if k.startswith(('media_player.', 'sonos:'))}
    if library is not None and (library not in keys or not library.startswith(prefix)
                                or (source == 'ha' and platforms.get(library, 'sonos') != 'sonos')):
        raise ConfigError(400, 'Choose a Sonos speaker that is shown on the controller for the library.')
    menu = config.get('music_menu') or {}
    for name in LIBRARY_KEYS: menu.pop(name, None)
    if library and source == 'sonos': menu['library'] = room_for(library)
    elif library: menu['library_entity'] = library
    if menu or 'music_menu' in config: config['music_menu'] = menu
    if source == 'sonos':
        settings = config.setdefault('sonos', {})
        preferences = settings.setdefault('room_preferences', {})
        for room in rooms + config['rooms']:
            if room.get('backend') == 'sonos': preferences[room_key(room)] = dict(room)
        if not automatic:
            hidden = (set(settings.get('hidden', [])) | (set(listing) - keys)) - keys
            settings['hidden'] = sorted(hidden)
            if library: settings['library_id'] = library[len('sonos:'):]
            else: settings.pop('library_id', None)


def reconcile_sonos_rooms(config, available, pending=None):
    """Advertise current-network speakers; keep settings for absent/hidden ones."""
    settings = config.get('sonos') or {}
    if config.get('backend') != 'sonos' or not settings.get('follow_network', True): return
    settings = config.setdefault('sonos', {})
    rooms = config.get('rooms', [])
    selected = {room_key(room) for room in rooms if room.get('backend') == 'sonos'}
    # Migrate an existing selection on the same network without enabling rooms
    # that the user had hidden. A disjoint household is a new network's selection.
    if 'hidden' not in settings:
        found = set(available) | set(pending or {})
        settings['hidden'] = sorted(found - selected) if found & selected else []
    hidden = set(settings['hidden'])
    preferences = settings.setdefault('room_preferences', {})
    for room in rooms:
        if room.get('backend') == 'sonos': preferences[room_key(room)] = dict(room)
    listing = dict(available)
    # A rediscovered, already selected speaker may still be finishing its first
    # read. Never re-add a previously failed speaker solely from cached topology.
    listing.update({key: name for key, name in (pending or {}).items() if key in selected})
    wanted = []
    for key in sorted(set(listing) - hidden, key=lambda key: (listing[key].lower(), key)):
        preference = preferences.get(key, {})
        custom = preference.get('name') or ''
        if custom == preference.get('last_known_name'): custom = ''
        wanted.append({'key': key, 'name': custom,
                       'icon': preference.get('icon') or ''})
    simulated = [{'key': room_key(room), 'name': room.get('name'), 'icon': room.get('icon')}
                 for room in rooms if not room_key_ref(room)]
    wanted = wanted[:max(0, MAX_SLOTS - len(simulated))] + simulated
    menu_library = (config.get('music_menu') or {}).get('library') or {}
    if menu_library.get('backend') == 'sonos': settings['library_id'] = menu_library['id']
    library = 'sonos:' + settings['library_id'] if settings.get('library_id') else None
    if library not in {item['key'] for item in wanted}: library = None
    apply_rooms(config, wanted, library, 'sonos', listing, {}, automatic=True)

