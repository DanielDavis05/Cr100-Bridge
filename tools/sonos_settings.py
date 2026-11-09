"""Shared, cached native Sonos settings for both playback backends.

Only the worker and explicitly requested setters perform network I/O. CR100
getters, capabilities and events consume verified snapshots. Descriptive About
metadata is never used to authorize a native command.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import ipaddress
import re
import threading
import time
import urllib.request
from urllib.parse import urljoin, urlsplit

from backends import BackendError
from sonos_identity import physical_uuid
from sonos_queue_art import _device_base, _NoRedirect, _xml
import sonos_upnp

TIMEOUT = 2
REFRESH_INTERVAL = 30
RETRY_INTERVAL = 5
STALE_AFTER = 90
MAX_XML = 1024 * 1024
MAX_DEVICES = 2             # bounded batch; each HTTP read also has a timeout
# CR100 4.3.2's native LineLevel menu (0x6cc9b8 in 25.2-50130) maps
# its ten choices to these gain values, rather than an integer 1..10 scale.
# ZP80 AudioIn SCPDs omit allowedValueRange, despite implementing this contract.
ZP80_LINE_IN_LEVELS = frozenset((0, 1, 2, 3, 4, 5, 6, 7, 9, 12))
SERVICES = {
    'RenderingControl': '/MediaRenderer/RenderingControl',
    'AVTransport': '/MediaRenderer/AVTransport',
    'DeviceProperties': '/DeviceProperties',
    'AudioIn': '/AudioIn',
}
SETTINGS_ACTIONS = {
    'RenderingControl': frozenset(('GetBass', 'SetBass', 'GetTreble', 'SetTreble',
        'GetLoudness', 'SetLoudness', 'GetSupportsOutputFixed', 'GetOutputFixed',
        'SetOutputFixed', 'GetVolume', 'SetVolume', 'GetMute', 'SetMute')),
    'DeviceProperties': frozenset(('GetLEDState', 'SetLEDState', 'GetAutoplayVolume',
        'GetUseAutoplayVolume', 'GetAutoplayLinkedZones', 'GetAutoplayRoomUUID')),
    'AVTransport': frozenset(('ConfigureSleepTimer', 'GetRemainingSleepTimerDuration')),
    'AudioIn': frozenset(('GetAudioInputAttributes', 'SetAudioInputAttributes',
                         'GetLineInLevel', 'SetLineInLevel')),
}


def handles(service, action):
    """Master volume/mute are ordinary backend controls; callers bypass them."""
    return action in SETTINGS_ACTIONS.get(service, ())


def local(tag):
    return tag.rpartition('}')[2]


def text(element, name):
    return next(((child.text or '').strip() for child in element if local(child.tag) == name), '')


def integer(value, low, high):
    if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
        raise BackendError('Expected an integer')
    result = int(value)
    if not low <= result <= high: raise BackendError('Setting is out of range')
    return str(result)


def boolean(value):
    found = {'0': '0', '1': '1', 'false': '0', 'true': '1'}.get(str(value).lower())
    if found is None: raise BackendError('Expected a boolean')
    return found


def duration(value):
    value = str(value)
    if value == '': return '', None        # Sonos cancellation, not immediate sleep
    match = re.fullmatch(r'(\d{1,2}):([0-5]\d):([0-5]\d)', value)
    if not match: raise BackendError('Invalid sleep timer duration')
    hours, minutes, seconds = map(int, match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    if total > 86399: raise BackendError('Sleep timer must be less than 24 hours')
    return value, total


def origin(value):
    found = _device_base(value)
    if found and value.rstrip('/') == found: return found
    # The direct backend's trusted loopback fixtures use ephemeral HTTP ports.
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'http' and ipaddress.ip_address(parsed.hostname).is_loopback
                and parsed.port and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment):
            return value
    except (ValueError, TypeError): pass
    return None


@dataclass
class Snapshot:
    token: tuple
    generation: int = 0
    actions: dict = field(default_factory=dict)
    variables: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    operations: frozenset = frozenset()
    healthy: bool = False
    updated: float = 0
    data_generation: int = 0
    confirmed_fixed: str | None = None
    fixed_uncertain: bool = False
    next_at: float = 0
    refresh_revision: int = 0
    discovered: bool = False
    model_number: str = ''
    io_lock: object = field(default_factory=threading.Lock)


class SettingsProvider:
    def __init__(self, binding_reader, changed, coordinator_reader=None, log=print,
                 reader=None, soap=None, clock=time.monotonic):
        self.binding_reader, self.changed = binding_reader, changed
        self.coordinator_reader = coordinator_reader or (lambda key: key)
        self.log, self.clock = log, clock
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.reader = reader or self.read_xml
        self.soap = soap or sonos_upnp.soap
        self.lock, self.refresh_lock = threading.RLock(), threading.Lock()
        self.registered, self.observed, self.last_tokens, self.snapshots = set(), {}, {}, {}
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = None

    def register(self, key):
        with self.lock: self.registered.add(key)
        self.wake.set()

    def request_refresh(self, key, output_fixed=None):
        """Schedule verified readback after a native event without doing event-thread I/O."""
        with self.lock:
            if key not in self.registered: return
        token = self.token(key)
        with self.lock:
            entry = self.snapshots.get(token)
            if entry is None: return
            # An event can announce fixed output before the worker reads it.
            # Keep volume/EQ blocked until readback confirms variable output.
            if output_fixed == '1': entry.fixed_uncertain = True
            entry.refresh_revision += 1
            entry.next_at = min(entry.next_at, self.clock())
        self.wake.set()

    def start(self):
        with self.lock:
            if self.thread is not None: return
            self.thread = threading.Thread(target=self.run, name='sonos-settings', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(TIMEOUT + 1)

    def read_xml(self, url, timeout):
        with self.opener.open(url, timeout=timeout) as response: raw = response.read(MAX_XML + 1)
        if len(raw) > MAX_XML: raise BackendError('Sonos description exceeds size limit')
        return _xml(raw)

    def xml(self, url):
        if self.stop.is_set(): raise BackendError('Settings provider closed')
        raw = self.reader(url, TIMEOUT)
        return raw if hasattr(raw, 'tag') else _xml(raw.encode() if isinstance(raw, str) else raw)

    def token(self, key, role='physical'):
        try:
            binding = self.binding_reader(key)
            base = origin(binding.native_url) if binding.native_sonos else None
            found = (binding.native_id, base) if base and physical_uuid(binding.native_id) else None
        except Exception: found = None
        with self.lock:
            previous = self.observed.get((key, role), found)
            self.observed[(key, role)] = found
            previous_native = self.last_tokens.get((key, role), found)
            if found is not None: self.last_tokens[(key, role)] = found
            if previous != found and found in self.snapshots:
                entry = self.snapshots[found]
                entry.generation += 1
                entry.healthy, entry.next_at = False, 0
                entry.discovered = False
                if previous_native != found:
                    # A different device/endpoint cannot inherit another binding's
                    # safety state. A temporary unavailable binding returning to
                    # the same native token must retain its fixed-output guard.
                    entry.confirmed_fixed, entry.fixed_uncertain = None, False
                self.wake.set()
            if found is None and previous in self.snapshots:
                entry = self.snapshots[previous]
                entry.generation += 1
                entry.healthy, entry.next_at, entry.discovered = False, 0, False
        return found

    def coordinator(self, key):
        try: return self.coordinator_reader(key)
        except Exception: return None

    def entry(self, key, timer=False):
        target = self.coordinator(key) if timer else key
        if target is None: return None
        token = self.token(target, 'timer' if timer else 'physical')
        with self.lock:
            entry = self.snapshots.get(token)
            if entry and entry.healthy and self.clock() - entry.updated <= STALE_AFTER: return entry
        return None

    def capabilities(self, key):
        physical, timer = self.entry(key), self.entry(key, timer=True)
        with self.lock:
            operations = set(physical.operations - {'sleep_timer'}) if physical else set()
            if physical and (physical.confirmed_fixed == '1' or physical.fixed_uncertain):
                operations.difference_update(('bass', 'treble', 'loudness', 'channel_volume', 'channel_mute'))
            if timer and 'sleep_timer' in timer.operations: operations.add('sleep_timer')
        return frozenset(operations)

    def volume_blocked(self, key):
        """Cache-only safety gate, including a fixed-mode write with no readback.

        Keep uncertainty separate from displayed OutputFixed: a timed-out write
        may have changed the physical output even without a confirmed value.
        Only a successful native fixed-output read or a new binding clears it.
        """
        token = self.token(key)
        with self.lock:
            entry = self.snapshots.get(token)
            return bool(entry and (entry.confirmed_fixed == '1' or entry.fixed_uncertain))

    def values(self, key):
        physical, timer = self.entry(key), self.entry(key, timer=True)
        physical_token = self.token(key)
        with self.lock:
            values = copy.deepcopy(physical.data) if physical else {}
            if physical is None:
                last = self.snapshots.get(physical_token)
                if last and last.data_generation == last.generation and last.data.get('OutputFixed') == '1':
                    # Keep a known fixed-output safety gate during a same-device
                    # outage. Getters still fault; no stale EQ or channel values
                    # become controllable. Rebinding invalidates this generation.
                    values['OutputFixed'] = '1'
            for name in ('RemainingSleepTimerDuration', 'CurrentSleepTimerGeneration'):
                values.pop(name, None)
            if timer and 'RemainingSleepTimerDuration' in timer.data:
                remaining = timer.data['RemainingSleepTimerDuration']
                if remaining:
                    _, seconds = duration(remaining)
                    seconds = max(0, seconds - int(max(0, self.clock() - timer.updated)))
                    remaining = '%d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)
                values['RemainingSleepTimerDuration'] = remaining
                values['CurrentSleepTimerGeneration'] = timer.data['CurrentSleepTimerGeneration']
        return values

    def run(self):
        while not self.stop.is_set():
            self.refresh()
            self.wake.wait(1); self.wake.clear()

    def targets(self):
        with self.lock: keys = tuple(self.registered)
        found = set()
        for key in keys:
            token = self.token(key)
            if token: found.add(token)
            coordinator = self.coordinator(key)
            if coordinator is not None:
                token = self.token(coordinator, 'timer')
                if token: found.add(token)
        return found

    def refresh(self):
        """Perform one bounded worker batch; useful for offline/native read tests."""
        if self.stop.is_set() or not self.refresh_lock.acquire(blocking=False): return
        try:
            targets, now = self.targets(), self.clock()
            with self.lock:
                for token in targets: self.snapshots.setdefault(token, Snapshot(token))
                due = sorted((self.snapshots[token] for token in targets
                              if now >= self.snapshots[token].next_at), key=lambda entry: entry.next_at)[:MAX_DEVICES]
                # Unavailability can temporarily hide an otherwise unchanged
                # binding. Keep selected rooms' last native safety guards dormant,
                # while withholding all settings until fresh identity/readback.
                protected = targets | {self.last_tokens.get((key, 'physical')) for key in self.registered}
                for token, entry in self.snapshots.items():
                    if token not in targets:
                        entry.healthy, entry.next_at, entry.discovered = False, 0, False
                excess = len(self.snapshots) - max(64, len(protected))
                dormant = sorted((entry for token, entry in self.snapshots.items() if token not in protected),
                                 key=lambda entry: entry.updated)
                for entry in dormant[:max(0, excess)]: self.snapshots.pop(entry.token)
            for entry in due:
                if self.stop.is_set(): break
                self.refresh_entry(entry)
        except Exception as error:
            self.log({'event': 'sonos-settings-worker-failed', 'error': str(error)})
        finally: self.refresh_lock.release()

    def notify(self, token):
        with self.lock: keys = tuple(self.registered)
        for key in keys:
            coordinator = self.coordinator(key)
            if self.token(key) == token or (coordinator is not None and self.token(coordinator, 'timer') == token):
                try: self.changed(key)
                except Exception as error: self.log({'event': 'sonos-settings-listener-error', 'error': str(error)})

    def refresh_entry(self, entry):
        with entry.io_lock:
            with self.lock:
                generation = entry.generation
                refresh_revision = entry.refresh_revision
                before = (entry.healthy, entry.operations, entry.data)
            try:
                if not entry.discovered: self.discover(entry)
                data, operations = self.read_values(entry)
                with self.lock:
                    if entry.generation != generation: return
                    entry.data, entry.operations = data, frozenset(operations)
                    entry.data_generation = generation
                    if 'OutputFixed' in data:
                        entry.confirmed_fixed = data['OutputFixed']
                        if refresh_revision == entry.refresh_revision: entry.fixed_uncertain = False
                    entry.healthy, entry.updated = True, self.clock()
                    entry.next_at = (entry.updated + REFRESH_INTERVAL
                                     if refresh_revision == entry.refresh_revision else entry.updated)
            except Exception as error:
                with self.lock:
                    if entry.generation != generation: return
                    entry.healthy, entry.next_at = False, self.clock() + RETRY_INTERVAL
                self.log({'event': 'sonos-settings-unavailable', 'device': entry.token[0], 'error': str(error)})
            with self.lock: changed = before != (entry.healthy, entry.operations, entry.data)
        if changed: self.notify(entry.token)

    def discover(self, entry):
        with self.lock: generation = entry.generation
        uuid, base = entry.token
        root = self.xml(base + '/xml/device_description.xml')
        device = next((child for child in root if local(child.tag) == 'device'), None)
        if (device is None or text(device, 'UDN') != 'uuid:' + uuid
                or not text(device, 'manufacturer').startswith('Sonos')):
            raise BackendError('Native Sonos description identity did not match binding')
        actions, variables = {}, {}
        for service in device.iter():
            if local(service.tag) != 'service': continue
            service_type = text(service, 'serviceType')
            name = next((name for name in SERVICES
                         if service_type == 'urn:schemas-upnp-org:service:%s:1' % name), None)
            if name is None: continue
            control = urljoin(base, text(service, 'controlURL'))
            scpd = urljoin(base, text(service, 'SCPDURL'))
            if control != base + SERVICES[name] + '/Control' or not self.same_origin(base, scpd):
                continue
            document = self.xml(scpd)
            actions[name] = {text(action, 'name') for action in document.iter() if local(action.tag) == 'action'}
            variables[name] = {}
            for variable in document.iter():
                if local(variable.tag) != 'stateVariable': continue
                allowed, ranges = [], {}
                for child in variable:
                    if local(child.tag) == 'allowedValueList': allowed = [(item.text or '') for item in child]
                    elif local(child.tag) == 'allowedValueRange': ranges = {local(item.tag): item.text for item in child}
                variables[name][text(variable, 'name')] = {'allowed': allowed, 'range': ranges,
                                                           'type': text(variable, 'dataType')}
        with self.lock:
            if entry.generation != generation:
                raise BackendError('Native Sonos binding changed during discovery')
            entry.actions, entry.variables, entry.discovered = actions, variables, True
            entry.model_number = text(device, 'modelNumber')

    @staticmethod
    def same_origin(base, url):
        parsed, expected = urlsplit(url), urlsplit(base)
        return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc) \
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment

    def call(self, entry, service, action, args=()):
        if self.stop.is_set(): raise BackendError('Settings provider closed')
        return self.soap(entry.token[1], service, action, args, TIMEOUT)

    def probe(self, entry, service, action, args=()):
        if action not in entry.actions.get(service, ()): return None
        try: return self.call(entry, service, action, args)
        except sonos_upnp.UPnPFault as error:
            if error.code is None: raise  # network outage affects all native settings
            return None                # model/action-specific refusal affects only this setting

    def read_values(self, entry):
        data, operations = {}, set()
        rc, avt, dp, ai = 'RenderingControl', 'AVTransport', 'DeviceProperties', 'AudioIn'
        instance = [('InstanceID', 0)]
        for name in ('Bass', 'Treble'):
            result = self.probe(entry, rc, 'Get' + name, instance)
            if result and 'Current' + name in result:
                data[name] = integer(result['Current' + name], -10, 10)
                if 'Set' + name in entry.actions[rc]: operations.add(name.lower())
        result = self.probe(entry, rc, 'GetLoudness', instance + [('Channel', 'Master')])
        if result and 'CurrentLoudness' in result:
            data['Loudness'] = boolean(result['CurrentLoudness'])
            data['LoudnessChannels'] = {'Master': data['Loudness']}
            if 'SetLoudness' in entry.actions[rc]: operations.add('loudness')
        for name, kind, op in (('Volume', 'volume', 'channel_volume'), ('Mute', 'mute', 'channel_mute')):
            channel_var = 'A_ARG_TYPE_MuteChannel' if name == 'Mute' else 'A_ARG_TYPE_Channel'
            allowed = entry.variables.get(rc, {}).get(channel_var, {}).get('allowed', ())
            values = {}
            for channel in ('LF', 'RF'):
                if channel not in allowed: continue
                result = self.probe(entry, rc, 'Get' + name, instance + [('Channel', channel)])
                if result and 'Current' + name in result:
                    raw = result['Current' + name]
                    values[channel] = integer(raw, 0, 100) if kind == 'volume' else boolean(raw)
            if values: data[name + 'Channels'] = values
            if all(channel in values for channel in ('LF', 'RF')) and 'Set' + name in entry.actions.get(rc, ()):
                operations.add(op)
        result = self.probe(entry, rc, 'GetSupportsOutputFixed', instance)
        if result and 'CurrentSupportsFixed' in result:
            data['SupportsOutputFixed'] = boolean(result['CurrentSupportsFixed'])
        result = self.probe(entry, rc, 'GetOutputFixed', instance)
        if result and 'CurrentFixed' in result:
            data['OutputFixed'] = boolean(result['CurrentFixed'])
            if data.get('SupportsOutputFixed') == '1' and 'SetOutputFixed' in entry.actions.get(rc, ()):
                operations.add('output_fixed')
        result = self.probe(entry, dp, 'GetLEDState')
        if result and result.get('CurrentLEDState') in ('On', 'Off'):
            data['LEDState'] = result['CurrentLEDState']
            if 'SetLEDState' in entry.actions[dp]: operations.add('led')
        # Legacy input-name popups read these independently of Master volume.
        # Preserve physical readback without enabling unimplemented setters.
        for action, field, name in (
                ('GetAutoplayVolume', 'CurrentVolume', 'AutoplayVolume'),
                ('GetUseAutoplayVolume', 'UseVolume', 'AutoplayUseVolume'),
                ('GetAutoplayLinkedZones', 'IncludeLinkedZones', 'AutoplayIncludeLinkedZones'),
                ('GetAutoplayRoomUUID', 'RoomUUID', 'AutoplayRoomUUID')):
            result = self.probe(entry, dp, action)
            if result is None or field not in result: continue
            value = result[field]
            try:
                if name == 'AutoplayVolume': value = integer(value, 0, 100)
                elif name != 'AutoplayRoomUUID': value = boolean(value)
                elif not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
                    continue
            except BackendError:
                continue  # Invalid optional readback must not disable EQ/volume.
            data[name] = value
        result = self.probe(entry, ai, 'GetAudioInputAttributes')
        if result and {'CurrentName', 'CurrentIcon'} <= result.keys():
            data.update(AudioInputName=result['CurrentName'], Icon=result['CurrentIcon'])
            if 'SetAudioInputAttributes' in entry.actions[ai]: operations.add('line_in_settings')
        result = self.probe(entry, ai, 'GetLineInLevel')
        if result and {'CurrentLeftLineInLevel', 'CurrentRightLineInLevel'} <= result.keys():
            for side in ('Left', 'Right'):
                data[side + 'LineInLevel'] = integer(result['Current' + side + 'LineInLevel'], -2147483648, 2147483647)
            if 'SetLineInLevel' in entry.actions[ai] and all(self.level_range(entry, side) for side in ('Left', 'Right')):
                operations.add('line_in_level')
        result = self.probe(entry, avt, 'GetRemainingSleepTimerDuration', instance)
        if result and {'RemainingSleepTimerDuration', 'CurrentSleepTimerGeneration'} <= result.keys():
            data['RemainingSleepTimerDuration'] = duration(result['RemainingSleepTimerDuration'])[0]
            data['CurrentSleepTimerGeneration'] = integer(result['CurrentSleepTimerGeneration'], 0, 4294967295)
            if 'ConfigureSleepTimer' in entry.actions[avt]: operations.add('sleep_timer')
        if data.get('OutputFixed') == '1':
            operations.difference_update(('bass', 'treble', 'loudness', 'channel_volume', 'channel_mute'))
        return data, operations

    @staticmethod
    def level_range(entry, side):
        variable = entry.variables.get('AudioIn', {}).get(side + 'LineInLevel', {})
        value = variable.get('range', {})
        if not value and entry.model_number == 'ZP80' and variable.get('type') == 'i4':
            return min(ZP80_LINE_IN_LEVELS), max(ZP80_LINE_IN_LEVELS)
        try:
            low, high = int(value['minimum']), int(value['maximum'])
            step = int(value.get('step') or 1)
            # The stock input UI has no step-size negotiation. Restrict writes
            # to an explicit native integer interval it can represent safely.
            return (low, high) if step == 1 and -2147483648 <= low <= high <= 2147483647 else None
        except (KeyError, TypeError, ValueError): return None

    def action(self, key, service, action, args):
        if not handles(service, action): return None
        if service == 'RenderingControl' and action in ('GetVolume', 'SetVolume', 'GetMute', 'SetMute') \
                and args.get('Channel', 'Master') == 'Master': return None
        if args.get('InstanceID', '0') not in (0, '0'): raise BackendError('Only instance 0 is supported')
        timer = service == 'AVTransport'
        entry = self.entry(key, timer=timer)
        if entry is None: raise BackendError('Native Sonos settings are unavailable')
        if action.startswith('Get'):
            return self.getter(key, entry, service, action, args)
        operation, native_args = self.setter(entry, service, action, args)
        if operation not in self.capabilities(key): raise BackendError('%s is unavailable for this player' % operation)
        with entry.io_lock:
            target = self.coordinator(key) if timer else key
            if target is None or self.token(target, 'timer' if timer else 'physical') != entry.token:
                raise BackendError('Native Sonos binding changed')
            if operation not in self.capabilities(key): raise BackendError('Native setting availability changed')
            with self.lock:
                generation = entry.generation
                refresh_revision = entry.refresh_revision
            try:
                if action == 'SetOutputFixed':
                    with self.lock: entry.fixed_uncertain = True
                self.call(entry, service, action, native_args)
                data, operations = self.read_values(entry)
                if not self.readback_matches(action, dict(native_args), data):
                    raise BackendError('Native setting readback did not confirm the requested value')
                current = self.coordinator(key) if timer else key
                if current is None or self.token(current, 'timer' if timer else 'physical') != entry.token:
                    raise BackendError('Native Sonos binding changed during readback')
                with self.lock:
                    if generation != entry.generation: raise BackendError('Native Sonos binding changed')
                    entry.data, entry.operations, entry.healthy = data, frozenset(operations), True
                    entry.data_generation = generation
                    if 'OutputFixed' in data:
                        entry.confirmed_fixed = data['OutputFixed']
                        if refresh_revision == entry.refresh_revision: entry.fixed_uncertain = False
                    entry.updated = self.clock()
                    entry.next_at = (entry.updated + REFRESH_INTERVAL
                                     if refresh_revision == entry.refresh_revision else entry.updated)
            except Exception as error:
                with self.lock:
                    entry.healthy, entry.next_at = False, self.clock() + RETRY_INTERVAL
                self.wake.set()
                self.notify(entry.token)
                raise BackendError('Native Sonos setting failed: %s' % error) from error
        self.notify(entry.token)
        return {}

    @staticmethod
    def readback_matches(action, args, data):
        mapping = {'SetBass': ('Bass', 'DesiredBass'), 'SetTreble': ('Treble', 'DesiredTreble'),
                   'SetLoudness': ('Loudness', 'DesiredLoudness'), 'SetLEDState': ('LEDState', 'DesiredLEDState'),
                   'SetOutputFixed': ('OutputFixed', 'DesiredFixed')}
        if action in mapping:
            field, argument = mapping[action]
            return data.get(field) == args[argument]
        if action in ('SetVolume', 'SetMute'):
            name = action[3:]
            return data.get(name + 'Channels', {}).get(args['Channel']) == args['Desired' + name]
        if action == 'SetAudioInputAttributes':
            # Native CR100 custom-name edits send an empty DesiredIcon. ZP80
            # preserves its existing icon in that case; the requested name must
            # still be confirmed exactly, and explicit icon edits are checked.
            return data.get('AudioInputName') == args['DesiredName'] and \
                (args['DesiredIcon'] == '' or data.get('Icon') == args['DesiredIcon'])
        if action == 'SetLineInLevel':
            return all(data.get(side + 'LineInLevel') == args['Desired' + side + 'LineInLevel'] for side in ('Left', 'Right'))
        if action == 'ConfigureSleepTimer':
            requested, seconds = duration(args['NewSleepTimerDuration'])
            actual = data.get('RemainingSleepTimerDuration')
            if actual is None: return False
            if requested == '': return actual == ''
            if actual == '': return seconds == 0     # an immediate native timer can already have expired
            _, remaining = duration(actual)
            return max(0, seconds - 5) <= remaining <= seconds
        return False

    def getter(self, key, entry, service, action, args):
        if action not in entry.actions.get(service, ()): raise BackendError('Native getter unavailable')
        values = self.values(key)
        mapping = {
            'GetBass': {'CurrentBass': 'Bass'}, 'GetTreble': {'CurrentTreble': 'Treble'},
            'GetOutputFixed': {'CurrentFixed': 'OutputFixed'},
            'GetSupportsOutputFixed': {'CurrentSupportsFixed': 'SupportsOutputFixed'},
            'GetLEDState': {'CurrentLEDState': 'LEDState'},
            'GetAutoplayVolume': {'CurrentVolume': 'AutoplayVolume'},
            'GetUseAutoplayVolume': {'UseVolume': 'AutoplayUseVolume'},
            'GetAutoplayLinkedZones': {'IncludeLinkedZones': 'AutoplayIncludeLinkedZones'},
            'GetAutoplayRoomUUID': {'RoomUUID': 'AutoplayRoomUUID'},
            'GetAudioInputAttributes': {'CurrentName': 'AudioInputName', 'CurrentIcon': 'Icon'},
            'GetLineInLevel': {'CurrentLeftLineInLevel': 'LeftLineInLevel', 'CurrentRightLineInLevel': 'RightLineInLevel'},
            'GetRemainingSleepTimerDuration': {'RemainingSleepTimerDuration': 'RemainingSleepTimerDuration',
                                               'CurrentSleepTimerGeneration': 'CurrentSleepTimerGeneration'},
        }
        if action in ('GetVolume', 'GetMute'):
            name = action[3:]; channel = args.get('Channel', 'Master')
            if channel not in ('LF', 'RF'): raise BackendError('Unsupported rendering channel')
            channels = values.get(name + 'Channels', {})
            if channel not in channels: raise BackendError('Native channel readback unavailable')
            return {'Current' + name: channels[channel]}
        if action == 'GetLoudness':
            if args.get('Channel', 'Master') != 'Master': raise BackendError('Only Master loudness is supported')
            mapping[action] = {'CurrentLoudness': 'Loudness'}
        fields = mapping[action]
        if not all(name in values for name in fields.values()): raise BackendError('Native setting readback unavailable')
        return {out: values[name] for out, name in fields.items()}

    def setter(self, entry, service, action, args):
        instance = [('InstanceID', 0)]
        if service == 'RenderingControl':
            if action in ('SetBass', 'SetTreble'):
                name = action[3:]
                return name.lower(), instance + [('Desired' + name, integer(args.get('Desired' + name), -10, 10))]
            if action == 'SetLoudness':
                if args.get('Channel', 'Master') != 'Master': raise BackendError('Only Master loudness is supported')
                return 'loudness', instance + [('Channel', 'Master'), ('DesiredLoudness', boolean(args.get('DesiredLoudness')))]
            if action in ('SetVolume', 'SetMute'):
                name, channel = action[3:], args.get('Channel')
                if channel not in ('LF', 'RF'): raise BackendError('Unsupported rendering channel')
                value = integer(args.get('DesiredVolume'), 0, 100) if name == 'Volume' else boolean(args.get('DesiredMute'))
                return 'channel_' + name.lower(), instance + [('Channel', channel), ('Desired' + name, value)]
            if action == 'SetOutputFixed':
                return 'output_fixed', instance + [('DesiredFixed', boolean(args.get('DesiredFixed')))]
        elif service == 'DeviceProperties' and action == 'SetLEDState':
            value = args.get('DesiredLEDState')
            if value not in ('On', 'Off'): raise BackendError('LED state must be On or Off')
            return 'led', [('DesiredLEDState', value)]
        elif service == 'AVTransport' and action == 'ConfigureSleepTimer':
            value, _ = duration(args.get('NewSleepTimerDuration', ''))
            return 'sleep_timer', instance + [('NewSleepTimerDuration', value)]
        elif service == 'AudioIn':
            if action == 'SetAudioInputAttributes':
                values = [(name, args.get(name)) for name in ('DesiredName', 'DesiredIcon')]
                if any(not isinstance(value, str) or len(value) > 256 or any(ord(ch) < 32 for ch in value)
                       for _, value in values): raise BackendError('Invalid Line-In name or icon')
                return 'line_in_settings', values
            if action == 'SetLineInLevel':
                values = []
                for side in ('Left', 'Right'):
                    bounds = self.level_range(entry, side)
                    if bounds is None: raise BackendError('Native Line-In level range is unavailable')
                    name = 'Desired' + side + 'LineInLevel'
                    value = integer(args.get(name), *bounds)
                    variable = entry.variables.get('AudioIn', {}).get(side + 'LineInLevel', {})
                    if not variable.get('range') and int(value) not in ZP80_LINE_IN_LEVELS:
                        raise BackendError('Unsupported native Line-In level')
                    values.append((name, value))
                return 'line_in_level', values
        raise BackendError('Native setting operation is unavailable')
