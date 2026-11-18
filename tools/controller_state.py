"""Stable controller identities, link keys and association IDs in bridge pairing state.

Each controller has its own 16-byte CCMP link key, delivered to it alone when it
pairs (netstart_pair.py). Forgetting a controller removes its record and key; pairing
it again adds it back with a fresh key. A household may have no controllers: the
bridge creates one on first run, and forgetting every controller returns to it.
"""
from __future__ import annotations

import re

MAX_AID = 2007
_MAC = re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z')
_KEY = re.compile(r'[0-9a-fA-F]{32}\Z')


def normalize_mac(value):
    if not isinstance(value, str) or not _MAC.fullmatch(value):
        raise ValueError('Invalid controller MAC')
    raw = bytes.fromhex(value.replace(':', ''))
    if raw == bytes(6) or raw[0] & 1:
        raise ValueError('Controller MAC must be a nonzero unicast address')
    return value.lower()


def normalize_key(value, mac):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError('Controller %s has no valid link key; pair it again' % mac)
    return value.lower()


def controller_records(state):
    """Return independent, validated records.

    Explicit AIDs are reserved before filling gaps so adding a controller never
    renumbers another one. AID stability also permits a dozing controller to
    resume with a PS-Poll after the bridge restarts.
    """
    listed = state.get('controllers', [])
    if not isinstance(listed, list):
        raise ValueError('controllers must be a list')
    records, macs, aids = [], set(), set()
    for record in listed:
        if not isinstance(record, dict):
            raise ValueError('Invalid controller record')
        record = dict(record)
        record['mac'] = normalize_mac(record.get('mac'))
        if record['mac'] in macs:
            raise ValueError('Duplicate controller MAC')
        macs.add(record['mac'])
        record['key'] = normalize_key(record.get('key'), record['mac'])
        if record.get('previous_key') is None:
            record.pop('previous_key', None)
        else:
            record['previous_key'] = normalize_key(record['previous_key'], record['mac'])
        aid = record.get('aid')
        if aid is not None:
            if type(aid) is not int or not 1 <= aid <= MAX_AID:
                raise ValueError(f'Controller AID must be 1..{MAX_AID}')
            if aid in aids:
                raise ValueError('Duplicate controller AID')
            aids.add(aid)
        records.append(record)
    available = (aid for aid in range(1, MAX_AID + 1) if aid not in aids)
    for record in records:
        if record.get('aid') is None:
            try:
                record['aid'] = next(available)
            except StopIteration:
                raise ValueError('Too many paired controllers') from None
    return records


def normalize_state(state):
    result = dict(state)
    result.pop('cr100_mac', None)
    result.pop('network_key', None)
    result['controllers'] = controller_records(state)
    return result


def merge_controller(state, record):
    """Add/update one controller, preserving its AID and unrelated metadata.

    When a paired controller gets a new key, the old one is kept as ``previous_key``
    until the link hears the new one: a pairing that stops halfway leaves the CR100
    on its old key.
    """
    result = normalize_state(state)
    record = dict(record)
    record['mac'] = normalize_mac(record.get('mac'))
    record['key'] = normalize_key(record.get('key'), record['mac'])
    record.pop('previous_key', None)
    for existing in result['controllers']:
        if existing['mac'] == record['mac']:
            aid, old_key = existing['aid'], existing['key']
            existing.update(record)
            existing['aid'] = aid
            if old_key != record['key']:
                existing['previous_key'] = old_key
            # An unchanged key (merging saved state onto itself) keeps any unconfirmed previous key.
            return normalize_state(result)
    # Newly paired standalone output may carry AID 1; allocate from this household.
    record.pop('aid', None)
    result['controllers'].append(record)
    return normalize_state(result)


def remove_controller(state, mac):
    """Forget one controller: drop its record, AID and keys. The household stays.

    Forgetting the last controller leaves the household with none, as on first run.
    """
    mac = normalize_mac(mac)
    result = normalize_state(state)
    kept = [record for record in result['controllers'] if record['mac'] != mac]
    if len(kept) == len(result['controllers']): raise LookupError('No such paired controller')
    result['controllers'] = kept
    return result


def confirm_key(state, mac, key):
    """Drop a controller's previous key once the link has heard its current one."""
    mac, result = normalize_mac(mac), normalize_state(state)
    for record in result['controllers']:
        if record['mac'] == mac and record['key'] == key.lower():
            record.pop('previous_key', None)
    return result
