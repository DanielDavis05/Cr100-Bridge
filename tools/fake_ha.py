"""A small fake Home Assistant for offline tests: WebSocket API + entity_picture proxy.

Implements what the bridge uses: auth, subscribe_events (state_changed), get_states,
call_service for media_player and sonos services (with plausible Sonos effects and
return_response), media_player/browse_media over a `media` tree,
config/entity_registry/list (platform from `platforms`, default sonos), and
GET /api/media_player_proxy/<entity> returning a picture. Runs on its own thread.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import threading

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

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


class FakeHA:
    def __init__(self, states):
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
        self.devices = []              # Sonos device registry (empty unless a test supplies one)
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
                    await self.result(ws, message_id, [
                        {'entity_id': e, 'platform': self.platforms.get(e, 'sonos'),
                         'config_entry_id': self.config_entries.get(e, self.platforms.get(e, 'sonos') + '-test-entry'),
                         'device_id': 'fake-' + e, **self.registry_entries.get(e, {})} for e in self.states])
                elif kind == 'config/device_registry/list':
                    await self.result(ws, message_id, self.devices)
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
                    self.service_requests.append((message['domain'], message['service'], entity_id,
                                                  copy.deepcopy(message['service_data'])))
                    if message['domain'] == 'sonos' and self.platforms.get(entity_id, 'sonos') != 'sonos':
                        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                                  'error': {'code': 'unsupported', 'message': 'Not a Sonos player'}}))
                        continue
                    if message['service'] == 'get_queue':   # read-only: not recorded as a call
                        await self.result(ws, message_id, {'context': {}, 'response': {
                            entity_id: copy.deepcopy(self.queues.get(entity_id, []))}})
                        continue
                    self.calls.append((message['service'], entity_id, message['service_data']))
                    if self.fail_services:
                        await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                                  'error': {'code': 'failed', 'message': 'Speaker offline'}}))
                        continue
                    await self.apply(message['service'], entity_id, message['service_data'])
                    await self.result(ws, message_id, {'context': {}})
                else:
                    await ws.send(json.dumps({'id': message_id, 'type': 'result', 'success': False,
                                              'error': {'code': 'unknown_command', 'message': kind}}))
        finally:
            self.sockets.pop(ws, None)

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
