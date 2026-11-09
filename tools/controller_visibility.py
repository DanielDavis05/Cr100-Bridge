"""Stable component identities and controller-local discovery preferences.

An omitted/null selection inherits the household's configured components. An
explicit empty list hides all components. These preferences narrow discovery;
they never enable a component disabled in the household configuration.
"""
from __future__ import annotations

BRIDGE_COMPONENT = 'cr100-bridge'
MAX_COMPONENTS = 4096


def room_component_key(ref, slot):
    """Use the backend identity, or a simulated room's persistent player slot."""
    if ref:
        if len(ref) != 2 or not all(isinstance(part, str) and part for part in ref):
            raise ValueError('Invalid room component reference')
        return '%s:%s' % tuple(ref)
    if type(slot) is not int or slot < 0:
        raise ValueError('Invalid simulated room slot')
    return 'sim:%d' % slot


def configured_components(config):
    """Configured primary rooms, without network reads or runtime dependencies."""
    result = []
    for index, room in enumerate(config.get('rooms', [])):
        ref = (('ha', room['entity']) if room.get('entity') else
               (room['backend'], room.get('id', '')) if room.get('backend') else None)
        key = room_component_key(ref, room.get('slot', index))
        name = room.get('name') or room.get('last_known_name') or (ref[1] if ref else 'Room %d' % (index + 1))
        result.append({'key': key, 'name': name, 'kind': 'room'})
    return result


def validate_selection(value, known_keys=None):
    """Validate an explicit selection; optionally restrict it to known identities."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_COMPONENTS:
        raise ValueError('Choose a list of components, or use the household selection.')
    result, seen = [], set()
    for key in value:
        if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ValueError('Invalid component identity.')
        if key in seen:
            raise ValueError('Each component can appear only once.')
        if known_keys is not None and key not in known_keys:
            raise ValueError('Unknown component: %s.' % key)
        result.append(key)
        seen.add(key)
    return result


def visible_components(config, mac):
    """Return None for inheritance, otherwise a set of allowed component keys."""
    profiles = (config or {}).get('controllers') or {}
    profile = (profiles.get(mac.lower()) or {}) if isinstance(mac, str) else {}
    selection = validate_selection(profile.get('visible_components'))
    return None if selection is None else frozenset(selection)


def component_visible(config, mac, key):
    selection = visible_components(config, mac)
    return selection is None or key in selection
