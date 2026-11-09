#!/usr/bin/env python3
"""Build a disposable native-speaker-settings prototype from an original CR100 SWF.

No guest image, installed firmware or physical player is modified. The output is
an unpackaged SWF for a copied emulator only, not a signed Sonos update.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import zlib

SUPPORTED = {
    '126c3e9fa13169a3749c835c0ce3c485fb47e34ac7404f63d770943deef6a2ef': '4.3 / 24.1-75101',
    '25bfa76d09396f8922ccadac21061714ef124c9d5088d1ec736ff166517ab1e1': '4.3.2 / 25.2-50130',
}
FUNCTION_ARGUMENTS = {
    'BridgeSettings_lookupRoom': ('udn',),
    'BridgeSettings_getLogicalInteger': ('settingName',),
    'BridgeSettings_setLogicalInteger': ('settingName', 'value'),
}


def u16(value): return struct.pack('<H', value)
def u32(value): return struct.pack('<I', value)
def zstring(value): return value.encode('utf-8') + b'\0'
def action(code, payload=b''):
    return bytes([code]) + (u16(len(payload)) + payload if code >= 0x80 else b'')


def push(value):
    if isinstance(value, bool): data = b'\x05' + bytes([value])
    elif isinstance(value, int): data = b'\x07' + struct.pack('<i', value)
    elif value is None: data = b'\x02'
    elif isinstance(value, str): data = b'\x00' + zstring(value)
    else: raise TypeError(value)
    return action(0x96, data)


def literal(value): return push(value)
def var(name): return push(name) + action(0x1c)
def member(obj, name): return obj + push(name) + action(0x4e)
def call(obj, name, *args):
    return b''.join(reversed(args)) + push(len(args)) + obj + push(name) + action(0x52)
def fncall(name, *args):
    return b''.join(reversed(args)) + push(len(args)) + push(name) + action(0x3d)
def obj(**fields):
    return b''.join(push(key) + value for key, value in fields.items()) + push(len(fields)) + action(0x43)
def array(items): return b''.join(reversed(items)) + push(len(items)) + action(0x42)
def local(name, value): return push(name) + value + action(0x3c)
def setvar(name, value): return push(name) + value + action(0x1d)
def setmember(source, name, value): return source + push(name) + value + action(0x4f)
def discard(value): return value + action(0x17)
def ret(value): return value + action(0x3e)


class Code:
    def __init__(self): self.parts, self.labels = [], {}
    def add(self, code): self.parts.append(code); return self
    def label(self, name): self.parts.append(('label', name)); return self
    def jump(self, name, conditional=False): self.parts.append(('jump', name, conditional)); return self
    def bytes(self):
        pos = 0
        for part in self.parts:
            if isinstance(part, bytes): pos += len(part)
            elif part[0] == 'label': self.labels[part[1]] = pos
            else: pos += 5
        result = bytearray()
        for part in self.parts:
            if isinstance(part, bytes): result += part
            elif part[0] == 'jump':
                distance = self.labels[part[1]] - len(result) - 5
                result += action(0x9d if part[2] else 0x99, struct.pack('<h', distance))
        return bytes(result)


def define(name, body, args=()):
    header = zstring(name) + u16(len(args)) + b''.join(zstring(arg) for arg in args) + u16(len(body))
    return action(0x9b, header) + body


def node(identifier, title, callback, *, container=False, room=None, extra=None):
    fields = {'id': identifier, 'parentId': member(var('this'), 'parentId'),
              'dc:title': title, 'upnp:class': literal('settings.container' if container else 'settings.item'),
              'isContainer': literal(container), 'decorationClass': literal(0),
              'populateChildren' if container else 'changeSetting': var(callback)}
    if room is not None: fields['zpObjUDN'] = room
    fields.update(extra or {})
    return obj(**fields)


def functions():
    body = setmember(var('this'), 'children', array([
        node(literal('@hhSettingsMenu'), var('STR_Setting_HHSettingsMenu'), 'Node_populateHHSettingsMenu', container=True),
        node(literal('@OnlineUpdatesMenu'), literal('Online Updates'), 'Node_updateSoftware',
             extra={'onOnlineUpdateDone': var('Node_onOnlineUpdateDone')}),
        node(literal('@Info'), var('STR_Setting_Info'), 'Node_Info'),
        node(literal('@BridgeSpeakerSettings'), literal('Speaker Settings'), 'BridgeSettings_populateRooms', container=True),
    ])) + ret(literal(True))
    result = {'Node_populateSettings': body}

    # A room can disappear while a local chooser remains open. Never dereference
    # a removed/incompatible native room merely because an old row still exists.
    code = Code().add(local('zp', call(var('Household'), 'lookupZonePlayer', var('udn'))))
    code.add(var('zp') + action(0x12)).jump('unavailable', True)
    code.add(member(var('zp'), 'isCompatible') + action(0x12)).jump('unavailable', True)
    code.add(member(var('zp'), 'invisible')).jump('unavailable', True)
    code.add(ret(var('zp'))).label('unavailable')
    code.add(discard(fncall('showDismissablePopup', literal('Speaker unavailable'), literal(4000))))
    code.add(ret(literal(None)))
    result['BridgeSettings_lookupRoom'] = code.bytes()

    code = Code().add(local('children', array([]))).add(local('ix', literal(0)))
    code.label('loop').add(var('ix') + member(var('Household'), 'totalZonePlayers') + action(0x48) + action(0x12)).jump('done', True)
    code.add(local('zp', call(var('Household'), 'getZonePlayerAt', var('ix'), literal('ANY'))))
    code.add(var('zp') + action(0x12)).jump('next', True)
    code.add(member(var('zp'), 'isCompatible') + action(0x12)).jump('next', True)
    code.add(member(var('zp'), 'invisible')).jump('next', True)
    code.add(local('child', node(literal('@BridgeSpeaker:') + member(var('zp'), 'UDN') + action(0x21),
        member(var('zp'), 'Title'), 'BridgeSettings_populateRoom', container=True, room=member(var('zp'), 'UDN'))))
    code.add(discard(call(var('children'), 'push', var('child'))))
    code.label('next').add(setvar('ix', var('ix') + action(0x50))).jump('loop')
    code.label('done').add(setmember(var('this'), 'children', var('children'))).add(ret(literal(True)))
    result['BridgeSettings_populateRooms'] = code.bytes()

    room = member(var('this'), 'zpObjUDN')
    entries = []
    for title, key in (('Bass', 'Bass/Master'), ('Treble', 'Treble/Master')):
        entries.append(node(literal('@Bridge' + title), literal(title), 'BridgeSettings_adjustInteger', room=room,
            extra={'settingName': literal(key), 'minVal': literal(-10), 'maxVal': literal(10),
                   'minLabel': literal('-10'), 'maxLabel': literal('+10')}))
    entries += [node(literal('@Loudness'), literal('Loudness'), 'BridgeSettings_loudness', room=room),
                node(literal('@ZPLedMenu'), literal('Status Light'), 'BridgeSettings_led', room=room)]
    code = Code().add(local('zp', fncall('BridgeSettings_lookupRoom', member(var('this'), 'zpObjUDN'))))
    code.add(var('zp') + action(0x12)).jump('unavailable', True)
    code.add(setmember(var('this'), 'children', array(entries))).add(ret(literal(True)))
    code.label('unavailable').add(setmember(var('this'), 'children', array([]))).add(ret(literal(True)))
    result['BridgeSettings_populateRoom'] = code.bytes()

    code = Code().add(local('zp', fncall('BridgeSettings_lookupRoom', member(var('this'), 'zpObjUDN'))))
    code.add(var('zp') + action(0x12)).jump('blocked', True)
    code.add(fncall('BrowsePanel_equalizationBlocked', var('this'))).jump('blocked', True)
    code.add(local('rc', member(var('zp'), 'RenderingControl')))
    # RenderingControl's original integer widget API uses twice the actual
    # Bass/Treble value. Keep the new UI in physical -10..+10 units instead.
    code.add(local('logical', obj(rc=var('rc'), getValue=var('BridgeSettings_getLogicalInteger'),
                                setValue=var('BridgeSettings_setLogicalInteger'))))
    code.add(local('proxy', obj(dataSrc=var('logical'), settingName=member(var('this'), 'settingName'),
        minVal=member(var('this'), 'minVal'), maxVal=member(var('this'), 'maxVal'),
        minLabel=member(var('this'), 'minLabel'), maxLabel=member(var('this'), 'maxLabel'),
        setVal=literal('setValue'), getVal=literal('getValue'), closeController=var('rc'),
        closeMethodName=literal('flshIntSetting'))))
    code.add(discard(call(var('RinconHWMon'), 'setScrollWheelParams', literal(2), literal(1), literal(0))))
    code.add(discard(call(var('g_PanelMgr'), 'createSettingsBar', member(var('this'), 'dc:title'),
        literal('CenteredSlider'), var('proxy'))))
    code.label('blocked').add(ret(literal(None)))
    result['BridgeSettings_adjustInteger'] = code.bytes()
    result['BridgeSettings_getLogicalInteger'] = ret(
        call(member(var('this'), 'rc'), 'getIntSetting', var('settingName')) + literal(2) + action(0x0d))
    result['BridgeSettings_setLogicalInteger'] = ret(
        call(member(var('this'), 'rc'), 'setIntSetting', var('settingName'), var('value') + literal(2) + action(0x0c)))
    for name, source, key in (('BridgeSettings_loudness', 'RenderingControl', 'Loudness'),
                              ('BridgeSettings_led', 'DeviceProperties', 'ZPLedMenu')):
        code = Code().add(local('zp', fncall('BridgeSettings_lookupRoom', member(var('this'), 'zpObjUDN'))))
        code.add(var('zp') + action(0x12)).jump('blocked', True)
        if key == 'Loudness':
            code.add(fncall('BrowsePanel_equalizationBlocked', var('this'))).jump('blocked', True)
        code.add(discard(call(var('g_PanelMgr'), 'createSettingsMenu', member(var('zp'), source), literal(key),
                             literal(None), literal('setSettingsMenuTitle'), var('g_BrowsePanel'))))
        code.label('blocked').add(ret(literal(None)))
        result[name] = code.bytes()
    return result


@dataclass
class Record:
    start: int
    end: int
    code: int
    payload: bytes
    body: bytes | None = None
    body_inline: bool = False
    name: str | None = None


def read_zstring(data, start):
    end = data.index(0, start)
    return data[start:end].decode('utf-8'), end + 1


def records(data):
    result, pos = [], 0
    while pos < len(data):
        start = pos; code = data[pos]; pos += 1
        length = struct.unpack_from('<H', data, pos)[0] if code >= 128 else 0
        if code >= 128: pos += 2
        payload = data[pos:pos + length]; pos += length
        if len(payload) != length: raise ValueError('Truncated action')
        body, inline, name = None, False, None
        if code in (0x9b, 0x8e):
            name, offset = read_zstring(payload, 0)
            count = struct.unpack_from('<H', payload, offset)[0]; offset += 2
            if code == 0x8e: offset += 3
            for _ in range(count):
                if code == 0x8e: offset += 1
                _, offset = read_zstring(payload, offset)
            size = struct.unpack_from('<H', payload, offset)[0]; offset += 2
            inline = len(payload) > offset
            if inline:
                body = payload[offset:offset + size]
                payload = payload[:offset]
            else:
                body = data[pos:pos + size]; pos += size
            if len(body) != size: raise ValueError('Truncated function')
        result.append(Record(start, pos, code, payload, body, inline, name))
        if code == 0:
            if pos != len(data): result.append(Record(pos, len(data), -1, data[pos:]))
            break
    return result


class Patcher:
    def __init__(self):
        self.replacements = functions()
        self.modified, self.definitions = [], []

    def actions(self, data):
        source = records(data); chunks = []; mapping = {}; patches = []
        for record in source:
            mapping[record.start] = sum(map(len, chunks))
            payload, body = record.payload, record.body
            if body is not None:
                if record.name == 'Node_populateSettings':
                    body = self.replacements[record.name]
                    self.modified.append(record.name)
                else: body = self.actions(body)
                payload = payload[:-2] + u16(len(body))
                chunk = action(record.code, payload + body) if record.body_inline else action(record.code, payload) + body
            elif record.code == -1: chunk = payload
            else:
                chunk = action(record.code, payload)
                if record.code in (0x99, 0x9d): patches.append((len(chunks), record))
            chunks.append(chunk)
        mapping[len(data)] = sum(map(len, chunks))
        for index, record in patches:
            target = record.end + struct.unpack('<h', record.payload)[0]
            if target not in mapping: raise ValueError('Branch target is not an action boundary')
            delta = mapping[target] - mapping[record.start] - len(chunks[index])
            chunks[index] = action(record.code, struct.pack('<h', delta))
        # Helpers are defined in the same action scope as the original function.
        if any(record.name == 'Node_populateSettings' for record in source):
            helpers = b''.join(define(name, body, FUNCTION_ARGUMENTS.get(name, ())) for name, body in self.replacements.items()
                               if name != 'Node_populateSettings')
            chunks.insert(0, helpers)
            self.definitions.extend(name for name in self.replacements if name != 'Node_populateSettings')
        return b''.join(chunks)

    def tags(self, data):
        result, pos = bytearray(), 0
        while pos < len(data):
            start = pos
            header = struct.unpack_from('<H', data, pos)[0]; pos += 2
            code, size = header >> 6, header & 63
            if size == 63: size = struct.unpack_from('<I', data, pos)[0]; pos += 4
            payload = data[pos:pos + size]; pos += size
            if len(payload) != size: raise ValueError('Truncated SWF tag')
            original = payload
            if code == 12: payload = self.actions(payload)
            elif code == 59: payload = payload[:2] + self.actions(payload[2:])
            elif code == 39: payload = payload[:4] + self.tags(payload[4:])
            if original == payload: result += data[start:pos]
            else:
                result += u16((code << 6) | 63) + u32(len(payload)) + payload
        return bytes(result)

    def patch(self, source):
        if source[:3] == b'CWS': source = b'FWS' + source[3:8] + zlib.decompress(source[8:])
        if source[:3] != b'FWS': raise ValueError('Expected an uncompressed/compressed SWF')
        if len(source) != struct.unpack_from('<I', source, 4)[0]: raise ValueError('Original SWF length mismatch')
        bits = source[8] >> 3
        header_end = 8 + (5 + bits * 4 + 7) // 8 + 4
        result = source[:header_end] + self.tags(source[header_end:])
        if self.modified != ['Node_populateSettings']: raise ValueError('Expected one exact original settings function')
        return result[:4] + u32(len(result)) + result[8:]


def inspect(source):
    """Independently walk rebuilt lengths, function bodies and jump boundaries."""
    named, opaque = [], []
    def actions(data):
        found = records(data)
        boundaries = {record.start for record in found} | {len(data)}
        for record in found:
            if record.code in (0x99, 0x9d):
                target = record.end + struct.unpack('<h', record.payload)[0]
                if target not in boundaries: raise ValueError('Invalid rebuilt jump boundary')
            if record.body is not None:
                if record.name:
                    named.append((record.name, hashlib.sha256(record.body).hexdigest()))
                actions(record.body)
    def tags(data):
        pos = 0
        while pos < len(data):
            header = struct.unpack_from('<H', data, pos)[0]; pos += 2
            code, size = header >> 6, header & 63
            if size == 63: size = struct.unpack_from('<I', data, pos)[0]; pos += 4
            payload = data[pos:pos + size]; pos += size
            if len(payload) != size: raise ValueError('Invalid rebuilt tag length')
            if code == 12: actions(payload)
            elif code == 59: actions(payload[2:])
            elif code == 39: tags(payload[4:])
            else: opaque.append((code, hashlib.sha256(payload).hexdigest()))
    if source[:3] != b'FWS' or len(source) != struct.unpack_from('<I', source, 4)[0]:
        raise ValueError('Invalid rebuilt header length')
    bits = source[8] >> 3
    tags(source[8 + (5 + bits * 4 + 7) // 8 + 4:])
    return named, opaque


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve(): raise ValueError('Refuse to overwrite original SWF')
    original = args.input.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest not in SUPPORTED: raise ValueError('Unknown input firmware; refuse to guess callback compatibility')
    patcher = Patcher(); output = patcher.patch(original)
    # Reparse every action/function/tag after rebuilding, with no mutation.
    before, opaque_before = inspect(original)
    after, opaque_after = inspect(output)
    helpers = set(patcher.definitions)
    if any(name in helpers for name, _ in before): raise ValueError('Prototype helper name collision')
    unchanged_before = sorted(row for row in before if row[0] != 'Node_populateSettings')
    unchanged_after = sorted(row for row in after if row[0] != 'Node_populateSettings' and row[0] not in helpers)
    if unchanged_before != unchanged_after: raise ValueError('An unrelated named function changed')
    if opaque_before != opaque_after: raise ValueError('An unrelated resource tag changed')
    if any(sum(name == helper for name, _ in after) != 1 for helper in helpers):
        raise ValueError('Missing or duplicate prototype helper')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output)
    receipt = {'input': str(args.input.resolve()), 'output': str(args.output.resolve()),
               'input_sha256': digest, 'firmware': SUPPORTED[digest],
               'output_sha256': hashlib.sha256(output).hexdigest(),
               'input_bytes': len(original), 'output_bytes': len(output),
               'modified_function': patcher.modified, 'new_functions': patcher.definitions,
               'scope': 'Disposable emulator SWF only; not a signed firmware package',
               'integer_units': 'UI -10..+10; getIntSetting/2 and setIntSetting(value*2)',
               'room_safety': 'Callbacks verify room still exists, is compatible and is visible',
               'verification': {'named_functions_before': len(before), 'named_functions_after': len(after),
                                'unrelated_named_functions_unchanged': True,
                                'all_non_action_resource_tags_unchanged': True,
                                'function_tag_lengths_and_branch_boundaries_valid': True},
               'menu': 'Music > System Settings > Speaker Settings > room > Bass/Treble/Loudness/Status Light'}
    args.output.with_suffix(args.output.suffix + '.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__': main()
