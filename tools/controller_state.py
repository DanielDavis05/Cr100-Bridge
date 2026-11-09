"""Stable controller identities and association IDs in bridge pairing state."""
from __future__ import annotations

import re

MAX_AID = 2007
_MAC = re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z')


def normalize_mac(value):
    if not isinstance(value, str) or not _MAC.fullmatch(value):
        raise ValueError('Invalid controller MAC')
    raw = bytes.fromhex(value.replace(':', ''))
    if raw == bytes(6) or raw[0] & 1:
        raise ValueError('Controller MAC must be a nonzero unicast address')
    return value.lower()


def controller_records(state):
    """Return independent, validated records; retain legacy single-controller state.

    Explicit AIDs are reserved before filling gaps so adding a controller never
    renumbers another one. AID stability also permits a dozing controller to
    resume with a PS-Poll after the bridge restarts.
    """
    listed = state.get('controllers', [])
    if not isinstance(listed, list):
        raise ValueError('controllers must be a list')
    if not listed and state.get('cr100_mac'):
        listed = [{'mac': state['cr100_mac'],
                   **{key: state[key] for key in ('txid', 'time') if key in state}}]
    records, macs, aids = [], set(), set()
    for record in listed:
        if not isinstance(record, dict):
            raise ValueError('Invalid controller record')
        record = dict(record)
        record['mac'] = normalize_mac(record.get('mac'))
        if record['mac'] in macs:
            raise ValueError('Duplicate controller MAC')
        macs.add(record['mac'])
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
    result['controllers'] = controller_records(state)
    if result['controllers']:
        result['cr100_mac'] = result['controllers'][0]['mac']
    return result


def merge_controller(state, record):
    """Add/update one controller, preserving its AID and unrelated metadata."""
    result = normalize_state(state)
    record = dict(record)
    legacy_mac = record.pop('cr100_mac', None)
    mac = normalize_mac(record.get('mac', legacy_mac))
    if legacy_mac is not None and normalize_mac(legacy_mac) != mac:
        raise ValueError('Conflicting controller MACs')
    record['mac'] = mac
    for existing in result['controllers']:
        if existing['mac'] == mac:
            aid = existing['aid']
            existing.update(record)
            existing['aid'] = aid
            return result
    # Newly paired standalone output may carry AID 1; allocate from this household.
    record.pop('aid', None)
    result['controllers'].append(record)
    return normalize_state(result)
