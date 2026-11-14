"""Sonos inputs and device information identified by Home Assistant's registries.

Home Assistant reports the playing x-rincon-stream URI, but does not expose a
line-in cable sensor or its configured name. Subscribe to AudioIn on those same
Sonos devices to obtain those values. A separate cached inventory reads identity
and bonded hardware for every Sonos model, including those without AudioIn.
Playback still goes through Home Assistant.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit

from sonos_identity import IdentityInventory
from room_capabilities import RoomBinding
import sonos_upnp
from sonos_queue_art import _device_base

REGISTRY_INTERVAL = 60
RETRY_INTERVAL = 5
NETWORK_TIMEOUT = 2
STREAM_PREFIX = 'x-rincon-stream:'


def source_uuid(uri):
    if not isinstance(uri, str) or not uri.startswith(STREAM_PREFIX): return None
    uuid = uri[len(STREAM_PREFIX):]
    return uuid if re.fullmatch(r'RINCON_[A-Za-z0-9_]{1,100}', uuid) else None


class SonosLineIn:
    def __init__(self, ha, changed, log=print, listener=None, reader=None, inventory=None):
        self.ha, self.changed, self.log = ha, changed, log
        self.listener = listener
        self.reader = reader
        self.inventory = inventory if inventory is not None else IdentityInventory(log)
        self.lock = threading.RLock()
        self.players = {}                # entity -> {uuid, base, room_name}
        self.registry_identities = {}    # recognized Sonos entities, including unreachable ones
        self.identity_seeds = {}
        self.bindings = {}              # all HA media players, independent of About descriptions
        self.registry_ready = threading.Event()
        self.inputs = {}                 # UUID -> {connected, name, icon}
        self.subscriptions = {}          # UUID -> (base, subscription)
        self.stop = threading.Event()
        self.thread = None
        self.closed_listener = False
        self.hub = getattr(ha, 'ha_hub', None)
        self.registry_changed = threading.Event()
        self._registry_listener = self.registry_changed.set
        if self.hub: self.hub.listeners.append(self._registry_listener)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='ha-sonos-inputs', daemon=True)
        self.thread.start()
        # Only the initial registry attempt is awaited, not device discovery.
        # HTTP getters and commands thereafter consume cached snapshots.
        self.registry_ready.wait(NETWORK_TIMEOUT * 2 + .5)

    def close(self):
        self.stop.set()
        self.registry_changed.set()
        if self.hub and self._registry_listener in self.hub.listeners:
            self.hub.listeners.remove(self._registry_listener)
        # Registry, input, and identity reads may still be in flight.
        # The worker owns listener shutdown until that in-flight operation ends.
        if self.thread: self.thread.join(NETWORK_TIMEOUT * 3 + sonos_upnp.SOAP_TIMEOUT + 1)
        if not self.thread or not self.thread.is_alive(): self.close_listener()

    def close_listener(self):
        with self.lock:
            if self.closed_listener: return
            self.closed_listener = True
        if self.listener: self.listener.close()

    def run(self):
        next_registry, next_identity, previous = 0, 0, None
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                try:
                    if now >= next_registry or self.registry_changed.is_set():
                        self.registry_changed.clear()
                        next_registry = now + (REGISTRY_INTERVAL if self.refresh() else RETRY_INTERVAL)
                        next_identity = time.monotonic() + RETRY_INTERVAL
                    elif now >= next_identity:
                        self.refresh_identities()
                        next_identity = time.monotonic() + RETRY_INTERVAL
                    current = self.sources()
                    # A failed renewal/expired subscription must withdraw stale inputs.
                    if current != previous:
                        previous = current
                        self.changed()
                except Exception:
                    next_registry = now + RETRY_INTERVAL
                    self.log({'event': 'line-in-discovery-unavailable'})
                self.registry_changed.wait(1)
        finally:
            self.close_listener()

    def refresh(self):
        """Resolve only Sonos entities and literal port-1400 registry addresses."""
        try:
            if self.hub:
                if not self.hub.ready.wait(NETWORK_TIMEOUT * 2):
                    raise ValueError('Home Assistant registry not ready')
                snapshot = self.hub.snapshot()
                entities, devices = snapshot['entities'], snapshot['devices']
            else:
                # Standalone callers without a player-process hub retain the
                # previous injectable API (including downstream extensions).
                entities = self.ha.call({'type': 'config/entity_registry/list'}, NETWORK_TIMEOUT)
                devices = self.ha.call({'type': 'config/device_registry/list'}, NETWORK_TIMEOUT)
            if not isinstance(entities, list) or not isinstance(devices, list):
                raise ValueError('Missing Sonos registry')
            devices = {d.get('id'): d for d in devices if isinstance(d, dict)}
            players, identities, seeds, bindings = {}, {}, {}, {}
            for entry in entities:
                if not isinstance(entry, dict): continue
                entity = entry.get('entity_id')
                if not isinstance(entity, str) or not entity.startswith('media_player.'): continue
                platform = entry.get('platform') if isinstance(entry.get('platform'), str) else ''
                config_entry = entry.get('config_entry_id')
                group_key = (platform, config_entry) if platform and isinstance(config_entry, str) and config_entry else None
                device_id = entry.get('device_id') if isinstance(entry.get('device_id'), str) else ''
                bindings[entity] = RoomBinding(platform=platform, device_id=device_id, group_key=group_key)
                if platform != 'sonos': continue
                device = devices.get(entry.get('device_id'), {})
                base = _device_base(device.get('configuration_url'))
                uuid = source_uuid(STREAM_PREFIX + str(entry.get('unique_id') or ''))
                if not uuid:
                    for identifier in device.get('identifiers') or []:
                        if not isinstance(identifier, (list, tuple)) or len(identifier) != 2: continue
                        domain, value = identifier
                        if domain == 'sonos' and source_uuid(STREAM_PREFIX + str(value)):
                            uuid = value
                            break
                if uuid and uuid.startswith(sonos_upnp.BRIDGE_PLAYERS):
                    bindings.pop(entity, None)
                    continue
                bindings[entity] = RoomBinding(platform=platform, device_id=device_id,
                                              native_id=uuid or '', native_url=base or '', group_key=group_key)
                name = (self.ha.state(entity).get('attributes') or {}).get('friendly_name') or entity
                # HA's sw_version is the friendly release, not the numeric UPnP
                # software/build version. Leave the latter unknown until read.
                fallback = {'id': uuid or entity, 'name': name,
                            'model': device.get('model') or 'Sonos (details unavailable)',
                            'display': device.get('sw_version') or '', 'software': '',
                            'hardware': device.get('hw_version') or '',
                            'serial': device.get('serial_number') or '',
                            'ip': urlsplit(base).hostname if base else '', 'extra': ''}
                identities[entity] = fallback
                seeds[entity] = {'uuid': uuid, 'base': base, 'room_name': name, 'fallback': fallback}
                if base and uuid and not uuid.startswith(sonos_upnp.BRIDGE_PLAYERS):
                    players[entity] = seeds[entity]
        except Exception:
            self.log({'event': 'line-in-registry-unavailable'})
            self.registry_ready.set()
            return False
        if self.stop.is_set(): return False
        with self.lock:
            previous, previous_identities, previous_bindings = self.players, self.registry_identities, self.bindings
            self.players = players
            self.registry_identities = identities
            self.identity_seeds = seeds
            self.bindings = bindings
            wanted = {p['uuid']: p['base'] for p in players.values()}
            removed = [uuid for uuid, (base, _) in self.subscriptions.items() if wanted.get(uuid) != base]
            old = [self.subscriptions.pop(uuid)[1] for uuid in removed]
            for uuid in removed: self.inputs.pop(uuid, None)
        self.registry_ready.set()
        for subscription in old: self.listener.unsubscribe(subscription)
        if self.stop.is_set(): return False
        if players and self.listener is None:
            self.listener = sonos_upnp.EventListener(self.log)
            self.listener.start()
        for uuid, base in wanted.items():
            if self.stop.is_set(): break
            with self.lock:
                if uuid in self.subscriptions: continue
            try:
                # Unsupported models need no AudioIn subscription or renewal loop.
                info = (self.reader or sonos_upnp.read_line_in)(base, NETWORK_TIMEOUT)
            except sonos_upnp.UPnPFault:
                continue
            if self.stop.is_set(): break
            with self.lock:
                self.inputs.setdefault(uuid, {'connected': False}).update(info)
            subscription = self.listener.subscribe(base, 'AudioIn',
                                                   lambda values, uuid=uuid: self.on_audio(uuid, values))
            with self.lock: self.subscriptions[uuid] = (base, subscription)
        if self.stop.is_set(): return False
        # All reads (including bonded hardware) stay on this worker. About
        # requests only access the cached snapshot through the methods below.
        identity_changes = self.inventory.refresh(seeds)
        if self.stop.is_set(): return False
        if players != previous or identities != previous_identities or identity_changes or bindings != previous_bindings:
            self.changed()
        return True

    def refresh_identities(self):
        # The inventory drains a bounded batch each time. Retry from cached
        # registry seeds between registry polls, including when HA is offline.
        if self.stop.is_set(): return
        with self.lock: seeds = dict(self.identity_seeds)
        changed = self.inventory.refresh(seeds)
        if changed and not self.stop.is_set(): self.changed()

    def on_audio(self, uuid, values):
        with self.lock:
            current = dict(self.inputs.get(uuid) or {'connected': False})
            updated = dict(current)
            if 'LineInConnected' in values:
                updated['connected'] = str(values['LineInConnected']).lower() in ('1', 'true')
            if 'AudioInputName' in values: updated['name'] = values['AudioInputName'] or 'Line-In'
            if 'Icon' in values: updated['icon'] = values['Icon']
            self.inputs[uuid] = updated
        if current != updated: self.changed()

    def uuid(self, entity):
        with self.lock: return (self.players.get(entity) or {}).get('uuid')

    def binding(self, entity):
        """Cached routing identity; About data is never command authorization."""
        with self.lock: return self.bindings.get(entity, RoomBinding())

    def identity(self, entity):
        with self.lock:
            fallback = self.registry_identities.get(entity)
            if fallback is None: return None
            fallback = dict(fallback)
        return self.inventory.identity(entity) or fallback

    def bonded_devices(self, entity):
        with self.lock:
            if entity not in self.registry_identities: return []
        return self.inventory.bonded_devices(entity)

    def entity(self, uuid):
        with self.lock:
            return next((entity for entity, player in self.players.items() if player['uuid'] == uuid), None)

    def sources(self):
        connected = getattr(self.ha, 'connected', None)
        if connected is not None and not connected.is_set(): return []
        with self.lock:
            found = []
            for entity, player in self.players.items():
                info = self.inputs.get(player['uuid']) or {}
                if not info.get('connected'): continue
                subscription = self.subscriptions.get(player['uuid'])
                if not subscription or not subscription[1].healthy: continue
                if self.ha.state(entity).get('state') in ('unavailable', 'unknown'): continue
                found.append({'id': entity, 'name': info.get('name') or 'Line-In',
                              'room_name': player['room_name'], 'icon': info.get('icon') or '',
                              'uri': STREAM_PREFIX + player['uuid']})
            return found
