#!/usr/bin/env python3
"""Web interface for the CR100 bridge: speakers, controllers, Spotify and the speaker source.

Served on port 8080 of every IPv4 interface behind a password; requests from the
CR100's own network (sonos0) are refused.
It edits bridge-config.json and applies speaker and music-service changes by sending
SIGHUP to zoneplayers.py, which restarts the virtual players in place. The radio link
(sonosnet_link.py) is never restarted from here: a restart resets the beacon clock and
costs the CR100 a drop. Controller status comes from the link's log (the 'stations' in
its stats lines), the DHCP leases, and each CR100's own /status pages.

Built for several controllers: the state file may list them under "controllers"
(today the link and pairing handle one, "cr100_mac").

Speaker source: the controller's rooms come from Home Assistant or straight from Sonos
players (config "backend", see backends.py). Switching keeps the other source's rooms aside
("stashed_sources"), so switching back restores them.

Usage: sudo python3 webui.py --config bridge-config.json --state bridge-state.json --run-dir <bridge_run dir>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import room_icons

_HERE = Path(__file__).resolve().parent
STATIC_DIR = _HERE / 'webui'
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
                '.ico': 'image/x-icon'}
CR100_NETWORK = ipaddress.ip_network('192.168.69.0/24')   # sonos0: never serve the controllers' side
MAX_SLOTS = 38                 # zoneplayers.MAX_SLOTS (player addresses .11-.48)
MAX_ROOM_NAME = 30
MIN_PASSWORD = 8
PBKDF2_ROUNDS = 300_000
SESSION_SECONDS = 30 * 24 * 3600
SESSION_COOKIE = 'bridge_session'
BODY_LIMIT = 64 * 1024
LINK_FRESH = 30                # s: a stats line older than this means the radio link is not running
CONNECTED_WITHIN = 20          # s since the CR100 was last heard
AWAY_AFTER = 120               # s searching or silent: asleep (standby) or out of range
DETAILS_POLL = 30              # s between /status reads of each connected CR100
STATUS_TIMEOUT = 3
HA_TEST_WAIT = 8
SONOS_FRESH = 10               # s a read of the Sonos household is reused
SOURCES = {'ha': 'Home Assistant', 'sonos': 'Sonos'}
LIBRARY_KEYS = ('library', 'library_entity')   # music_menu keys naming where the Music menu is read
MAX_HOSTS = 16
SPOTIFY_SIGN_IN_TTL = 900
LINK_EVENTS = ('associated', 'resumed', 'cr100-searching', 'link-heard', 'link-silent', 'radio-lost',
               'radio-reopened', 'disassoc', 'deauth')
FREQ_CHANNEL = {2412: 1, 2437: 6, 2462: 11}


class ApiError(Exception):
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


def write_secret(path, text):
    """Atomically replace a file with mode 0600, whatever mode the old one had."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(text)
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise


def resolve(base_file, value, default):
    """A path from the config, relative to the config file's directory."""
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else Path(base_file).resolve().parent / path


# --- password and sessions ---------------------------------------------------------
class Auth:
    """One password for the page, stored as a PBKDF2 hash in the web interface's data file."""
    def __init__(self, data):
        self.data, self.sessions, self.lock = data, {}, threading.Lock()

    @property
    def password_set(self):
        return 'password' in self.data.values

    def set_password(self, password):
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise ApiError(400, 'Use a password of at least %d characters.' % MIN_PASSWORD)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PBKDF2_ROUNDS)
        self.data.update(password={'salt': salt.hex(), 'rounds': PBKDF2_ROUNDS, 'hash': digest.hex()})

    def check(self, password):
        stored = self.data.values.get('password')
        if not stored or not isinstance(password, str): return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(stored['salt']), stored['rounds'])
        return hmac.compare_digest(digest.hex(), stored['hash'])

    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self.lock: self.sessions[token] = time.time() + SESSION_SECONDS
        return token

    def valid(self, token):
        with self.lock:
            expiry = self.sessions.get(token or '')
            if expiry and expiry < time.time(): self.sessions.pop(token, None); expiry = None
            return bool(expiry)

    def end(self, token):
        with self.lock: self.sessions.pop(token or '', None)


class DataFile:
    """The web interface's own small state (password hash, cached Spotify account), mode 0600."""
    def __init__(self, path):
        self.path, self.lock = Path(path), threading.Lock()
        self.values = json.loads(self.path.read_text()) if self.path.exists() else {}

    def update(self, **values):
        with self.lock:
            self.values.update(values)
            write_json(self.path, self.values, mode=0o600)


# --- the bridge --------------------------------------------------------------------
class Bridge:
    def __init__(self, config_path, state_path, run_dir, players_pid, data, log=print, ha_factory=None,
                 token_exchange=None, profile_fetch=None, managed=False):
        self.config_path, self.state_path = Path(config_path), Path(state_path)
        self.run_dir = Path(run_dir) if run_dir else None
        self.players_pid, self.data, self.log = Path(players_pid), data, log
        self.config_lock = threading.Lock()
        self.ha_factory = ha_factory or default_ha_factory
        self.token_exchange = token_exchange or default_token_exchange
        self.profile_fetch = profile_fetch or default_profile_fetch
        self.ha, self.ha_info, self.registry = None, {}, (None, {})
        self.sonos_cache, self.sonos_lock = (0, None), threading.Lock()
        self.details, self.known_ips = {}, {}
        self.spotify_pending = None
        self.stop = threading.Event()
        self.managed = managed

    # config --------------------------------------------------------------------
    def config(self):
        return json.loads(self.config_path.read_text())

    def update_config(self, change):
        with self.config_lock:
            config = self.config()
            change(config)
            write_json(self.config_path, config)
            return config

    def reload_players(self):
        """SIGHUP zoneplayers.py: it restarts in place with the new config (not the radio)."""
        try:
            pid = int(self.players_pid.read_text().strip())
            os.kill(pid, signal.SIGHUP)
        except (OSError, ValueError):
            return {'applied': False, 'message': 'Saved. The bridge is not running here, so the change '
                                                 'applies when it next starts.'}
        self.log({'event': 'players-reload', 'pid': pid})
        return {'applied': True, 'message': 'Saved. The controller shows the change in a few seconds.'}

    def start(self):
        if self.source() == 'ha': self.connect_ha()
        threading.Thread(target=self.poll_controllers, name='controller-poll', daemon=True).start()

    def close(self):
        self.stop.set()
        if self.ha: self.ha.close()

    # Home Assistant --------------------------------------------------------------
    def ha_settings(self):
        settings = self.config().get('home_assistant') or {}
        return settings.get('url', ''), resolve(self.config_path, settings.get('token_file'), 'ha-token')

    def connect_ha(self):
        if self.ha: self.ha.close(); self.ha = None
        self.ha_info, self.registry = {}, (None, {})
        url, token_file = self.ha_settings()
        try: token = token_file.read_text().strip()
        except OSError: token = ''
        if not url or not token:
            self.ha_info = {'error': 'Add the Home Assistant address and a token.'}
            return
        self.ha = self.ha_factory(url, token, self.on_ha_log)
        self.ha.start()

    def on_ha_log(self, record):
        event = record.get('event')
        if event == 'ha-connected': self.ha_info = {'version': record.get('version')}
        elif event == 'ha-auth-failed': self.ha_info = {'error': 'Home Assistant rejected the token.'}
        elif event == 'ha-disconnected': self.ha_info = {'error': 'Cannot reach Home Assistant.'}
        self.log(record)

    def ha_connected(self):
        return bool(self.ha and self.ha.connected.is_set())

    def media_players(self):
        if not self.ha: return {}
        return {e: s for e, s in list(self.ha.states.items()) if e.startswith('media_player.')}

    def platforms(self):
        """entity_id -> integration (sonos, cast, ...), from the entity registry, cached a minute."""
        fetched, platforms = self.registry
        if self.ha_connected() and (fetched is None or time.monotonic() - fetched > 60):
            try:
                entries = self.ha.call({'type': 'config/entity_registry/list'})
                platforms = {e['entity_id']: e.get('platform') for e in entries if e.get('entity_id')}
                self.registry = (time.monotonic(), platforms)
            except Exception as error:   # older Home Assistant or a non-admin token: no badges
                self.log({'event': 'ha-registry-failed', 'error': repr(error)})
                self.registry = (time.monotonic(), platforms)
        return platforms

    def home_assistant(self):
        url, token_file = self.ha_settings()
        info = dict(self.ha_info)
        return {'url': url, 'token_saved': token_file.exists(), 'connected': self.ha_connected(),
                'version': info.get('version'), 'error': None if self.ha_connected() else info.get('error'),
                'media_players': len(self.media_players())}

    def test_home_assistant(self, url, token):
        url = check_url(url)
        if not token:
            _, token_file = self.ha_settings()
            try: token = token_file.read_text().strip()
            except OSError: raise ApiError(400, 'Paste a long-lived access token to test.')
        seen = {}
        client = self.ha_factory(url, token, lambda record: seen.setdefault(record.get('event'), record))
        client.start()
        try:
            if client.connected.wait(HA_TEST_WAIT):
                players = sum(1 for e in client.states if e.startswith('media_player.'))
                version = (seen.get('ha-connected') or {}).get('version')
                return {'ok': True, 'message': 'Connected to Home Assistant %s. It has %d media players.'
                        % (version or '', players)}
            if 'ha-auth-failed' in seen: return {'ok': False, 'message': 'Home Assistant rejected the token.'}
            return {'ok': False, 'message': 'Could not reach Home Assistant at %s.' % url}
        finally:
            client.close()

    def save_home_assistant(self, url, token):
        url = check_url(url)
        _, token_file = self.ha_settings()
        if token:
            if len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ApiError(400, 'That does not look like an access token.')
            token_file.parent.mkdir(parents=True, exist_ok=True)
            write_secret(token_file, token + '\n')
        def change(config):
            settings = config.setdefault('home_assistant', {})
            settings['url'] = url
            settings.setdefault('token_file', str(token_file))
        self.update_config(change)
        self.connect_ha()
        return self.reload_players()

    # speaker source ----------------------------------------------------------------
    def source(self, config=None):
        """Where the controller's rooms come from (config "backend"): 'ha', the default, or 'sonos'."""
        name = (config if config is not None else self.config()).get('backend') or 'ha'
        return name if name in SOURCES else 'ha'

    def source_connected(self, source=None, wait=True):
        """Whether the source answers; without wait, from what is known now (the status badge)."""
        source = source or self.source()
        if source != 'sonos': return self.ha_connected()
        return not (self.sonos_household() if wait else self.sonos_known())['error']

    def source_status(self):
        source = self.source()
        return {'source': source, 'sources': [{'name': n, 'label': l} for n, l in SOURCES.items()],
                'home_assistant': self.home_assistant(), 'sonos': self.sonos()}

    def switch_source(self, source):
        """Take the controller's rooms from another source. The current source's rooms and Music
        menu choice are set aside in the config and come back when it is chosen again; a source
        chosen for the first time shows its Sonos speakers."""
        if source not in SOURCES: raise ApiError(400, 'Choose Home Assistant or Sonos.')
        current = self.source()
        if source == current: return {'applied': False, 'message': 'The controller already uses %s.' % SOURCES[source]}
        listing = self.listing(source)
        platforms = self.platforms() if source == 'ha' else {}
        defaults = [key for key in listing if source == 'sonos' or platforms.get(key) == 'sonos'][:MAX_SLOTS]

        def change(config):
            # Remember the outgoing rooms' slots, so the new source's speakers take unused ones
            # and every speaker keeps its player identity on the controller across switches.
            slots = config.setdefault('player_slots', {})
            for index, room in enumerate(config.get('rooms', [])):
                if room_key_ref(room): slots.setdefault(room_key(room), int(room.get('slot', index)))
            stash = config.setdefault('stashed_sources', {})
            menu = config.get('music_menu') or {}
            stash[current] = {'rooms': config.get('rooms', []), 'music_menu': {k: menu[k] for k in LIBRARY_KEYS if k in menu}}
            restored = stash.pop(source, None)
            for key in LIBRARY_KEYS: menu.pop(key, None)
            config['backend'] = source
            if restored:
                config['rooms'] = restored['rooms']
                menu.update(restored['music_menu'])
                if menu: config['music_menu'] = menu
                else: config.pop('music_menu', None)
            else:
                config['rooms'] = []
                if menu: config['music_menu'] = menu
                else: config.pop('music_menu', None)
                self.apply_rooms(config, [{'key': key} for key in defaults], None, source, listing, platforms)

        self.update_config(change)
        if source == 'ha': self.connect_ha()
        elif self.ha: self.ha.close(); self.ha = None
        self.log({'event': 'source-switched', 'from': current, 'to': source})
        return self.reload_players()

    # Sonos -------------------------------------------------------------------------
    def sonos_hosts(self, config=None):
        hosts = ((config if config is not None else self.config()).get('sonos') or {}).get('hosts') or []
        return [hosts] if isinstance(hosts, str) else list(hosts)

    def sonos_household(self, fresh=False, hosts=None):
        """The Sonos household as this page shows it: zones with what each group plays. The
        configured one is re-read at most every SONOS_FRESH s; other hosts (a test) every time."""
        import sonos_upnp
        with self.sonos_lock:
            cached_at, cached = self.sonos_cache
            if hosts is None and not fresh and cached is not None and time.monotonic() - cached_at < SONOS_FRESH:
                return cached
            try:
                _, groups, zones = sonos_upnp.household(self.sonos_hosts() if hosts is None else hosts, STATUS_TIMEOUT)
            except sonos_upnp.UPnPFault as error:
                result = {'error': 'No Sonos speakers answered (%s). Check that the bridge is on their network, '
                                   'or add a speaker\'s address.' % error, 'zones': []}
            else:
                listed = []
                for coordinator, members in groups:
                    state, playing = sonos_playing(zones[coordinator]['base'])
                    for uuid in members:
                        listed.append({'uuid': uuid, 'name': zones[uuid]['name'], 'base': zones[uuid]['base'],
                                       'state': state, 'playing': playing,
                                       'group': [zones[u]['name'] for u in members] if len(members) > 1 else []})
                result = {'error': None, 'zones': listed}
            if hosts is None: self.sonos_cache = (time.monotonic(), result)
            return result

    def sonos_known(self):
        """The last read of the Sonos household without waiting for the network; a stale one
        starts a fresh read in the background."""
        cached_at, cached = self.sonos_cache
        if (cached is None or time.monotonic() - cached_at >= SONOS_FRESH) and not self.sonos_lock.locked():
            threading.Thread(target=self.sonos_household, name='sonos-read', daemon=True).start()
        return cached or {'error': 'The Sonos speakers have not been read yet.', 'zones': []}

    def sonos(self):
        hosts, household = self.sonos_hosts(), self.sonos_household()
        return {'hosts': hosts, 'connected': not household['error'], 'error': household['error'],
                'speakers': sorted(z['name'] for z in household['zones'])}

    def test_sonos(self, hosts):
        household = self.sonos_household(hosts=check_hosts(hosts))
        if household['error']: return {'ok': False, 'message': household['error']}
        names = sorted(z['name'] for z in household['zones'])
        return {'ok': True, 'message': 'Found %d Sonos speaker%s: %s.' % (len(names), '' if len(names) == 1 else 's',
                                                                         ', '.join(names))}

    def save_sonos(self, hosts):
        hosts = check_hosts(hosts)
        self.update_config(lambda config: config.setdefault('sonos', {}).update(hosts=hosts))
        self.sonos_cache = (0, None)
        if self.source() == 'sonos': return self.reload_players()
        return {'applied': False, 'message': 'Saved. Sonos is used once you choose it as the speaker source.'}

    # speakers --------------------------------------------------------------------
    def listing(self, source):
        """key -> the source's own name, for every speaker the source knows now."""
        if source == 'sonos':
            return {'sonos:' + zone['uuid']: zone['name'] for zone in self.sonos_household()['zones']}
        return {entity: friendly(state) for entity, state in self.media_players().items()}

    def speakers(self):
        config = self.config()
        source = self.source(config)
        rows, shown = [], set()
        hidden_icons = config.get('room_icons') or {}   # chosen while shown, kept while hidden
        menu = config.get('music_menu') or {}
        if source == 'sonos':
            zones = {'sonos:' + z['uuid']: z for z in self.sonos_household()['zones']}
            for room in config.get('rooms', []):
                key = room_key(room)
                shown.add(key)
                rows.append(sonos_row(key, zones.get(key), room, True, room.get('icon')))
            for key in sorted(zones, key=lambda k: zones[k]['name'].lower()):
                if key not in shown: rows.append(sonos_row(key, zones[key], {}, False, hidden_icons.get(key)))
            library = 'sonos:' + menu['library']['id'] if (menu.get('library') or {}).get('backend') == 'sonos' else None
        else:
            states, platforms = self.media_players(), self.platforms()
            for room in config.get('rooms', []):
                entity = room.get('entity')
                key = room_key(room)
                shown.add(key)
                rows.append(self.speaker_row(key, entity, states.get(entity), platforms, room.get('name'), True,
                                             room.get('icon')))
            for entity in sorted(states, key=lambda e: friendly(states[e]).lower()):
                if entity not in shown:
                    rows.append(self.speaker_row(entity, entity, states[entity], platforms, None, False,
                                                 hidden_icons.get(entity)))
            library = menu.get('library_entity')
        return {'speakers': rows, 'library': library, 'library_entity': library, 'source': source,
                'source_label': SOURCES[source], 'source_connected': self.source_connected(source),
                'ha_connected': self.ha_connected(),
                'max_shown': MAX_SLOTS, 'icons': [{'name': n, 'label': l} for n, l, _ in room_icons.PICTURES],
                'icon_guesses': room_icons.GUESSES, 'default_icon': room_icons.DEFAULT}

    @staticmethod
    def speaker_row(key, entity, state, platforms, custom_name, shown, custom_icon=None):
        attributes = (state or {}).get('attributes', {})
        ha_name = attributes.get('friendly_name') or (entity.split('.', 1)[-1].replace('_', ' ').title()
                                                      if entity else None)
        title, artist = attributes.get('media_title'), attributes.get('media_artist')
        return row(key, ha_name, custom_name, shown, custom_icon, entity=entity, simulated=entity is None,
                   missing=entity is not None and state is None, state=(state or {}).get('state'),
                   playing=' – '.join(x for x in (title, artist) if x) or None, sub=entity or '',
                   platform=platforms.get(entity) if entity else None,
                   sonos=platforms.get(entity) == 'sonos' if entity else False)

    def save_speakers(self, payload):
        wanted = payload.get('rooms')
        if not isinstance(wanted, list): raise ApiError(400, 'Expected a list of rooms.')
        if len(wanted) > MAX_SLOTS: raise ApiError(400, 'The controller can show at most %d speakers.' % MAX_SLOTS)
        source = self.source()
        listing, platforms = self.listing(source), self.platforms() if source == 'ha' else {}
        library = payload.get('library', payload.get('library_entity')) or None
        self.update_config(lambda config: self.apply_rooms(config, wanted, library, source, listing, platforms))
        self.log({'event': 'speakers-saved', 'rooms': [r.get('key') for r in wanted]})
        return self.reload_players()

    @staticmethod
    def apply_rooms(config, wanted, library, source, listing, platforms):
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
            if not isinstance(key, str) or key in keys: raise ApiError(400, 'Each speaker can appear once.')
            keys.add(key)
            if key.startswith(prefix):
                if key not in listing and key not in existing:
                    raise ApiError(400, '%s is not a %s.' % (key, 'Sonos speaker on this network' if source == 'sonos'
                                                              else 'Home Assistant media player'))
                ref = key
            elif key in existing and not room_key_ref(existing[key]):
                ref = None   # a simulated room from the config file
            else:
                raise ApiError(400, 'Unknown speaker %s.' % key)
            custom = (item.get('name') or '').strip()
            if len(custom) > MAX_ROOM_NAME:
                raise ApiError(400, 'Keep room names to %d characters.' % MAX_ROOM_NAME)
            default = listing.get(ref) if ref else existing[key].get('name')
            if not default and ref in existing: default = existing[ref].get('name')
            if custom == default: custom = ''
            name = custom or default or (ref.split('.', 1)[-1].replace('_', ' ').title() if source == 'ha' else ref)
            if name.lower() in names: raise ApiError(400, 'Two rooms are both named %s.' % name)
            names.add(name.lower())
            room = room_for(ref) if ref else {}
            if custom or not ref or not listing.get(ref): room['name'] = name
            icon = item.get('icon') or ''
            if icon and icon not in room_icons.ICONS: raise ApiError(400, 'Unknown room icon %s.' % icon)
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
            raise ApiError(400, 'Choose a Sonos speaker that is shown on the controller for the library.')
        menu = config.get('music_menu') or {}
        for name in LIBRARY_KEYS: menu.pop(name, None)
        if library and source == 'sonos': menu['library'] = room_for(library)
        elif library: menu['library_entity'] = library
        if menu or 'music_menu' in config: config['music_menu'] = menu

    # controllers -----------------------------------------------------------------
    def state(self):
        try: return json.loads(self.state_path.read_text())
        except FileNotFoundError: return {}

    def pairing_status(self):
        if not self.managed: return {'available': False, 'running': False}
        result = subprocess.run(['systemctl', 'show', 'cr100-pair.service',
                                 '--property=ActiveState', '--property=Result'],
                                capture_output=True, text=True, timeout=5, check=True)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return {'available': True, 'running': values.get('ActiveState') in ('activating', 'active', 'deactivating'),
                'failed': values.get('Result', 'success') != 'success'}

    def pair_controller(self):
        if not self.managed: raise ApiError(400, 'Pairing requires the installed bridge services.')
        subprocess.run(['systemctl', '--no-block', 'start', 'cr100-pair.service'],
                       capture_output=True, text=True, timeout=5, check=True)
        return {'message': 'Pairing started. Put the CR100 in its add-to-household setup mode.'}

    def controller_macs(self, state):
        listed = [c.get('mac') for c in state.get('controllers', []) if c.get('mac')]
        return [m.lower() for m in listed] or ([state['cr100_mac'].lower()] if state.get('cr100_mac') else [])

    def link_status(self):
        """The radio link's latest stats line and recent events, from its log."""
        path = self.run_dir / 'link.jsonl' if self.run_dir else None
        if not path or not path.exists(): return {'running': False, 'stations': {}, 'events': []}
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - 256 * 1024))
            lines = source.read().decode('utf-8', 'replace').splitlines()
        stats, events = None, []
        for line in lines:
            try: record = json.loads(line)
            except ValueError: continue
            if record.get('event') == 'stats' and 'wall' in record: stats = record
            elif record.get('event') in LINK_EVENTS: events.append(record)
        if not stats: return {'running': False, 'stations': {}, 'events': []}
        offset = stats['wall'] - stats['t']   # wall clock of this link process's t = 0
        timeline = [{'event': e['event'], 'time': round(offset + e['t'], 1), 'reason': e.get('reason')}
                    for e in events if e['t'] <= stats['t'] + 60][-12:]
        age = max(0.0, time.time() - stats['wall'])
        stations = {}
        for station in stats.get('stations', []):   # 'last heard' counted to now, not to the log line
            heard = station.get('last_heard_s')
            stations[station['mac'].lower()] = {**station, 'last_heard_s': None if heard is None else round(heard + age, 1)}
        return {'running': age < LINK_FRESH, 'stations': stations, 'events': timeline}

    def leases(self):
        found = {}
        if self.run_dir and (self.run_dir / 'leases').exists():
            for line in (self.run_dir / 'leases').read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3: found[parts[1].lower()] = parts[2]
        try:
            neighbours = subprocess.run(['ip', 'neigh', 'show', 'dev', 'sonos0'], capture_output=True,
                                        text=True, timeout=2).stdout
            for address, mac in re.findall(r'^(\S+) lladdr (\S+)', neighbours, re.M):
                found.setdefault(mac.lower(), address)
        except (OSError, subprocess.SubprocessError):
            pass
        for mac, address in found.items(): self.known_ips[mac] = address
        return dict(self.known_ips)

    def controllers(self):
        state, config = self.state(), self.config()
        names = config.get('controllers') or {}
        link, ips = self.link_status(), self.leases()
        result = []
        for index, mac in enumerate(self.controller_macs(state)):
            station = link['stations'].get(mac)
            status, label = controller_status(link['running'], station)
            details = self.details.get(mac, {})
            result.append({'mac': mac, 'name': (names.get(mac) or {}).get('name') or
                           ('Controller' if index == 0 else 'Controller %d' % (index + 1)),
                           'status': status, 'status_label': label, 'ip': ips.get(mac),
                           'last_heard_s': station.get('last_heard_s') if station else None,
                           'events': link['events'] if len(state.get('controllers', [])) <= 1 else [],
                           **details})
        return {'controllers': result, 'household': state.get('household'),
                'channel': FREQ_CHANNEL.get(state.get('freq')), 'radio_running': link['running'],
                'multi_controller': False, 'pairing': self.pairing_status()}

    def rename_controller(self, mac, name):
        name = (name or '').strip()
        if not 0 < len(name) <= MAX_ROOM_NAME: raise ApiError(400, 'Give the controller a name up to %d characters.'
                                                               % MAX_ROOM_NAME)
        if mac.lower() not in self.controller_macs(self.state()): raise ApiError(404, 'No such controller.')
        self.update_config(lambda c: c.setdefault('controllers', {}).setdefault(mac.lower(), {}).update(name=name))
        return {'message': 'Renamed.'}

    def poll_controllers(self):
        while not self.stop.wait(1 if not self.details else DETAILS_POLL):
            try: self.refresh_details()
            except Exception as error: self.log({'event': 'controller-poll-failed', 'error': repr(error)})

    def refresh_details(self):
        link, ips = self.link_status(), self.leases()
        for mac in self.controller_macs(self.state()):
            station, ip = link['stations'].get(mac), ips.get(mac)
            if not ip or not station or (station.get('last_heard_s') or 1e9) > 60: continue
            details = read_cr100_status(ip)
            if details: self.details[mac] = {**details, 'details_at': round(time.time())}

    # Spotify ---------------------------------------------------------------------
    def spotify_settings(self):
        settings = self.config().get('spotify_music_service') or {}
        return settings, resolve(self.config_path, settings.get('token_file'), 'spotify-token.json')

    def spotify(self):
        from spotify_auth import DEFAULT_REDIRECT_URI, status
        settings, token_file = self.spotify_settings()
        client_id = settings.get('client_id', '')
        authorized = bool(client_id) and status(client_id, token_file).get('authorized', False)
        account = self.data.values.get('spotify_account') if authorized else None
        return {'enabled': bool(settings) and settings.get('enabled', True), 'name': settings.get('name', 'Spotify'),
                'client_id': client_id, 'redirect_uri': settings.get('redirect_uri', DEFAULT_REDIRECT_URI),
                'authorized': authorized, 'account': account,
                'signing_in': bool(self.spotify_pending and self.spotify_pending['expires'] > time.time())}

    def save_spotify(self, payload):
        client_id = (payload.get('client_id') or '').strip()
        name = (payload.get('name') or '').strip() or 'Spotify'
        if client_id and not re.fullmatch(r'[0-9a-fA-F]{32}', client_id):
            raise ApiError(400, 'A Spotify Client ID is 32 letters and digits, from the Spotify developer dashboard.')
        if len(name) > 24: raise ApiError(400, 'Keep the service name to 24 characters.')
        enabled = bool(payload.get('enabled'))
        def change(config):
            settings = config.setdefault('spotify_music_service', {})
            settings.update(enabled=enabled, name=name, client_id=client_id)
        self.update_config(change)
        return self.reload_players()

    def spotify_begin(self):
        from spotify_auth import DEFAULT_REDIRECT_URI, authorization_url, pkce_pair
        settings, _ = self.spotify_settings()
        client_id = settings.get('client_id')
        if not client_id: raise ApiError(400, 'Save your Spotify Client ID first.')
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        redirect_uri = settings.get('redirect_uri', DEFAULT_REDIRECT_URI)
        self.spotify_pending = {'verifier': verifier, 'state': state, 'redirect_uri': redirect_uri,
                                'client_id': client_id, 'expires': time.time() + SPOTIFY_SIGN_IN_TTL}
        return {'url': authorization_url(client_id, redirect_uri, state, challenge)}

    def spotify_finish(self, pasted):
        pending = self.spotify_pending
        if not pending or pending['expires'] < time.time():
            raise ApiError(400, 'Start again with Sign in to Spotify; that sign-in expired.')
        query = urllib.parse.urlsplit((pasted or '').strip()).query or (pasted or '').strip().lstrip('?')
        values = urllib.parse.parse_qs(query)
        if values.get('error'):
            self.spotify_pending = None
            raise ApiError(400, 'Spotify reports that access was declined.')
        codes, states = values.get('code', []), values.get('state', [])
        if len(codes) != 1 or len(states) != 1:
            raise ApiError(400, 'Paste the whole address from the page Spotify sent you to. It contains "code=".')
        if not hmac.compare_digest(states[0], pending['state']):
            raise ApiError(400, 'That address is from a different sign-in. Start again with Sign in to Spotify.')
        _, token_file = self.spotify_settings()
        self.token_exchange(pending, codes[0], token_file)
        self.spotify_pending = None
        try: account = self.profile_fetch(pending['client_id'], token_file)
        except Exception as error:
            self.log({'event': 'spotify-profile-failed', 'error': repr(error)}); account = None
        self.data.update(spotify_account=account)
        self.log({'event': 'spotify-signed-in'})
        return {'message': 'Signed in to Spotify.'}

    def spotify_sign_out(self):
        from spotify_client import write_token_file
        settings, token_file = self.spotify_settings()
        write_token_file(token_file, {'client_id': settings.get('client_id', ''), 'authorization_required': True})
        self.data.update(spotify_account=None)
        self.log({'event': 'spotify-signed-out'})
        return {'message': 'Signed out of Spotify.'}

    # overview --------------------------------------------------------------------
    def overview(self):
        controllers = self.controllers()['controllers']
        best = min((c['status'] for c in controllers), key=STATUS_ORDER.index, default='none')
        source = self.source()
        return {'controller': best, 'controllers_connected': sum(c['status'] in ('connected', 'dozing')
                                                                  for c in controllers),
                'controllers': len(controllers), 'home_assistant': self.ha_connected(),
                'source': SOURCES[source], 'source_connected': self.source_connected(source, wait=False)}


STATUS_ORDER = ['connected', 'dozing', 'searching', 'away', 'radio-off', 'none']


def controller_status(running, station):
    if not running: return 'radio-off', 'Bridge radio is not running'
    if not station: return 'away', 'Not seen yet'
    heard = station.get('last_heard_s')
    if station.get('associated') and not station.get('searching'):
        return ('connected', 'Connected') if heard is not None and heard < CONNECTED_WITHIN \
            else ('dozing', 'Connected, idle')
    if heard is None or heard > AWAY_AFTER: return 'away', 'Asleep or out of range'
    return 'searching', 'Reconnecting'


def read_cr100_status(ip):
    """Signal, firmware, uptime and recent link drops from a CR100's own /status pages."""
    def page(name):
        url = 'http://%s:3400/status/%s' % (ip, name)
        with urllib.request.urlopen(url, timeout=STATUS_TIMEOUT) as response:
            return html.unescape(re.sub(r'<[^>]+>', '', response.read(512 * 1024).decode('utf-8', 'replace')))
    try:
        assoc, uptime = page('getassoc'), page('uptime')
    except (OSError, ValueError):
        return None
    details = {}
    match = re.search(r'rssi = (\d+)', assoc)
    if match: details['signal'] = signal_bars(int(match.group(1)))
    seconds = parse_uptime(uptime)
    if seconds is not None: details['uptime_s'] = seconds
    try:
        version = page('VERSION').strip().splitlines()
        if version: details['firmware'] = version[0].strip()[:40]
    except (OSError, ValueError):
        pass
    try:
        dmesg = page('dmesg')
        drops = [int(t) for t in re.findall(r'\((\d+)\)Lost connectivity', dmesg)]
        if seconds is not None: details['drops_last_hour'] = sum(1 for t in drops if t > seconds - 3600)
    except (OSError, ValueError):
        pass
    return details


def signal_bars(rssi):
    """Atheros RSSI is dB above the noise floor; four bars like the CR100's own icon."""
    return sum(rssi >= limit for limit in (10, 20, 30, 40))


def parse_uptime(text):
    match = re.search(r'up\s+(?:(\d+)\s+days?,\s*)?(?:(\d+):(\d+)|(\d+)\s+min)', text)
    if not match: return None
    days, hours, minutes, only_minutes = match.groups()
    return int(days or 0) * 86400 + (int(hours) * 3600 + int(minutes) * 60 if hours else int(only_minutes) * 60)


def room_key(room):
    """The web interface's key for a config room: its entity, sonos:<UUID>, or sim:<name>."""
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


def row(key, source_name, custom_name, shown, custom_icon, entity=None, simulated=False, missing=False, state=None,
        playing=None, sub='', platform=None, sonos=False):
    """One line of the speakers page."""
    name = custom_name or source_name or key.split(':', 1)[-1]
    custom_icon = room_icons.picture(custom_icon) or ''   # older configs may name a same-picture room
    return {'key': key, 'entity': entity, 'name': name, 'custom_name': custom_name or '',
            'icon': custom_icon or room_icons.guess(name), 'custom_icon': custom_icon,
            'source_name': source_name, 'ha_name': source_name, 'shown': shown, 'simulated': simulated,
            'missing': missing, 'state': state, 'playing': playing, 'sub': sub, 'platform': platform, 'sonos': sonos}


SONOS_STATES = {'PLAYING': 'playing', 'PAUSED_PLAYBACK': 'paused', 'TRANSITIONING': 'buffering', 'STOPPED': 'idle'}


def sonos_row(key, zone, room, shown, custom_icon=None):
    if not room_key_ref(room) and shown:   # a simulated room alongside Sonos speakers
        return row(key, None, room.get('name'), True, custom_icon, simulated=True)
    sub = urllib.parse.urlsplit(zone['base']).hostname if zone else key[len('sonos:'):]
    if zone and zone['group']: sub += ' · grouped with ' + ', '.join(n for n in zone['group'] if n != zone['name'])
    return row(key, zone['name'] if zone else None, room.get('name'), shown, custom_icon, missing=zone is None,
               state=zone['state'] if zone else None, playing=zone['playing'] if zone else None, sub=sub,
               platform='sonos', sonos=True)


def sonos_playing(base):
    """('playing' | 'paused' | ..., 'title – artist' or None) for a group coordinator."""
    from sonos_upnp import UPnPFault, parse_didl, soap
    try:
        state = soap(base, 'AVTransport', 'GetTransportInfo', [('InstanceID', 0)], STATUS_TIMEOUT)
        position = soap(base, 'AVTransport', 'GetPositionInfo', [('InstanceID', 0)], STATUS_TIMEOUT)
    except UPnPFault:
        return 'unavailable', None
    track = next(iter(parse_didl(position.get('TrackMetaData'))), {})
    stream = track.get('stream_content', '')
    title = stream if stream and not stream.startswith('ZPSTR_') else track.get('title')
    playing = ' – '.join(x for x in (title, None if stream else track.get('artist')) if x) or None
    return SONOS_STATES.get(state.get('CurrentTransportState'), 'idle'), playing


def check_hosts(value):
    """Sonos speaker addresses from the page: a list or one comma/space separated string."""
    hosts = value if isinstance(value, list) else re.split(r'[\s,]+', value or '')
    hosts = [h.strip() for h in hosts if isinstance(h, str) and h.strip()]
    if len(hosts) > MAX_HOSTS: raise ApiError(400, 'List at most %d speaker addresses.' % MAX_HOSTS)
    for host in hosts:
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}(:\d{1,5})?', host):
            raise ApiError(400, '%s is not a speaker address. Use an address like 192.168.1.30.' % host)
    return hosts


def friendly(state):
    return ((state or {}).get('attributes') or {}).get('friendly_name') or ''


def check_url(url):
    url = (url or '').strip().rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ApiError(400, 'Enter the Home Assistant address, like http://192.168.1.148:8123.')
    return url


def default_ha_factory(url, token, log):
    from ha_client import HomeAssistant
    return HomeAssistant(url, token, log)


def default_token_exchange(pending, code, token_file):
    from spotify_auth import TOKEN_ENDPOINT
    from spotify_client import SpotifyError, _json_request, save_token_response
    try:
        response = _json_request('POST', TOKEN_ENDPOINT, fields={
            'grant_type': 'authorization_code', 'client_id': pending['client_id'], 'code': code,
            'redirect_uri': pending['redirect_uri'], 'code_verifier': pending['verifier']})
        save_token_response(token_file, pending['client_id'], response)
    except SpotifyError as error:
        raise ApiError(400, 'Spotify did not accept that sign-in (%s). Start again.' % error.message) from None


def default_profile_fetch(client_id, token_file):
    from spotify_client import SpotifyClient
    me = SpotifyClient(client_id, token_file, log=lambda record: None).get('/me')
    return {'name': me.get('display_name') or me.get('id'), 'plan': me.get('product')}


# --- HTTP -------------------------------------------------------------------------
def make_handler(bridge, auth, log):
    routes = {}

    def route(method, pattern, public=False):
        def register(function):
            routes.setdefault(method, []).append((re.compile(pattern + '$'), function, public))
            return function
        return register

    @route('GET', '/api/session', public=True)
    def session(handler, body, match):
        return {'authenticated': auth.valid(handler.session_token()), 'password_set': auth.password_set}

    @route('POST', '/api/setup', public=True)
    def setup(handler, body, match):
        if auth.password_set: raise ApiError(409, 'A password is already set. Sign in with it.')
        auth.set_password(body.get('password'))
        log({'event': 'password-set', 'from': handler.client_address[0]})
        handler.new_cookie = auth.new_session()
        return {'authenticated': True}

    @route('POST', '/api/login', public=True)
    def login(handler, body, match):
        if not auth.check(body.get('password')):
            time.sleep(1)   # slow down guessing
            log({'event': 'login-failed', 'from': handler.client_address[0]})
            raise ApiError(401, 'That password is not right.')
        handler.new_cookie = auth.new_session()
        return {'authenticated': True}

    @route('POST', '/api/logout', public=True)
    def logout(handler, body, match):
        auth.end(handler.session_token())
        handler.new_cookie = ''
        return {'authenticated': False}

    @route('POST', '/api/password')
    def change_password(handler, body, match):
        if not auth.check(body.get('current')): raise ApiError(400, 'The current password is not right.')
        auth.set_password(body.get('password'))
        return {'message': 'Password changed.'}

    route('GET', '/api/overview')(lambda h, b, m: bridge.overview())
    route('GET', '/api/speakers')(lambda h, b, m: bridge.speakers())
    route('PUT', '/api/speakers')(lambda h, b, m: bridge.save_speakers(b))
    route('GET', '/api/controllers')(lambda h, b, m: bridge.controllers())
    route('POST', '/api/controllers/pair')(lambda h, b, m: bridge.pair_controller())
    route('PUT', r'/api/controllers/([0-9a-fA-F:]{17})')(lambda h, b, m: bridge.rename_controller(m.group(1), b.get('name')))
    route('GET', '/api/spotify')(lambda h, b, m: bridge.spotify())
    route('PUT', '/api/spotify')(lambda h, b, m: bridge.save_spotify(b))
    route('POST', '/api/spotify/sign-in')(lambda h, b, m: bridge.spotify_begin())
    route('POST', '/api/spotify/finish')(lambda h, b, m: bridge.spotify_finish(b.get('address')))
    route('POST', '/api/spotify/sign-out')(lambda h, b, m: bridge.spotify_sign_out())
    route('GET', '/api/home-assistant')(lambda h, b, m: bridge.home_assistant())
    route('POST', '/api/home-assistant/test')(lambda h, b, m: bridge.test_home_assistant(b.get('url'), b.get('token')))
    route('PUT', '/api/home-assistant')(lambda h, b, m: bridge.save_home_assistant(b.get('url'), b.get('token')))
    route('GET', '/api/source')(lambda h, b, m: bridge.source_status())
    route('PUT', '/api/source')(lambda h, b, m: bridge.switch_source(b.get('source')))
    route('GET', '/api/sonos')(lambda h, b, m: bridge.sonos())
    route('POST', '/api/sonos/test')(lambda h, b, m: bridge.test_sonos(b.get('hosts')))
    route('PUT', '/api/sonos')(lambda h, b, m: bridge.save_sonos(b.get('hosts')))

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'CR100Bridge'
        sys_version = ''
        new_cookie = None

        def log_message(self, *args): pass

        def session_token(self):
            match = re.search(SESSION_COOKIE + r'=([\w-]+)', self.headers.get('Cookie', ''))
            return match.group(1) if match else None

        def allowed_client(self):
            try: address = ipaddress.ip_address(self.client_address[0])
            except ValueError: return False
            if address.version == 6 and address.ipv4_mapped: address = address.ipv4_mapped
            return address not in CR100_NETWORK

        def do_GET(self): self.dispatch('GET')
        def do_POST(self): self.dispatch('POST')
        def do_PUT(self): self.dispatch('PUT')

        def dispatch(self, method):
            if not self.allowed_client():
                return self.reply(403, {'error': 'The bridge settings are not available from the controller network.'})
            path = urllib.parse.urlsplit(self.path).path
            if method == 'GET' and not path.startswith('/api/'): return self.static(path)
            for pattern, function, public in routes.get(method, []):
                match = pattern.match(path)
                if not match: continue
                if method != 'GET' and self.headers.get('X-Bridge') != '1':
                    return self.reply(403, {'error': 'Missing request header.'})   # cross-site forms cannot set it
                if not public and not auth.valid(self.session_token()):
                    return self.reply(401, {'error': 'Sign in first.'})
                try:
                    body = self.read_body() if method != 'GET' else {}
                    return self.reply(200, function(self, body, match))
                except ApiError as error:
                    return self.reply(error.status, {'error': error.message})
                except Exception as error:
                    log({'event': 'api-error', 'path': path, 'error': repr(error)})
                    return self.reply(500, {'error': 'Something went wrong on the bridge: %s' % error})
            self.reply(404, {'error': 'Not found.'})

        def read_body(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length > BODY_LIMIT: raise ApiError(413, 'Request too large.')
            if not length: return {}
            try: body = json.loads(self.rfile.read(length))
            except ValueError: raise ApiError(400, 'Expected JSON.') from None
            if not isinstance(body, dict): raise ApiError(400, 'Expected a JSON object.')
            return body

        def security_headers(self):
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'")

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            if self.new_cookie is not None:
                age = SESSION_SECONDS if self.new_cookie else 0
                self.send_header('Set-Cookie', '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict'
                                 % (SESSION_COOKIE, self.new_cookie, age))
            self.security_headers()
            self.end_headers()
            self.wfile.write(data)

        def static(self, path):
            name = 'index.html' if path in ('/', '') else path.lstrip('/')
            if name.startswith('static/'): name = name[len('static/'):]
            target = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file() or target.suffix not in STATIC_TYPES:
                return self.reply(404, {'error': 'Not found.'})
            data = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', STATIC_TYPES[target.suffix])
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')   # a few small files; always the deployed copy
            self.security_headers()
            self.end_headers()
            self.wfile.write(data)

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def listen(port, handler):
    """Every IPv4 interface (IPv4 only)."""
    return Server(('0.0.0.0', port), handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, default=Path('bridge-config.json'))
    parser.add_argument('--state', type=Path, default=Path('bridge-state.json'))
    parser.add_argument('--run-dir', type=Path, help="bridge_run.sh's output directory (link log, leases)")
    parser.add_argument('--players-pid', type=Path, default=Path('/run/cr100-players.pid'))
    parser.add_argument('--data', type=Path,
                        help='password hash and cached Spotify account (created, mode 0600)')
    parser.add_argument('--managed', action='store_true', help='enable pairing through systemd')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    log = lambda record: print(json.dumps({'time': round(time.time(), 3), **record}), flush=True)
    data = DataFile(args.data or args.config.resolve().parent / 'webui.json')
    bridge = Bridge(args.config, args.state, args.run_dir, args.players_pid, data, log, managed=args.managed)
    auth = Auth(data)
    bridge.start()
    server = listen(args.port, make_handler(bridge, auth, log))
    log({'event': 'webui-start', 'port': args.port, 'address': server.server_address[0],
         'password_set': auth.password_set})
    # SIGHUP restarts this process in place (same PID for bridge_run.sh), e.g. after an update.
    # Sessions live in memory, so open pages sign in again.
    reload = threading.Event()
    def on_hup(*_):
        reload.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGHUP, on_hup)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.close()
    if reload.is_set():
        log({'event': 'webui-reload'})
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == '__main__':
    main()
