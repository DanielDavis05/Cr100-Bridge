"""Cached physical Sonos identities and explicit bonds, independent of playback groups.

Only refresh() performs network I/O. Call it on a backend worker; the CR100's
request threads use identity() and bonded_devices(), which return cached copies.
Home Assistant registries can supply the seed and fallback metadata without
exposing a media_player for every bonded Sub or surround.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from sonos_queue_art import MAX_RESPONSE, _device_base, _NoRedirect, _xml

REFRESH_INTERVAL = 60
ERROR_INTERVAL = 5
NETWORK_TIMEOUT = 2
MAX_REQUESTS = 4             # bound each refresh on a large household
BRIDGE_PREFIX = 'RINCON_0269'
FIELDS = ('id', 'name', 'model', 'software', 'display', 'hardware', 'serial', 'ip', 'extra')
SERVICE = 'urn:schemas-upnp-org:service:ZoneGroupTopology:1'


def physical_uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r'RINCON_[A-Za-z0-9_]{1,100}', value):
        return None
    return None if value.startswith(BRIDGE_PREFIX) else value


def local(tag):
    return tag.rpartition('}')[2]


def topology_inventory(text):
    """Return physical records and a graph containing bonds, never playback grouping.

    Nested Satellite elements identify their parent directly. Stereo pairs and
    other bonded hardware can instead be sibling members named by ChannelMapSet
    or HTSatChanMapSet. Invisible alone does not identify a parent.
    """
    root = _xml(text.encode('utf-8') if isinstance(text, str) else text)
    records, bonds, maps = {}, {}, []

    def add(element, group, parent=None):
        uuid = physical_uuid(element.get('UUID'))
        if not uuid: return
        records[uuid] = {'id': uuid, 'base': _device_base(element.get('Location')),
                         'name': element.get('ZoneName') or '',
                         'software': element.get('SoftwareVersion') or '', 'group': group}
        bonds.setdefault(uuid, set())
        if parent:
            bonds[uuid].add(parent)
            bonds.setdefault(parent, set()).add(uuid)
        for key in ('ChannelMapSet', 'HTSatChanMapSet'):
            if element.get(key): maps.append((uuid, group, element.get(key)))
        for child in element:
            if local(child.tag) == 'Satellite': add(child, group, uuid)

    for index, group in enumerate(root.iter()):
        if local(group.tag) != 'ZoneGroup': continue
        group_id = (index, group.get('ID'))
        for member in group:
            if local(member.tag) == 'ZoneGroupMember': add(member, group_id)
    for owner, group, value in maps:
        members = {physical_uuid(part.partition(':')[0]) for part in value.split(';')}
        members = {uuid for uuid in members if uuid in records and records[uuid]['group'] == group}
        # A map belongs to the product advertising it. Refuse an unrelated map.
        if owner not in members: continue
        for uuid in members:
            bonds[uuid].update(members - {uuid})
    return records, bonds


class IdentityInventory:
    def __init__(self, log=print, opener=None, clock=time.monotonic, max_requests=MAX_REQUESTS):
        self.log, self.clock = log, clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.max_requests = max(1, int(max_requests))
        self.lock, self.refresh_lock = threading.Lock(), threading.Lock()
        self.identities, self.bonded = {}, {}
        self.devices = {}           # UUID -> last successful metadata and retry deadline
        self.topologies = []       # one cached raw inventory per discovered household
        self.room_topologies = {}
        self.cursor = 0

    def identity(self, room_id):
        with self.lock:
            value = self.identities.get(room_id)
            return dict(value) if value is not None else None

    def bonded_devices(self, room_id):
        with self.lock: return [dict(value) for value in self.bonded.get(room_id, ())]

    def read(self, request):
        with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            return _xml(response.read(MAX_RESPONSE + 1))

    def read_topology(self, base):
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                '<u:GetZoneGroupState xmlns:u="%s"/></s:Body></s:Envelope>' % SERVICE).encode()
        root = self.read(urllib.request.Request(base + '/ZoneGroupTopology/Control', body,
            {'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"%s#GetZoneGroupState"' % SERVICE}))
        value = next((element.text for element in root.iter() if local(element.tag) == 'ZoneGroupState'), None)
        if not value: raise ValueError('Missing Sonos topology')
        return topology_inventory(value)

    def read_description(self, uuid, base):
        root = self.read(base + '/xml/device_description.xml')
        device = next((element for element in root if local(element.tag) == 'device'), None)
        if device is None: raise ValueError('Missing Sonos device')
        values = {local(child.tag): (child.text or '').strip() for child in device}
        udn = values.get('UDN', '').removeprefix('uuid:')
        if udn != uuid: raise ValueError('Unexpected Sonos device identity')
        if not (values.get('softwareVersion') or values.get('modelName') or values.get('modelNumber')):
            raise ValueError('Missing Sonos device information')
        return {'id': uuid, 'name': values.get('roomName', ''),
                'model': values.get('modelName') or values.get('displayName') or values.get('modelNumber', ''),
                'software': values.get('softwareVersion', ''), 'display': values.get('displayVersion', ''),
                'hardware': values.get('hardwareVersion', ''), 'serial': values.get('serialNum', ''),
                'ip': urlsplit(base).hostname or '', 'extra': values.get('extraVersion', '')}

    def refresh(self, seeds):
        """Refresh a bounded batch and return room IDs whose cached snapshots changed.

        seeds maps backend room IDs to uuid/base/room_name and optional fallback
        metadata. Invalid endpoints are never contacted; supplied fallback data
        can still honestly describe a recognized but unreachable Sonos product.
        Last successful data survives temporary failures, with a short retry.
        """
        if not self.refresh_lock.acquire(blocking=False): return set()
        try: return self._refresh(seeds)
        finally: self.refresh_lock.release()

    def _refresh(self, seeds):
        now, budget = self.clock(), [self.max_requests]
        topology_budget = max(1, self.max_requests // 2)
        identities, bonded, visited = {}, {}, set()
        self.pending = []
        self.room_topologies = {key: value for key, value in self.room_topologies.items() if key in seeds}
        items = list(seeds.items())
        if items:
            offset = self.cursor % len(items)
            items = items[offset:] + items[:offset]
            self.cursor += 1
        for room_id, seed in items:
            if isinstance(seed.get('uuid'), str) and seed['uuid'].startswith(BRIDGE_PREFIX):
                bonded[room_id] = []
                continue
            uuid, base = physical_uuid(seed.get('uuid')), _device_base(seed.get('base'))
            if not uuid:
                fallback = seed.get('fallback')
                if fallback:
                    identities[room_id] = self.metadata(fallback, {'name': seed.get('room_name')},
                                                       {'id': fallback.get('id') or room_id})
                bonded[room_id] = []
                continue
            entry = self.room_topologies.get(room_id)
            if entry is not None and entry.get('seed_ids', {}).get(room_id) != uuid: entry = None
            if entry is None:
                candidates = [item for item in self.topologies if uuid in item['records']]
                entry = max(candidates, key=lambda item: item['updated']) if candidates else None
            if entry is None and base:
                entry = {'base': base, 'records': {}, 'bonds': {}, 'next_at': 0, 'updated': 0, 'seed_ids': {}}
                self.topologies.append(entry)
            if entry is not None:
                entry['seed_ids'][room_id] = uuid
                self.room_topologies[room_id] = entry
                moved = bool(base and entry['base'] != base and uuid in entry['records']
                             and entry['records'][uuid]['base'] != base)
                if base and id(entry) not in visited and budget[0] and topology_budget \
                        and (now >= entry['next_at'] or moved):
                    visited.add(id(entry)); budget[0] -= 1
                    topology_budget -= 1
                    try:
                        records, graph = self.read_topology(base)
                        entry.update(base=base, records=records, bonds=graph, next_at=now + REFRESH_INTERVAL,
                                     updated=now)
                    except Exception:
                        entry.update(base=base, next_at=now + ERROR_INTERVAL)
                        self.log({'event': 'sonos-identity-topology-unavailable'})
            records, graph = (entry['records'], entry['bonds']) if entry else ({}, {})
            record = records.get(uuid, {})
            identities[room_id] = self.device(uuid, base or record.get('base'),
                self.metadata(seed.get('fallback'), record, {'name': seed.get('room_name'), 'id': uuid}), now)
            found, pending = {uuid}, [uuid]
            while pending:
                current = pending.pop()
                for child in set(graph.get(current, ())) - found:
                    found.add(child); pending.append(child)
            bonded[room_id] = [self.device(child, records[child].get('base'), records[child], now)
                               for child in sorted(found - {uuid})]
        # Oldest/missing descriptions go first, so a small request budget cannot
        # indefinitely starve later rooms or satellites on slower refresh callers.
        requests = {}
        for uuid, base, _, _ in self.pending:
            cached = self.devices[uuid]
            if base and (cached['base'] != base or now >= cached['next_at']):
                requests.setdefault(uuid, base)
        for uuid in sorted(requests, key=lambda key: self.devices[key]['next_at']):
            if not budget[0]: break
            budget[0] -= 1
            cached, base = self.devices[uuid], requests[uuid]
            try:
                info = self.read_description(uuid, base)
                cached.update(base=base, info=info, next_at=now + REFRESH_INTERVAL)
            except Exception:
                cached.update(base=base, next_at=now + ERROR_INTERVAL)
                self.log({'event': 'sonos-identity-unavailable', 'device': uuid})
        for uuid, base, fallback, result in self.pending:
            result.update(self.device_metadata(uuid, base, fallback))
        with self.lock:
            changed = {key for key in self.identities.keys() | identities.keys()
                       if self.identities.get(key) != identities.get(key) or self.bonded.get(key) != bonded.get(key)}
            self.identities, self.bonded = identities, bonded
        # Bound memory even when devices are repeatedly replaced or rooms switch households.
        if len(self.devices) > 256:
            self.devices = dict(sorted(self.devices.items(), key=lambda item: item[1]['used'])[-256:])
        referenced = {id(entry) for entry in self.room_topologies.values()}
        self.topologies = [entry for entry in self.topologies if id(entry) in referenced][-64:]
        for entry in self.topologies:
            entry['seed_ids'] = {key: uuid for key, uuid in entry['seed_ids'].items() if key in seeds}
        return changed

    @staticmethod
    def metadata(*sources):
        result = {key: '' for key in FIELDS}
        for source in sources:
            if not source: continue
            for key in FIELDS:
                value = source.get(key)
                if isinstance(value, str) and value: result[key] = value[:512]
        return result

    def device(self, uuid, base, fallback, now):
        base = _device_base(base)
        cached = self.devices.setdefault(uuid, {'base': base, 'info': {}, 'next_at': 0, 'used': now})
        cached['used'] = now
        result = self.device_metadata(uuid, base, fallback)
        self.pending.append((uuid, base, fallback, result))
        return result

    def device_metadata(self, uuid, base, fallback):
        result = self.metadata(fallback, self.devices[uuid]['info'], {'id': uuid})
        # Current topology/registry addresses and selected-room names win over old descriptions.
        if base: result['ip'] = urlsplit(base).hostname or ''
        if fallback.get('name'): result['name'] = fallback['name'][:512]
        return result
