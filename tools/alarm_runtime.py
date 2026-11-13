"""Production bindings for global alarms, Radio and actual household backends.

``attach_alarms`` constructs but does not start the scheduler. The player process
owns start/close; a separate web process uses RadioAlarmProvider with an executor-
free AlarmService and its persisted command queue. No live Spotify adapter is
installed here: another provider can resolve queue requests through this executor.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid
import wave

from alarms import (AlarmError, AlarmMediaRef, AlarmProviderRegistry, AlarmService,
                    AlarmStore, CallbackAlarmExecutor, ExecutionUncertain,
                    PlaybackRequest)
from backends import BackendError
from radio_playback import RADIO_URI, radio_id


class RadioAlarmProvider:
    def __init__(self, catalog, store): self.catalog, self.store = catalog, store

    def describe(self, reference):
        identity = reference.content_id
        if not re.fullmatch(r'(?:station:s\d+|episode:t\d+|custom:[\w-]+)', identity):
            raise AlarmError('Choose a Radio station, custom station, or episode', 402)
        if identity.startswith('custom:'):
            item = self.store.custom(identity)
            if item is None: raise AlarmError('Custom alarm station no longer exists', 701)
            return {'title': item['title']}
        saved = next((x for x in self.store.favorites() + self.store.history() if x['id'] == identity), None)
        if saved: return {'title': saved['title']}
        item = self.catalog.metadata(identity)
        return {'title': item.title if hasattr(item, 'title') else item['title']}

    def sources(self):
        seen, result = set(), []
        for item in self.store.favorites() + self.store.custom_stations() + self.store.history():
            identity = item['id']
            if identity in seen or not identity.startswith(('station:', 'custom:', 'episode:')): continue
            seen.add(identity); result.append({'content_id': identity})
        return result

    def resolve(self, reference, context):
        self.describe(reference)
        item = self.catalog.metadata(reference.content_id)
        kind = item.kind if hasattr(item, 'kind') else item['kind']
        # The runtime calls RadioPlayback.play with this canonical ID. That path
        # resolves current compatible streams and retains the Radio art session.
        return PlaybackRequest('track' if kind == 'episode' else 'live',
                               reference.content_id,
                               title=item.title if hasattr(item, 'title') else item['title'],
                               preserve_queue=kind != 'episode')

    def from_native(self, uri, metadata=''):
        identity = radio_id(uri)
        if identity is None and uri.startswith('x-cr100-radio:'):
            identity = unquote(uri[len('x-cr100-radio:'):])
        if identity is None and re.fullmatch(r'(?:station:s\d+|episode:t\d+|custom:[\w-]+)', uri):
            identity = uri
        if identity is None:
            match = re.match(r'x-sonosapi-(?:stream|radio):(s\d+)(?:\?|$)', uri)
            if match: identity = 'station:' + match[1]
        return None if identity is None else AlarmMediaRef('radio', identity)


def make_radio_alarm_provider(catalog, store): return RadioAlarmProvider(catalog, store)


def chime_wav(seconds=60, sample_rate=8000):
    """A locally served audible fallback, without external media dependencies."""
    seconds = max(1, min(int(seconds), 120))
    # One second of an enveloped major triad, repeated; a minute is <1 MiB PCM.
    samples = bytearray()
    for index in range(sample_rate):
        position = index / sample_rate
        envelope = max(0.0, min(1.0, position / .02, (.8 - position) / .25))
        value = sum(math.sin(2 * math.pi * freq * position) for freq in (523.25, 659.25, 783.99))
        samples.extend(struct.pack('<h', int(7000 * envelope * value / 3)))
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(sample_rate)
        target.writeframes(bytes(samples) * seconds)
    return output.getvalue()


def _fingerprint(value): return hashlib.sha256(str(value or '').encode()).hexdigest()


class HouseholdAlarmExecutor:
    def __init__(self, house, store, config=None, *, clock=time.time):
        self.house, self.store, self.config, self.clock = house, store, config or {}, clock
        self.lock = threading.RLock()

    def udn(self, key):
        key = str(key or '')
        if key in self.house.by_udn: return key
        for player in self.house.players:
            room = player.state.backend
            aliases = {str(getattr(player, 'component_key', ''))}
            if room:
                backend, identity = room.ref
                aliases.update((identity, '%s:%s' % (backend, identity)))
            if key in aliases: return player.config.udn
        raise AlarmError('Alarm room is not currently configured: %s' % key, 701)

    def topology_identity(self, key):
        """Event identity covers About endpoints without granting playback access."""
        key = str(key or '')
        # Identity refresh can replace informational endpoints concurrently.
        # Copy its dictionary rather than taking the household lock while an
        # alarm event already holds the alarm store lock.
        informational = getattr(self.house, 'about_players', {}).copy()
        players = tuple(self.house.players) + tuple(informational.values())
        player = next((player for player in players if player.config.udn == key), None)
        if player is None:
            player = self.house.by_udn.get(self.udn(key))
        if player is None: raise AlarmError('Component is no longer advertised', 701)
        return player.config.udn, getattr(player.config, 'boot_sequence', 1)

    def player(self, key, coordinator=True):
        udn = self.udn(key)
        if coordinator: udn = self.house.coordinator.get(udn, udn)
        player = self.house.by_udn.get(udn)
        if player is None or player.state.backend is None or not player.state.backend.available:
            raise AlarmError('Alarm speaker is unavailable')
        return player

    def _counter(self, udn): return self.store.get_meta('playback-generation:' + udn, 0)

    def before_action(self, room_key, service, action, args, *, coordinator=True):
        """Revoke timer ownership when a manual source/queue selection commits.

        Volume/mute, normal clock polling and alarm Snooze do not revoke ownership.
        A new selection of the same URI still revokes an old alarm's timer.
        """
        if service != 'AVTransport': return
        selection = action in ('SetAVTransportURI', 'Next', 'Previous', 'RemoveAllTracksFromQueue') or (
            action == 'Seek' and args.get('Unit') == 'TRACK_NR')
        if not selection: return
        udn = self.udn(room_key)
        if coordinator: udn = self.house.coordinator.get(udn, udn)
        with self.lock: self.store.set_meta('playback-generation:' + udn, self._counter(udn) + 1)

    @staticmethod
    def source(room):
        avt = getattr(room, 'avt', None)
        if isinstance(avt, dict): return str(avt.get('AVTransportURI') or avt.get('CurrentTrackURI') or '')
        attributes = getattr(room, 'attributes', None)
        if isinstance(attributes, dict): return str(attributes.get('media_content_id') or '')
        return str(room.track_reference() or '')

    def _target(self, context):
        selected = self.player(context['room'], coordinator=False)
        if not context.get('include_linked'):
            members = self.house.members(selected.config.udn)
            if len(members) > 1:
                room = selected.state.backend
                room.require('group')
                room.unjoin()
                # The command selected a standalone physical room; HA/UPnP event
                # caches may lag its acknowledgement. Don't route through the
                # previous virtual coordinator while those events catch up.
                lock = getattr(self.house, 'lock', self.lock)
                with lock: self.house.coordinator[selected.config.udn] = selected.config.udn
            return selected
        return self.player(selected.config.udn)

    def _volume(self, player, context):
        players = self.house.members(player.config.udn) if context.get('include_linked') else [player]
        for member in players:
            room = member.state.backend
            if room is None or not room.available: raise AlarmError('A linked alarm speaker is unavailable')
            room.require('volume')
            # send_* confirms real backend commands instead of the UI's delayed
            # coalescing sender, whose asynchronous errors cannot confirm volume.
            room.send_volume(context['volume'])
            if room.supports('mute'): room.send_mute(False)

    def _remember(self, handle, player, context, *, before, expected, radio_generation=None):
        record = {'room': player.config.udn, 'created_at': self.clock(),
                  'run_id': context.get('run_id'), 'counter': self._counter(player.config.udn),
                  'source': _fingerprint(expected), 'previous': _fingerprint(before),
                  'radio_generation': radio_generation}
        self.store.set_meta('runtime-handle:' + handle, record)
        return handle

    def execute(self, request, context):
        try:
            player = self._target(context)
            self._volume(player, context)
            room = player.state.backend
            before = self.source(room)
            reference = AlarmMediaRef.parse(context['media_ref'])
            if reference.provider == 'radio':
                generation = self.house.radio_playback.play(player.config.udn, reference.content_id)
                session = self.house.radio_playback.session(player.config.udn)
                if session is None or generation != session.generation:
                    raise ExecutionUncertain('Radio playback did not establish its ownership session')
                expected = session.stream
            else:
                self.house.radio_playback.cancel(player.config.udn)
                generation = None
                if request.kind == 'queue':
                    room.require('provider_play'); room.play_provider(request.uri, 'playlist')
                elif request.kind == 'live': room.require('radio_play'); room.play_radio(request.uri, request.metadata)
                else:
                    from content import Node, TRACK_CLASS
                    room.play_item(Node(reference.content_id, '0', request.title, request.uri, 'track', False,
                                        TRACK_CLASS, uri=request.uri))
                expected = self.source(room)
                if not expected or expected == before: expected = request.uri
            return self._remember(context['run_id'], player, context, before=before, expected=expected,
                                  radio_generation=generation)
        except AlarmError: raise
        except ExecutionUncertain: raise
        except BackendError as error:
            # Backends often wrap transport timeouts. Their acknowledgement is
            # ambiguous: don't start a second source/chime on top automatically.
            raise ExecutionUncertain('Alarm renderer acknowledgement failed') from error

    def owns(self, context, handle):
        record = self.store.get_meta('runtime-handle:' + handle)
        if not record: return None
        try:
            player = self.player(context['room'])
            if player.config.udn != record['room'] or self._counter(record['room']) != record['counter']: return False
            session = self.house.radio_playback.session(player.config.udn)
            if record.get('radio_generation') and session is not None and session.generation != record['radio_generation']:
                reference = context.get('media_ref') or {}
                if getattr(session, 'retries', 0) > 0 and session.item_id == reference.get('content_id'):
                    # A bounded bridge-controlled reconnect is still the same
                    # alarm selection; manual selections already revoked counter.
                    record.update(radio_generation=session.generation,
                                  source=_fingerprint(session.stream), created_at=self.clock())
                    self.store.set_meta('runtime-handle:' + handle, record)
                else: return False
            current = _fingerprint(self.source(player.state.backend))
            if current == record['source']: return True
            if self.clock() - record['created_at'] < 8 and current == record['previous']: return True
            return False
        except AlarmError:
            return None

    def recover(self, context):
        handle = context.get('run_id')
        if handle and self.owns(context, handle) is True: return handle
        fallback = 'chime:' + str(handle)
        if handle and self.owns(context, fallback) is True: return fallback
        return None

    def stop(self, context, handle):
        if self.owns(context, handle) is not True:
            raise AlarmError('Alarm no longer owns this playback')
        player = self.player(context['room'])
        # Owned buzzer playback may omit Stop from its advertised actions.
        try: player.state.backend.stop()
        except BackendError as error:
            raise AlarmError('Could not stop alarm playback: ' + str(error)) from error
        self.house.radio_playback.cancel(player.config.udn)
        return True

    def capture(self, context):
        player = self.player(context['room'])
        source = self.source(player.state.backend)
        if not source: raise AlarmError('No active source for a sleep timer')
        handle = 'sleep:' + uuid.uuid4().hex
        session = self.house.radio_playback.session(player.config.udn)
        return self._remember(handle, player, context, before=source, expected=source,
                              radio_generation=session.generation if session else None)

    def chime_url(self, room):
        settings = self.config.get('alarms') or {}
        url = settings.get('chime_url')
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise AlarmError('Configure a renderer-reachable HTTP chime URL')
            return url
        binding = getattr(room, 'binding', None)
        target = urlsplit(getattr(binding, 'native_url', '') or '').hostname
        if not target:
            ha = getattr(getattr(room, 'backend', None), 'ha', None)
            target = urlsplit(getattr(ha, 'url', '') or getattr(ha, 'base', '') or '').hostname
        target = target or '192.0.2.1'
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9)); host = probe.getsockname()[0]
        except OSError as error: raise AlarmError('No LAN-reachable chime address is configured') from error
        if host.startswith('127.') or host in ('0.0.0.0', '192.168.69.1'):
            raise AlarmError('Chime requires a speaker-reachable LAN address')
        port = int((self.config.get('webui') or {}).get('port', 8080))
        return 'http://%s:%d/alarm-chime.wav' % (host, port)

    def fallback(self, context):
        try:
            player = self._target(context); room = player.state.backend
            self._volume(player, context)
            before = self.source(room)
            self.house.radio_playback.cancel(player.config.udn)
            binding = getattr(room, 'binding', None)
            native = getattr(binding, 'is_sonos', False) or getattr(room.backend, 'name', '') == 'sonos'
            source = 'x-rincon-buzzer:0' if native else self.chime_url(room)
            room.require('radio_play'); room.play_radio(source, '')
            return self._remember('chime:' + context['run_id'], player, context, before=before, expected=source)
        except AlarmError: raise
        except BackendError as error: raise ExecutionUncertain('Fallback renderer acknowledgement failed') from error

    def bridge_sleep(self, room_key):
        try: return not self.player(room_key).state.backend.supports('sleep_timer')
        except AlarmError: return False

    def callbacks(self):
        return CallbackAlarmExecutor(self.execute, self.owns, self.stop, fallback=self.fallback,
                                     recover=self.recover, capture=self.capture)


def attach_alarms(house, config_path, catalog, store):
    """Attach production callbacks; caller starts/closes returned AlarmService."""
    from radio_management import state_paths
    from alarm_timezones import native_timezone, native_timezone_index
    config_path = Path(config_path)
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    settings = config.get('alarms') or {}
    registry = AlarmProviderRegistry(); registry.register('radio', RadioAlarmProvider(catalog, store))
    alarm_store = AlarmStore(state_paths(config_path)[1])
    runtime = HouseholdAlarmExecutor(house, alarm_store, config)
    log = getattr(house, 'log', lambda value: None)
    def changed(event):
        for player in house.players:
            if getattr(player, 'http_thread', None):
                player.publish_changed('AlarmClock' if event['event'] in ('alarms-changed', 'alarm-clock-changed') else 'AVTransport')
                if event['event'] == 'alarm-run-changed': player.publish_changed('ZoneGroupTopology')
    zone = settings.get('timezone') or config.get('timezone') or os.environ.get('TZ') or 'UTC'
    service = AlarmService(alarm_store, providers=registry, executor=runtime.callbacks(), timezone=zone,
                           room_resolver=runtime.udn, native_room=runtime.udn, log=log,
                           on_change=changed, handle_sleep=True, bridge_sleep=runtime.bridge_sleep,
                           timezone_from_index=native_timezone,
                           topology_identity=runtime.topology_identity)
    if alarm_store.get_meta('timezone_index') is None:
        alarm_store.set_meta('timezone_index', native_timezone_index(service.timezone))
        alarm_store.set_meta('timezone_dst', service.timezone not in ('UTC', 'Etc/UTC', 'GMT'))
    service.runtime = runtime
    house.alarm_runtime = runtime
    house.alarms = service
    return service
