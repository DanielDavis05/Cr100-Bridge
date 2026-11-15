"""Preview of a controller's Music menu: the user's own MainUI.swf, run by Ruffle in the web UI.

The bridge ships no Sonos file. From a controller firmware (.upd) the user uploaded on the
Firmware panel it reads opt/swf/MainUI.swf, the device fonts in opt/fonts and the English
string tables opt/resources/{shared,cr100}/en-US.xml out of the image's root filesystem, and
builds a preview copy in the bridge's local state (never in the repository or a release):

* The strings, then our precompiled ActionScript host layer (controller_preview_host/), are
  injected as root DoActions in front of the first original one, and a small hook before the
  first frame ends. Every original tag survives byte-for-byte; nothing else changes.
* Each font gets a name table that Ruffle matches to the SWF's device-font name, with a
  bold-flagged copy where the SWF asks for bold.

Only MainUI builds whose SHA-256 is listed in SUPPORTED are used: the host layer was checked
against those. The web UI feeds the result SMAPI from the bridge's own Spotify service
(webui.py, /api/spotify/preview/...).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import threading
import xml.etree.ElementTree as ET

import patch_controller_settings_ui as avm1
from online_updates import CramFS, upd_rootfs

HOST_FILE = Path(__file__).resolve().parent / 'controller_preview_host' / 'host-actions.json'
BUILD_FORMAT = 1
SUPPORTED = {
    '25bfa76d09396f8922ccadac21061714ef124c9d5088d1ec736ff166517ab1e1': '4.3.2',
}
SUPPORTED_LABEL = '4.3.2'
MAINUI = 'opt/swf/MainUI.swf'
STRING_TABLES = ('opt/resources/shared/en-US.xml', 'opt/resources/cr100/en-US.xml')
# (firmware file, device-font name used by MainUI.swf, bold); see opt/fonts/swffontmap.txt.
FONT_FACES = (
    ('stan0755.ttf', 'standard 07_55', False),
    ('stan0765.ttf', 'standard 07_65', False),
    ('stan0765.ttf', 'standard 07_65', True),
    ('stan0955.ttf', 'standard 09_55', False),
    ('stan0965.ttf', 'standard 09_65', False),
    ('stan0965.ttf', 'standard 09_65', True),
    ('luxisr.ttf', 'Luxi Sans', False),
    ('luxisb.ttf', 'Luxi Sans', True),
)
FONT_LIMIT = 512 * 1024
STRINGS_LIMIT = 1024 * 1024
MOVIE_LIMIT = 4 * 1024 * 1024
KEY = re.compile(r'[0-9a-f]{24}\Z')
FILE_NAME = re.compile(r'(?:MainUI\.swf|fonts/[a-z0-9_-]{1,40}\.ttf)\Z')
STRING_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')


class PreviewError(ValueError):
    """The firmware cannot produce a preview; the message is safe to show."""


def host_actions(path=HOST_FILE):
    """The compiled host layer: {'bootstrap': bytes, 'hooks': bytes, 'sources_sha256': str}."""
    data = json.loads(Path(path).read_text())
    if data.get('format') != 1 or data.get('swf_version') != 6:
        raise ValueError('Unknown controller preview host format')
    result = {'sources_sha256': data['sources_sha256']}
    for field in ('bootstrap', 'hooks'):
        payload = bytes.fromhex(data[field])
        if not payload or payload[-1] != 0:
            raise ValueError('Controller preview host actions must end with ActionEnd')
        avm1.records(payload)
        result[field] = payload
    return result


def font_file_name(name, bold):
    return re.sub(r'[^a-z0-9_]+', '-', name.lower()).strip('-') + ('-bold' if bold else '') + '.ttf'


# --- firmware assets ------------------------------------------------------------------
def firmware_assets(rootfs):
    """MainUI.swf, the font files and the string tables of one root filesystem image."""
    image = CramFS(rootfs)
    movie = image.read(MAINUI, MOVIE_LIMIT)
    fonts = {name: image.read('opt/fonts/' + name, FONT_LIMIT) for name, _, _ in FONT_FACES}
    strings = [image.read(path, STRINGS_LIMIT) for path in STRING_TABLES]
    return {'movie': movie, 'fonts': fonts, 'strings': strings}


def string_values(*tables):
    """id -> text for every <string id=...> in the firmware's resource files, later files win."""
    values = {}
    for raw in tables:
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise PreviewError('The firmware string table has an unexpected format.')
        for entry in ET.fromstring(raw):
            ident, text = entry.attrib.get('id'), ''.join(entry.itertext())
            if isinstance(ident, str) and STRING_ID.fullmatch(ident) and '\0' not in text:
                values[ident] = text
    if not values:
        raise PreviewError('The firmware has no English interface strings.')
    return values


def strings_action(values):
    """One DoAction that sets each string as a _global, as the native host does."""
    return b''.join(avm1.setmember(avm1.var('_global'), key, avm1.push(value))
                    for key, value in values.items()) + b'\0'


def device_font(data, name, bold=False):
    """A copy of a TrueType font whose names (and bold flags) match a SWF device-font name.

    Ruffle registers fonts from fontSources under their own full name and style, so the
    firmware's luxisr.ttf would otherwise be "Luxi Sans Regular" and miss.
    """
    try:
        count = struct.unpack_from('>H', data, 4)[0]
        tables = {}
        for index in range(count):
            tag, _, offset, length = struct.unpack_from('>4sIII', data, 12 + 16 * index)
            if offset + length > len(data):
                raise PreviewError('A firmware font is damaged.')
            tables[tag] = (index, offset, length)
        if b'name' not in tables or b'head' not in tables or (bold and b'OS/2' not in tables):
            raise PreviewError('A firmware font is missing a required table.')
    except struct.error:
        raise PreviewError('A firmware font is damaged.') from None
    records = []
    for name_id, value in ((1, name), (2, 'Bold' if bold else 'Regular'), (4, name), (6, name.replace(' ', '-'))):
        records.append((1, 0, 0, name_id, value.encode('latin-1')))
        records.append((3, 1, 0x409, name_id, value.encode('utf-16-be')))
    records.sort()
    header, strings = b'', b''
    for platform, encoding, language, name_id, value in records:
        header += struct.pack('>6H', platform, encoding, language, name_id, len(value), len(strings))
        strings += value
    table = struct.pack('>3H', 0, len(records), 6 + 12 * len(records)) + header + strings
    result = bytearray(data)
    result += bytes(-len(result) % 4)
    struct.pack_into('>4sIII', result, 12 + 16 * tables[b'name'][0], b'name', 0, len(result), len(table))
    result += table
    if bold:
        head = tables[b'head'][1]
        struct.pack_into('>H', result, head + 44, struct.unpack_from('>H', result, head + 44)[0] | 1)
        os2 = tables[b'OS/2'][1]
        selection = struct.unpack_from('>H', result, os2 + 62)[0]
        struct.pack_into('>H', result, os2 + 62, (selection | 0x20) & ~0x40)
    return bytes(result)


def _tags(data):
    position = 0
    while position < len(data):
        start = position
        header = struct.unpack_from('<H', data, position)[0]
        position += 2
        code, size = header >> 6, header & 63
        if size == 63:
            size = struct.unpack_from('<I', data, position)[0]
            position += 4
        payload = data[position:position + size]
        position += size
        if len(payload) != size:
            raise PreviewError('The firmware interface file is damaged.')
        yield code, data[start:position]


def _tag(code, payload):
    return struct.pack('<HI', (code << 6) | 63, len(payload)) + payload


def inject(movie, strings, host):
    """The movie with strings + host before the first DoAction and the hook before frame 1 ends."""
    if movie[:4] != b'FWS\x06' or struct.unpack_from('<I', movie, 4)[0] != len(movie):
        raise PreviewError('The firmware interface file is not the expected Flash 6 movie.')
    end = 8 + (5 + (movie[8] >> 3) * 4 + 7) // 8 + 4
    original = list(_tags(movie[end:]))
    result = bytearray(movie[:end])
    bootstrap, hooks = False, False
    for code, raw in original:
        if code == 12 and not bootstrap:
            result += _tag(12, strings) + _tag(12, host['bootstrap'])
            bootstrap = True
        if code == 1 and bootstrap and not hooks:
            result += _tag(12, host['hooks'])
            hooks = True
        result += raw
    if not (bootstrap and hooks):
        raise PreviewError('The firmware interface file has no first-frame script.')
    result[4:8] = struct.pack('<I', len(result))
    result = bytes(result)
    avm1.inspect(result)
    # Injection only: every original tag survives, in order and byte-for-byte.
    remaining = iter(raw for _, raw in _tags(result[end:]))
    if not all(any(candidate == raw for candidate in remaining) for _, raw in original):
        raise PreviewError('Building the preview changed an original tag.')
    return result


def build(rootfs, host):
    """{'movie', 'fonts' {file name: bytes}, 'firmware', 'mainui_sha256'} for one root filesystem."""
    try:
        assets = firmware_assets(rootfs)
    except ValueError as error:
        raise PreviewError('The firmware has no readable controller interface (%s).' % error) from None
    digest = hashlib.sha256(assets['movie']).hexdigest()
    if digest not in SUPPORTED:
        raise PreviewError('The uploaded firmware\'s controller interface is not one the preview supports '
                           '(it supports %s).' % SUPPORTED_LABEL)
    movie = inject(assets['movie'], strings_action(string_values(*assets['strings'])), host)
    fonts = {font_file_name(name, bold): device_font(assets['fonts'][source], name, bold)
             for source, name, bold in FONT_FACES}
    return {'movie': movie, 'fonts': fonts, 'firmware': SUPPORTED[digest], 'mainui_sha256': digest}


# --- the bridge's preview cache --------------------------------------------------------
class PreviewStore:
    """Built previews in the bridge's local state, one per supported MainUI build.

    `packages` lists the stored uploads as (mac, sha256, path), newest first
    (online_updates.UpdateManager.packages). An upload is read once; the index remembers
    which preview it produced, or why it produced none.
    """
    def __init__(self, directory, packages, host_file=HOST_FILE, log=print):
        self.directory, self.packages, self.host_file, self.log = Path(directory), packages, host_file, log
        self.lock = threading.Lock()
        self._host = None

    def host(self):
        if self._host is None:
            self._host = host_actions(self.host_file)
        return self._host

    def _key(self, mainui_sha256):
        material = '%s:%s:%d' % (mainui_sha256, self.host()['sources_sha256'], BUILD_FORMAT)
        return hashlib.sha256(material.encode()).hexdigest()[:24]

    def _index(self):
        try:
            index = json.loads((self.directory / 'index.json').read_text())
            return index if isinstance(index, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path, data):
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
            os.replace(temporary, path)
        except BaseException:
            try: os.unlink(temporary)
            except OSError: pass
            raise

    def _store(self, built):
        key = self._key(built['mainui_sha256'])
        target = self.directory / key
        if (target / 'receipt.json').is_file():
            return key
        staging = Path(tempfile.mkdtemp(prefix='.build-', dir=self.directory))
        try:
            (staging / 'fonts').mkdir()
            (staging / 'MainUI.swf').write_bytes(built['movie'])
            for name, data in built['fonts'].items():
                (staging / 'fonts' / name).write_bytes(data)
            receipt = {'firmware': built['firmware'], 'mainui_sha256': built['mainui_sha256'],
                       'fonts': sorted(built['fonts']), 'format': BUILD_FORMAT}
            (staging / 'receipt.json').write_text(json.dumps(receipt, indent=1) + '\n')
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return key

    def _entry(self, index, sha256, path):
        entry = index.get(sha256)
        if isinstance(entry, dict) and entry.get('host') == self.host()['sources_sha256']:
            if entry.get('key') is None or (self.directory / entry['key'] / 'receipt.json').is_file():
                return entry
        try:
            from online_updates import _read_upload
            built = build(upd_rootfs(_read_upload(path)), self.host())
            entry = {'key': self._store(built), 'firmware': built['firmware']}
        except (PreviewError, ValueError, OSError) as error:
            entry = {'key': None, 'reason': str(error) if isinstance(error, PreviewError)
                     else 'The uploaded firmware could not be read for a preview.'}
            self.log({'event': 'controller-preview-unavailable', 'upload': sha256[:12], 'error': str(error)[:200]})
        entry['host'] = self.host()['sources_sha256']
        index[sha256] = entry
        return entry

    def status(self, mac=None):
        """The preview to show for a controller's menu: the controller's own upload first."""
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            packages = list(self.packages())
            packages.sort(key=lambda item: item[0] != mac)
            index, before = self._index(), None
            before = json.dumps(index, sort_keys=True)
            found, reason = None, None
            for _, sha256, path in packages:
                entry = self._entry(index, sha256, path)
                if entry.get('key'):
                    found = entry
                    break
                reason = reason or entry.get('reason')
            # Forget uploads that are gone, and previews nothing points to any more.
            current = {sha256 for _, sha256, _ in packages}
            for sha256 in [s for s in index if s not in current]:
                del index[sha256]
            if json.dumps(index, sort_keys=True) != before:
                self._write(self.directory / 'index.json', (json.dumps(index, indent=1, sort_keys=True) + '\n').encode())
            keep = {entry.get('key') for entry in index.values()}
            for child in self.directory.iterdir():
                if child.is_dir() and KEY.fullmatch(child.name) and child.name not in keep:
                    shutil.rmtree(child, ignore_errors=True)
        if found is None:
            return {'available': False, 'reason': reason or (
                'Upload controller firmware %s on a controller\'s Firmware panel to preview its own '
                'interface here.' % SUPPORTED_LABEL)}
        receipt = json.loads((self.directory / found['key'] / 'receipt.json').read_text())
        return {'available': True, 'key': found['key'], 'firmware': found['firmware'], 'fonts': receipt['fonts']}

    def path(self, key, name):
        """A built preview file, or None."""
        if not KEY.fullmatch(key or '') or not FILE_NAME.fullmatch(name or ''):
            return None
        path = self.directory / key / name
        return path if path.is_file() and not path.is_symlink() else None
