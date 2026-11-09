"""Stock-CR100 speaker controls through a nonplayable SMAPI catalogue.

Browsing reads the backend's verified settings cache. Only the native OK-menu
action (``createItem(favorite=...)``) applies an issued value. Its stock label is
"Add to My Speaker Settings Library"; this service does not create music or
provide a stream. The existing Room/SettingsProvider owns native validation,
physical/coordinator routing, writes, readback and ordinary setting events.
"""
from __future__ import annotations

import collections
import copy
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from backends import BackendError
from smapi_service import SmapiFault, SmapiService, collection, text
from sonos_settings import duration

PREFIX = 'ss:'
SETTINGS = frozenset(('bass', 'treble', 'loudness', 'led', 'output_fixed',
                      'channel_volume', 'sleep_timer'))
STATE_FIELDS = ('Bass', 'Treble', 'Loudness', 'LEDState', 'OutputFixed',
                'SupportsOutputFixed', 'VolumeChannels', 'CurrentSleepTimerGeneration')
TIMER_PRESETS = (15, 30, 45, 60, 90, 120)
BALANCE_PRESETS = ((0, 100, 'Right'), (50, 100, 'Right +50'),
                   (75, 100, 'Right +25'), (100, 100, 'Center'),
                   (100, 75, 'Left +25'), (100, 50, 'Left +50'),
                   (100, 0, 'Left'))


class SettingsFault(SmapiFault):
    """A safe speaker-settings error displayed by the controller."""


@dataclass
class RoomView:
    stable_id: str
    key: str
    name: str
    room: object
    binding: tuple
    ref: object
    capabilities: frozenset
    values: dict
    coordinator: tuple | None


@dataclass
class IssuedAction:
    ident: str
    stable_id: str
    setting: str
    desired: object
    fingerprint: str
    created: float
    title: str
    room_name: str


@dataclass
class Receipt:
    action: IssuedAction
    created: float
    result: str = ''
    fault: tuple | None = None


def label(value, limit=100):
    value = ''.join(ch for ch in str(value) if ord(ch) >= 32 and ord(ch) != 127)
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def binding_token(binding):
    return (binding.platform, binding.device_id, binding.native_id,
            binding.native_url, binding.group_key)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def integer(value, low, high):
    if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
        return None
    result = int(value)
    return result if low <= result <= high else None


def info(ident, title):
    node = collection(ident, title)
    node['canEnumerate'] = False
    return node


class SpeakerSettingsService(SmapiService):
    startup_event = 'speaker-settings-service'
    response_event = 'speaker-settings-smapi'

    preflight_playback = True

    def __init__(self, host, port, rooms, service_id=244, name='Speaker Settings', log=print,
                 *, clock=time.monotonic, action_limit=1024, receipt_limit=256, action_ttl=600):
        if not callable(rooms): raise ValueError('Speaker settings need a room supplier')
        if action_limit < 1 or receipt_limit < 1 or action_ttl <= 0:
            raise ValueError('Setting action bounds must be positive')
        self.rooms, self.clock = rooms, clock
        self.action_limit, self.receipt_limit, self.action_ttl = action_limit, receipt_limit, action_ttl
        self.lock = threading.RLock()
        self.actions, self.receipts = collections.OrderedDict(), collections.OrderedDict()
        self.action_keys = {}
        self.component_allowed = lambda stable_id, address: True
        super().__init__(host, port, service_id, name, log, capabilities='3')

    def descriptor(self):
        values = super().descriptor()
        services = ET.fromstring(values['AvailableServiceDescriptorList'])
        services.find('./Service/Policy').set('PollInterval', '30')
        values['AvailableServiceDescriptorList'] = ET.tostring(services, encoding='unicode')
        return values

    def resolve_playback(self, uri, metadata='', controller_ip=None):
        raise ValueError('Speaker settings cannot be played. Choose a value and use Add to Library.')

    def owns(self, uri, metadata=''):
        uri, metadata = str(uri or ''), str(metadata or '')
        try:
            sid = parse_qs(urlsplit(uri).query).get('sid', ())
        except ValueError:
            sid = ()
        return (str(self.service_id) in sid or uri.startswith(PREFIX)
                or 'SA_RINCON%d_' % self.service_type in metadata
                or re.search(r'(?i)(?:^|[:/])(?:[0-9a-f]{8})?ss(?::|%3a)', uri) is not None)

    def _allowed(self, stable_id):
        return self.component_allowed(stable_id, getattr(self.request, 'address', None))

    def _require_visible_action(self, action):
        if not self._allowed(action.stable_id):
            raise SettingsFault('Client.ItemNotFound', 'This speaker setting is unavailable.')

    def _views(self):
        views, seen = {}, set()
        for stable_id, name, room in self.rooms():
            stable_id = str(stable_id)
            if not self._allowed(stable_id): continue
            if stable_id in seen: raise SettingsFault('Server.ServiceUnavailable', 'Duplicate speaker identity.')
            seen.add(stable_id)
            key = hashlib.sha256(stable_id.encode()).hexdigest()[:24]
            caps, values, bound, ref, coordinator = frozenset(), {}, (), None, None
            try:
                binding = room.binding
                bound, ref = binding_token(binding), room.ref
                if room.available and binding.native_sonos:
                    caps = frozenset(room.operation_capabilities()) & SETTINGS
                    values = copy.deepcopy(room.settings_values())
                    if values.get('OutputFixed') == '1':
                        caps -= {'bass', 'treble', 'loudness', 'channel_volume'}
                    if 'sleep_timer' in caps:
                        backend = room.backend
                        target = backend.settings_coordinator(room.key)
                        if target is None:
                            caps -= {'sleep_timer'}
                        else:
                            coordinator = (str(target), binding_token(backend.settings_binding(target)))
            except Exception:
                # A disappearing room withdraws its controls; browsing still
                # shows an informational row and never launches discovery.
                caps, values, coordinator = frozenset(), {}, None
            views[key] = RoomView(stable_id, key, label(name), room, bound, ref, caps, values, coordinator)
        return views

    def _fingerprint(self, view):
        state = {name: view.values[name] for name in STATE_FIELDS if name in view.values}
        return hashlib.sha256(canonical([view.stable_id, view.ref, view.binding,
            sorted(view.capabilities), state, view.coordinator]).encode()).hexdigest()

    def _settings(self, view):
        values, caps, result = view.values, view.capabilities, []
        for key, title in (('bass', 'Bass'), ('treble', 'Treble')):
            value = integer(values.get(title), -10, 10)
            if key in caps and value is not None:
                result.append((key, '%s: %+d' % (title, value)))
        if 'loudness' in caps and values.get('Loudness') in ('0', '1'):
            result.append(('loudness', 'Loudness: ' + ('On' if values['Loudness'] == '1' else 'Off')))
        if 'led' in caps and values.get('LEDState') in ('On', 'Off'):
            result.append(('led', 'Status light: ' + values['LEDState']))
        if ('output_fixed' in caps and values.get('SupportsOutputFixed') == '1'
                and values.get('OutputFixed') in ('0', '1')):
            result.append(('output_fixed', 'Line-out: ' + ('Fixed' if values['OutputFixed'] == '1' else 'Variable')))
        channels = values.get('VolumeChannels') or {}
        if 'channel_volume' in caps and all(integer(channels.get(side), 0, 100) is not None for side in ('LF', 'RF')):
            left, right = int(channels['LF']), int(channels['RF'])
            balance = next((name for lf, rf, name in BALANCE_PRESETS if (left, right) == (lf, rf)),
                           'L%d / R%d' % (left, right))
            result.append(('balance', 'Balance: ' + balance))
        if 'sleep_timer' in caps and integer(values.get('CurrentSleepTimerGeneration'), 0, 4294967295) is not None:
            try:
                remaining, _ = duration(values['RemainingSleepTimerDuration'])
                result.append(('sleep_timer', 'Sleep timer: ' + (remaining or 'Off')))
            except (BackendError, KeyError):
                pass
        return result

    def _prune(self):
        now = self.clock()
        for ident, action in tuple(self.actions.items()):
            if now - action.created >= self.action_ttl:
                self.actions.pop(ident)
        for ident, receipt in tuple(self.receipts.items()):
            if now - receipt.created >= self.action_ttl:
                self.receipts.pop(ident)
        while len(self.actions) > self.action_limit: self.actions.popitem(last=False)
        while len(self.receipts) > self.receipt_limit: self.receipts.popitem(last=False)
        self.action_keys = {key: ident for key, ident in self.action_keys.items() if ident in self.actions}

    def _issue(self, view, setting, desired, title):
        self._prune()
        fingerprint = self._fingerprint(view)
        key = (view.stable_id, setting, canonical(desired), fingerprint)
        ident = self.action_keys.get(key)
        if ident not in self.actions:
            ident = PREFIX + 'a:' + secrets.token_urlsafe(18)
            action = IssuedAction(ident, view.stable_id, setting, desired, fingerprint,
                                  self.clock(), title, view.name)
            self.actions[ident], self.action_keys[key] = action, ident
            self._prune()
        return self._action_node(self.actions[ident])

    @staticmethod
    def _action_node(action):
        return {'id': action.ident, 'itemType': 'track', 'title': action.title,
                'room_name': action.room_name, 'canPlay': False}

    def _choices(self, view, setting):
        values = view.values
        choices = []
        if setting in ('bass', 'treble'):
            current = int(values[setting.title()])
            choices = [(value, '%s %+d' % (setting.title(), value), value == current) for value in range(-10, 11)]
        elif setting == 'loudness':
            choices = [(value, 'Loudness ' + name, values['Loudness'] == value) for value, name in (('0', 'Off'), ('1', 'On'))]
        elif setting == 'led':
            choices = [(value, 'Status light ' + value, values['LEDState'] == value) for value in ('Off', 'On')]
        elif setting == 'output_fixed':
            choices = [(value, 'Line-out ' + name, values['OutputFixed'] == value)
                       for value, name in (('0', 'Variable'), ('1', 'Fixed'))]
        elif setting == 'balance':
            channels = values['VolumeChannels']
            choices = [([left, right], 'Balance ' + name,
                        int(channels['LF']) == left and int(channels['RF']) == right)
                       for left, right, name in BALANCE_PRESETS]
        elif setting == 'sleep_timer':
            choices = [('', 'Cancel sleep timer', values['RemainingSleepTimerDuration'] == '')]
            choices += [('%d:%02d:00' % (minutes // 60, minutes % 60),
                         'Sleep after %d minutes' % minutes, False) for minutes in TIMER_PRESETS]
        return [self._issue(view, setting, value, title + (' (current)' if current else ''))
                for value, title, current in choices]

    def _node(self, ident, views):
        if ident in self.actions:
            self._require_visible_action(self.actions[ident])
            return self._action_node(self.actions[ident])
        if ident in self.receipts:
            self._require_visible_action(self.receipts[ident].action)
            return self._action_node(self.receipts[ident].action)
        if ident == 'root': return collection('root', self.name)
        if ident == 'search': return collection('search', 'Speaker settings')
        if ident == PREFIX + 'help': return collection(ident, 'How to apply a setting')
        if ident.startswith(PREFIX + 'help:'):
            lines = self._help()
            found = next((node for node in lines if node['id'] == ident), None)
            if found: return found
        if ident == PREFIX + 'empty': return info(ident, 'No configured speaker rooms')
        parts = ident.split(':')
        if len(parts) in (3, 4) and parts[:2] == ['ss', 'r'] and parts[2] in views:
            view = views[parts[2]]
            if len(parts) == 3: return collection(ident, view.name)
            if parts[3] == 'unavailable': return info(ident, 'No supported speaker settings available')
            for setting, title in self._settings(view):
                if parts[3] == setting: return collection(ident, title)
        raise SettingsFault('Client.ItemNotFound', 'This setting view expired. Reopen Speaker Settings.')

    def _help(self):
        return [info(PREFIX + 'help:%d' % index, line) for index, line in enumerate((
            'Set OK Button to Show Menu first.', 'Choose a room, setting and value.',
            'Press OK, then choose',
            'Add to My %s Library' % label(self.name), 'applies the value to that speaker.',
            'Browse again to see confirmed values.', 'Balance writes left, then right.',
            'A failed balance change may be partial.'))]

    def _children(self, ident, views):
        if ident in ('root', 'search'):
            rooms = [collection(PREFIX + 'r:' + view.key, view.name) for view in views.values()]
            return [self._node(PREFIX + 'help', views)] + (rooms or [self._node(PREFIX + 'empty', views)])
        if ident == PREFIX + 'help': return self._help()
        parts = ident.split(':')
        if len(parts) in (3, 4) and parts[:2] == ['ss', 'r'] and parts[2] in views:
            view = views[parts[2]]
            settings = self._settings(view)
            if len(parts) == 3:
                return [collection(ident + ':' + key, title) for key, title in settings] or [
                    info(ident + ':unavailable', 'No supported speaker settings available')]
            if parts[3] in {key for key, _ in settings}: return self._choices(view, parts[3])
        self._node(ident, views)
        return []

    def entry(self, node, wrap=True):
        if node['itemType'] == 'track':
            fields = ('<id>%s</id><itemType>track</itemType><title>%s</title><mimeType>audio/mpeg</mimeType>'
                '<trackMetadata><artist>%s</artist><album>Speaker settings</album>'
                '<duration>0</duration><canPlay>false</canPlay><canSkip>false</canSkip>'
                '<canAddToFavorites>true</canAddToFavorites></trackMetadata>'
                % (text(node['id']), text(node['title']), text(node['room_name'])))
            return '<mediaMetadata>%s</mediaMetadata>' % fields if wrap else fields
        fields = ('<id>%s</id><itemType>container</itemType><title>%s</title>'
            '<canPlay>false</canPlay><canEnumerate>%s</canEnumerate>'
            '<canAddToFavorites>false</canAddToFavorites><canScroll>false</canScroll>'
            % (text(node['id']), text(node['title']), 'true' if node.get('canEnumerate', True) else 'false'))
        return '<mediaCollection>%s</mediaCollection>' % fields if wrap else fields

    def _page(self, nodes, args, wrapper):
        index, count = integer(args.get('index', '0'), 0, 2147483647), integer(args.get('count', '100'), 0, 500)
        if index is None or count is None:
            raise SettingsFault('Client.InvalidRequest', 'Invalid setting page.')
        chosen = nodes[index:index + count]
        return ('<%s><index>%d</index><count>%d</count><total>%d</total>%s</%s>'
                % (wrapper, index, len(chosen), len(nodes), ''.join(self.entry(node) for node in chosen), wrapper))

    def _confirmed(self, action, before, after):
        if before.binding != after.binding or before.ref != after.ref:
            return False
        values, setting, desired = after.values, action.setting, action.desired
        if setting in ('bass', 'treble'): return integer(values.get(setting.title()), -10, 10) == desired
        if setting == 'loudness': return values.get('Loudness') == desired
        if setting == 'led': return values.get('LEDState') == desired
        if setting == 'output_fixed': return values.get('OutputFixed') == desired
        if setting == 'balance':
            channels = values.get('VolumeChannels') or {}
            return [integer(channels.get(side), 0, 100) for side in ('LF', 'RF')] == desired
        if setting == 'sleep_timer':
            if before.coordinator != after.coordinator: return False
            if values.get('CurrentSleepTimerGeneration') == before.values.get('CurrentSleepTimerGeneration'): return False
            try:
                actual, remaining = duration(values['RemainingSleepTimerDuration'])
                _, wanted = duration(desired)
                return actual == '' if desired == '' else remaining is not None and max(0, wanted - 5) <= remaining <= wanted
            except (BackendError, KeyError): return False
        return False

    def _write(self, action, view):
        room, setting, desired = view.room, action.setting, action.desired
        operation = 'channel_volume' if setting == 'balance' else setting
        room.require(operation)
        args = {'InstanceID': '0'}
        if setting in ('bass', 'treble'):
            action_name = setting.title()
            calls = [('RenderingControl', 'Set' + action_name, dict(args, **{'Desired' + action_name: str(desired)}))]
        elif setting == 'loudness':
            calls = [('RenderingControl', 'SetLoudness', dict(args, Channel='Master', DesiredLoudness=desired))]
        elif setting == 'led': calls = [('DeviceProperties', 'SetLEDState', {'DesiredLEDState': desired})]
        elif setting == 'output_fixed': calls = [('RenderingControl', 'SetOutputFixed', dict(args, DesiredFixed=desired))]
        elif setting == 'sleep_timer': calls = [('AVTransport', 'ConfigureSleepTimer', dict(args, NewSleepTimerDuration=desired))]
        else:
            calls = [('RenderingControl', 'SetVolume', dict(args, Channel=side, DesiredVolume=str(value)))
                     for side, value in zip(('LF', 'RF'), desired)]
        for service, method, arguments in calls:
            # Each backend/provider also revalidates the live cached capability
            # and binding immediately before its native write and readback.
            fresh = self._views().get(view.key)
            if (fresh is None or fresh.binding != view.binding or fresh.ref != view.ref
                    or fresh.values.get('OutputFixed') != view.values.get('OutputFixed')
                    or operation not in fresh.capabilities
                    or (setting == 'sleep_timer' and fresh.coordinator != view.coordinator)):
                raise BackendError('Native setting target or capability changed')
            room.require(operation)
            if room.settings_action(service, method, arguments) is None:
                raise BackendError('No native settings route')

    def _apply(self, ident):
        self._prune()
        receipt = self.receipts.get(ident)
        if receipt:
            self._require_visible_action(receipt.action)
            if receipt.fault: raise SettingsFault(*receipt.fault)
            return receipt.result
        action = self.actions.get(ident)
        if action is None:
            raise SettingsFault('Client.ItemNotFound', 'This setting action expired. Reopen its folder.')
        self._require_visible_action(action)
        views = self._views()
        view = next((view for view in views.values() if view.stable_id == action.stable_id), None)
        if (view is None or self._fingerprint(view) != action.fingerprint
                or action.setting not in {key for key, _ in self._settings(view)}):
            raise SettingsFault('Client.ItemNotFound', 'Speaker settings changed. Reopen the setting folder.')
        receipt = Receipt(action, self.clock())
        # Consumed tokens leave the issued table. If a bounded receipt is later
        # evicted, the old token can only fault, never execute a second write.
        self.actions.pop(ident)
        # Holding the service lock across the provider write makes concurrent
        # retries idempotent, including failures after one balance channel wrote.
        try:
            self._write(action, view)
            after = next((item for item in self._views().values() if item.stable_id == action.stable_id), None)
            if after is None or not self._confirmed(action, view, after):
                raise BackendError('The backend did not confirm the requested value')
            receipt.result = '<createItemResult>%sreceipt:%s</createItemResult>' % (PREFIX, secrets.token_urlsafe(12))
        except Exception:
            message = ('Balance failed; one channel may have changed. Reopen Balance to check.'
                       if action.setting == 'balance' else 'Setting failed or readback is unavailable. Reopen the setting to check.')
            receipt.fault = ('Server.ServiceUnavailable', message)
        self.receipts[ident] = receipt
        self._prune()
        if receipt.fault: raise SettingsFault(*receipt.fault)
        return receipt.result

    def dispatch(self, method, args):
        with self.lock:
            self._prune()
            if method == 'createItem':
                if set(args) != {'favorite'}:
                    raise SettingsFault('Client.InvalidRequest', 'Set OK Button to Show Menu, then choose a value and Add to Library.')
                return self._apply(args['favorite'])
            if method in ('getMediaURI', 'deleteItem'):
                raise SettingsFault('Client.InvalidRequest', 'Speaker settings cannot be played. Use Show Menu and Add to Library.')
            if method == 'getScrollIndices': return '<getScrollIndicesResult></getScrollIndicesResult>'
            views = self._views()
            if method == 'getLastUpdate':
                state = [(view.stable_id, view.name, self._fingerprint(view)) for view in views.values()]
                # The legacy controller stores catalogue generations in a
                # signed integer. Keep the stable digest inside its positive
                # range, including when the digest's high bit is set.
                version = int.from_bytes(hashlib.sha256(canonical(state).encode()).digest()[:4], 'big') % 2147483647 + 1
                return ('<getLastUpdateResult><catalog>%d</catalog><favorites>%d</favorites>'
                        '<pollInterval>30</pollInterval></getLastUpdateResult>' % (version, version))
            ident = args.get('id', '')
            if method == 'getMetadata': return self._page(self._children(ident, views), args, 'getMetadataResult')
            if method == 'search':
                term = args.get('term', '').casefold()
                nodes = [node for node in self._children('root', views) if term in node['title'].casefold()]
                return self._page(nodes, args, 'searchResult')
            if method == 'getMediaMetadata':
                return '<getMediaMetadataResult>%s</getMediaMetadataResult>' % self.entry(self._node(ident, views), wrap=False)
            if method == 'getExtendedMetadata':
                return '<getExtendedMetadataResult>%s</getExtendedMetadataResult>' % self.entry(self._node(ident, views))
            raise SettingsFault('Server.NotImplemented', 'This speaker settings action is unavailable.')

    def error_fault(self, error):
        if isinstance(error, (SmapiFault, ET.ParseError, ValueError, TypeError, AttributeError, OverflowError)):
            return super().error_fault(error)
        return 'Server.ServiceUnavailable', 'Speaker settings are unavailable. Try again shortly.'
