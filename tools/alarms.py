"""Global, provider-independent alarms for the bridge and native CR100 adapters.

Providers resolve canonical references at firing time; credentials and resolved
playback URLs are never written to the alarm database. Executors implement actual
playback and ownership checks. A successful execute/fallback callback MUST return
an opaque ownership handle, not a stream URL or an authentication token.

The scheduler uses UTC occurrence keys and a durable dispatch journal. Ambiguous
speaker acknowledgements are reconciled rather than blindly replayed. Recurring
wall times in a DST gap are skipped, and a repeated wall time fires once, at its
first occurrence. Missed alarms older than ``missed_grace`` are not played late.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time as wall_time, timedelta, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable
from urllib.parse import quote, unquote
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
NATIVE_TIME_SERVER = '0.sonostime.pool.ntp.org,1.sonostime.pool.ntp.org,2.sonostime.pool.ntp.org,3.sonostime.pool.ntp.org'
SCHEMA_VERSION = 1
ACTIVE = ('preparing', 'playing', 'uncertain')
PENDING = ('claimed', 'preparing', 'retry')


class AlarmError(Exception):
    def __init__(self, message, code=501):
        super().__init__(message)
        self.code = code


class ExecutionUncertain(Exception):
    """Playback may have started; reconcile before another playback/fallback."""


def _integer(value, low, high, name):
    if isinstance(value, bool): raise AlarmError('Invalid %s' % name, 402)
    try: result = int(value)
    except (ValueError, TypeError): raise AlarmError('Invalid %s' % name, 402) from None
    if str(value).strip() != str(result) or not low <= result <= high:
        raise AlarmError('Invalid %s' % name, 402)
    return result


def _boolean(value):
    if value is True or value in ('1', 'true'): return True
    if value is False or value in ('0', 'false'): return False
    raise AlarmError('Expected a boolean', 402)


def _duration(value, *, allow_empty=True):
    if value in (None, '') and allow_empty: return None
    if isinstance(value, int) and not isinstance(value, bool):
        return _integer(value, 1, 7 * 86400, 'duration')
    match = re.fullmatch(r'(\d{1,3}):([0-5]\d):([0-5]\d)', str(value))
    if not match: raise AlarmError('Expected H:MM:SS duration', 402)
    seconds = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
    return _integer(seconds, 1, 7 * 86400, 'duration')


def hms(seconds):
    seconds = max(0, int(seconds))
    return '%d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def _zone(value):
    fixed = re.fullmatch(r'UTC([+-])(\d{2}):([0-5]\d)', str(value))
    if fixed:
        minutes = int(fixed[2]) * 60 + int(fixed[3])
        if minutes >= 24 * 60: raise AlarmError('Invalid fixed timezone offset', 402)
        return timezone(timedelta(minutes=minutes if fixed[1] == '+' else -minutes), str(value))
    try: return ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise AlarmError('Unknown IANA timezone', 402) from None


def recurrence_days(value):
    """Python weekdays; Sonos ON_ digits use Sunday=0, Monday=1."""
    if value in ('DAILY', 'ONCE'): return set(range(7))
    if value == 'WEEKDAYS': return set(range(5))
    if value == 'WEEKENDS': return {5, 6}
    if isinstance(value, str) and re.fullmatch(r'ON_[0-6]{1,7}', value):
        digits = value[3:]
        if len(set(digits)) != len(digits): raise AlarmError('Duplicate recurrence day', 402)
        return {(int(digit) + 6) % 7 for digit in digits}
    raise AlarmError('Unsupported alarm recurrence', 402)


@dataclass(frozen=True)
class AlarmMediaRef:
    provider: str
    content_id: str
    account_ref: str | None = None

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls): value = asdict(value)
        if not isinstance(value, dict) or set(value) - {'provider', 'content_id', 'account_ref'}:
            raise AlarmError('Expected a canonical media reference, without credentials', 402)
        provider, content = value.get('provider'), value.get('content_id')
        if not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', provider):
            raise AlarmError('Invalid alarm provider', 402)
        if not isinstance(content, str) or not content or len(content) > 2048 or any(ord(c) < 32 for c in content):
            raise AlarmError('Invalid canonical content ID', 402)
        if re.match(r'(?i)(https?|rtsp|icy)://', content) or '?' in content or '#' in content:
            raise AlarmError('Store a canonical content ID instead of a temporary URL', 402)
        account = value.get('account_ref')
        if account is not None and (not isinstance(account, str) or not re.fullmatch(r'[\w.:-]{1,160}', account)):
            raise AlarmError('Invalid account reference', 402)
        return cls(provider, content, account)

    def as_dict(self): return asdict(self)

    def uri(self):
        return 'x-cr100-alarm:%s:%s%s' % (self.provider, quote(self.content_id, safe=''),
            '' if self.account_ref is None else '?account=' + quote(self.account_ref, safe=''))


@dataclass(frozen=True)
class PlaybackRequest:
    """Ephemeral; never persisted. kind is live, track, queue, or chime."""
    kind: str
    uri: str
    metadata: str = ''
    title: str = ''
    preserve_queue: bool = True
    items: tuple = ()

    def __post_init__(self):
        if self.kind not in ('live', 'track', 'queue', 'chime') or not isinstance(self.uri, str) or not self.uri:
            raise AlarmError('Provider returned an invalid playback request')


@dataclass(frozen=True)
class NativeAlarmProgram:
    uri: str
    metadata: str = ''
    stable: bool = False
    renderer_reachable: bool = False


class AlarmProviderRegistry:
    """Providers: describe(ref), resolve(ref, context), optional sources/from_native."""
    def __init__(self, providers=None):
        self.providers = dict(providers or {})

    def register(self, name, provider):
        AlarmMediaRef.parse({'provider': name, 'content_id': 'validation'})
        if not callable(getattr(provider, 'describe', None)) or not callable(getattr(provider, 'resolve', None)):
            raise TypeError('Alarm providers need describe() and resolve()')
        self.providers[name] = provider
        return provider

    def provider(self, reference):
        provider = self.providers.get(reference.provider)
        if provider is None: raise AlarmError('Alarm source provider is unavailable: %s' % reference.provider)
        return provider

    def describe(self, reference):
        reference = AlarmMediaRef.parse(reference)
        try: result = self.provider(reference).describe(reference)
        except AlarmError: raise
        except Exception as error:
            raise AlarmError('Alarm source is unavailable: ' + type(error).__name__) from error
        if not isinstance(result, dict) or not isinstance(result.get('title'), str) or not result['title']:
            raise AlarmError('Provider cannot describe this alarm source')
        return {'title': result['title'][:256], 'provider': reference.provider,
                'content_id': reference.content_id, 'account_ref': reference.account_ref}

    def resolve(self, reference, context):
        request = self.provider(reference).resolve(reference, context)
        if not isinstance(request, PlaybackRequest): raise AlarmError('Provider returned no playback request')
        return request

    def sources(self):
        result = []
        for name, provider in self.providers.items():
            reader = getattr(provider, 'sources', None)
            try:
                items = [] if reader is None else reader()
                for item in items:
                    ref = AlarmMediaRef.parse(item.get('media_ref', {'provider': name,
                                               'content_id': item.get('content_id'),
                                               'account_ref': item.get('account_ref')}))
                    result.append({'media_ref': ref.as_dict(), **self.describe(ref)})
            except Exception as error:
                result.append({'provider': name, 'available': False, 'error': type(error).__name__})
        return result

    def from_native(self, uri, metadata=''):
        if uri.startswith('x-cr100-alarm:'):
            body = uri[len('x-cr100-alarm:'):]
            identity, _, account = body.partition('?account=')
            provider, separator, content = identity.partition(':')
            if not separator: raise AlarmError('Invalid alarm reference', 402)
            return AlarmMediaRef.parse({'provider': provider, 'content_id': unquote(content),
                                       'account_ref': unquote(account) if account else None})
        if uri == 'x-rincon-buzzer:0': return AlarmMediaRef('chime', 'default')
        for provider in self.providers.values():
            decoder = getattr(provider, 'from_native', None)
            if decoder:
                result = decoder(uri, metadata)
                if result is not None: return AlarmMediaRef.parse(result)
        raise AlarmError('This alarm source is not registered', 402)


class CallbackAlarmExecutor:
    """Actual backend operations, injected without coupling alarms to HA/Sonos.

    execute(request, context)->opaque handle; owns(context, handle)->bool|None;
    stop(context, handle). Optional fallback(context) and recover(context) also
    return handles. capture(context) supplies ownership for shared sleep timers.
    Callbacks must be bounded. Only explicit True authorizes stopping playback.
    """
    def __init__(self, execute, owns, stop, *, fallback=None, recover=None, capture=None):
        self._execute, self._owns, self._stop = execute, owns, stop
        self._fallback, self._recover, self._capture = fallback, recover, capture

    @staticmethod
    def handle(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value):
            raise ExecutionUncertain('Executor must return an opaque playback ownership handle')
        return value

    def execute(self, request, context): return self.handle(self._execute(request, context))
    def owns(self, context, handle): return self._owns(context, handle)
    def stop(self, context, handle): return self._stop(context, handle)
    def fallback(self, context):
        if self._fallback is None: raise AlarmError('No compatible fallback chime is available')
        return self.handle(self._fallback(context))
    def recover(self, context):
        if self._recover is None: return None
        result = self._recover(context)
        return None if result is None else self.handle(result)
    def capture(self, context):
        if self._capture is None: raise AlarmError('This backend cannot safely own a bridge sleep timer')
        return self.handle(self._capture(context))


class AlarmStore:
    """SQLite transactions serialize native/web edits and occurrence claims."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            version = self.get_meta('schema_version', SCHEMA_VERSION)
            if version != SCHEMA_VERSION: raise AlarmError('Unsupported alarm database version')
            self.set_meta('schema_version', SCHEMA_VERSION)
            self.db.execute('CREATE TABLE IF NOT EXISTS alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, alarm_id INTEGER NOT NULL, '
                            'occurrence REAL NOT NULL, data TEXT NOT NULL, UNIQUE(alarm_id, occurrence))')
            self.db.execute('CREATE TABLE IF NOT EXISTS sleep (room TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL)')

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def increment(self, key):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            value = int(self.get_meta(key, 0)) + 1
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
            return value

    @property
    def version(self): return int(self.get_meta('alarm_version', 1))

    def records(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM alarms ORDER BY id')]

    def get(self, alarm_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM alarms WHERE id=?', (alarm_id,)).fetchone()
            if row is None: raise AlarmError('No such alarm', 701)
            return json.loads(row[0])

    def save(self, record, expected_revision=None):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if record.get('id') is None:
                cursor = self.db.execute('INSERT INTO alarms(data) VALUES (?)', ('{}',))
                record = {**record, 'id': cursor.lastrowid, 'revision': 1}
            else:
                old = self.get(record['id'])
                if expected_revision is not None and old['revision'] != expected_revision:
                    raise AlarmError('Alarm changed; reload before saving', 409)
                record = {**record, 'revision': old['revision'] + 1}
            self.db.execute('UPDATE alarms SET data=? WHERE id=?', (json.dumps(record), record['id']))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('alarm_version', json.dumps(self.version + 1)))
            return record

    def delete(self, alarm_id):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.get(alarm_id)
            self.db.execute('DELETE FROM alarms WHERE id=?', (alarm_id,))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('alarm_version', json.dumps(self.version + 1)))

    def claim(self, alarm, occurrence, now, *, parent_id=None):
        identity = '%s:%.6f' % (alarm['id'], occurrence)
        run_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        context = {'run_id': run_id, 'alarm_id': alarm['id'], 'room': alarm['room'],
                   'occurrence': occurrence, 'generation': run_id,
                   'include_linked': alarm['include_linked'], 'volume': alarm['volume'],
                   'play_mode': alarm['play_mode'], 'duration': alarm['duration'],
                   'media_ref': alarm['media_ref']}
        record = {'id': run_id, 'alarm_id': alarm['id'], 'occurrence': occurrence,
                  'created_at': now, 'status': 'claimed', 'context': context,
                  'alarm_revision': alarm['revision'], 'snapshot': alarm,
                  'handle': None, 'deadline': None, 'attempts': 0, 'parent_id': parent_id}
        with self.lock, self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?,?)',
                                    (run_id, alarm['id'], occurrence, json.dumps(record)))
            return record if cursor.rowcount else None

    def runs(self, statuses=None):
        with self.lock:
            records = [json.loads(row[0]) for row in self.db.execute('SELECT data FROM runs ORDER BY occurrence')]
        return records if statuses is None else [r for r in records if r['status'] in statuses]

    def run(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update_run(self, run_id, **values):
        with self.lock, self.db:
            record = self.run(run_id)
            if record is None: raise AlarmError('No such alarm occurrence', 701)
            record.update(values)
            self.db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(record), run_id))
            return record

    def sleep_records(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM sleep')]

    def save_sleep(self, room, record):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO sleep VALUES (?,?)', (room, json.dumps(record)))

    def queue_command(self, record):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO commands VALUES (?,?,?)',
                            (record['id'], 'pending', json.dumps(record)))
            return self.command(record['id'])

    def command(self, command_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM commands WHERE id=?', (command_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def commands(self, state=None):
        with self.lock:
            rows = self.db.execute('SELECT data FROM commands WHERE state=?', (state,)) if state else self.db.execute('SELECT data FROM commands')
            return [json.loads(row[0]) for row in rows]

    def update_command(self, command_id, **values):
        with self.lock, self.db:
            record = self.command(command_id)
            if record is None: raise AlarmError('No such alarm command', 701)
            record.update(values)
            self.db.execute('UPDATE commands SET state=?,data=? WHERE id=?',
                            (record['state'], json.dumps(record), command_id))
            return record

    def claim_command(self, command_id):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            record = self.command(command_id)
            if record is None or record['state'] != 'pending': return None
            record['state'] = 'processing'
            self.db.execute('UPDATE commands SET state=?,data=? WHERE id=?',
                            ('processing', json.dumps(record), command_id))
            return record

    def close(self):
        with self.lock: self.db.close()


def next_occurrence(alarm, after, *, inclusive=False):
    """UTC timestamp of the next valid wall time; spring gaps skip, fall repeats once."""
    zone = _zone(alarm['timezone'])
    current = datetime.fromtimestamp(after, UTC)
    start = current.astimezone(zone).date()
    clock = wall_time.fromisoformat(alarm['time'])
    days = recurrence_days(alarm['recurrence'])
    once = date.fromisoformat(alarm['once_date']) if alarm.get('once_date') else None
    for offset in range(370):
        day = start + timedelta(days=offset)
        if once is not None and day != once: continue
        if day.weekday() not in days: continue
        local = datetime.combine(day, clock).replace(tzinfo=zone, fold=0)
        candidate = local.astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None): continue
        stamp = candidate.timestamp()
        if stamp > after or (inclusive and stamp == after): return stamp
        if once is not None and day >= once: return None
    return None


class AlarmScheduler:
    def __init__(self, service, interval=1.0):
        self.service, self.interval = service, interval
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stopping.clear()
        def run():
            while not self.stopping.is_set():
                try: self.service.tick()
                except Exception as error: self.service.log({'event': 'alarm-scheduler-error', 'error': type(error).__name__})
                self.stopping.wait(self.interval)
        self.thread = threading.Thread(target=run, name='bridge-alarms', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(15)
            if self.thread.is_alive(): raise AlarmError('Alarm executor has not completed shutdown')


class AlarmService:
    def __init__(self, path, *, providers=None, executor=None, timezone='UTC', clock=time.time,
                 on_change=None, log=print, room_resolver=None, native_room=None,
                 missed_grace=300, retry_delay=5, max_attempts=2, handle_sleep=False,
                 native_delegate=None, timezone_from_index=None, autostart=False,
                 bridge_sleep=None, topology_identity=None):
        self.store = path if isinstance(path, AlarmStore) else AlarmStore(path)
        self.providers = providers if providers is not None else AlarmProviderRegistry()
        self.executor, self.system_clock = executor, clock
        self.clock = lambda: self.system_clock() + float(self.store.get_meta('clock_offset', 0))
        self.on_change, self.log = on_change, log
        self.room_resolver, self.native_room = room_resolver or str, native_room or str
        self.missed_grace, self.retry_delay, self.max_attempts = missed_grace, retry_delay, max_attempts
        self.handle_sleep, self.native_delegate = handle_sleep, native_delegate
        self.bridge_sleep = bridge_sleep or (lambda room: True)
        self.topology_identity = topology_identity or (lambda room: (self.native_room(room), 1))
        self.timezone_from_index = timezone_from_index
        self._tick_lock = threading.Lock()
        self._edit_lock = threading.RLock()
        self._closed = False
        self.store.set_meta('timezone', self.store.get_meta('timezone', str(_zone(timezone))))
        self.store.set_meta('scheduler_cursor', self.store.get_meta('scheduler_cursor', self.clock()))
        self.scheduler = AlarmScheduler(self)
        if self.executor is not None: self._recover()
        if autostart: self.start()

    @property
    def timezone(self): return self.store.get_meta('timezone', 'UTC')

    def _notify(self, event='alarms-changed', **values):
        message = {'event': event, 'version': self.store.version, **values}
        self.log(message)
        if self.on_change:
            try: self.on_change(message)
            except Exception as error:
                self.log({'event': 'alarm-notification-error', 'error': type(error).__name__})

    def _validate(self, values, old=None):
        if not isinstance(values, dict): raise AlarmError('Expected an alarm object', 402)
        old = old or {}
        data = {**old, **values}
        reference = AlarmMediaRef.parse(data.get('media_ref'))
        # Editing an already unavailable source remains possible (e.g. disable it).
        unchanged = old.get('media_ref') == reference.as_dict()
        if reference.provider == 'chime' and reference.content_id == 'default': title = 'Sonos Chime'
        elif unchanged:
            title = old['title']
        else: title = self.providers.describe(reference)['title']
        raw_time = str(data.get('time', '07:00:00'))
        if re.fullmatch(r'\d\d:\d\d', raw_time): raw_time += ':00'
        if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d:[0-5]\d', raw_time):
            raise AlarmError('Expected a local HH:MM:SS alarm time', 402)
        zone = str(_zone(data.get('timezone', self.timezone)))
        recurrence = data.get('recurrence', 'DAILY'); recurrence_days(recurrence)
        room = data.get('room')
        if not isinstance(room, str) or not room or len(room) > 256 or any(ord(c) < 32 for c in room):
            raise AlarmError('Choose an alarm room', 402)
        owner = data.get('execution_owner', 'bridge')
        if owner not in ('bridge', 'native', 'blocked'): raise AlarmError('Unknown alarm execution owner', 402)
        play_mode = data.get('play_mode', 'NORMAL')
        if play_mode not in ('NORMAL', 'REPEAT_ALL', 'SHUFFLE_NOREPEAT', 'SHUFFLE', 'REPEAT_ONE', 'SHUFFLE_REPEAT_ONE'):
            raise AlarmError('Unsupported alarm play mode', 402)
        now = self.clock()
        record = {'id': old.get('id'), 'revision': old.get('revision', 0),
                  'created_at': old.get('created_at', now), 'updated_at': now,
                  'owner_since': old.get('owner_since', old.get('created_at', now)),
                  'enabled': _boolean(data.get('enabled', True)), 'time': raw_time,
                  'timezone': zone, 'recurrence': recurrence,
                  'room': room, 'include_linked': _boolean(data.get('include_linked', False)),
                  'volume': _integer(data.get('volume', 20), 0, 100, 'alarm volume'),
                  'duration': _duration(data.get('duration')), 'media_ref': reference.as_dict(),
                  'title': title, 'play_mode': play_mode, 'execution_owner': owner,
                  'native_id': old.get('native_id'), 'handoff': old.get('handoff')}
        if recurrence == 'ONCE':
            once_date = data.get('once_date')
            if once_date is None:
                probe = {**record, 'recurrence': 'DAILY'}
                candidate = next_occurrence(probe, now, inclusive=True)
                once_date = datetime.fromtimestamp(candidate, _zone(zone)).date().isoformat()
            try: date.fromisoformat(once_date)
            except (ValueError, TypeError): raise AlarmError('Invalid one-time alarm date', 402) from None
            record['once_date'] = once_date
        return record

    def save(self, values, *, expected_revision=None):
        with self._edit_lock:
            alarm_id = values.get('id') if isinstance(values, dict) else None
            if alarm_id is not None and expected_revision is None and 'revision' in values:
                expected_revision = values['revision']
            old = self.store.get(_integer(alarm_id, 1, 2**31-1, 'alarm ID')) if alarm_id is not None else None
            record = self._validate(values, old)
            desired_owner = record['execution_owner']
            old_owner = old['execution_owner'] if old else 'bridge'
            needs_native = desired_owner == 'native' or old_owner == 'native' or bool(old and old.get('handoff'))
            if needs_native:
                record['handoff'] = {'old_native_id': old.get('native_id') if old else None,
                                     'desired_owner': desired_owner, 'desired_enabled': record['enabled'],
                                     'new_native_id': None, 'phase': 'disable-old'}
                record['execution_owner'], record['enabled'] = 'blocked', False
            saved = self.store.save(record, expected_revision)
            for run in self.store.runs(PENDING):
                if run['alarm_id'] == saved['id']: self.store.update_run(run['id'], status='cancelled')
            if needs_native:
                saved = self._reconcile_native(saved)
            self._notify(alarm_id=saved['id'])
            return saved

    def list(self):
        records = self.store.records()
        for record in records:
            record['next_occurrence'] = next_occurrence(record, self.clock()) if record['enabled'] else None
            runs = [run for run in self.store.runs() if run['alarm_id'] == record['id']]
            record['last_run'] = self._public_run(runs[-1]) if runs else None
        return records

    @staticmethod
    def _public_run(run):
        return {key: run.get(key) for key in ('id', 'alarm_id', 'occurrence', 'status', 'deadline',
                                             'resume_at', 'error', 'fallback', 'attempts')}

    def delete(self, alarm_id, *, expected_revision=None):
        alarm_id = _integer(alarm_id, 1, 2**31-1, 'alarm ID')
        with self._edit_lock:
            record = self.store.get(alarm_id)
            if expected_revision is not None and record['revision'] != expected_revision:
                raise AlarmError('Alarm changed; reload before deleting', 409)
            native_ids = {record.get('native_id')}
            if record.get('handoff'):
                native_ids.update((record['handoff'].get('old_native_id'), record['handoff'].get('new_native_id')))
            for native_id in native_ids - {None}:
                self._disable_native(native_id)
                if self.native_delegate.remove(native_id) is not True:
                    raise AlarmError('Native alarm deletion could not be confirmed')
            for run in self.store.runs(ACTIVE + ('snoozed', 'retry', 'claimed')):
                if run['alarm_id'] == alarm_id:
                    if self.executor is None and run['status'] in ('playing', 'uncertain'):
                        self.command('stop', room=run['context']['room'], run_id=run['id'])
                    else: self._stop_run(run, 'cancelled')
            self.store.delete(alarm_id)
            self._notify(alarm_id=alarm_id)

    def sources(self): return self.providers.sources()

    def snapshot(self):
        """Safe for a separate web process; constructing/reading starts no scheduler."""
        return {'alarms': self.list(), 'sources': self.sources(), 'timezone': self.timezone,
                'version': self.store.version,
                'runs': [self._public_run(r) for r in self.store.runs()[-100:]],
                'commands': self.store.commands()[-100:],
                'scheduler': {'last_tick': self.store.get_meta('scheduler_cursor')}}

    def command(self, action, *, room=None, alarm_id=None, run_id=None, seconds=540, request_id=None):
        """Durable web->player command. Reusing request_id does not dispatch twice."""
        if action not in ('stop', 'snooze', 'run', 'test', 'cancel_snooze'):
            raise AlarmError('Unknown alarm command', 402)
        command_id = request_id or str(uuid.uuid4())
        if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', command_id):
            raise AlarmError('Invalid alarm request ID', 402)
        if action in ('run', 'test'):
            alarm_id = _integer(alarm_id, 1, 2**31-1, 'alarm ID')
            self.store.get(alarm_id)
        elif not isinstance(room, str) or not room:
            raise AlarmError('Choose an alarm room', 402)
        record = {'id': command_id, 'action': action, 'room': room, 'alarm_id': alarm_id,
                  'run_id': run_id, 'seconds': _integer(seconds, 1, 86400, 'snooze duration'),
                  'created_at': self.clock(), 'state': 'pending'}
        existing = self.store.command(command_id)
        if existing and any(existing.get(k) != record.get(k) for k in ('action', 'room', 'alarm_id', 'run_id', 'seconds')):
            raise AlarmError('Request ID is already used for another command', 409)
        return self.store.queue_command(record)

    def _commands(self, now):
        for pending in self.store.commands('pending'):
            command = self.store.claim_command(pending['id'])
            if command is None: continue
            try:
                if now - command['created_at'] > self.missed_grace:
                    raise AlarmError('Alarm command expired while bridge was unavailable')
                if command['action'] in ('run', 'test'):
                    result = self.run_now(command['alarm_id'], occurrence=command['created_at'])
                elif command['action'] == 'snooze': result = self.snooze(command['room'], command['seconds'])
                else: result = self.stop(command['room'], command.get('run_id'))
                self.store.update_command(command['id'], state='complete', result=result, completed_at=now)
            except Exception as error:
                self.store.update_command(command['id'], state='failed', error=str(error) if isinstance(error, AlarmError) else type(error).__name__, completed_at=now)

    def references(self, provider, content_id):
        return [r for r in self.store.records() if r['media_ref']['provider'] == provider
                and r['media_ref']['content_id'] == content_id]

    def _context(self, run):
        return {**run['context'], 'room': self.room_resolver(run['context']['room'])}

    def _recover(self):
        # A process may have died after commanding playback but before saving its
        # acknowledgement. Never dispatch those journal entries a second time.
        for run in self.store.runs(('claimed', 'preparing', 'playing', 'uncertain')):
            try:
                context = self._context(run)
                handle = run.get('handle')
                if handle and self.executor and self.executor.owns(context, handle) is True:
                    continue
                recovered = self.executor.recover(context) if self.executor else None
                if recovered:
                    self.store.update_run(run['id'], status='playing', handle=recovered,
                        deadline=run.get('deadline') or (run['created_at'] + context['duration'] if context['duration'] else None))
                else: self.store.update_run(run['id'], status='uncertain', error='Restart interrupted dispatch; playback requires reconciliation')
            except Exception:
                self.store.update_run(run['id'], status='uncertain', error='Playback ownership could not be reconciled')
        for command in self.store.commands('processing'):
            self.store.update_command(command['id'], state='uncertain',
                                      error='Restart interrupted command; inspect playback before retrying')

    def _disable_native(self, native_id):
        delegate = self.native_delegate
        if delegate is None: raise AlarmError('Native alarm delegate is unavailable; ownership is blocked')
        delegate.set_enabled(native_id, False)
        state = delegate.inspect(native_id)
        if not isinstance(state, dict) or state.get('enabled') is not False:
            raise AlarmError('Old native alarm disablement was not confirmed; new owner remains blocked')

    def _reconcile_native(self, record):
        handoff = record.get('handoff')
        if not handoff: return record
        delegate = self.native_delegate
        if delegate is None: raise AlarmError('Native execution is unavailable; alarm is blocked')
        try:
            if handoff['phase'] == 'disable-old':
                if handoff.get('old_native_id'): self._disable_native(handoff['old_native_id'])
                handoff['phase'] = 'install' if handoff['desired_owner'] == 'native' else 'finish'
                record = self.store.save({**record, 'handoff': handoff})
            if handoff['phase'] == 'install':
                reference = AlarmMediaRef.parse(record['media_ref'])
                provider = self.providers.provider(reference)
                reader = getattr(provider, 'native_program', None)
                program = reader(reference, record) if reader else None
                if not isinstance(program, NativeAlarmProgram) or not program.stable or not program.renderer_reachable:
                    raise AlarmError('Native alarms need a verified stable renderer-reachable source')
                if delegate.supports(record, program) is not True:
                    raise AlarmError('Native renderer does not support this alarm program')
                # The run ID is an idempotency key for install: delegates must
                # recover the same managed native ID after an interrupted call.
                native_id = delegate.install(record, program, enabled=False,
                                             idempotency_key='bridge-alarm:%d' % record['id'])
                if not isinstance(native_id, (str, int)) or str(native_id) == '':
                    raise AlarmError('Native delegate returned no managed alarm ID')
                handoff['new_native_id'], handoff['phase'] = str(native_id), 'enable'
                record = self.store.save({**record, 'handoff': handoff})
            if handoff['phase'] == 'enable':
                delegate.set_enabled(handoff['new_native_id'], handoff['desired_enabled'])
                state = delegate.inspect(handoff['new_native_id'])
                if not isinstance(state, dict) or state.get('enabled') is not handoff['desired_enabled']:
                    raise AlarmError('Native alarm enablement was not confirmed')
                handoff['phase'] = 'finish'
                record = self.store.save({**record, 'handoff': handoff})
            if handoff['phase'] == 'finish':
                record = self.store.save({**record, 'execution_owner': handoff['desired_owner'],
                    'enabled': handoff['desired_enabled'],
                    'owner_since': self.clock() + 0.000001,
                    'native_id': handoff.get('new_native_id') if handoff['desired_owner'] == 'native' else None,
                    'handoff': None})
            return record
        except Exception:
            self._notify('alarm-ownership-blocked', alarm_id=record['id'])
            raise

    def _execute(self, run, now):
        if self.executor is None:
            self.store.update_run(run['id'], status='failed', error='No alarm playback executor is configured')
            return
        if run['status'] not in ('claimed', 'retry'): return
        if now - run['occurrence'] > self.missed_grace:
            self.store.update_run(run['id'], status='missed', error='Alarm retry expired while bridge was unavailable')
            return
        run = self.store.update_run(run['id'], status='preparing', attempts=run['attempts'] + 1)
        try: context = self._context(run)
        except AlarmError as error:
            self.store.update_run(run['id'], status='failed', error=str(error))
            return
        fallback, request = False, None
        try:
            reference = AlarmMediaRef.parse(context['media_ref'])
            if reference.provider == 'chime': fallback = True
            else: request = self.providers.resolve(reference, context)
        except Exception as error:
            if run['attempts'] < self.max_attempts and now - run['occurrence'] + self.retry_delay <= self.missed_grace:
                self.store.update_run(run['id'], status='retry', retry_at=now + self.retry_delay,
                                      error='Source resolution failed: ' + type(error).__name__)
                return
            fallback = True
        # Resolve may perform I/O: an edit/delete while it was pending cancels it.
        current = self.store.run(run['id'])
        if current is None or current['status'] != 'preparing': return
        try:
            handle = self.executor.fallback(context) if fallback else self.executor.execute(request, context)
            handle = CallbackAlarmExecutor.handle(handle)
        except ExecutionUncertain:
            self.store.update_run(run['id'], status='uncertain', error='Playback acknowledgement is uncertain')
            self._notify('alarm-run-changed', run_id=run['id'])
            return
        except AlarmError as error:
            if not fallback:
                try:
                    handle, fallback = self.executor.fallback(context), True
                    handle = CallbackAlarmExecutor.handle(handle)
                except ExecutionUncertain:
                    self.store.update_run(run['id'], status='uncertain', error='Fallback acknowledgement is uncertain')
                    return
                except AlarmError as fallback_error:
                    self.store.update_run(run['id'], status='failed', error='Playback and fallback failed: ' + type(fallback_error).__name__)
                    return
                except Exception:
                    self.store.update_run(run['id'], status='uncertain', error='Fallback failed without a confirmed playback result')
                    return
            else:
                self.store.update_run(run['id'], status='failed', error='Fallback failed: ' + type(error).__name__)
                return
        except Exception:
            self.store.update_run(run['id'], status='uncertain', error='Executor failed without a confirmed playback result')
            return
        self.store.update_run(run['id'], status='playing', handle=handle, started_at=now,
                              fallback=fallback, deadline=now + context['duration'] if context['duration'] else None)
        self.store.increment('alarm_run_sequence')
        self.store.set_meta('last_alarm_room', context['room'])
        self._notify('alarm-run-changed', run_id=run['id'])

    def _stop_run(self, run, status='stopped'):
        if run['status'] in ('playing', 'uncertain'):
            if not self.executor or not run.get('handle'):
                raise AlarmError('Cannot stop playback whose ownership is uncertain')
            owned = self.executor.owns(self._context(run), run['handle'])
            if owned is None: raise AlarmError('Cannot confirm alarm playback ownership')
            if owned is True: self.executor.stop(self._context(run), run['handle'])
            else: status = 'superseded'
        self.store.update_run(run['id'], status=status, ended_at=self.clock(), resume_at=None)
        self._notify('alarm-run-changed', run_id=run['id'])

    def active(self, room):
        room = self.room_resolver(room)
        runs = []
        for run in self.store.runs(ACTIVE + ('snoozed',)):
            try: target = self.room_resolver(run['context']['room'])
            except AlarmError: continue  # Another room can disappear before its next tick.
            if target == room: runs.append(run)
        return runs

    def stop(self, room, run_id=None):
        runs = [r for r in self.active(room) if run_id is None or r['id'] == run_id]
        if not runs: return False
        for run in runs: self._stop_run(run)
        return True

    def snooze(self, room, seconds=9 * 60):
        seconds = _integer(seconds, 1, 86400, 'snooze duration')
        runs = [r for r in self.active(room) if r['status'] == 'playing']
        if not runs: raise AlarmError('No running alarm to snooze', 701)
        run = runs[-1]
        if self.executor.owns(self._context(run), run['handle']) is not True:
            raise AlarmError('Alarm no longer owns this playback')
        self.executor.stop(self._context(run), run['handle'])
        self.store.update_run(run['id'], status='snoozed', resume_at=self.clock() + seconds, deadline=None)
        self._notify('alarm-run-changed', run_id=run['id'])
        return self._public_run(self.store.run(run['id']))

    def set_sleep_timer(self, room, seconds):
        room = self.room_resolver(room)
        previous = next((r for r in self.store.sleep_records() if r['room'] == room), {})
        generation = int(previous.get('generation', 0)) + 1
        if seconds in (None, ''):
            record = {'room': room, 'expires': None, 'generation': generation, 'handle': None}
        else:
            seconds = _integer(seconds, 1, 7 * 86400, 'sleep duration')
            if not self.executor: raise AlarmError('No sleep timer playback executor')
            context = {'room': room, 'purpose': 'sleep', 'generation': str(uuid.uuid4())}
            record = {'room': room, 'expires': self.clock() + seconds, 'generation': generation,
                      'handle': self.executor.capture(context), 'context': context}
        self.store.save_sleep(room, record)
        self._notify('sleep-timer-changed', room=room)
        return self.sleep_timer(room)

    def sleep_timer(self, room):
        room = self.room_resolver(room)
        record = next((r for r in self.store.sleep_records() if r['room'] == room), {})
        expires = record.get('expires')
        remaining = '' if expires is None else hms(max(0, expires - self.clock()))
        return {'RemainingSleepTimerDuration': remaining,
                'CurrentSleepTimerGeneration': str(record.get('generation', 0))}

    def tick(self, now=None):
        if not self._tick_lock.acquire(blocking=False): return
        try:
            now = self.clock() if now is None else float(now)
            if self.executor is None:
                raise AlarmError('Only the player process with an executor may run the scheduler')
            self._commands(now)
            previous = float(self.store.get_meta('scheduler_cursor', now))
            if now < previous: previous = now - 1   # wall-clock adjustment; journal still deduplicates
            floor = max(previous, now - self.missed_grace)
            for alarm in self.store.records():
                if alarm.get('handoff'):
                    try: self._reconcile_native(alarm)
                    except Exception: pass
                    continue
                if not alarm['enabled'] or alarm['execution_owner'] not in ('bridge', 'native'): continue
                occurrence = next_occurrence(alarm, max(floor, alarm['created_at'], alarm.get('owner_since', 0)), inclusive=True)
                while occurrence is not None and occurrence <= now:
                    run = self.store.claim(alarm, occurrence, now)
                    if run:
                        if alarm['execution_owner'] == 'native':
                            self.store.update_run(run['id'], status='delegated', native_id=alarm.get('native_id'))
                        else: self._execute(run, now)
                    occurrence = next_occurrence(alarm, occurrence)
                # Record a missed occurrence without playing it hours/days late.
                if previous < floor:
                    missed = next_occurrence(alarm, max(previous, alarm['created_at']), inclusive=True)
                    if missed is not None and missed < floor:
                        run = self.store.claim(alarm, missed, now)
                        if run: self.store.update_run(run['id'], status='missed', error='Missed while bridge was unavailable')
            for run in self.store.runs(('retry', 'snoozed', 'playing', 'uncertain')):
                if run['status'] == 'retry' and run.get('retry_at', 0) <= now:
                    self._execute(run, now)
                elif run['status'] == 'snoozed' and run.get('resume_at', float('inf')) <= now:
                    try: alarm = self.store.get(run['alarm_id'])
                    except AlarmError:
                        self.store.update_run(run['id'], status='cancelled'); continue
                    if not alarm['enabled'] or alarm['execution_owner'] != 'bridge':
                        self.store.update_run(run['id'], status='cancelled'); continue
                    if now - run['resume_at'] > self.missed_grace:
                        self.store.update_run(run['id'], status='missed'); continue
                    child = self.store.claim(alarm, run['resume_at'], now, parent_id=run['id'])
                    self.store.update_run(run['id'], status='resumed')
                    if child: self._execute(child, now)
                elif run['status'] in ('playing', 'uncertain'):
                    try: context = self._context(run)
                    except AlarmError as error:
                        self.store.update_run(run['id'], status='failed', error=str(error)); continue
                    try:
                        handle = run.get('handle')
                        if not handle and self.executor:
                            handle = self.executor.recover(context)
                            if handle:
                                run = self.store.update_run(run['id'], handle=handle, status='playing',
                                    deadline=run.get('deadline') or (run['created_at'] + context['duration'] if context['duration'] else None))
                        owned = self.executor.owns(context, handle) if handle and self.executor else None
                        if owned is False:
                            self.store.update_run(run['id'], status='superseded')
                            self._notify('alarm-run-changed', run_id=run['id'])
                        elif owned is True and run.get('deadline') is not None and run['deadline'] <= now:
                            self._stop_run(run, 'expired')
                    except Exception as error:
                        self.log({'event': 'alarm-reconciliation-error', 'run_id': run['id'], 'error': type(error).__name__})
            for timer in self.store.sleep_records():
                if timer.get('expires') is not None and timer['expires'] <= now:
                    if self.executor and self.executor.owns(timer['context'], timer['handle']) is True:
                        self.executor.stop(timer['context'], timer['handle'])
                    self.store.save_sleep(timer['room'], {**timer, 'expires': None,
                                                         'generation': timer['generation'] + 1})
                    self._notify('sleep-timer-changed', room=timer['room'])
            self.store.set_meta('scheduler_cursor', now)
        finally: self._tick_lock.release()

    def run_now(self, alarm_id, *, occurrence=None):
        alarm = self.store.get(_integer(alarm_id, 1, 2**31-1, 'alarm ID'))
        if alarm['execution_owner'] != 'bridge': raise AlarmError('Alarm is delegated or ownership is blocked')
        now = self.clock()
        occurrence = now if occurrence is None else float(occurrence)
        run = self.store.claim(alarm, occurrence, now)
        if run: self._execute(run, now)
        return self._public_run(self.store.run(run['id']) if run else next(r for r in self.store.runs() if r['alarm_id'] == alarm['id'] and r['occurrence'] == occurrence))

    def set_timezone(self, name, *, apply_existing=False, notify=True):
        self.store.set_meta('timezone', str(_zone(name)))
        if apply_existing:
            for alarm in self.store.records(): self.save({**alarm, 'timezone': str(_zone(name))})
        self.store.set_meta('time_generation', self.store.get_meta('time_generation', 1) + 1)
        if notify: self._notify('alarm-clock-changed')

    def native_time_rule(self):
        from alarm_timezones import native_timezone_rule
        instant = self.clock()
        return native_timezone_rule(self.timezone, datetime.fromtimestamp(instant, UTC).year, instant=instant)

    def set_clock_offset(self, offset, *, time_server):
        # A household clock is independent of the host OS. Alarms share that
        # clock; duration timers retain their remaining real elapsed time when
        # a manual adjustment or Internet-time restoration changes the clock.
        with self._tick_lock, self._edit_lock, self.store.lock, self.store.db:
            previous = float(self.store.get_meta('clock_offset', 0))
            delta = offset - previous
            for timer in self.store.sleep_records():
                if timer.get('expires') is not None:
                    timer['expires'] += delta
                    self.store.db.execute('UPDATE sleep SET data=? WHERE room=?',
                                          (json.dumps(timer), timer['room']))
            for run in self.store.runs(ACTIVE + PENDING + ('snoozed',)):
                shifted = {key: run[key] + delta for key in ('deadline', 'resume_at', 'created_at')
                           if run.get(key) is not None}
                self.store.db.execute('UPDATE runs SET data=? WHERE id=?',
                                      (json.dumps({**run, **shifted}), run['id']))
            # Creation/ownership bounds prevent a newly-created alarm from
            # firing before it existed; keep them valid across backwards edits.
            for alarm in self.store.records():
                for key in ('created_at', 'owner_since'):
                    if key in alarm: alarm[key] += delta
                self.store.db.execute('UPDATE alarms SET data=? WHERE id=?', (json.dumps(alarm), alarm['id']))
            values = {'clock_offset': offset, 'time_server': time_server,
                      'scheduler_cursor': self.system_clock() + offset,
                      'time_generation': self.store.get_meta('time_generation', 1) + 1}
            self.store.db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                      [(key, json.dumps(value)) for key, value in values.items()])
        self._notify('alarm-clock-changed')

    def native_list(self):
        from xml.etree.ElementTree import Element, SubElement, tostring
        root = Element('Alarms')
        for alarm in self.store.records():
            try: room = self.native_room(alarm['room'])
            except AlarmError:
                self.log({'event': 'alarm-room-unavailable', 'alarm_id': alarm['id']})
                continue
            reference = AlarmMediaRef.parse(alarm['media_ref'])
            uri = 'x-rincon-buzzer:0' if reference.provider == 'chime' else reference.uri()
            item = SubElement(root, 'Alarm', {'ID': str(alarm['id']), 'StartTime': alarm['time'],
                'Duration': '' if alarm['duration'] is None else hms(alarm['duration']),
                'Recurrence': alarm['recurrence'], 'Enabled': '1' if alarm['enabled'] else '0',
                'RoomUUID': room, 'ProgramURI': uri,
                'ProgramMetaData': self._title_metadata(alarm['title']), 'PlayMode': alarm['play_mode'],
                'Volume': str(alarm['volume']), 'IncludeLinkedZones': '1' if alarm['include_linked'] else '0'})
        return {'CurrentAlarmList': tostring(root, encoding='unicode'),
                'CurrentAlarmListVersion': str(self.store.version)}

    @staticmethod
    def _title_metadata(title):
        return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><item id="alarm" parentID="0" restricted="true">'
                '<dc:title>%s</dc:title></item></DIDL-Lite>' % html.escape(title))

    def action(self, service, action, args, room_key=None):
        """Native SOAP out-arguments, or None for another existing action route."""
        if service == 'AlarmClock':
            if action == 'ListAlarms': return self.native_list()
            if action in ('CreateAlarm', 'UpdateAlarm'):
                room = self.room_resolver(args.get('RoomUUID') or room_key or '')
                reference = self.providers.from_native(args.get('ProgramURI', ''), args.get('ProgramMetaData', ''))
                values = {'room': room, 'time': args.get('StartLocalTime'),
                    'recurrence': args.get('Recurrence'), 'enabled': args.get('Enabled'),
                    'include_linked': args.get('IncludeLinkedZones'), 'volume': args.get('Volume'),
                    'duration': args.get('Duration'), 'play_mode': args.get('PlayMode', 'NORMAL'),
                    'media_ref': reference.as_dict()}
                if action == 'UpdateAlarm': values['id'] = args.get('ID')
                saved = self.save(values)
                return {'AssignedID': str(saved['id'])} if action == 'CreateAlarm' else {}
            if action == 'DestroyAlarm': self.delete(args.get('ID')); return {}
            if action == 'GetTimeNow':
                now = datetime.fromtimestamp(self.clock(), UTC)
                return {'CurrentUTCTime': now.strftime('%Y-%m-%d %H:%M:%S'),
                        'CurrentLocalTime': now.astimezone(_zone(self.timezone)).strftime('%Y-%m-%d %H:%M:%S'),
                        'CurrentTimeZone': self.native_time_rule(),
                        'CurrentTimeGeneration': str(self.store.get_meta('time_generation', 1))}
            if action == 'GetHouseholdTimeAtStamp':
                # This is an opaque Sonos synchronization stamp, not a wall
                # clock timestamp. Never fall through to the peer's UTC-now
                # placeholder and claim that the stamp was converted.
                raise AlarmError('Household time synchronization stamps are unsupported', 501)
            if action == 'GetTimeServer':
                return {'CurrentTimeServer': self.store.get_meta('time_server', NATIVE_TIME_SERVER)}
            if action == 'SetTimeServer':
                server = args.get('DesiredTimeServer')
                if not isinstance(server, str) or len(server) > 1024 or any(ord(c) < 32 for c in server):
                    raise AlarmError('Invalid time server', 402)
                # Internet time follows the Pi's synchronized system clock.
                # The native controller sends its built-in pool list or ''.
                self.set_clock_offset(0 if server else float(self.store.get_meta('clock_offset', 0)),
                                      time_server=server)
                return {}
            if action == 'SetTimeNow':
                from alarm_timezones import native_rule_zone
                value, rule = args.get('DesiredTime'), args.get('TimeZoneForDesiredTime')
                try: local = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError): raise AlarmError('Invalid manual time', 402) from None
                if isinstance(rule, str) and re.fullmatch(r'[0-9a-fA-F]{28}', rule):
                    # A fixed current-offset fallback cannot describe another
                    # date in a complex timezone year. The controller echoes
                    # our current rule, so retain its actual household zone.
                    zone = _zone(self.timezone) if rule.lower() == self.native_time_rule() else native_rule_zone(rule, local)
                else: zone = _zone(rule or self.timezone)
                desired = local.replace(tzinfo=zone).timestamp()
                self.set_clock_offset(desired - self.system_clock(), time_server='')
                return {}
            if action == 'GetFormat':
                return {'CurrentTimeFormat': self.store.get_meta('time_format', '24H'),
                        'CurrentDateFormat': self.store.get_meta('date_format', 'YMD')}
            if action == 'SetFormat':
                time_format, date_format = args.get('DesiredTimeFormat'), args.get('DesiredDateFormat')
                if time_format not in ('12H', '24H') or date_format not in ('YMD', 'DMY', 'MDY'):
                    raise AlarmError('Unsupported clock format', 402)
                self.store.set_meta('time_format', time_format); self.store.set_meta('date_format', date_format)
                self._notify('alarm-clock-changed'); return {}
            if action in ('GetTimeZone', 'GetTimeZoneAndRule'):
                from alarm_timezones import native_timezone_index
                result = {'Index': str(self.store.get_meta('timezone_index', native_timezone_index(self.timezone))),
                          'AutoAdjustDst': '1' if self.store.get_meta('timezone_dst', True) else '0'}
                if action == 'GetTimeZoneAndRule': result['CurrentTimeZone'] = self.native_time_rule()
                return result
            if action == 'SetTimeZone':
                if not self.timezone_from_index: raise AlarmError('Native timezone index mapping is unavailable')
                index = _integer(args.get('Index'), 0, 10000, 'timezone index')
                adjust = _boolean(args.get('AutoAdjustDst'))
                name = self.timezone_from_index(index, adjust)
                with self._edit_lock:
                    self.store.set_meta('timezone_index', index)
                    self.store.set_meta('timezone_dst', adjust)
                    self.set_timezone(name, apply_existing=True, notify=False)
                self._notify('alarm-clock-changed'); return {}
            if action == 'GetTimeZoneRule':
                if not self.timezone_from_index: raise AlarmError('Native timezone index mapping is unavailable')
                index = _integer(args.get('Index'), 0, 10000, 'timezone index')
                from alarm_timezones import native_timezone_rule
                instant = self.clock()
                return {'TimeZone': native_timezone_rule(self.timezone_from_index(index, True),
                        datetime.fromtimestamp(instant, UTC).year, instant=instant)}
        if service == 'AVTransport':
            if action == 'GetRunningAlarmProperties':
                try: running = [r for r in self.active(room_key) if r['status'] in ('playing', 'snoozed')]
                except AlarmError: running = []
                run = running[-1] if running else None
                return {'AlarmID': str(run['alarm_id']) if run else '0',
                        'GroupID': str(self.room_resolver(room_key)) if run else '',
                        'LoggedStartTime': datetime.fromtimestamp(run.get('started_at', run['created_at']), UTC).strftime('%Y-%m-%d %H:%M:%S') if run else ''}
            if action == 'SnoozeAlarm':
                self.snooze(room_key, _duration(args.get('Duration') or '0:09:00', allow_empty=False)); return {}
            if action == 'RunAlarm':
                self.run_now(args.get('AlarmID')); return {}
            if action == 'Stop' and self.active(room_key): self.stop(room_key); return {}
            if self.handle_sleep and self.bridge_sleep(room_key) and action == 'ConfigureSleepTimer':
                self.set_sleep_timer(room_key, _duration(args.get('NewSleepTimerDuration'))); return {}
            if self.handle_sleep and self.bridge_sleep(room_key) and action == 'GetRemainingSleepTimerDuration': return self.sleep_timer(room_key)
        return None

    def event_values(self, service, room_key=None):
        # Publishers can already be preparing an event when shutdown begins.
        # Keep the complete snapshot and connection close under the same lock.
        with self.store.lock:
            if self._closed: return {}
            return self._event_values(service, room_key)

    def _event_values(self, service, room_key=None):
        if service == 'AlarmClock':
            return {'AlarmListVersion': str(self.store.version),
                    'TimeGeneration': str(self.store.get_meta('time_generation', 1)),
                    'TimeZone': self.native_time_rule(), 'TimeServer': self.store.get_meta('time_server', NATIVE_TIME_SERVER),
                    'TimeFormat': self.store.get_meta('time_format', '24H'),
                    'DateFormat': self.store.get_meta('date_format', 'YMD')}
        if service == 'ZoneGroupTopology':
            source = self.store.get_meta('last_alarm_room', room_key)
            try: identity, boot = self.topology_identity(source)
            except AlarmError:
                try: identity, boot = self.topology_identity(room_key)
                except AlarmError: return {}  # An endpoint may have just disappeared.
            # Observed Sonos wire form is UDN:BootSeq:Sequence, not a bare count.
            # https://github.com/home-assistant/core/issues/88486
            return {'AlarmRunSequence': '%s:%s:%s' % (identity, boot, self.store.get_meta('alarm_run_sequence', 0))}
        if service == 'AVTransport':
            try: runs = [r for r in self.active(room_key) if r['status'] in ('playing', 'snoozed')]
            except AlarmError: return {}  # Informational devices are not alarm targets.
            run = runs[-1] if runs else None
            values = {'AlarmRunning': '1' if run else '0',
                      'SnoozeRunning': '1' if run and run['status'] == 'snoozed' else '0',
                      'AlarmIDRunning': str(run['alarm_id']) if run else '0',
                      'AlarmLoggedStartTime': datetime.fromtimestamp(run.get('started_at', run['created_at']), UTC).strftime('%Y-%m-%d %H:%M:%S') if run else ''}
            if self.handle_sleep and self.bridge_sleep(room_key):
                values['SleepTimerGeneration'] = self.sleep_timer(room_key)['CurrentSleepTimerGeneration']
            return values
        return {}

    def start(self): self.scheduler.start()
    def close(self):
        if self._closed: return
        self.scheduler.close()
        with self.store.lock:
            if self._closed: return
            self._closed = True
            self.store.close()
