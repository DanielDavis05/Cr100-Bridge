"""Explicit, versioned controller UI events over native SystemProperties SOAP.

Only SetString(CR100_UIEvent) executes handlers. Result/capability reads never
dispatch an action. The protocol contains no Python names, scripts, or URLs.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import re
import os
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote, unquote

EVENT_VARIABLE = 'CR100_UIEvent'
RESULT_PREFIX = 'CR100_UIResult:'
CAPABILITIES_VARIABLE = 'CR100_UICapabilities'
SESSION_VARIABLE = 'CR100_UISession'
ROOM_CAPABILITIES_VARIABLE = 'CR100_RoomCapabilities'
AUTOPLAY_VARIABLE = 'CR100_SpotifyAutoplay'
MAX_WIRE_BYTES = 4096
MAX_PARAMS = 16
MAX_ACTION_LIST_BYTES = 1024
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,63}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')


class UIEventError(ValueError):
    """An expected action failure whose message is safe for the controller."""
    def __init__(self, code, message):
        # Bound provider-supplied failures too: even a bad handler must produce
        # a retainable receipt after it may have changed a device.
        message = str(message).encode('utf-8', errors='replace')[:512].decode('utf-8', errors='ignore')
        super().__init__(message)
        self.code = code if isinstance(code, str) and NAME.fullmatch(code) else 'action_failed'


@dataclass(frozen=True)
class UIResult:
    message: str = 'Done'
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UIContext:
    request_id: str
    action: str
    controller_id: str
    controller_ip: str
    room_udn: str
    player: object
    household: object

    @property
    def state(self): return self.player.state

    @property
    def room(self): return self.state.backend


def _decode(value):
    if re.search(r'%(?![0-9a-fA-F]{2})', value):
        raise UIEventError('invalid_request', 'Invalid percent encoding')
    try:
        decoded = unquote(value, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise UIEventError('invalid_request', 'Invalid UTF-8') from None
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        raise UIEventError('invalid_request', 'Control characters are not allowed')
    return decoded


def encode_params(params):
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise UIEventError('invalid_result', 'Too many result values')
    parts = []
    for key, value in params.items():
        if not isinstance(key, str) or not NAME.fullmatch(key):
            raise UIEventError('invalid_result', 'Invalid result field')
        if isinstance(value, bool): value = 'true' if value else 'false'
        elif isinstance(value, int): value = str(value)
        if not isinstance(value, str) or len(value.encode('utf-8')) > 1024:
            raise UIEventError('invalid_result', 'Invalid result value')
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise UIEventError('invalid_result', 'Invalid result value')
        parts.append(quote(key, safe='') + '=' + quote(value, safe=''))
    return '&'.join(parts)


def decode_params(value):
    if not value: return {}
    parts = value.split('&')
    if len(parts) > MAX_PARAMS:
        raise UIEventError('invalid_request', 'Too many parameters')
    result = {}
    for part in parts:
        if part.count('=') != 1:
            raise UIEventError('invalid_request', 'Invalid parameter')
        key, val = map(_decode, part.split('=', 1))
        if not NAME.fullmatch(key) or key in result or len(val.encode('utf-8')) > 1024:
            raise UIEventError('invalid_request', 'Invalid or duplicate parameter')
        result[key] = val
    return result


def encode_request(request_id, action, room_udn='', params=None):
    """Reference encoder; SWF uses escape on these same five field values."""
    return '|'.join(quote(value, safe='') for value in
                    ('v1', request_id, action, room_udn, encode_params(params or {})))


def parse_request(wire):
    if not isinstance(wire, str) or len(wire.encode('utf-8')) > MAX_WIRE_BYTES:
        raise UIEventError('invalid_request', 'UI request is too large')
    parts = wire.split('|')
    if len(parts) != 5:
        raise UIEventError('invalid_request', 'Expected five UI request fields')
    version, request_id, action, room_udn, params = map(_decode, parts)
    if version != 'v1':
        raise UIEventError('unsupported_version', 'Unsupported UI event version')
    if not REQUEST_ID.fullmatch(request_id) or not NAME.fullmatch(action) or len(room_udn) > 128:
        raise UIEventError('invalid_request', 'Invalid request identity, action, or room')
    return request_id, action, room_udn, decode_params(params)


def response(request_id, ok, code, message, data=None):
    if not isinstance(message, str) or len(message.encode('utf-8')) > 512:
        raise UIEventError('invalid_result', 'Invalid result message')
    fields = ('v1', request_id, 'ok' if ok else 'error', code, message, encode_params(data or {}))
    wire = '|'.join(quote(value, safe='') for value in fields)
    if len(wire.encode('utf-8')) > MAX_WIRE_BYTES:
        raise UIEventError('invalid_result', 'UI result is too large')
    return wire


class UIEventGateway:
    """Named handlers and a bounded retry/result cache, shared by all players."""
    def __init__(self, household, *, capacity=256, ttl=600, clock=time.monotonic,
                 require_controller=False):
        if not 1 <= capacity <= 4096 or not 1 <= ttl <= 3600:
            raise ValueError('Invalid UI receipt bounds')
        self.household, self.capacity, self.ttl, self.clock = household, capacity, ttl, clock
        self.require_controller = require_controller
        self.handlers, self.receipts = {}, OrderedDict()
        self.autoplay_config_path = None
        self.lock = threading.RLock()
        self.register('bridge.ping', self._ping, scope='bridge', mutates=False)
        self.register('bridge.info', self._info, scope='bridge', mutates=False)
        self.register('bridge.status', self._status, scope='bridge', mutates=False)
        self.register('radio.setting.apply', self._radio_setting_apply)

    def register(self, name, handler, *, scope='room', mutates=True):
        if not isinstance(name, str) or not NAME.fullmatch(name) or not callable(handler):
            raise ValueError('Invalid UI event handler')
        if scope not in ('room', 'bridge') or not isinstance(mutates, bool):
            raise ValueError('Invalid UI event handler policy')
        with self.lock:
            if name in self.handlers: raise ValueError('Duplicate UI event action: ' + name)
            if len(','.join([*self.handlers, name]).encode('ascii')) > MAX_ACTION_LIST_BYTES:
                raise ValueError('UI event action registry is full')
            self.handlers[name] = (handler, scope, mutates)

    def register_provider(self, provider):
        factory = getattr(provider, 'ui_event_handlers', None)
        if not callable(factory): return
        handlers = factory()
        if not isinstance(handlers, dict): raise ValueError('UI event handlers must be a mapping')
        for name, spec in handlers.items():
            if isinstance(spec, dict): self.register(name, **spec)
            else: self.register(name, spec)

    @staticmethod
    def _no_params(params):
        if params: raise UIEventError('invalid_parameters', 'This action takes no parameters')

    def _ping(self, context, params):
        self._no_params(params)
        return UIResult('Bridge connected', {'version': '1'})

    def _info(self, context, params):
        self._no_params(params)
        build = self.household.build
        data = {'build': str(build.get('version') or build.get('display') or 'dev')[:128],
                'revision': str(build.get('software') or build.get('sw') or '')[:128],
                'room': context.player.config.room[:128],
                'actions': ','.join(sorted(self.handlers))}
        # The deployed native UI displays only the result message, and its
        # percent decoder accepts printable ASCII. Keep full metadata in data
        # while making the original controller's small dialog useful too.
        def display(value, limit):
            return ''.join(char if 32 <= ord(char) <= 126 else '?' for char in value)[:limit]
        version = display(data['build'], 64)
        revision = display(data['revision'], 32)
        room = display(data['room'], 64)
        message = 'CR100 Bridge. Build: ' + version
        if revision: message += ' (' + revision + ')'
        message += '. Room: ' + room + '.'
        return UIResult(message, data)

    def _status(self, context, params):
        self._no_params(params)
        room = context.room
        backend = getattr(room, 'backend', None)
        name = str(getattr(backend, 'name', 'simulated' if room is None else 'unknown'))
        available = bool(getattr(room, 'available', False)) if room is not None else False
        provider_for = getattr(room, 'native_settings_provider', None)
        provider = provider_for() if callable(provider_for) else None
        caps = sorted(provider.capabilities(room.key)) if provider is not None else []
        radio = getattr(self.household, 'radio_service', None) is not None
        safe_name = ''.join(c if 32 <= ord(c) <= 126 else '?' for c in name)[:32]
        message = ('Backend: ' + safe_name + '. Room: ' + ('available' if available else 'unavailable')
                   + '. Speaker settings: ' + ('supported' if caps else 'unavailable')
                   + '. Radio: ' + ('enabled' if radio else 'disabled') + '.')
        return UIResult(message, {'backend': name[:32], 'available': available,
                                  'speaker_settings': bool(caps), 'radio': radio})

    def _radio_setting_apply(self, context, params):
        if set(params) != {'token'} or not params['token']:
            raise UIEventError('invalid_parameters', 'Choose an explicit Radio setting')
        service = self.household.radio_service
        if service is None:
            raise UIEventError('unavailable', 'Radio is not enabled')
        from radio_catalog import _decoded
        identity = service._catalog_identity(params['token'])
        expected = None
        if identity.startswith('settings-apply:'):
            try:
                expected = _decoded(identity[len('settings-apply:'):])
                if set(expected) != {'name', 'value'} or not isinstance(expected['name'], str):
                    raise ValueError('Invalid Radio setting')
            except (ValueError, TypeError, KeyError):
                raise UIEventError('invalid_parameters', 'Invalid Radio setting token') from None
        elif identity not in ('settings-action:refresh', 'settings-action:clear_cache',
                              'settings-action:clear_history', 'settings-action:clear_searches',
                              'settings-action:reset'):
            raise UIEventError('invalid_parameters', 'Choose an explicit Radio setting action')
        try:
            handled = service.native_action('SystemProperties', 'SetString',
                {'VariableName': 'CR100_RadioAction', 'StringValue': params['token']}, context.state)
        except ValueError:
            raise UIEventError('invalid_parameters', 'Radio setting could not be applied') from None
        if handled is None:
            raise UIEventError('unavailable', 'Radio setting action is unavailable')
        snapshot = service.store.snapshot()
        data = {'revision': snapshot['revision']}
        if expected is not None:
            name = expected['name']
            if name not in snapshot['settings'] or snapshot['settings'][name] != expected['value']:
                raise UIEventError('readback_failed', 'Radio setting readback did not confirm the requested value')
            data.update(setting=name, value=snapshot['settings'][name])
        return UIResult('Radio setting applied', data)

    def _identity(self, address):
        identity = self.household.controller_resolver(address) if address is not None else None
        if identity: return str(identity)
        if self.require_controller or address is None:
            raise UIEventError('unknown_controller', 'Controller identity is unavailable')
        # Standalone emulator/tests without pairing have only their source IP.
        return 'ip:' + address

    def _prune(self):
        now = self.clock()
        for key in list(self.receipts):
            if self.receipts[key][0] <= now: self.receipts.pop(key)
        while len(self.receipts) > self.capacity: self.receipts.popitem(last=False)

    def dispatch(self, wire, player, address):
        request_id, action, room_udn, params = parse_request(wire)
        identity = self._identity(address)
        context = UIContext(request_id, action, identity, address, room_udn or player.config.udn,
                            player, self.household)
        # Serialize lookup, execution and receipt: concurrent retries execute once.
        with self.lock:
            self._prune()
            key = (identity, request_id)
            fingerprint = (action, room_udn, tuple(sorted(params.items())), player.config.udn)
            if key in self.receipts:
                _, previous, result = self.receipts[key]
                if previous != fingerprint:
                    raise UIEventError('request_conflict', 'Request ID was already used for another action')
                return result
            try:
                if action not in self.handlers:
                    raise UIEventError('unsupported_action', 'This UI action is not supported')
                handler, scope, _ = self.handlers[action]
                if room_udn and room_udn != player.config.udn:
                    raise UIEventError('room_mismatch', 'UI action room does not match the selected player')
                if scope == 'room' and player.state.informational:
                    raise UIEventError('unsupported_room', 'Choose a playback room for this action')
                result = handler(context, params)
                if result is None: result = UIResult()
                if isinstance(result, dict): result = UIResult(data=result)
                if not isinstance(result, UIResult):
                    raise UIEventError('invalid_result', 'UI action returned an invalid result')
                result = response(request_id, True, 'done', result.message, result.data)
            except UIEventError as error:
                result = response(request_id, False, error.code, str(error))
            except Exception as error:
                self.household.log({'event': 'ui-event-failed', 'action': action,
                                    'error_type': type(error).__name__})
                result = response(request_id, False, 'action_failed', 'The action could not be completed')
            self.receipts[key] = (self.clock() + self.ttl, fingerprint, result)
            self._prune()
            return result

    def native_action(self, player, service, action, args, address):
        if service != 'SystemProperties': return None
        variable = args.get('VariableName', '')
        if variable in (ROOM_CAPABILITIES_VARIABLE, AUTOPLAY_VARIABLE):
            if action != 'GetString':
                raise UIEventError('invalid_request', 'Controller settings status is read-only')
            self._identity(address)
            if variable == AUTOPLAY_VARIABLE:
                value = 'unavailable'
                if self.autoplay_config_path is not None:
                    from music_service_registry import provider_settings
                    try:
                        config = json.loads(Path(self.autoplay_config_path).read_text())
                        settings = provider_settings(config, 'spotify')
                        if settings and settings.get('enabled', True) is True:
                            value = 'on' if settings.get('autoplay', False) is True else 'off'
                    except (OSError, ValueError, TypeError):
                        pass
                return {'StringValue': value}
            # Native hardware personas are compatibility identities, not proof
            # of capabilities. Read the verified provider for this exact room.
            room = player.state.backend
            provider_for = getattr(room, 'native_settings_provider', None)
            provider = provider_for() if callable(provider_for) else None
            caps = set(provider.capabilities(room.key)) if provider is not None else set()
            allowed = {'bass', 'treble', 'loudness', 'led', 'output_fixed',
                       'line_in_settings', 'line_in_level'}
            values = sorted(caps & allowed)
            if 'channel_volume' in caps:
                values.append('balance')
            return {'StringValue': 'v1|' + ''.join(value + '|' for value in sorted(values))}
        if variable == EVENT_VARIABLE:
            if action != 'SetString':
                raise UIEventError('invalid_request', 'UI events require an explicit SetString')
            self.dispatch(args.get('StringValue', ''), player, address)
            return {}
        if variable == CAPABILITIES_VARIABLE:
            if action != 'GetString':
                raise UIEventError('invalid_request', 'UI capabilities are read-only')
            return {'StringValue': response('', True, 'capabilities', 'UI events v1',
                                            {'actions': ','.join(sorted(self.handlers))})}
        if variable == SESSION_VARIABLE:
            if action != 'GetString':
                raise UIEventError('invalid_request', 'UI session identifiers are read-only')
            self._identity(address)
            # A fresh movie must not reuse a timer/VM-RandomNumber-derived ID
            # while the bridge still retains its earlier mutation receipts.
            # This is uniqueness, not a replacement authentication mechanism.
            return {'StringValue': os.urandom(12).hex()}
        if variable.startswith(RESULT_PREFIX):
            if action != 'GetString':
                raise UIEventError('invalid_request', 'UI results are read-only')
            request_id = variable[len(RESULT_PREFIX):]
            if not REQUEST_ID.fullmatch(request_id):
                raise UIEventError('invalid_request', 'Invalid result request ID')
            identity = self._identity(address)
            with self.lock:
                self._prune()
                receipt = self.receipts.get((identity, request_id))
                result = receipt[2] if receipt else response(request_id, False, 'not_found',
                                                           'No retained result for this request')
            return {'StringValue': result}
        return None


def register_spotify_autoplay(gateway, config_path):
    """Save the shared preference without restarting players or issuing transport."""
    if config_path is None: return
    gateway.autoplay_config_path = config_path
    from bridge_config import update_config_file
    from music_service_registry import provider_settings
    def apply(context, params):
        if set(params) != {'desired'} or params['desired'] not in ('true', 'false'):
            raise UIEventError('invalid_parameters', 'Autoplay requires desired=true or desired=false')
        desired = params['desired'] == 'true'
        def change(config):
            settings = provider_settings(config, 'spotify')
            if not settings or settings.get('enabled', True) is not True:
                raise UIEventError('unavailable', 'Spotify is not enabled')
            settings['autoplay'] = desired
        update_config_file(config_path, change)
        return UIResult('Autoplay ' + ('On' if desired else 'Off') + '. Current track keeps playing.',
                        {'desired': desired, 'scope': 'household', 'saved': True})
    gateway.register('spotify.autoplay.set', apply, scope='bridge')
