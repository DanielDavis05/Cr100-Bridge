"""Home Assistant WebSocket client for the bridge.

One connection on a background asyncio thread, reconnecting after any failure.
It keeps every entity's latest state (get_states, then state_changed events) and
lets the threaded UPnP side call services and other commands synchronously.
Protocol: https://developers.home-assistant.io/docs/api/websocket
"""
from __future__ import annotations

import asyncio
import itertools
import json
import threading
import urllib.request

from websockets.asyncio.client import connect

RECONNECT_DELAY = 5
AUTH_RETRY_DELAY = 60   # a rejected token will not fix itself quickly
CALL_TIMEOUT = 10


class HAError(Exception):
    pass


class HomeAssistant:
    def __init__(self, url, token, log=print):
        self.url, self.token, self.log = url.rstrip('/'), token, log
        self.states = {}          # entity_id -> HA state object
        self.listeners = []       # fn(entity_id, new_state or None), run on the client thread
        self.connected = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.ids = itertools.count(1)
        self.pending = {}
        self.ws = None
        self.closing = False
        self.subscription = None
        self.thread = threading.Thread(target=self.loop.run_forever, name='ha-client', daemon=True)

    # --- lifecycle --------------------------------------------------------------
    def start(self):
        self.thread.start()

        async def begin(): self.runner = asyncio.create_task(self.run())
        asyncio.run_coroutine_threadsafe(begin(), self.loop).result(5)

    def close(self):
        self.closing = True

        async def finish():
            self.runner.cancel()
            try: await self.runner
            except BaseException: pass
        try: asyncio.run_coroutine_threadsafe(finish(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(3)

    async def run(self):
        while not self.closing:
            delay = RECONNECT_DELAY
            try:
                await self.session()
            except asyncio.CancelledError:
                raise
            except PermissionError as error:
                self.log({'event': 'ha-auth-failed', 'error': str(error)}); delay = AUTH_RETRY_DELAY
            except Exception as error:
                self.log({'event': 'ha-disconnected', 'error': repr(error)})
            finally:
                self.connected.clear(); self.ws = None
                for future in self.pending.values():
                    if not future.done(): future.set_exception(HAError('Connection lost'))
                self.pending.clear()
            await asyncio.sleep(delay)

    async def session(self):
        ws_url = 'ws' + self.url[len('http'):] + '/api/websocket'
        async with connect(ws_url, max_size=None, open_timeout=10) as ws:
            if json.loads(await ws.recv()).get('type') != 'auth_required': raise HAError('No auth prompt')
            await ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
            reply = json.loads(await ws.recv())
            if reply.get('type') != 'auth_ok': raise PermissionError(reply.get('message', reply.get('type')))
            self.ws = ws
            reader = asyncio.create_task(self.read(ws))
            try:
                # Subscribe first: the snapshot that follows is newer than any event before it.
                self.subscription = next(self.ids)
                await self.request({'type': 'subscribe_events', 'event_type': 'state_changed'}, self.subscription)
                states = {s['entity_id']: s for s in await self.request({'type': 'get_states'})}
                for entity_id in set(self.states) | set(states):
                    if self.states.get(entity_id) != states.get(entity_id):
                        self.update(entity_id, states.get(entity_id))
                self.connected.set()
                self.log({'event': 'ha-connected', 'version': reply.get('ha_version'), 'entities': len(states)})
                await reader
            finally:
                reader.cancel()

    async def read(self, ws):
        async for raw in ws:
            message = json.loads(raw)
            if message.get('type') == 'event' and message.get('id') == self.subscription:
                data = message['event']['data']
                self.update(data['entity_id'], data.get('new_state'))
            elif message.get('type') == 'result':
                future = self.pending.pop(message.get('id'), None)
                if future is None or future.done(): continue
                if message.get('success'): future.set_result(message.get('result'))
                else: future.set_exception(HAError((message.get('error') or {}).get('message', 'Failed')))

    def update(self, entity_id, state):
        if state is None: self.states.pop(entity_id, None)
        else: self.states[entity_id] = state
        for listener in list(self.listeners):
            try: listener(entity_id, state)
            except Exception as error: self.log({'event': 'ha-listener-error', 'error': repr(error)})

    async def request(self, payload, message_id=None):
        if self.ws is None: raise HAError('Not connected')
        message_id = message_id or next(self.ids)
        future = self.loop.create_future()
        self.pending[message_id] = future
        await self.ws.send(json.dumps({**payload, 'id': message_id}))
        return await future

    # --- thread-safe API --------------------------------------------------------
    def call(self, payload, timeout=CALL_TIMEOUT):
        if not self.connected.is_set(): raise HAError('Home Assistant is not connected')
        return asyncio.run_coroutine_threadsafe(self.request(payload), self.loop).result(timeout)

    def call_service(self, domain, service, entity_id, data=None, response=False, timeout=CALL_TIMEOUT):
        payload = {'type': 'call_service', 'domain': domain, 'service': service,
                   'service_data': data or {}, 'target': {'entity_id': entity_id}}
        if response: payload['return_response'] = True
        result = self.call(payload, timeout)
        return (result or {}).get('response') if response else result

    def state(self, entity_id):
        return self.states.get(entity_id) or {}

    def fetch(self, path, timeout=CALL_TIMEOUT):
        """GET a Home Assistant URL (e.g. an entity_picture proxy path) or an absolute URL."""
        url = path if path.startswith(('http://', 'https://')) else self.url + path
        headers = {'Authorization': 'Bearer ' + self.token} if url.startswith(self.url) else {}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return response.read(8 * 1024 * 1024)
