"""Controller preview: firmware extraction, hash gate, injection, fonts, cache and SMAPI identity.

Every firmware input here is invented: a tiny CramFS with a stand-in Flash 6 movie, fonts and
string tables. No Sonos file is read or needed.
"""
import hashlib
import http.client
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

import controller_preview as preview
import online_updates
import patch_controller_settings_ui as avm1
from smapi_service import PREVIEW_HEADER
from spotify_menu import default_items, scoped_id
from spotify_service import SpotifyService
from test_online_updates import build_test_package

CR1 = '00:0e:58:12:34:56'
CR2 = '00:0e:58:02:6f:aa'


def build_cramfs(files):
    """A real CramFS image holding `files` ({'a/b/c': bytes}); directories are implied."""
    tree = {}
    for path, data in files.items():
        node = tree
        parts = path.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data
    image = bytearray(76)

    def entries(node):
        # Lay out one directory's entries, then each child's content; return (offset, size).
        # CramFS stores offsets in 4-byte units, so every directory and file starts aligned.
        image.extend(bytes(-len(image) % 4))
        start = len(image)
        slots = []
        for name, child in node.items():
            raw = name.encode()
            padded = (len(raw) + 3) // 4 * 4
            mode = (stat.S_IFDIR | 0o755) if isinstance(child, dict) else (stat.S_IFREG | 0o644)
            slots.append((len(image), mode, child, padded))
            image.extend(struct.pack('<3I', mode, 0, padded // 4) + raw.ljust(padded, b'\0'))
        size = len(image) - start
        for position, mode, child, padded in slots:
            if isinstance(child, dict):
                offset, length = entries(child)
            else:
                image.extend(bytes(-len(image) % 4))
                offset, length = len(image), len(child)
                blocks = [zlib.compress(child[n:n + 4096]) for n in range(0, len(child), 4096)]
                image.extend(bytes(4 * len(blocks)))
                for index, block in enumerate(blocks):
                    image.extend(block)
                    struct.pack_into('<I', image, offset + index * 4, len(image))
            struct.pack_into('<2I', image, position + 4, length, (offset // 4 << 6) | (padded // 4))
        return start, size

    offset, size = entries(tree)
    struct.pack_into('<3I', image, 64, stat.S_IFDIR | 0o755, size, offset // 4 << 6)
    struct.pack_into('<4I', image, 0, 0x28cd3d45, len(image), 3, 0)
    image[16:32] = b'Compressed ROMFS'
    struct.pack_into('<I', image, 32, zlib.crc32(image) & 0xffffffff)
    return bytes(image)


def swf_tag(code, payload):
    return struct.pack('<HI', (code << 6) | 63, len(payload)) + payload


def build_movie(marker=b'stock'):
    """A stand-in Flash 6 movie: a shape-like opaque tag, the first DoAction, ShowFrame, End."""
    rect = bytes([0x78, 0x00, 0x05, 0x5f, 0x00, 0x00, 0x0f, 0xa0, 0x00])
    body = (rect + struct.pack('<HH', 12 << 8, 1) + swf_tag(9, b'\x01\x02\x03')
            + swf_tag(12, avm1.setvar('stock', avm1.push(marker.decode())) + b'\0')
            + swf_tag(43, b'Browse\0') + swf_tag(1, b'') + swf_tag(12, b'\0') + swf_tag(1, b'') + swf_tag(0, b''))
    return b'FWS\x06' + struct.pack('<I', 8 + len(body)) + body


def build_font(full_name='Original Regular'):
    """A minimal TrueType table directory with name, head and OS/2 tables."""
    name = struct.pack('>3H', 0, 1, 18) + struct.pack('>6H', 3, 1, 0x409, 4, len(full_name) * 2, 0) \
        + full_name.encode('utf-16-be')
    tables = [(b'OS/2', bytes(96)), (b'head', bytes(54)), (b'name', name)]
    data = bytearray(struct.pack('>I4H', 0x00010000, len(tables), 0, 0, 0))
    offset = 12 + 16 * len(tables)
    blobs = b''
    for tag, blob in tables:
        data += struct.pack('>4sIII', tag, 0, offset + len(blobs), len(blob))
        blobs += blob + bytes(-len(blob) % 4)
    return bytes(data + blobs)


def build_strings(group):
    rows = {'shared': [('STR_Search', 'Search'), ('STR_GenericError', 'Something went wrong')],
            'cr100': [('STR_Music', 'Music'), ('RHHSTR_Rooms', 'Rooms')]}[group]
    return ('<?xml version="1.0" encoding="utf-8"?><strings>%s</strings>'
            % ''.join('<string id="%s">%s</string>' % row for row in rows)).encode()


def build_rootfs(movie=None, **overrides):
    files = {'opt/swf/MainUI.swf': movie or build_movie(),
             'opt/resources/shared/en-US.xml': build_strings('shared'),
             'opt/resources/cr100/en-US.xml': build_strings('cr100'),
             'opt/resources/cr100/de-DE.xml': b'<strings/>'}
    for source, _, _ in preview.FONT_FACES:
        files['opt/fonts/' + source] = build_font()
    files.update(overrides)
    return build_cramfs(files)


def name_records(font):
    count = struct.unpack_from('>H', font, 4)[0]
    tables = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack_from('>4sIII', font, 12 + 16 * index)
        tables[tag] = (offset, length)
    offset, _ = tables[b'name']
    _, total, strings = struct.unpack_from('>3H', font, offset)
    found = {}
    for index in range(total):
        platform, _, _, name_id, length, start = struct.unpack_from('>6H', font, offset + 6 + 12 * index)
        raw = font[offset + strings + start:offset + strings + start + length]
        if platform == 3:
            found[name_id] = raw.decode('utf-16-be')
    return found, tables


def supported(movie):
    return patch.dict(preview.SUPPORTED, {hashlib.sha256(movie).hexdigest(): 'test build'})


class CramFSReader(unittest.TestCase):
    def test_reads_exact_paths_lists_directories_and_keeps_mainui_reader(self):
        image = build_rootfs()
        fs = online_updates.CramFS(image)
        self.assertEqual(fs.read('opt/swf/MainUI.swf'), build_movie())
        self.assertEqual(online_updates._mainui_from_cramfs(image), build_movie())
        self.assertEqual(sorted(fs.listdir('opt/resources/cr100')), ['de-DE.xml', 'en-US.xml'])
        large = bytes(range(256)) * 64
        self.assertEqual(online_updates.CramFS(build_cramfs({'a/big.bin': large})).read('a/big.bin'), large)

    def test_missing_directory_and_oversized_files_are_refused(self):
        fs = online_updates.CramFS(build_rootfs())
        for path in ('opt/swf/Missing.swf', 'opt/missing/x', 'opt/swf'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                fs.read(path)
        with self.assertRaises(ValueError):
            fs.read('opt/swf/MainUI.swf', limit=10)

    def test_checksum_and_format_are_verified_once(self):
        image = bytearray(build_rootfs())
        image[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            online_updates.CramFS(bytes(image))
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            online_updates.CramFS(b'not a filesystem' * 8)

    def test_upd_rootfs_returns_the_single_filesystem_record(self):
        rootfs = build_rootfs()
        package, _ = build_test_package(rootfs=rootfs)
        self.assertEqual(online_updates.upd_rootfs(package), rootfs)
        with self.assertRaises(online_updates.UpdateError):
            online_updates.upd_rootfs(package[:40])


class PreviewBuild(unittest.TestCase):
    def setUp(self):
        self.host = preview.host_actions()

    def test_checked_in_host_actions_match_their_actionscript_sources(self):
        data = json.loads(preview.HOST_FILE.read_text())
        digest = hashlib.sha256()
        for name in ('bootstrap.as', 'music.as', 'hooks.as'):
            digest.update(name.encode() + b'\0' + (preview.HOST_FILE.parent / name).read_bytes() + b'\0')
        self.assertEqual(data['sources_sha256'], digest.hexdigest(),
                         'Run tools/controller_preview_host/build_host.py after editing the .as files')
        for field in ('bootstrap', 'hooks'):
            self.assertEqual(self.host[field][-1], 0)
            avm1.records(self.host[field])

    def test_only_supported_mainui_builds_are_used(self):
        self.assertEqual(preview.SUPPORTED, {
            '25bfa76d09396f8922ccadac21061714ef124c9d5088d1ec736ff166517ab1e1': '4.3.2'})
        with self.assertRaisesRegex(preview.PreviewError, 'supports 4.3.2'):
            preview.build(build_rootfs(), self.host)

    def test_injection_adds_strings_host_and_hook_and_keeps_every_original_tag(self):
        movie = build_movie()
        with supported(movie):
            built = preview.build(build_rootfs(movie), self.host)
        output = built['movie']
        self.assertEqual((built['firmware'], built['mainui_sha256']), ('test build', hashlib.sha256(movie).hexdigest()))
        self.assertEqual(struct.unpack_from('<I', output, 4)[0], len(output))
        end = 8 + (5 + (movie[8] >> 3) * 4 + 7) // 8 + 4
        self.assertEqual(output[:4], movie[:4])
        self.assertEqual(output[8:end], movie[8:end])
        original = [raw for _, raw in preview._tags(movie[end:])]
        result = [raw for _, raw in preview._tags(output[end:])]
        values = preview.string_values(build_strings('shared'), build_strings('cr100'))
        strings = swf_tag(12, preview.strings_action(values))
        # Opaque tag, strings, host, original first DoAction, FrameLabel, hook, ShowFrame, ...
        self.assertEqual(result, [original[0], strings, swf_tag(12, self.host['bootstrap']), original[1], original[2],
                                  swf_tag(12, self.host['hooks'])] + original[3:])
        self.assertIn(b'STR_GenericError\0', strings)
        self.assertIn(b'Something went wrong\0', strings)

    def test_movies_without_a_first_frame_script_or_flash_6_header_are_refused(self):
        movie = build_movie()
        compressed = b'CWS' + movie[3:]
        no_script = movie.replace(swf_tag(12, avm1.setvar('stock', avm1.push('stock')) + b'\0'), b'')
        no_script = no_script[:4] + struct.pack('<I', len(no_script)) + no_script[8:]
        no_script = no_script.replace(swf_tag(12, b'\0'), b'')
        no_script = no_script[:4] + struct.pack('<I', len(no_script)) + no_script[8:]
        for candidate in (compressed, no_script):
            with self.subTest(candidate=candidate[:3]), self.assertRaises(preview.PreviewError):
                preview.inject(candidate, b'\0', self.host)

    def test_string_tables_reject_entities_and_skip_unusable_ids(self):
        with self.assertRaises(preview.PreviewError):
            preview.string_values(b'<!DOCTYPE x [<!ENTITY a "b">]><strings/>')
        values = preview.string_values(b'<strings><string id="OK_1">a</string><string id="bad id">b</string>'
                                       b'<string>c</string></strings>', b'<s><string id="OK_1">later</string></s>')
        self.assertEqual(values, {'OK_1': 'later'})

    def test_fonts_are_renamed_to_the_device_font_names_with_bold_copies(self):
        movie = build_movie()
        with supported(movie):
            fonts = preview.build(build_rootfs(movie), self.host)['fonts']
        self.assertEqual(sorted(fonts), sorted(preview.font_file_name(name, bold) for _, name, bold in preview.FONT_FACES))
        names, tables = name_records(fonts['luxi-sans-bold.ttf'])
        self.assertEqual((names[1], names[2], names[4]), ('Luxi Sans', 'Bold', 'Luxi Sans'))
        font = fonts['luxi-sans-bold.ttf']
        self.assertEqual(struct.unpack_from('>H', font, tables[b'head'][0] + 44)[0] & 1, 1)
        self.assertEqual(struct.unpack_from('>H', font, tables[b'OS/2'][0] + 62)[0] & 0x60, 0x20)
        names, tables = name_records(fonts['standard-07_55.ttf'])
        self.assertEqual((names[4], names[2]), ('standard 07_55', 'Regular'))
        self.assertEqual(struct.unpack_from('>H', fonts['standard-07_55.ttf'], tables[b'head'][0] + 44)[0] & 1, 0)

    def test_damaged_fonts_are_refused(self):
        with self.assertRaises(preview.PreviewError):
            preview.device_font(b'\0\1\0\0\0\1' + bytes(10) + struct.pack('>4sIII', b'name', 0, 1 << 20, 4), 'X')
        with self.assertRaises(preview.PreviewError):
            preview.device_font(b'\0', 'X')


class Store(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = build_movie()
        gate = supported(self.movie)
        gate.start()
        self.addCleanup(gate.stop)
        self.packages, self.logs = [], []
        self.store = preview.PreviewStore(self.root / 'preview', lambda: list(self.packages), log=self.logs.append)

    def package(self, name, rootfs):
        path = self.root / (name + '.upd')
        path.write_bytes(build_test_package(rootfs=rootfs)[0])
        return path

    def test_no_upload_explains_how_to_get_a_preview(self):
        status = self.store.status(CR1)
        self.assertEqual(status['available'], False)
        self.assertIn('4.3.2', status['reason'])

    def test_controller_own_upload_comes_first_and_files_are_served_by_key(self):
        other = self.package('other', build_rootfs(build_movie(b'other')))
        own = self.package('own', build_rootfs(self.movie))
        self.packages = [(CR2, 'b' * 64, other), (CR1, 'a' * 64, own)]
        status = self.store.status(CR1)
        self.assertEqual((status['available'], status['firmware']), (True, 'test build'))
        self.assertTrue(preview.KEY.fullmatch(status['key']))
        movie = self.store.path(status['key'], 'MainUI.swf').read_bytes()
        self.assertEqual(movie[:3], b'FWS')
        self.assertEqual(self.store.path(status['key'], 'fonts/' + status['fonts'][0]).suffix, '.ttf')
        for key, name in ((status['key'], '../index.json'), (status['key'], 'receipt.json'),
                          ('../' + status['key'], 'MainUI.swf'), ('0' * 24, 'MainUI.swf')):
            self.assertIsNone(self.store.path(key, name))

    def test_unsupported_upload_is_read_once_and_reported_then_a_later_supported_one_wins(self):
        unsupported = self.package('old', build_rootfs(build_movie(b'other')))
        self.packages = [(CR1, 'c' * 64, unsupported)]
        with patch.object(preview, 'build', wraps=preview.build) as builds:
            first = self.store.status(CR1)
            second = self.store.status(CR1)
        self.assertEqual(builds.call_count, 1)
        self.assertEqual(first, second)
        self.assertFalse(first['available'])
        self.assertIn('not one the preview supports', first['reason'])
        good = self.package('good', build_rootfs(self.movie))
        self.packages.insert(0, (CR2, 'd' * 64, good))
        self.assertTrue(self.store.status(CR1)['available'])

    def test_unreadable_upload_is_reported_without_internal_details(self):
        broken = self.root / 'broken.upd'
        broken.write_bytes(b'not a firmware file at all')
        self.packages = [(CR1, 'e' * 64, broken)]
        status = self.store.status(CR1)
        self.assertEqual(status, {'available': False,
                                  'reason': 'The uploaded firmware could not be read for a preview.'})

    def test_removed_uploads_are_forgotten_and_their_previews_deleted(self):
        self.packages = [(CR1, 'a' * 64, self.package('own', build_rootfs(self.movie)))]
        key = self.store.status(CR1)['key']
        self.assertTrue((self.root / 'preview' / key).is_dir())
        self.packages = []
        self.assertFalse(self.store.status(CR1)['available'])
        self.assertFalse((self.root / 'preview' / key).exists())
        self.assertEqual(json.loads((self.root / 'preview/index.json').read_text()), {})

    def test_update_manager_lists_stored_uploads_newest_first_without_tokens(self):
        rootfs = build_rootfs(self.movie)
        package, modulus = build_test_package(rootfs=rootfs)
        clock = iter(range(1000, 2000, 10))
        with patch.object(online_updates, 'VENDOR_MODULUS', modulus):
            manager = online_updates.UpdateManager(self.root / 'config.json', clock=lambda: next(clock))
            manager.stage_controller(CR1, package, arm=False)
            manager.stage_controller(CR2, package + b'', arm=False)
        listed = manager.packages()
        self.assertEqual([mac for mac, _, _ in listed], [CR2, CR1])
        self.assertTrue(all(path.read_bytes() == package for _, _, path in listed))
        self.assertEqual({sha for _, sha, _ in listed}, {hashlib.sha256(package).hexdigest()})


class FakeClient:
    def get(self, path, params=None):
        raise LookupError(path)


def menu(label, locked=False):
    return {'items': [{'id': 'entry_x', 'kind': 'spotify', 'target': 'spotify:playlist:' + 'A' * 22,
                       'label': label, 'locked': locked}] + default_items()}


class PreviewIdentity(unittest.TestCase):
    def setUp(self):
        self.notices = []
        settings = {'controller_menus': {CR1: menu('Kitchen shortcut', locked=True), CR2: menu('Den shortcut')}}
        self.service = SpotifyService('127.0.0.1', 0, FakeClient(), log=lambda _: None, menu_settings=settings,
                                      controller_resolver=lambda address: CR2,
                                      on_locked=lambda address, message: self.notices.append(address))
        self.service.start()
        self.addCleanup(self.service.close)

    def call(self, method, body, headers=None):
        envelope = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    '<%s xmlns="http://www.sonos.com/Services/1.1">%s</%s></s:Body></s:Envelope>'
                    % (method, body, method)).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=5)
        try:
            connection.request('POST', '/smapi', envelope, {'Content-Type': 'text/xml', **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def root(self, headers=None):
        return self.call('getMetadata', '<id>root</id><index>0</index><count>10</count>', headers)

    def test_preview_header_selects_the_named_controller_menu(self):
        self.assertIn('Den shortcut', self.root()[1])
        status, body = self.root({PREVIEW_HEADER: CR1.upper()})
        self.assertEqual(status, 200)
        self.assertIn('Kitchen shortcut', body)
        self.assertNotIn('Den shortcut', body)

    def test_preview_lock_fault_does_not_notify_the_real_controller(self):
        status, body = self.call('getMetadata', '<id>%s</id><index>0</index><count>10</count>'
                                 % scoped_id('entry_x', 'spotify:playlist:' + 'A' * 22), {PREVIEW_HEADER: CR1})
        self.assertEqual(status, 500)
        self.assertIn('Client.FeatureLocked', body)
        self.assertEqual(self.notices, [])

    def test_preview_is_browse_only_well_formed_and_gets_its_own_identity(self):
        for headers, method, body in (({PREVIEW_HEADER: 'not-a-mac'}, 'getMetadata', '<id>root</id>'),
                                      ({PREVIEW_HEADER: CR1}, 'getScrollIndices', '<id>root</id>'),
                                      ({PREVIEW_HEADER: CR1}, 'getMediaURI', '<id>x</id>')):
            with self.subTest(method=method, headers=headers):
                status, reply = self.call(method, body, headers)
                self.assertEqual(status, 500)
                self.assertIn('Client.InvalidRequest', reply)
        with self.service.request_context('127.0.0.1', CR1):
            self.assertEqual(self.service.caller(), (CR1, 'preview:' + CR1))
            self.assertEqual(self.service.ui_actions.identity(), 'preview:' + CR1)
        with self.service.request_context('192.0.2.7'):
            self.assertEqual(self.service.caller(), (CR2, CR2))

    def test_preview_header_from_another_address_is_refused(self):
        class Request:
            client_address = ('192.0.2.50', 4000)
            class headers:
                @staticmethod
                def get_all(name, default=None):
                    return [CR1] if name == PREVIEW_HEADER else default
        with self.assertRaises(Exception) as caught:
            self.service.preview_controller(Request, 'getMetadata')
        self.assertEqual(caught.exception.code, 'Client.InvalidRequest')
        Request.client_address = ('127.0.0.1', 4000)
        self.assertEqual(self.service.preview_controller(Request, 'getMetadata'), CR1)


if __name__ == '__main__':
    unittest.main()
