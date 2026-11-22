"""A small fake Home Assistant for offline tests: WebSocket API + entity_picture proxy.

Implements auth, state_changed, states, media/sonos services, light/switch/boolean/
fan/scene/script services, browse_media, entity/device/area/floor registries, and
the picture proxy. smart_device_fixture() supplies an invented multi-area home;
default construction retains the media-only fixture behavior. All sockets bind
loopback and run on a background thread.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import io
import json
import threading

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.exceptions import ConnectionClosed

TOKEN = 'test-token'


def picture_bytes(size=(640, 640)):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', size, (200, 40, 40)).save(out, 'JPEG', progressive=True, quality=90)
    return out.getvalue()


def media(title, content_id, content_type, media_class, can_play=True, children=None, thumbnail=None):
    """A browse_media node; children=None means a leaf."""
    return {'title': title, 'media_content_id': content_id, 'media_content_type': content_type,
            'media_class': media_class, 'can_play': can_play, 'can_expand': children is not None,
            'thumbnail': thumbnail, 'children': children}


def sonos_state(entity_id, name, state='idle', **attributes):
    base = {'friendly_name': name, 'supported_features': 8321599, 'volume_level': 0.2,
            'is_volume_muted': False, 'shuffle': False, 'repeat': 'off', 'group_members': [entity_id]}
    base.update(attributes)
    return {'entity_id': entity_id, 'state': state, 'attributes': base}


def smart_device_fixture():
    """Invented two-room home for device integration tests and emulator runs.

    Usage: ``fake = FakeHA(**smart_device_fixture())``. No device has a network
    address: native Sonos matching uses synthetic registry identifiers only.
    """
    states = [sonos_state('media_player.lantern', 'Lantern'),
              sonos_state('media_player.workshop', 'Workshop')]
    entities, devices = [], []
    for area, uuid in (('lantern', 'RINCON_00000000001101400'),
                       ('workshop', 'RINCON_00000000001201400')):
        device = 'speaker-' + area
        devices.append({'id': device, 'area_id': area, 'identifiers': [['sonos', uuid]],
                        'model': 'Sonos One', 'manufacturer': 'Sonos', 'disabled_by': None})
        entities.append({'entity_id': 'media_player.' + area, 'platform': 'sonos',
                         'device_id': device, 'unique_id': uuid, 'area_id': None,
                         'config_entry_id': 'synthetic-sonos', 'name': None,
                         'disabled_by': None, 'hidden_by': None, 'entity_category': None})

    def add(entity, name, state, area='lantern', platform='test', category=None,
            disabled=None, hidden=None, registry_name=None, **attributes):
        device = 'device-' + entity
        devices.append({'id': device, 'area_id': area, 'identifiers': [[platform, entity]],
                        'disabled_by': None})
        entities.append({'entity_id': entity, 'platform': platform, 'device_id': device,
                         'area_id': None, 'name': registry_name, 'original_name': name,
                         'disabled_by': disabled, 'hidden_by': hidden, 'entity_category': category,
                         'config_entry_id': 'synthetic-' + platform, 'unique_id': entity})
        states.append({'entity_id': entity, 'state': state,
                       'attributes': {'friendly_name': name, **attributes}})

    dim = {'supported_color_modes': ['brightness'], 'color_mode': 'brightness', 'supported_features': 0}
    add('light.lantern_ceiling', 'Lantern Ceiling', 'on', brightness=153, **dim)
    add('light.lantern_counter_left', 'Lantern Counter Left', 'on', brightness=102, **dim)
    add('light.lantern_counter_right', 'Lantern Counter Right', 'off', brightness=204, **dim)
    add('light.lantern_counter', 'Lantern Counter', 'on', platform='hue', brightness=102,
        entity_id=['light.lantern_counter_left', 'light.lantern_counter_right'], **dim)
    add('light.lantern_all', 'Lantern All', 'on', platform='group', brightness=128,
        entity_id=['light.lantern_ceiling', 'light.lantern_counter_left', 'light.lantern_counter_right'], **dim)
    add('switch.lantern_plug', 'Lantern Coffee Plug', 'on')
    add('input_boolean.lantern_guest_mode', 'Lantern Guest Mode', 'off')
    add('fan.lantern_extractor', 'Lantern Extractor', 'on', percentage=40, supported_features=1)
    add('scene.lantern_evening', 'Lantern Evening', '2026-01-01T00:00:00+00:00')
    add('script.lantern_welcome', 'Lantern Welcome', 'off')
    add('light.workshop_bench', 'Workshop Bench', 'on', area='workshop', brightness=255, **dim)
    add('light.workshop_overhead', 'Workshop Overhead', 'off', area='workshop', brightness=128, **dim)
    add('light.landing_lamp', 'Landing Lamp', 'on', area='landing', brightness=64, **dim)
    add('climate.lantern_thermostat', 'Lantern Thermostat', 'heat',
        hvac_modes=['off', 'heat', 'cool', 'heat_cool', 'fan_only'], hvac_action='heating',
        current_temperature=71, current_humidity=45, temperature=72,
        target_temp_low=68, target_temp_high=72, min_temp=50, max_temp=90,
        target_temp_step=1, temperature_unit='°F',
        fan_modes=['auto', 'low', 'medium', 'high', 'on'], fan_mode='auto',
        supported_features=1 | 2 | 8 | 128 | 256)
    add('climate.lantern_radiator', 'Lantern Radiator', 'heat',
        hvac_modes=['off', 'heat'], hvac_action='idle',
        current_temperature=21.0, temperature=20.5, min_temp=10, max_temp=32,
        target_temp_step=0.5, temperature_unit='°C',
        supported_features=1 | 128 | 256)
    add('climate.workshop_cooler', 'Workshop Cooler', 'cool', area='workshop',
        hvac_modes=['off', 'cool', 'fan_only'], hvac_action='cooling',
        current_temperature=75, temperature=72, min_temp=60, max_temp=85,
        target_temp_step=1, temperature_unit='°F',
        fan_modes=['auto', 'low', 'high'], fan_mode='auto',
        supported_features=1 | 8 | 128 | 256)
    add('climate.landing_hvac', 'Landing HVAC', 'heat_cool', area='landing',
        hvac_modes=['off', 'heat', 'cool', 'heat_cool', 'auto'], hvac_action='idle',
        current_temperature=70, current_humidity=40, temperature=70,
        target_temp_low=68, target_temp_high=74, min_temp=50, max_temp=90,
        target_temp_step=1, temperature_unit='°F',
        supported_features=1 | 2 | 128 | 256)
    add('climate.landing_offline', 'Landing Offline Thermostat', 'unavailable', area='landing',
        hvac_modes=['off', 'heat'], min_temp=50, max_temp=90,
        target_temp_step=1, temperature_unit='°F', supported_features=1)
    add('switch.lantern_loudness', 'Lantern Loudness', 'on', platform='sonos', category='config')
    add('switch.lantern_crossfade', 'Lantern Crossfade', 'off', platform='sonos', category='config')
    add('light.lantern_hidden', 'Lantern Hidden', 'on', hidden='user', brightness=255, **dim)
    add('light.lantern_disabled', 'Lantern Disabled', 'on', disabled='integration', brightness=255, **dim)
    add('sensor.lantern_temperature', 'Lantern Temperature', '21')
    entities.append({'entity_id': 'light.registry_only', 'area_id': 'lantern', 'platform': 'test',
                     'disabled_by': None, 'hidden_by': None, 'entity_category': None})
    return {'states': states, 'registries': {'entities': entities, 'devices': devices,
        'areas': [{'area_id': 'lantern', 'name': 'Lantern', 'floor_id': 'ground'},
                  {'area_id': 'workshop', 'name': 'Workshop', 'floor_id': 'ground'},
                  {'area_id': 'landing', 'name': 'Landing', 'floor_id': 'upper'}],
        'floors': [{'floor_id': 'ground', 'name': 'Ground Floor', 'level': 0},
                   {'floor_id': 'upper', 'name': 'Upper Floor', 'level': 1}]}}


class FakeHA:
    def __init__(self, states, registries=None):
        self.states = {s['entity_id']: copy.deepcopy(s) for s in states}
        self.calls = []                # (service, entity_id, service_data)
        self.picture = picture_bytes()
        self.picture_requests = 0
        self.sockets = {}              # websocket -> subscription id
        self.fail_services = False
        self.media = {}                # (content_id, content_type) -> browse_media node
        self.browses = []              # (content_id, content_type) asked for
        self.queues = {}               # entity -> [queue item]
        self.platforms = {}            # entity -> integration for the entity registry (default sonos)
        self.config_entries = {}       # entity -> HA integration instance / grouping scope
        self.registry_entries = {}     # optional realistic registry identity overrides
        self.service_requests = []     # includes read-only actions and their domains
        # None: a light reaches a commanded brightness in one state_changed.
        # {'steps': n, 'span': seconds}: it walks there in n events over the span
        # (a light with a transition), and its groups follow each step.
        self.settle = None
        self.settling = {}             # entity -> running settle task
        registries = registries or {}
        self.entities = copy.deepcopy(registries.get('entities'))  # None preserves legacy auto-registry
        self.devices = copy.deepcopy(registries.get('devices') or [])
        self.areas = copy.deepcopy(registries.get('areas') or [])
        self.floors = copy.deepcopy(registries.get('floors') or [])
        self.registry_requests = []
        self.available = True
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.closing = False
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.serve(), self.loop).result(5)

    def run_loop(self):
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.port

    async def serve(self):
        self.server = await serve(self.handler, '127.0.0.1', 0, process_request=self.http)
        self.port = self.server.sockets[0].getsockname()[1]

    def close(self):
        if self.closing:
            if threading.current_thread() is not self.thread: self.thread.join(3)
            return
        self.closing = True
        async def stop():
            try:
                self.server.close(); await self.server.wait_closed()
            finally:
                self.loop.call_soon(self.loop.stop)
        if threading.current_thread() is self.thread:
            self.loop.create_task(stop())
            return
        try: asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        finally: self.thread.join(3)

    def http(self, connection, request):
        if request.path == '/api/websocket': return None
        if request.path.startswith('/api/media_player_proxy/'):
            if request.headers.get('Authorization') != 'Bearer ' + TOKEN and 'token=' not in request.path:
                return Response(401, 'Unauthorized', Headers(), b'')
            self.picture_requests += 1
            return Response(200, 'OK', Headers([('Content-Type', 'image/jpeg'),
                                                ('Content-Length', str(len(self.picture)))]), self.picture)
        return Response(404, 'Not Found', Headers([('Content-Length', '0')]), b'')

    async def handler(self, ws):
        if not self.available:
            await ws.close(code=1013, reason='Home Assistant unavailable')
            return
        await ws.send(json.dumps({'type': 'auth_required', 'ha_version': 'fake'}))
        auth = json.loads(await ws.recv())
        if auth.get('access_token') != TOKEN:
            await ws.send(json.dumps({'type': 'auth_invalid', 'message': 'Invalid access token'})); return
        await ws.send(json.dumps({'type': 'auth_ok', 'ha_version': 'fake'}))
        try:
            async for raw in ws:
                message = json.loads(raw)
                kind, message_id = message['type'], message['id']
                if kind == 'subscribe_events':
                    self.sockets[ws] = message_id
                    await self.result(ws, message_id, None)
                elif kind == 'get_states':
                    await self.result(ws, message_id, list(self.states.values()))
                elif kind == 'config/entity_registry/list':
                    self.registry_requests.append(kind)
                    await self.result(ws, message_id, self.entity_registry())
                elif kind in ('config/device_registry/list', 'config/area_registry/list', 'config/floor_registry/list'):
                    self.registry_requests.append(kind)
                    name = {'device': 'devices', 'area': 'areas', 'floor': 'floors'}[kind.split('/')[1].split('_')[0]]
                    await self.result(ws, message_id, getattr(self, name))
                elif kind == 'media_player/browse_media':
                    key = (message.get('media_content_id', ''), message.get('media_content_type', ''))
                    self.browses.append(key)
                    node = self.media.get(key) if 'media_content_id' in message else self.media.get(('', 'root'))
                    if node is None:
                        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                                  'error': {'code': 'browse_media_failed', 'message': 'Media not found'}}))
                    else:
                        await self.result(ws, message_id, {**node, 'children': [
                            {k: v for k, v in child.items() if k != 'children'} for child in node['children'] or []]})
                elif kind == 'call_service':
                    entity_id = message['target']['entity_id']
                    targets = entity_id if isinstance(entity_id, list) else [entity_id]
                    self.service_requests.append((message['domain'], message['service'], entity_id,
                                                  copy.deepcopy(message['service_data'])))
                    if message['domain'] == 'sonos' and any(self.platforms.get(entity, 'sonos') != 'sonos' for entity in targets):
                        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                                  'error': {'code': 'unsupported', 'message': 'Not a Sonos player'}}))
                        continue
                    if message['service'] == 'get_queue':   # read-only: not recorded as a call
                        await self.result(ws, message_id, {'context': {}, 'response': {
                            entity: copy.deepcopy(self.queues.get(entity, [])) for entity in targets}})
                        continue
                    self.calls.append((message['service'], entity_id, message['service_data']))
                    if self.fail_services:
                        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                                  'error': {'code': 'failed', 'message': 'Speaker offline'}}))
                        continue
                    for entity in targets:
                        if message['domain'] in ('light', 'switch', 'input_boolean', 'fan', 'scene', 'script', 'climate'):
                            await self.apply_device(message['domain'], message['service'], entity, message['service_data'])
                        else:
                            await self.apply(message['service'], entity, message['service_data'])
                    if message['domain'] == 'light': await self.refresh_light_groups()
                    await self.result(ws, message_id, {'context': {}})
                else:
                    await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                              'error': {'code': 'unknown_command', 'message': kind}}))
        except ConnectionClosed:
            pass
        finally:
            self.sockets.pop(ws, None)

    def entity_registry(self):
        if self.entities is not None:
            return [{**entry, **self.registry_entries.get(entry['entity_id'], {})} for entry in self.entities]
        return [{'entity_id': entity, 'platform': self.platforms.get(entity, 'sonos'),
                 'config_entry_id': self.config_entries.get(entity, self.platforms.get(entity, 'sonos') + '-test-entry'),
                 'device_id': 'fake-' + entity, **self.registry_entries.get(entity, {})} for entity in self.states]

    def registry_snapshot(self):
        return copy.deepcopy({'entities': self.entity_registry(), 'devices': self.devices,
                              'areas': self.areas, 'floors': self.floors})

    def set_available(self, available):
        """Drop real WebSockets and deny reconnects, retaining the server's port."""
        async def change_availability():
            self.available = bool(available)
            if not self.available:
                await asyncio.gather(*(ws.close(code=1013, reason='Home Assistant unavailable')
                                       for ws in list(self.sockets)), return_exceptions=True)
        asyncio.run_coroutine_threadsafe(change_availability(), self.loop).result(5)

    async def apply_device(self, domain, service, entity, data, visited=None):
        if entity not in self.states: return
        visited = set() if visited is None else visited
        if entity in visited: return
        visited.add(entity)
        attributes = self.states[entity]['attributes']
        if domain == 'light' and isinstance(attributes.get('entity_id'), list):
            for member in attributes['entity_id']:
                await self.apply_device(domain, service, member, data, visited)
        if domain in ('scene', 'script') and service == 'turn_on':
            value = datetime.datetime.now(datetime.timezone.utc).isoformat() if domain == 'scene' else 'on'
            await self.change(entity, value, activation_count=attributes.get('activation_count', 0) + 1)
        elif service in ('turn_on', 'turn_off'):
            changes = {}
            if domain == 'light' and 'brightness_pct' in data:
                changes['brightness'] = round(max(0, min(100, data['brightness_pct'])) * 255 / 100)
                if self.settle and service == 'turn_on' and attributes.get('brightness') is not None:
                    previous = self.settling.pop(entity, None)
                    if previous: previous.cancel()
                    start = attributes['brightness']
                    if self.states[entity]['state'] != 'on': await self.change(entity, 'on')
                    self.settling[entity] = asyncio.ensure_future(
                        self.walk(entity, start, changes['brightness'], dict(self.settle)))
                    return
            if domain == 'fan' and 'percentage' in attributes:
                changes['percentage'] = (attributes.get('percentage') or 100) if service == 'turn_on' else 0
            await self.change(entity, 'on' if service == 'turn_on' else 'off', **changes)
        elif domain == 'fan' and service == 'set_percentage':
            percentage = max(0, min(100, data['percentage']))
            await self.change(entity, 'on' if percentage else 'off', percentage=percentage)
        elif domain == 'climate':
            if service == 'set_hvac_mode':
                mode = data['hvac_mode']
                changes = {}
                if 'hvac_action' in attributes:
                    cur = attributes.get('current_temperature')
                    tgt = attributes.get('temperature')
                    low = attributes.get('target_temp_low')
                    high = attributes.get('target_temp_high')
                    if mode == 'off': changes['hvac_action'] = 'off'
                    elif mode == 'fan_only': changes['hvac_action'] = 'fan'
                    elif mode == 'dry': changes['hvac_action'] = 'drying'
                    elif mode == 'heat':
                        changes['hvac_action'] = 'heating' if (cur is not None and tgt is not None and tgt > cur) else 'idle'
                    elif mode == 'cool':
                        changes['hvac_action'] = 'cooling' if (cur is not None and tgt is not None and tgt < cur) else 'idle'
                    else:
                        if cur is not None and low is not None and low > cur: changes['hvac_action'] = 'heating'
                        elif cur is not None and high is not None and high < cur: changes['hvac_action'] = 'cooling'
                        else: changes['hvac_action'] = 'idle'
                await self.change(entity, mode, **changes)
            elif service == 'set_temperature':
                changes = {k: data[k] for k in ('temperature', 'target_temp_low', 'target_temp_high') if k in data}
                mode = data.get('hvac_mode') or self.states[entity]['state']
                if 'hvac_action' in attributes:
                    cur = attributes.get('current_temperature')
                    tgt = changes.get('temperature', attributes.get('temperature'))
                    low = changes.get('target_temp_low', attributes.get('target_temp_low'))
                    high = changes.get('target_temp_high', attributes.get('target_temp_high'))
                    if mode == 'heat' and cur is not None and tgt is not None:
                        changes['hvac_action'] = 'heating' if tgt > cur else 'idle'
                    elif mode == 'cool' and cur is not None and tgt is not None:
                        changes['hvac_action'] = 'cooling' if tgt < cur else 'idle'
                    elif mode in ('heat_cool', 'auto') and cur is not None:
                        if low is not None and low > cur: changes['hvac_action'] = 'heating'
                        elif high is not None and high < cur: changes['hvac_action'] = 'cooling'
                        else: changes['hvac_action'] = 'idle'
                await self.change(entity, data.get('hvac_mode'), **changes)
            elif service == 'set_fan_mode':
                await self.change(entity, None, fan_mode=data['fan_mode'])

    async def walk(self, entity, start, end, settle):
        steps, span = max(1, int(settle['steps'])), float(settle['span'])
        try:
            for index in range(1, steps + 1):
                await asyncio.sleep(span / steps)
                await self.change(entity, 'on', brightness=round(start + (end - start) * index / steps))
                await self.refresh_light_groups()
        except asyncio.CancelledError:
            pass
        finally:
            if self.settling.get(entity) is asyncio.current_task(): self.settling.pop(entity, None)

    async def refresh_light_groups(self):
        for entity, state in list(self.states.items()):
            members = state['attributes'].get('entity_id')
            if not entity.startswith('light.') or not isinstance(members, list) or not members: continue
            on = [self.states[member] for member in members if member in self.states and self.states[member]['state'] == 'on']
            brightness = [member['attributes']['brightness'] for member in on if 'brightness' in member['attributes']]
            changes = {'brightness': round(sum(brightness) / len(brightness))} if brightness else {}
            value = 'on' if on else 'off'
            if state['state'] != value or any(state['attributes'].get(key) != val for key, val in changes.items()):
                await self.change(entity, value, **changes)

    async def result(self, ws, message_id, result):
        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': True, 'result': result}))

    async def apply(self, service, entity_id, data):
        attributes = self.states[entity_id]['attributes']
        if service == 'volume_set': await self.change(entity_id, volume_level=data['volume_level'])
        elif service == 'volume_mute': await self.change(entity_id, is_volume_muted=data['is_volume_muted'])
        elif service == 'media_play': await self.change(entity_id, state='playing')
        elif service == 'media_pause': await self.change(entity_id, state='paused')
        elif service == 'shuffle_set': await self.change(entity_id, shuffle=data['shuffle'])
        elif service == 'repeat_set': await self.change(entity_id, repeat=data['repeat'])
        elif service == 'media_seek': await self.change(entity_id, media_position=data['seek_position'])
        elif service == 'join':
            members = attributes['group_members'] + [m for m in data['group_members']
                                                     if m not in attributes['group_members']]
            for member in members: await self.change(member, group_members=members)
        elif service == 'unjoin':
            remaining = [m for m in attributes['group_members'] if m != entity_id]
            for member in remaining: await self.change(member, group_members=remaining)
            await self.change(entity_id, group_members=[entity_id])
        elif service == 'play_media':
            if data['media_content_id'].startswith('x-rincon-stream:'):
                # Model HA Sonos: a request targeting a member acts on its
                # coordinator, whose media state is shared by the whole group.
                for member in attributes.get('group_members') or [entity_id]:
                    if member in self.states:
                        await self.change(member, 'playing', media_content_id=data['media_content_id'],
                                          media_title='Line-in', source='Line-in', queue_position=None,
                                          media_position=None, media_duration=None)
                return
            queue = self.queues.setdefault(entity_id, [])
            item = {'media_title': data['media_content_id'], 'media_content_id': data['media_content_id']}
            position = int(attributes.get('queue_position') or 0)
            if data['media_content_type'] == 'favorite_item_id' or data.get('enqueue') == 'replace':
                queue[:] = [item]; position = 1
            elif data.get('enqueue') == 'next': queue.insert(position, item)
            elif data.get('enqueue') == 'add': queue.append(item)
            else: queue.insert(position, item); position += 1
            await self.change(entity_id, 'playing' if not data.get('enqueue') or data.get('enqueue') == 'replace'
                              else None, queue_size=len(queue), queue_position=position or None)
        elif service == 'play_queue':
            await self.change(entity_id, 'playing', queue_position=data['queue_position'] + 1)
        elif service == 'remove_from_queue':
            queue = self.queues.setdefault(entity_id, [])
            del queue[data['queue_position']]
            await self.change(entity_id, queue_size=len(queue))
        elif service == 'clear_playlist':
            self.queues[entity_id] = []
            await self.change(entity_id, queue_size=0)

    async def change(self, entity_id, state=None, **attributes):
        old = copy.deepcopy(self.states[entity_id])
        new = self.states[entity_id]
        if state: new['state'] = state
        new['attributes'].update(attributes)
        event = {'event_type': 'state_changed', 'data': {'entity_id': entity_id, 'old_state': old,
                                                           'new_state': copy.deepcopy(new)}}
        for ws, subscription in list(self.sockets.items()):
            await ws.send(json.dumps({'id': subscription, 'type': 'event', 'event': event}))

    def add_media(self, node):
        """Make a browse_media node and everything under it browsable by (id, type)."""
        self.media[(node['media_content_id'], node['media_content_type'])] = node
        for child in node['children'] or []:
            if child['children'] is not None: self.add_media(child)

    def set(self, entity_id, state=None, **attributes):
        """Change an entity from the test thread, as if the speaker changed by itself."""
        asyncio.run_coroutine_threadsafe(self.change(entity_id, state, **attributes), self.loop).result(5)
