"""Occurrence-aware native Spotify actions for the custom CR100 interface.

Browse only records what this controller actually saw. Explicit UI events fetch
and validate a complete finite collection before the first queue mutation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
import time
from urllib.parse import unquote

from backends import BackendError
from sonos_upnp import UPnPFault
from spotify_menu import FeatureLocked, LOCK_MESSAGE, scoped_id
from ui_events import UIEventError, UIResult

MAX_COLLECTION = 1000
OCCURRENCE_TTL = 300
MAX_CONTEXTS = 256
PLAN_TIMEOUT = 20
NATIVE_HEADER = re.compile(r'^[0-9a-fA-F]{8}(?=bm:|spotify:)')
COLLECTION = re.compile(r'spotify:(album|playlist):([A-Za-z0-9]{22})\Z')


@dataclass(frozen=True)
class OccurrencePlan:
    tracks: tuple
    index: int
    key: tuple
    epoch: int

    @property
    def songs(self): return self.tracks[self.index:]


def native_id(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise UIEventError('invalid_parameters', 'Invalid Spotify item identity')
    for _ in range(3):
        if '%' not in value: break
        if re.search(r'%(?![0-9a-fA-F]{2})', value):
            raise UIEventError('invalid_parameters', 'Invalid Spotify item encoding')
        try: value = unquote(value, encoding='utf-8', errors='strict')
        except UnicodeError:
            raise UIEventError('invalid_parameters', 'Invalid Spotify item encoding') from None
    return NATIVE_HEADER.sub('', value, count=1)


class SpotifyUIActions:
    def __init__(self, service, *, clock=time.monotonic):
        self.service, self.clock = service, clock
        self.contexts = OrderedDict()
        self.lock = threading.RLock()
        self.queue_locks = {}
        self.epoch = 0

    def handlers(self):
        prefix = 'spotify.%d.' % self.service.service_id
        return {prefix + 'play_from': self.play_from, prefix + 'queue_from': self.queue_from}

    def identity(self):
        service = self.service
        address = getattr(service.request, 'address', None)
        identity = service.controller_resolver(address) if service.controller_resolver and address else None
        return str(identity) if identity else 'ip:' + str(address)

    def browse_snapshot(self, parent):
        """Optional read-only provenance; unavailable metadata never breaks browse."""
        match = COLLECTION.fullmatch(parent)
        if not match or match[1] != 'playlist': return None
        try:
            return self._snapshot(*match.groups())
        except UIEventError:
            return None

    def record(self, parent, index, total, nodes, snapshot=None, requested_count=None):
        if not COLLECTION.fullmatch(parent) or type(total) is not int or total < 0 \
                or type(index) is not int or not 0 <= index <= total or len(nodes) > total - index:
            return
        entry = getattr(self.service.request, 'entry', None)
        parent_id = scoped_id(entry, parent) if entry is not None else parent
        policy = self.service._entry(entry) if entry is not None else None
        policy = (policy['id'], policy['kind'], policy['target']) if policy else None
        key = (self.identity(), parent_id)
        tracks = {index + n: node['id'] for n, node in enumerate(nodes)} if total <= MAX_COLLECTION else {}
        with self.lock:
            hit = self.contexts.get(key)
            # Native clicktrackcontext asks for row zero alone to prefetch list
            # metadata. It did not replace the displayed list. Preserve only a
            # matching, live finite browse that already showed additional rows;
            # this read must neither authorize new positions nor renew its TTL.
            if index == 0 and requested_count == 1 and len(nodes) == 1 and 1 < total <= MAX_COLLECTION \
                    and hit is not None and hit['expires'] > self.clock() \
                    and hit['total'] == total and hit['snapshot'] == snapshot \
                    and hit['version'] == self.service.catalog_version and hit['policy'] == policy \
                    and len(hit['observed']) > 1 and hit['observed'].get(0) == tracks[0]:
                return
            if hit is None or index == 0 or hit['expires'] <= self.clock() or hit['total'] != total \
                    or hit['snapshot'] != snapshot or hit['version'] != self.service.catalog_version \
                    or hit['policy'] != policy \
                    or any(pos in hit['observed'] and hit['observed'][pos] != uri for pos, uri in tracks.items()):
                hit = {'total': total, 'observed': {}, 'expires': self.clock() + OCCURRENCE_TTL,
                       'snapshot': snapshot, 'version': self.service.catalog_version,
                       'policy': policy, 'epoch': self.epoch}
                self.epoch += 1
                self.contexts[key] = hit
            hit['observed'].update(tracks)
            hit['expires'] = self.clock() + OCCURRENCE_TTL
            self.contexts.move_to_end(key)
            while len(self.contexts) > MAX_CONTEXTS:
                self.contexts.popitem(last=False)

    def _scope(self, ident):
        ident = native_id(ident)
        if self.service.menu_settings is None:
            return None, ident
        try:
            entry, raw = self.service._resolve_scope(ident)
        except FeatureLocked:
            raise UIEventError('feature_locked', LOCK_MESSAGE) from None
        except (LookupError, ValueError):
            raise UIEventError('unavailable_context', 'This menu route changed. Reopen Spotify from Music.') from None
        return entry['id'], raw

    def _check_scope(self):
        try:
            self.service._check_scope()
        except FeatureLocked:
            raise UIEventError('feature_locked', LOCK_MESSAGE) from None
        except (LookupError, ValueError):
            raise UIEventError('unavailable_context', 'This menu route changed. Reopen Spotify from Music.') from None

    def _fresh(self, path, params=None):
        self._check_scope()
        deadline = getattr(self.service.request, 'ui_deadline', None)
        if deadline is not None and self.clock() >= deadline:
            raise UIEventError('loading_timeout', 'This collection took too long to load. Try a shorter selection.')
        try:
            result = self.service.client.get(path, params=params)
        except Exception:
            raise UIEventError('provider_unavailable', 'Spotify could not load this collection. Try again.') from None
        self._check_scope()
        if deadline is not None and self.clock() >= deadline:
            raise UIEventError('loading_timeout', 'This collection took too long to load. Try again.')
        if not isinstance(result, dict):
            raise UIEventError('provider_unavailable', 'Spotify returned an unreadable collection')
        return result

    def _snapshot(self, kind, ident):
        if kind != 'playlist': return None
        value = self._fresh('/playlists/' + ident, {'fields': 'snapshot_id'}).get('snapshot_id')
        if not isinstance(value, str) or not value or len(value) > 256:
            raise UIEventError('unverifiable_collection', 'Spotify could not verify this playlist. Reopen it and try again.')
        return value

    def plan(self, context, params):
        if set(params) != {'parent', 'index', 'track'} or not re.fullmatch(r'0|[1-9][0-9]{0,5}', params['index']):
            raise UIEventError('invalid_parameters', 'Choose a track in an album or playlist')
        parent_id, track_id = native_id(params['parent']), native_id(params['track'])
        with self.service.request_context(context.controller_ip):
            try:
                entry, parent = self._scope(parent_id)
                track_entry, track = self._scope(track_id)
                if entry != track_entry:
                    raise UIEventError('invalid_context', 'Track and collection menu contexts differ')
            except (LookupError, ValueError) as error:
                if isinstance(error, UIEventError): raise
                raise UIEventError('unavailable_context', str(error)) from None
            match = COLLECTION.fullmatch(parent)
            if not match:
                raise UIEventError('unsupported_collection', 'Open an album or playlist to use Play from here')
            key, index = (self.identity(), parent_id), int(params['index'])
            with self.lock:
                hit = self.contexts.get(key)
                if hit is None or hit['expires'] <= self.clock():
                    raise UIEventError('expired_context', 'Reopen this track list before using its actions')
                hit = {**hit, 'observed': dict(hit['observed'])}
            if hit['version'] != self.service.catalog_version:
                raise UIEventError('expired_context', 'Reopen this track list after its catalogue changed')
            if hit['total'] > MAX_COLLECTION:
                raise UIEventError('unsupported_collection', 'This action supports complete collections of up to 1,000 tracks')
            observed = hit['observed']
            if observed.get(index) != track:
                raise UIEventError('unissued_occurrence', 'This track occurrence was not shown to this controller')
            kind, ident = match.groups()
            with self.service._scope(entry):
                snapshot = self._snapshot(kind, ident)
                if kind == 'playlist' and (not hit['snapshot'] or snapshot != hit['snapshot']):
                    raise UIEventError('collection_changed', 'This playlist changed or could not be verified. Reopen it.')
                path = '/%ss/%s/%s' % (kind, ident, 'tracks' if kind == 'album' else 'items')
                nodes, offset, total = [], 0, None
                for _ in range(MAX_COLLECTION // 50):
                    page = self._fresh(path, {'offset': offset, 'limit': 50})
                    rows, page_total = page.get('items'), page.get('total')
                    if type(page_total) is not int or not 0 <= page_total <= MAX_COLLECTION or not isinstance(rows, list):
                        raise UIEventError('unsupported_collection', 'This action supports complete collections of up to 1,000 tracks')
                    if total is None: total = page_total
                    if total != page_total or len(rows) != min(50, total - offset):
                        raise UIEventError('collection_changed', 'This collection changed. Reopen the track list.')
                    for row in rows:
                        item = (row.get('item') or row.get('track')) if kind == 'playlist' and isinstance(row, dict) else row
                        try: node = self.service._node(item, 'track')
                        except Exception:
                            # Catalogue errors are intentionally mapped to a short,
                            # actionable message without leaking provider details.
                            raise UIEventError('unavailable_track', 'This collection contains an unavailable or unsupported track') from None
                        if not node.get('canPlay'):
                            raise UIEventError('unavailable_track', 'This collection contains an unplayable track')
                        nodes.append(node)
                    offset += len(rows)
                    if offset == total: break
                if total is None or len(nodes) != total or total != hit['total'] or index >= total:
                    raise UIEventError('collection_changed', 'This collection changed. Reopen the track list.')
                if any(pos >= total or nodes[pos]['id'] != uri for pos, uri in observed.items()):
                    raise UIEventError('collection_changed', 'The displayed track order changed. Reopen the track list.')
                if self._snapshot(kind, ident) != snapshot:
                    raise UIEventError('collection_changed', 'This playlist changed while loading. Try again.')
                self._check_scope()
            return OccurrencePlan(tuple(node['id'] for node in nodes), index, key, hit['epoch'])

    def _validate_plan(self, plan):
        with self.lock:
            current = self.contexts.get(plan.key)
            if current is None or current['epoch'] != plan.epoch or current['expires'] <= self.clock() \
                    or current['version'] != self.service.catalog_version:
                raise UIEventError('expired_context', 'This browse context changed. Reopen the track list.')
            if any(pos >= len(plan.tracks) or plan.tracks[pos] != uri for pos, uri in current['observed'].items()):
                raise UIEventError('collection_changed', 'The displayed track order changed. Reopen the track list.')

    def _target(self, context, replace):
        room = context.room
        if room is None or not room.available or getattr(room.backend, 'name', '') != 'sonos':
            raise UIEventError('unsupported_room', 'Play from here currently requires a direct Sonos room')
        coordinator = room.backend.coordinator_of(room.key)
        room = room.backend.room(coordinator)
        if not room.available:
            raise UIEventError('unsupported_room', 'The Sonos group coordinator is unavailable')
        for operation in ('provider_enqueue', 'queue_clear', 'queue_play') if replace else ('provider_enqueue',):
            if not room.supports(operation):
                raise UIEventError('unsupported_room', 'This room does not expose the required queue controls')
        return room

    @staticmethod
    def _enqueue_plan(room, prepared):
        """Try another account encoding only after an explicit rejection.

        A timeout/unreadable acknowledgement can follow a successful enqueue;
        retrying an alternate encoding in that case can duplicate the occurrence.
        """
        failure = None
        for item in prepared:
            try:
                return room.add_to_queue(item.uri, item.metadata, False)
            except BackendError as error:
                failure = error
                fault = error.__cause__
                if not isinstance(fault, UPnPFault) or fault.code != 800:
                    raise
        if failure is not None: raise failure
        raise BackendError('Empty validated playback plan')

    def _execute(self, context, params, replace):
        room = self._target(context, replace)
        initial_target = tuple(room.ref)
        deadline = self.clock() + PLAN_TIMEOUT
        with self.service.request_context(context.controller_ip):
            previous = getattr(self.service.request, 'ui_deadline', None)
            self.service.request.ui_deadline = deadline
            try:
                plan = self.plan(context, params)
            finally:
                self.service.request.ui_deadline = previous
        # Validate all native encodings before clearing or changing the queue.
        songs = plan.songs
        try:
            plans = [(song, room.backend.playback_adapters.prepare(song, 'music')) for song in songs]
        except (BackendError, ValueError):
            raise UIEventError('unsupported_encoding', 'This music cannot be queued on the selected speaker') from None
        key = tuple(room.ref)
        with self.lock:
            if key not in self.queue_locks:
                # One lock per active configured room, rather than per request.
                if len(self.queue_locks) >= 128:
                    raise UIEventError('busy', 'Too many active queue targets')
                self.queue_locks[key] = threading.RLock()
            queue_lock = self.queue_locks[key]
        with queue_lock:
            if self.clock() >= deadline:
                raise UIEventError('loading_timeout', 'This collection took too long to load. Try again.')
            if tuple(self._target(context, replace).ref) != initial_target:
                raise UIEventError('room_changed', 'This room changed groups. Select it again before applying.')
            # Recheck menu policy after network reads and immediately before I/O.
            with self.service.request_context(context.controller_ip):
                self._scope(params['parent'])
                self._validate_plan(plan)
            policy = self.service.playback_policy
            if replace:
                for observer in context.household.playback_policies:
                    callback = getattr(observer, 'before_action', None)
                    if callable(callback): callback(room, 'AVTransport', 'SetAVTransportURI', {})
            added = 0
            try:
                if replace: room.clear_queue()
                for song, prepared in plans:
                    if tuple(self._target(context, replace).ref) != initial_target:
                        raise BackendError('Room changed groups during queue update')
                    with self.service.request_context(context.controller_ip):
                        self._scope(params['parent'])
                        self._validate_plan(plan)
                    # Execute the exact fully validated plan. Re-preparing here
                    # could fail only after Play from here cleared the old queue.
                    result = self._enqueue_plan(room, prepared)
                    if not isinstance(result, dict) or str(result.get('NumTracksAdded')) != '1':
                        raise BackendError('The renderer did not confirm one queued track')
                    added += 1
                    if not replace and policy is not None:
                        callback = getattr(policy, 'queue_added', None)
                        if callable(callback): callback(room, song, result, as_next=False)
                if replace:
                    if tuple(self._target(context, replace).ref) != initial_target:
                        raise BackendError('Room changed groups before playback')
                    room.play_track(1)
                    coordinator = context.household.coordinator.get(context.player.config.udn, context.player.config.udn)
                    context.household.source_action_committed(coordinator, 'AVTransport', 'SetAVTransportURI', {})
                    if policy is not None:
                        callback = getattr(policy, 'after_action', None)
                        if callable(callback): callback(room, 'AVTransport', 'Play', {})
            except Exception:
                raise UIEventError('partial_queue', 'Confirmed %d of %d additions. Playback or the last request may also have changed; check the queue before retrying.' % (added, len(songs))) from None
        return UIResult(('Playing' if replace else 'Added') + ' %d tracks from this position' % added,
                        {'tracks': added, 'operation': 'play_from' if replace else 'queue_from'})

    def play_from(self, context, params): return self._execute(context, params, True)
    def queue_from(self, context, params): return self._execute(context, params, False)
