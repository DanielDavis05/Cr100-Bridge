"""Read-only device browsing and guarded, queued absolute HA actions.

The catalogue is pure; this owner supplies issued views, event coalescing and
the only explicit device-write path. HA event callbacks only enqueue work.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from content import ContentSource, Node, PAGE_LIMIT
from ha_client import HARequestUncertain
from smart_devices import SmartCatalogue, area_container, room_container
from ui_events import UIEventError, UIResult

EXTERNAL_INTERVAL = 2.0
CONFIRM_TIMEOUT = 3.0
MAX_VIEWS = 512
MAX_PENDING = 64
ACTION_TIMEOUT = 10.0
DOMAINS = {'light', 'switch', 'input_boolean', 'fan', 'scene', 'script'}


@dataclass
class View:
    key: tuple
    udn: str
    address: str | None
    container: str
    rooms: tuple
    rows: tuple
    signature: tuple
    nonce: str = field(default_factory=lambda: secrets.token_hex(6))
    epoch: int = 1
    token: str = ''
    painted: tuple = ()


@dataclass
class Job:
    context: object
    params: dict
    targets: tuple
    feedback: str
    started: float


class SmartDeviceService:
    def __init__(self, household, hub, config=None, *, clock=time.monotonic):
        self.household, self.hub, self.client = household, hub, hub.client
        self.config, self.clock = config or {}, clock
        self.enabled = self.config.get('smart_devices', True) is True
        self.session = secrets.token_hex(8)
        self.condition = threading.Condition(threading.RLock())
        self.views, self.tokens = OrderedDict(), {}
        self.updates, self.last_publish, self.trailing = {}, {}, {}
        self.pending, self.confirmations = [], []
        self.feedback_modes = OrderedDict()
        self.state_versions = {}
        self.uncertain = []  # (expanded targets, actual HA result acknowledgement)
        self.changed_entities, self.refresh_all = set(), True
        self.own_until, self.entity_index = {}, {}
        self.closed, self.revision = False, 0
        self.catalogue = SmartCatalogue(hub.snapshot(), dict(self.client.states))
        self.connected = self.client.connected.is_set()
        self._index()
        self._state_listener = self.state_changed
        self._hub_listener = self.hub_changed
        self.client.listeners.append(self._state_listener)
        self.hub.listeners.append(self._hub_listener)
        self.content = ContentSource()
        for action in ('power', 'level', 'activate'):
            household.ui_events.register('devices.' + action, self.action, scope='room', mutates=True)
        self.thread = threading.Thread(target=self.run, name='smart-devices', daemon=True)
        self.thread.start()

    def available(self):
        return self.enabled and self.client.connected.is_set() and bool(self.catalogue.entities)

    def _rooms(self, udn, address):
        # The bridge's topology, rather than controller-supplied members, is
        # authoritative. Refresh its cached backend membership before issuing or
        # checking a view, including the final check immediately before a call.
        self.household.sync_groups()
        entries = self.config.get('rooms') or []
        import backends
        configured = {backends.room_ref(entry): entry for entry in entries if backends.room_ref(entry)}
        rooms = []
        for player in self.household.members(udn, address):
            room = player.state.backend
            ref = room.ref if room else None
            entry = configured.get(ref, {})
            identity = room.backend.identity(room.key) if room else None
            spec = {'backend': ref[0] if ref else '', 'key': ref[1] if ref else player.config.udn,
                    'name': player.config.room, 'identity': identity or {}}
            if 'devices_area' in entry: spec['devices_area'] = entry['devices_area']
            spec['area_id'] = self.catalogue.resolve_area(spec)
            rooms.append(spec)
        return tuple(sorted(rooms, key=lambda room: (room['name'].casefold(), room['key'])))

    @staticmethod
    def _room_signature(rooms):
        return tuple((r['backend'], r['key'], r['name'], r.get('area_id')) for r in rooms)

    def _token(self, view):
        return self.session + '-' + view.nonce + '-' + format(view.epoch, 'x')

    def _visible(self, rows):
        if not self.enabled or not self.client.connected.is_set(): return ('disconnected',)
        return tuple((r.row_id, r.title, r.upnp_class, r.folder) for r in rows)

    def _view(self, state, container):
        address = getattr(state.http_source, 'address', None)
        rooms = self._rooms(state.config.udn, address)
        key = (state.config.udn, address, self._room_signature(rooms), container)
        view = self.views.get(key)
        try: rows = tuple(self.catalogue.rows(container, rooms))
        except KeyError:
            if self.enabled and self.client.connected.is_set(): raise
            rows = ()
        signature = self.catalogue.structural_signature(rows)
        if view is None:
            view = View(key, state.config.udn, address, container, rooms, rows, signature)
            view.token, view.painted = self._token(view), self._visible(rows)
            self.views[key] = view
            self.tokens[view.token] = view
            while len(self.views) > MAX_VIEWS:
                _, old = self.views.popitem(last=False)
                self.tokens.pop(old.token, None)
        else:
            self._replace_view(view, rows, signature)
            self.views.move_to_end(key)
        return view

    def _replace_view(self, view, rows, signature):
        structural = signature != view.signature
        if structural:
            self.tokens.pop(view.token, None)
            view.epoch += 1
            view.token = self._token(view)
            self.tokens[view.token] = view
        visible = self._visible(rows)
        changed = structural or visible != view.painted
        view.rows, view.signature, view.painted = rows, signature, visible
        return changed, structural

    def _index(self):
        # Include registry candidates without a state, so their first state can
        # add a row. Every unrelated state_changed costs just this dict lookup.
        self.entity_index = {
            entity_id: True for entity_id, e in self.catalogue.registry_entities.items()
            if entity_id.split('.', 1)[0] in DOMAINS
            and not any(e.get(k) for k in ('disabled_by', 'hidden_by', 'entity_category'))
            and self.catalogue.resolve_area({'backend': 'ha', 'key': entity_id}) is not None}
        self.entity_index.update({entity_id: True for entity_id in self.catalogue.referenced_entities})

    def state_changed(self, entity_id, state):
        if self.entity_index.get(entity_id) is None: return
        with self.condition:
            self.state_versions[entity_id] = self.state_versions.get(entity_id, 0) + 1
            self.changed_entities.add(entity_id)
            self.condition.notify()

    def hub_changed(self):
        with self.condition:
            self.refresh_all = True
            self.condition.notify()

    def _refresh(self):
        previous = self.catalogue
        states = dict(self.client.states)
        now = self.clock()
        # A mixed callback batch can include our action and an unrelated
        # external update. Only a changed entity still owned by our action can
        # bypass the cap, and only in a view that actually depends on it.
        own_changes = {entity for entity, due in self.own_until.items()
                       if due >= now and previous.states.get(entity) != states.get(entity)}
        connected = self.client.connected.is_set()
        availability = connected != self.connected
        self.connected = connected
        self.catalogue = SmartCatalogue(self.hub.snapshot(), states, previous=previous)
        self._index()
        for key, view in list(self.views.items()):
            rooms = self._rooms(view.udn, view.address)
            if self._room_signature(rooms) != self._room_signature(view.rooms):
                # Membership changes revoke every issued context, even when a
                # particular area's row set happens to stay identical.
                self.tokens.pop(view.token, None)
                self.views.pop(key, None)
                self._publish(view.udn, view.container, bypass=True)
                continue
            try: rows = tuple(self.catalogue.rows(view.container, rooms))
            except KeyError: rows = ()
            row_availability = tuple((r.row_id, r.available) for r in rows) != \
                tuple((r.row_id, r.available) for r in view.rows)
            old_rows = view.rows
            changed, structural = self._replace_view(view, rows, self.catalogue.structural_signature(rows))
            if changed:
                own = bool(own_changes) and any(
                    own_changes.intersection(self._expanded_targets(row, row.targets, catalogue))
                    for catalogue, candidates in ((previous, old_rows), (self.catalogue, rows))
                    for row in candidates)
                self._publish(view.udn, view.container,
                              bypass=own or availability or row_availability or structural)
        self.revision += 1

    def _publish(self, udn, container, *, bypass=False):
        key, now = (udn, container), self.clock()
        if not bypass and now - self.last_publish.get(key, -EXTERNAL_INTERVAL) < EXTERNAL_INTERVAL:
            self.trailing[key] = self.last_publish[key] + EXTERNAL_INTERVAL
            return
        self.trailing.pop(key, None)
        self.last_publish[key] = now
        self.updates[key] = self.updates.get(key, 1) + 1
        player = self.household.by_udn.get(udn)
        if player: player.publish_changed('ContentDirectory')

    def event_ids(self, udn):
        with self.condition:
            return ','.join('%s,%d' % (container, version) for (room, container), version
                            in sorted(self.updates.items()) if room == udn)

    def browse(self, state, args):
        with self.condition:
            self._refresh()
            object_id = args['ObjectID']
            flag = args.get('BrowseFlag', 'BrowseDirectChildren')
            if flag == 'BrowseMetadata' and not object_id.startswith('SD:'):
                candidates = [v for v in reversed(self.views.values()) if v.udn == state.config.udn
                              and v.address == getattr(state.http_source, 'address', None)
                              and any(r.row_id == object_id for r in v.rows)]
                if not candidates: raise self.household.zp.UPnPError(701, 'No such device row')
                view = candidates[0]
                rows = tuple(r for r in view.rows if r.row_id == object_id)
            else:
                if self.enabled and self.client.connected.is_set() \
                        and object_id not in self.catalogue.containers(self._rooms(state.config.udn,
                            getattr(state.http_source, 'address', None))):
                    # Synthetic All Lights ids are leaves and may be requested
                    # as metadata just like the raw entity ids.
                    candidates = [v for v in reversed(self.views.values()) if v.udn == state.config.udn
                                  and v.address == getattr(state.http_source, 'address', None)
                                  and any(r.row_id == object_id for r in v.rows)]
                    if flag != 'BrowseMetadata' or not candidates:
                        raise self.household.zp.UPnPError(701, 'No such device container')
                    view = candidates[0]
                    rows = tuple(r for r in view.rows if r.row_id == object_id)
                else:
                    view = self._view(state, object_id)
                    rows = view.rows
                    if flag == 'BrowseMetadata':
                        title, parent = self._container_metadata(view)
                        node = Node(object_id, parent, title, None, None, True,
                                    'object.container.sdView.' + view.token, playable=False, child_count=len(rows))
                        return self._response(state, view, [node], 1)
            if not self.enabled or not self.client.connected.is_set():
                nodes = [Node('SD:unavailable', view.container, 'Home Assistant unavailable', None,
                              None, False, 'object.item.sdUnavailable.inert.none.' + view.token, playable=False)]
            else:
                nodes = [Node(r.row_id, view.container, r.title, None, None, r.folder,
                              ('object.container.sdView' if r.folder else r.upnp_class) + '.' + view.token,
                              playable=False) for r in rows]
            total = len(nodes)
            if flag != 'BrowseMetadata':
                try: start, count = int(args.get('StartingIndex') or 0), int(args.get('RequestedCount') or 0)
                except ValueError: raise self.household.zp.UPnPError(402, 'Invalid page') from None
                if start < 0 or count < 0: raise self.household.zp.UPnPError(402, 'Invalid page')
                nodes = nodes[start:start + min(count or PAGE_LIMIT, PAGE_LIMIT)]
            return self._response(state, view, nodes, total)

    def _response(self, state, view, nodes, total):
        # Also carry the token on an empty DIDL response. Actionable rows repeat
        # it in their cached class, the native firmware's readable transport.
        didl = self.content.didl(nodes, state.config.udn).replace('>', ' r:sdView="' + view.token + '">', 1)
        return {'Result': didl, 'NumberReturned': str(len(nodes)),
                'TotalMatches': str(total), 'UpdateID': str(self.updates.get((view.udn, view.container), 1))}

    def _container_metadata(self, view):
        for parent in reversed(self.views.values()):
            if parent.udn != view.udn or parent.address != view.address: continue
            row = next((r for r in parent.rows if r.folder and r.row_id == view.container), None)
            if row: return row.name, parent.container
        container = view.container
        if container == 'SD:': return 'Devices', '0'
        if container == 'SD:other': return 'Other Rooms', 'SD:'
        if container.startswith('SD:r:'):
            room = next((r for r in view.rooms if room_container(r) == container), {})
            return room.get('name', 'Room'), 'SD:'
        if container.startswith('SD:floor:'):
            floor = self.catalogue.floors.get(unquote(container[len('SD:floor:'):]), {})
            return floor.get('name') or 'No Floor', 'SD:other'
        if container.startswith('SD:a:'):
            area_id, _, kind = container[len('SD:a:'):].partition(':')
            area_id = unquote(area_id)
            if kind: return ('Scenes' if kind == 'scenes' else 'Individual Lights'), area_container(area_id)
            return self.catalogue.areas.get(area_id, {}).get('name') or area_id, 'SD:other'
        return 'Devices', '0'

    def handles_metadata(self, object_id):
        if object_id not in self.entity_index: return False
        with self.condition:
            return any(any(row.row_id == object_id for row in view.rows) for view in self.views.values())

    def _subscriptions(self, player, address):
        now = self.clock()
        with player.state.lock:
            return [sid for sid, sub in player.state.subscriptions.items()
                    if sub['service'] == 'AVTransport' and sub['expires'] > now
                    and (sub.get('source') or urlsplit(sub.get('callback', '')).hostname) == address]

    def feedback(self, player, address):
        return 'notify' if self._subscriptions(player, address) else 'deferred'

    def feedback_for(self, controller_id, request_id):
        with self.condition:
            hit = self.feedback_modes.get((controller_id, request_id))
            return hit[1] if hit and hit[0] > self.clock() else 'deferred'

    def _error_feedback(self, context, feedback, error):
        if feedback != 'notify': return
        sids = self._subscriptions(context.player, context.controller_ip)
        if not sids: return  # Retained failure remains available to deferred clients.
        # Overrides never alter the room's global AVTransport state. Actual
        # serialization/delivery finishes before its ordered OK reset begins.
        def body(status, message):
            event = ET.Element('Event', {'xmlns': 'urn:schemas-upnp-org:metadata-1-0/AVT/'})
            instance = ET.SubElement(event, 'InstanceID', {'val': '0'})
            ET.SubElement(instance, 'TransportStatus', {'val': status})
            ET.SubElement(instance, 'TransportErrorDescription', {'val': message})
            outer = ET.Element('e:propertyset', {'xmlns:e': 'urn:schemas-upnp-org:event-1-0'})
            ET.SubElement(ET.SubElement(outer, 'e:property'), 'LastChange').text = ET.tostring(event, encoding='unicode')
            return ET.tostring(outer, encoding='utf-8')
        context.player.notify(sids[-1], body('ERROR_DEVICES', str(error)))
        context.player.notify(sids[-1], body('OK', ''))

    def _validate(self, context, params):
        if self.closed or not self.enabled or not self.client.connected.is_set():
            raise UIEventError('unavailable', 'Home Assistant unavailable')
        self._refresh()
        view = self.tokens.get(params.get('view'))
        if view is None or view.udn != context.room_udn or view.address != context.controller_ip:
            raise UIEventError('list_changed', 'List changed. Select again.')
        rooms = self._rooms(context.room_udn, context.controller_ip)
        if self._room_signature(rooms) != self._room_signature(view.rooms):
            raise UIEventError('list_changed', 'List changed. Select again.')
        row = next((row for row in view.rows if row.row_id == params.get('id')), None)
        if row is None or row.folder:
            raise UIEventError('list_changed', 'List changed. Select again.')
        if not row.available: raise UIEventError('unavailable', 'Device unavailable')
        action = context.action.rsplit('.', 1)[-1]
        allowed = {'view', 'id', 'feedback'} | ({'desired'} if action == 'power' else {'value'} if action == 'level' else set())
        if set(params) - {'feedback'} != allowed - {'feedback'} or params.get('feedback', 'auto') not in ('auto', 'notify', 'deferred'):
            raise UIEventError('invalid_parameters', 'Invalid device action')
        if action == 'power':
            if row.domain in ('scene', 'script') or params['desired'] not in ('on', 'off'):
                raise UIEventError('unsupported_action', 'Device cannot switch power')
        elif action == 'level':
            value = params['value']
            if not row.level_kind or not value.isascii() or not value.isdigit() or len(value) > 3 or not 0 <= int(value) <= 100:
                raise UIEventError('unsupported_action', 'Device has no level control')
        elif action != 'activate' or row.domain not in ('scene', 'script'):
            raise UIEventError('unsupported_action', 'Device cannot be started')
        self.uncertain = [(targets, receipt) for targets, receipt in self.uncertain if not receipt.is_set()]
        affected = self._expanded_targets(row, row.targets)
        if any(affected.intersection(targets) for targets, _ in self.uncertain):
            raise UIEventError('busy', 'Device busy. Try again.')
        return row

    def _expanded_targets(self, row, targets, catalogue=None):
        catalogue = catalogue or self.catalogue
        affected, remaining = set(targets), list(row.member_closure or row.members)
        while remaining:
            member = remaining.pop()
            if member in affected: continue
            affected.add(member)
            remaining.extend(catalogue.member_graph.get(member, ()))
        return affected

    def action(self, context, params):
        started = self.clock()
        # Mode is bridge-owned and frozen before validation, including rejected
        # actions. The firmware queries this small flag after SetString, then
        # reads UIResult only when the frozen mode says deferred.
        feedback = 'deferred' if params.get('feedback') == 'deferred' else self.feedback(context.player, context.controller_ip)
        with self.condition:
            now = self.clock()
            for key, (expiry, _) in list(self.feedback_modes.items()):
                if expiry <= now: self.feedback_modes.pop(key, None)
            self.feedback_modes[(context.controller_id, context.request_id)] = (now + 600, feedback)
            while len(self.feedback_modes) > 256: self.feedback_modes.popitem(last=False)
        try:
            with self.condition:
                row = self._validate(context, params)
                if len(self.pending) >= MAX_PENDING:
                    raise UIEventError('busy', 'Device busy. Try again.')
                job = Job(context, dict(params), tuple(row.targets), feedback, started)
                superseded = []
                if context.action != 'devices.activate':
                    for old in self.pending:
                        if old.context.action == 'devices.activate': continue
                        old.targets = tuple(target for target in old.targets if target not in job.targets)
                        if not old.targets: superseded.append(old)
                    for old in superseded: self.pending.remove(old)
                self.pending.append(job)
                self.condition.notify()
            for old in superseded: self._finish(old, outcome='superseded')
            return UIResult('', {'pending': True})
        except UIEventError as error:
            # Rejections are also caller-scoped and have a retained error result.
            threading.Thread(target=self._error_feedback, args=(context, feedback, error), daemon=True).start()
            self._log(context, 'rejected', started, params.get('id', ''))
            raise

    def _log(self, context, outcome, started, row_id=''):
        self.household.log({'event': 'smart-device-action', 'action': context.action,
                            'room': context.room_udn, 'row_id': row_id,
                            'outcome': outcome, 'duration_ms': round((self.clock() - started) * 1000)})

    def _finish(self, job, error=None, outcome='ok'):
        self.household.ui_events.complete(job.context, UIResult('Started' if job.context.action == 'devices.activate' else '',
                                                               {'pending': False}), error)
        self.household.log({'event': 'smart-device-action', 'action': job.context.action,
                            'room': job.context.room_udn, 'row_id': job.params.get('id', ''),
                            'outcome': 'error' if error else outcome,
                            'duration_ms': round((self.clock() - job.started) * 1000)})
        if error:
            with self.condition: self._affected(job.targets)
            self._error_feedback(job.context, job.feedback, error)

    def _execute(self, job):
        row = None
        try:
            with self.condition:
                row = self._validate(job.context, job.params)
                remaining_budget = ACTION_TIMEOUT - (self.clock() - job.started)
                if remaining_budget <= 0:
                    raise UIEventError('busy', 'Device busy. Try again.')
                # Another queued intent may have replaced only overlapping
                # members. The surviving subset still belongs to this issued row.
                targets = tuple(t for t in row.targets if t in job.targets)
                versions = tuple(self.state_versions.get(t, 0) for t in targets)
                affected = self._expanded_targets(row, targets)
                for target in affected: self.own_until[target] = self.clock() + 10 + CONFIRM_TIMEOUT
            if not targets: return self._finish(job, outcome='superseded')
            action = job.context.action.rsplit('.', 1)[-1]
            data = {}
            if action == 'power': service = 'turn_' + job.params['desired']
            elif action == 'activate': service = 'turn_on'
            elif row.domain == 'fan': service, data = 'set_percentage', {'percentage': int(job.params['value'])}
            elif int(job.params['value']) == 0: service = 'turn_off'
            else: service, data = 'turn_on', {'brightness_pct': int(job.params['value'])}
            # This single worker is the only sender: overlapping group/member
            # targets can never have concurrent calls, across controllers too.
            self.client.call_service(row.domain, service, list(targets), data, timeout=remaining_budget)
            with self.condition:
                for target in affected: self.own_until[target] = self.clock() + CONFIRM_TIMEOUT
                if all(self.state_versions.get(t, 0) == version for t, version in zip(targets, versions)):
                    self.confirmations.append((self.clock() + CONFIRM_TIMEOUT, targets, versions))
                self.condition.notify()
            self._finish(job)
        except UIEventError as error:
            self._finish(job, error)
        except HARequestUncertain as error:
            with self.condition:
                self.uncertain.append((affected, error.acknowledged))
            message = 'Light not responding' if row and row.domain == 'light' else 'Device not responding'
            self._finish(job, UIEventError('device_failed', message))
        except Exception:
            message = 'Light not responding' if row and row.domain == 'light' else 'Device not responding'
            self._finish(job, UIEventError('device_failed', message))

    def _affected(self, targets):
        targets = set(targets)
        for view in list(self.views.values()):
            if any(targets.intersection(row.targets) for row in view.rows):
                self._publish(view.udn, view.container, bypass=True)

    def run(self):
        while True:
            with self.condition:
                if self.closed: return
                changed, all_changed = self.changed_entities, self.refresh_all
                self.changed_entities, self.refresh_all = set(), False
                if changed or all_changed:
                    self._refresh()
                now = self.clock()
                for key, due in list(self.trailing.items()):
                    if due <= now: self._publish(*key, bypass=True)
                for due, targets, versions in self.confirmations[:]:
                    if any(self.state_versions.get(t, 0) != version for t, version in zip(targets, versions)):
                        self.confirmations.remove((due, targets, versions))
                        continue
                    if due <= now:
                        self.confirmations.remove((due, targets, versions))
                        self._affected(targets)
                self.own_until = {e: due for e, due in self.own_until.items() if due > now}
                job = self.pending.pop(0) if self.pending else None
                if job is None:
                    self.condition.wait(.1)
                    continue
            self._execute(job)

    def status(self):
        with self.condition:
            rooms = []
            for player in self.household.players:
                specs = self._rooms(player.config.udn, None)
                ref = player.state.backend.ref if player.state.backend else ('', player.config.udn)
                spec = next((r for r in specs if (r['backend'], r['key']) == ref), None)
                area = self.catalogue.areas.get(spec.get('area_id'), {}) if spec else {}
                rooms.append({'room': player.config.room, 'area': area.get('name') or None})
            return {'configured': True, 'enabled': self.enabled, 'connected': self.client.connected.is_set(),
                    'areas': len(self.catalogue.catalogue_areas), 'devices': len(self.catalogue.entities), 'rooms': rooms}

    def close(self):
        with self.condition:
            self.closed = True
            queued, self.pending = self.pending, []
            self.condition.notify_all()
        if self._state_listener in self.client.listeners: self.client.listeners.remove(self._state_listener)
        if self._hub_listener in self.hub.listeners: self.hub.listeners.remove(self._hub_listener)
        for job in queued: self._finish(job, UIEventError('unavailable', 'Home Assistant unavailable'))
        self.thread.join(12)
