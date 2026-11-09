#!/usr/bin/env python3
"""Guarded Radio-only UI adaptation for a copied CR100 emulator SWF.

The original native Radio provider injects separate saved-station/show folders.
This adapter groups them into Favorites and decorates explicit non-audio setting
choices. It never installs firmware or edits the supplied input file.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from patch_controller_settings_ui import (SUPPORTED, Code, Patcher, action, array, call,
    define, discard, fncall, inspect, literal, local, member, obj, push, records,
    ret, setmember, setvar, u16, u32, var)

def allow_zip_menu(body):
    branches = [record for record in records(body) if record.code == 0x9d]
    if len(branches) != 1 or len(branches[0].payload) != 2:
        raise ValueError('Unexpected original Radio country gate')
    branch = branches[0]
    return body[:branch.start+3] + b'\x00\x00' + body[branch.end:]


ARGS = {
    'RadioUI_originalCreateCDNode': ('parentNode', 'ixChild'),
    'RadioUI_findChild': ('parent', 'wantedId', 'wantedTitle'),
    'RadioUI_onApplied': ('upnpOp', 'upnpRet'),
}


def helpers():
    result = {}
    # Read actual native cached children so generated provider object IDs and
    # native search/local-location behaviors stay genuine firmware operations.
    c = Code().add(local('count', call(var('BrowseCacheMgr'), 'getContainerTotalMatches',
        member(var('parent'), 'cdUDN'), member(var('parent'), 'id'), literal(True))))
    c.add(local('ix', literal(0))).label('loop')
    c.add(var('ix') + var('count') + action(0x48) + action(0x12)).jump('missing', True)
    c.add(local('child', fncall('createCDNode', var('parent'), var('ix'))))
    c.add(member(var('child'), 'id') + var('wantedId') + action(0x49)).jump('found', True)
    c.add(member(var('child'), 'dc:title') + var('wantedTitle') + action(0x49)).jump('found', True)
    c.add(setvar('ix', var('ix') + action(0x50))).jump('loop')
    c.label('found').add(ret(var('child'))).label('missing').add(ret(literal(None)))
    result['RadioUI_findChild'] = c.bytes()

    c = Code().add(local('children', array([])))
    c.add(local('favorite', fncall('createRootCDNode', literal('Favorites'),
        member(var('Household'), 'radioUDN'), literal('R:0'), literal('0'), literal(False))))
    c.add(setmember(var('favorite'), 'async', literal(True)))
    c.add(discard(call(var('children'), 'push', var('favorite'))))
    c.add(local('child', fncall('RadioUI_findChild', var('this'), literal('search'), literal('Search'))))
    c.add(discard(call(var('children'), 'push', var('child'))))
    c.add(local('child', fncall('RadioUI_findChild', var('this'), literal('L'), literal('__native_local__'))))
    c.add(discard(call(var('children'), 'push', var('child'))))
    for title in ('Music', 'Talk', 'Sports', 'Location', 'Podcasts', 'By Language', 'Radio Settings'):
        label = 'after_' + title.replace(' ', '_')
        c.add(local('child', fncall('RadioUI_findChild', var('this'), literal('__remote__'), literal(title))))
        c.add(var('child') + action(0x12)).jump(label, True)
        if title == 'Radio Settings':
            c.add(setmember(var('child'), 'bridgeRadioSettings', literal(True)))
            c.add(setmember(var('Household'), 'bridgeRadioSettingsRootId', member(var('child'), 'id')))
        c.add(discard(call(var('children'), 'push', var('child')))).label(label)
    c.add(setmember(var('this'), 'children', var('children'))).add(ret(literal(True)))
    result['RadioUI_populateRoot'] = c.bytes()

    # Settings use the original player's explicit SystemProperties writer.
    # The special Radio provider does not implement generic SMAPI library ops.
    c = Code().add(local('zp', fncall('ZoneGroups_GetAssociatedZP')))
    c.add(local('status', call(member(var('zp'), 'SystemProperties'), 'setString',
        literal('CR100_RadioAction'), member(var('this'), 'id'))))
    c.add(var('status') + literal(0) + action(0x49) + action(0x12)).jump('failure', True)
    c.add(discard(call(var('BrowseCacheMgr'), 'invalidateContainerHierarchy',
        member(var('Household'), 'radioUDN'), member(var('Household'), 'bridgeRadioSettingsRootId'))))
    c.add(discard(call(var('BrowseCacheMgr'), 'forcePoll')))
    c.add(discard(fncall('showDismissablePopup', literal('Radio setting applied'), literal(2000))))
    c.add(ret(literal(None))).label('failure')
    c.add(discard(fncall('showDismissablePopup', literal('Could not apply Radio setting'), literal(4000))))
    c.add(ret(literal(None)))
    result['RadioUI_applyChoice'] = c.bytes()

    # Radio's original location popup is reused from both Local Radio and the
    # new settings section, retaining the original city picker and ZIP keyboard.
    result['RadioUI_changeLocation'] = discard(call(var('g_BrowsePanel'), 'setRadioLocation')) + ret(literal(False))
    return result


def add_root():
    c = Code().add(local('associated', fncall('ZoneGroups_GetAssociatedZP')))
    c.add(var('associated') + action(0x12)).jump('done', True)
    c.add(local('child', fncall('createRootCDNode', var('STR_InternetRadio'),
        member(var('Household'), 'radioUDN'), literal('0'), var('parentId'), literal(False))))
    c.add(setmember(var('child'), 'populateChildren', var('RadioUI_populateRoot')))
    c.add(setmember(var('child'), 'async', literal(True)))
    c.add(setmember(var('child'), 'softbuttonPolicies', array([var('SB_inherit'), literal(None), var('SB_radioFavorites')])))
    c.add(setmember(var('child'), 'softbuttonScrollPolicies', array([literal(None), var('SBS_setLocation'), literal(None)])))
    c.add(discard(call(var('pArray'), 'push', var('child')))).label('done').add(ret(literal(None)))
    return c.bytes()


def create_node():
    c = Code().add(local('child', fncall('RadioUI_originalCreateCDNode', var('parentNode'), var('ixChild'))))
    c.add(var('child') + action(0x12)).jump('done', True)
    # Only deliberate action IDs from the native Radio catalog are settings.
    # Native Radio does not expose its generic favorite capability on these
    # non-audio rows, so do not use it to gate explicit settings actions.
    c.add(member(var('child'), 'cdUDN') + member(var('Household'), 'radioUDN') + action(0x49) + action(0x12)).jump('done', True)
    c.add(call(member(var('child'), 'id'), 'indexOf', literal('settings-apply')) + literal(0) + action(0x48))
    c.add(call(member(var('child'), 'id'), 'indexOf', literal('settings-action')) + literal(0) + action(0x48) + action(0x10) + action(0x12)).jump('choice', True)
    c.add(member(var('parentNode'), 'bridgeRadioSettings') + action(0x12)).jump('done', True)
    c.add(setmember(var('child'), 'bridgeRadioSettings', literal(True)))
    c.add(member(var('child'), 'dc:title') + literal('Local Radio Location') + action(0x49)).jump('location', True)
    c.jump('done').label('choice')
    c.add(setmember(var('child'), 'upnp:class', literal('settings.item')))
    c.add(setmember(var('child'), 'isContainer', literal(False)))
    c.add(setmember(var('child'), 'decorationClass', literal(0)))
    c.add(setmember(var('child'), 'changeSetting', var('RadioUI_applyChoice'))).jump('done')
    c.label('location').add(setmember(var('child'), 'onOK', var('RadioUI_changeLocation')))
    c.label('done').add(ret(var('child')))
    return c.bytes()


class RadioPatcher(Patcher):
    def __init__(self):
        self.replacements = {'addInternetRadioRootNode': add_root(), 'createCDNode': create_node(),
                             'BrowsePanel_setRadioLocation': allow_zip_menu}
        self.helper_bodies = helpers()
        self.modified, self.definitions = [], []

    def actions(self, data):
        source = records(data); chunks, mapping, branches = [], {}, []
        for record in source:
            mapping[record.start] = sum(map(len, chunks))
            payload, body = record.payload, record.body
            if body is not None:
                if record.name in self.replacements:
                    prefix = b''
                    if record.name == 'createCDNode':
                        prefix = define('RadioUI_originalCreateCDNode', body, ARGS['RadioUI_originalCreateCDNode'])
                        self.definitions.append('RadioUI_originalCreateCDNode')
                    replacement = self.replacements[record.name]
                    body = replacement(body) if callable(replacement) else replacement
                    self.modified.append(record.name)
                    payload = payload[:-2] + u16(len(body))
                    chunk = prefix + (action(record.code, payload + body) if record.body_inline else action(record.code, payload) + body)
                else:
                    body = self.actions(body)
                    payload = payload[:-2] + u16(len(body))
                    chunk = action(record.code, payload + body) if record.body_inline else action(record.code, payload) + body
            elif record.code == -1: chunk = payload
            else:
                chunk = action(record.code, payload)
                if record.code in (0x99, 0x9d): branches.append((len(chunks), record))
            chunks.append(chunk)
        mapping[len(data)] = sum(map(len, chunks))
        for index, record in branches:
            target = record.end + struct.unpack('<h', record.payload)[0]
            if target not in mapping: raise ValueError('Original branch boundary is invalid')
            chunks[index] = action(record.code, struct.pack('<h', mapping[target] - mapping[record.start] - len(chunks[index])))
        if any(record.name == 'addInternetRadioRootNode' for record in source):
            chunks.insert(0, b''.join(define(name, body, ARGS.get(name, ())) for name, body in self.helper_bodies.items()))
            self.definitions.extend(self.helper_bodies)
        return b''.join(chunks)

    def patch(self, source):
        if source[:3] == b'CWS': source = b'FWS' + source[3:8] + zlib.decompress(source[8:])
        if source[:3] != b'FWS' or len(source) != struct.unpack_from('<I', source, 4)[0]:
            raise ValueError('Invalid source SWF')
        bits = source[8] >> 3
        end = 8 + (5 + bits * 4 + 7) // 8 + 4
        output = source[:end] + self.tags(source[end:])
        if sorted(self.modified) != sorted(self.replacements): raise ValueError('Expected exact native Radio functions once each')
        return output[:4] + u32(len(output)) + output[8:]


def patch(source):
    digest = hashlib.sha256(source).hexdigest()
    if digest not in SUPPORTED: raise ValueError('Unknown CR100 SWF; refuse to guess Radio callback compatibility')
    p = RadioPatcher(); output = p.patch(source)
    before, resources_before = inspect(source)
    after, resources_after = inspect(output)
    names = set(p.definitions) | set(p.modified)
    if any(name in p.definitions for name, _ in before): raise ValueError('Radio helper collision')
    if sorted(row for row in before if row[0] not in names) != sorted(row for row in after if row[0] not in names):
        raise ValueError('Unrelated native function changed')
    if resources_before != resources_after: raise ValueError('Unrelated resource changed')
    return output, {'firmware': SUPPORTED[digest], 'input_sha256': digest,
                    'output_sha256': hashlib.sha256(output).hexdigest(),
                    'modified_functions': p.modified, 'new_functions': p.definitions,
                    'scope': 'Copied emulator SWF only; no installed controller modified',
                    'unrelated_functions_and_resources_unchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve(): raise ValueError('Never overwrite original firmware')
    output, receipt = patch(args.input.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output)
    args.output.with_suffix(args.output.suffix + '.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))

if __name__ == '__main__': main()
