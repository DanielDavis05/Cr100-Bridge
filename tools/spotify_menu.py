"""Validated controller-local Spotify navigation and scoped item identities."""
from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit

LOCK_MESSAGE = 'This feature has been locked by your administrator'
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z', re.I)
SPOTIFY_URI = re.compile(r'spotify:(playlist|album|track):([A-Za-z0-9]{22})\Z')
BUILTINS = {'search': 'Search', 'playlists': 'My Playlists', 'albums': 'Saved Albums', 'tracks': 'Liked Songs'}
ENTRY_ID = re.compile(r'(?:search|playlists|albums|tracks|entry_[A-Za-z0-9_-]{1,64})\Z')
FOLDER_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
ALIAS_PREFIX = 'bm:'
MAX_MENU_ITEMS = 64


class FeatureLocked(ValueError):
    def __init__(self):
        super().__init__(LOCK_MESSAGE)


def default_items():
    return [{'id': key, 'kind': 'builtin', 'target': key, 'label': label, 'locked': False}
            for key, label in BUILTINS.items()]


def normalize_spotify_uri(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('Paste a Spotify playlist, album, single, or track link.')
    value = value.strip()
    if SPOTIFY_URI.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.hostname != 'open.spotify.com' or parsed.port not in (None, 443) \
                or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        match = re.fullmatch(r'/(?:intl-[A-Za-z-]+/)?(playlist|album|track)/([A-Za-z0-9]{22})/?', parsed.path)
        if not match:
            raise ValueError
        return 'spotify:%s:%s' % match.groups()
    except ValueError:
        raise ValueError('Paste a full open.spotify.com playlist, album, single, or track link.') from None


def validate_items(items):
    if not isinstance(items, list) or not 4 <= len(items) <= MAX_MENU_ITEMS:
        raise ValueError('Keep the four standard sections and at most %d menu entries.' % MAX_MENU_ITEMS)
    result, seen, builtins = [], set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Invalid Spotify menu entry.')
        ident, kind, target = item.get('id'), item.get('kind'), item.get('target')
        if not isinstance(ident, str) or not ENTRY_ID.fullmatch(ident) or ident in seen:
            raise ValueError('Menu entries need unique, stable identifiers.')
        seen.add(ident)
        label = item.get('label')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 \
                or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError('Give each menu entry a name of 1 to 80 characters.')
        locked = item.get('locked', False)
        if type(locked) is not bool:
            raise ValueError('A feature lock must be on or off.')
        if kind == 'builtin':
            if target not in BUILTINS or ident != target or target in builtins:
                raise ValueError('Invalid standard Spotify section.')
            builtins.add(target)
        elif kind == 'spotify':
            if not ident.startswith('entry_'):
                raise ValueError('Custom shortcuts need their own identifier.')
            target = normalize_spotify_uri(target)
        elif kind == 'folder':
            if not ident.startswith('entry_') or not isinstance(target, str) or not FOLDER_ID.fullmatch(target):
                raise ValueError('Choose a valid Spotify folder.')
        else:
            raise ValueError('Unsupported Spotify menu entry.')
        result.append({'id': ident, 'kind': kind, 'target': target, 'label': label.strip(), 'locked': locked})
    if builtins != set(BUILTINS):
        raise ValueError('Keep Search, My Playlists, Saved Albums, and Liked Songs in the menu.')
    return result


def menu_for_controller(settings, mac):
    menus = (settings or {}).get('controller_menus') or {}
    profile = menus.get(mac.lower()) if isinstance(mac, str) else None
    if profile is None:
        return default_items()
    if not isinstance(profile, dict):
        raise ValueError('Invalid controller Spotify menu.')
    return validate_items(profile.get('items'))


def scoped_id(entry_id, raw_id):
    if not isinstance(entry_id, str) or not ENTRY_ID.fullmatch(entry_id) \
            or not isinstance(raw_id, str) or not 1 <= len(raw_id) <= 512 \
            or any(ord(c) < 32 for c in raw_id):
        raise ValueError('Invalid scoped Spotify item.')
    encoded = base64.urlsafe_b64encode(raw_id.encode('utf-8')).decode('ascii').rstrip('=')
    return ALIAS_PREFIX + entry_id + ':' + encoded


def unscoped_id(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError('Invalid scoped Spotify item.')
    parts = value.split(':', 2)
    if len(parts) != 3 or parts[0] != 'bm' or not ENTRY_ID.fullmatch(parts[1]) \
            or not re.fullmatch(r'[A-Za-z0-9_-]+', parts[2]):
        raise ValueError('Invalid scoped Spotify item.')
    try:
        raw = base64.b64decode(parts[2] + '=' * (-len(parts[2]) % 4), altchars=b'-_', validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('Invalid scoped Spotify item.') from None
    if scoped_id(parts[1], raw) != value:
        raise ValueError('Noncanonical scoped Spotify item.')
    return parts[1], raw
