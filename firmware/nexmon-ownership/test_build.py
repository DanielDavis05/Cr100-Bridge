"""Independent binary guards, native-call checks, and sanitized ownership tests."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

import build as overlay

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(os.environ.get('CR100_NEXMON_BASE', ROOT / 'runs/stability-audit-2026-11-03/installed-nexmon.bin'))
NEXMON = Path(os.environ.get('CR100_NEXMON_SOURCE', '/private/tmp/cr100-nexmon-audit'))


def decode_bl(address, encoded):
    """Decode the ARMv7 Thumb BL immediate independently from the build encoder."""
    upper, lower = struct.unpack('<HH', encoded)
    if upper & 0xf800 != 0xf000 or lower & 0xd000 != 0xd000:
        raise ValueError('Not a Thumb BL')
    sign, j1, j2 = (upper >> 10) & 1, (lower >> 13) & 1, (lower >> 11) & 1
    value = (sign << 24) | ((1 ^ j1 ^ sign) << 23) | ((1 ^ j2 ^ sign) << 22)
    value |= (upper & 0x3ff) << 12 | (lower & 0x7ff) << 1
    if sign: value -= 1 << 25
    return address + 4 + value


class OwnershipBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name)
        cls.receipt = overlay.build(BASE, NEXMON, cls.output)
        cls.base = BASE.read_bytes()
        cls.candidate = (cls.output / 'cyfmac43455-sdio-standard-owned.bin').read_bytes()
        cls.manifest = json.loads((cls.output / 'manifest.json').read_text())

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def word(self, address):
        return struct.unpack_from('<I', self.candidate, address - overlay.RAM_START)[0]

    def test_five_guarded_ranges_reconstruct_exact_candidate_without_other_changes(self):
        reconstructed = bytearray(self.base)
        touched = set()
        self.assertEqual(len(self.manifest['patches']), 5)
        for patch in self.manifest['patches']:
            offset = patch['offset']
            expected, replacement = bytes.fromhex(patch['expected']), bytes.fromhex(patch['replacement'])
            self.assertEqual(len(expected), len(replacement))
            self.assertEqual(self.base[offset:offset + len(expected)], expected)
            indexes = set(range(offset, offset + len(expected)))
            self.assertFalse(touched & indexes)
            touched |= indexes
            reconstructed[offset:offset + len(replacement)] = replacement
        self.assertEqual(reconstructed, self.candidate)
        self.assertEqual(len(self.candidate), overlay.BASE_SIZE)
        self.assertEqual(overlay.sha256(self.base), self.manifest['base_sha256'])
        self.assertEqual(overlay.sha256(self.candidate), self.manifest['patched_sha256'])

    def test_native_chain_conversion_runs_before_new_filter_and_consumed_path_is_unchanged(self):
        self.assertEqual(self.word(0x2037c0), 0x1a1eed)
        self.assertEqual(decode_bl(0x1a1f6e, self.candidate[0x1a1f6e-overlay.RAM_START:0x1a1f72-overlay.RAM_START]), 0x19eed8)
        self.assertEqual(decode_bl(0x1a1f78, self.candidate[0x1a1f78-overlay.RAM_START:0x1a1f7c-overlay.RAM_START]),
                         int(self.receipt['symbols']['ownership_filter'], 16))
        self.assertEqual(self.candidate[0x1a1f5a-overlay.RAM_START:0x1a1f78-overlay.RAM_START],
                         self.base[0x1a1f5a-overlay.RAM_START:0x1a1f78-overlay.RAM_START])
        self.assertEqual(self.candidate[0x1a1f7c-overlay.RAM_START:0x1a2050-overlay.RAM_START],
                         self.base[0x1a1f7c-overlay.RAM_START:0x1a2050-overlay.RAM_START])
        self.assertEqual(self.word(0x20e264), int(self.receipt['symbols']['ownership_ioctl'], 16) | 1)
        self.assertEqual(self.word(0x19849c), int(self.receipt['symbols']['beacon_stamp_build'], 16) | 1)

    def test_old_nexmon_code_is_intact_and_new_code_stays_in_reserved_storage(self):
        start, end = overlay.PATCH_START - overlay.RAM_START, overlay.OVERLAY_START - overlay.RAM_START
        self.assertEqual(self.candidate[start:end], self.base[start:end])
        self.assertLessEqual(int(self.receipt['overlay_end'], 16), overlay.PATCH_END)
        self.assertEqual(self.word(0x19a3d8), overlay.PATCH_START)
        stats = int(self.receipt['symbols']['ownership_stats'], 16) - overlay.RAM_START
        self.assertEqual(struct.unpack_from('<12I', self.candidate, stats),
                         (0x4f4e5243, 1, 48, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    def test_incorrect_installed_or_pristine_images_are_rejected(self):
        pristine = (NEXMON / 'firmwares/bcm43455c0/7_45_206/brcmfmac43455-sdio.bin').read_bytes()
        wrong = bytearray(self.base); wrong[0] ^= 1
        with self.assertRaisesRegex(ValueError, 'exact reviewed'): overlay.checked_base(wrong, pristine)
        wrong = bytearray(pristine); wrong[0] ^= 1
        with self.assertRaisesRegex(ValueError, 'pristine'): overlay.checked_base(self.base, wrong)
        with self.assertRaises(ValueError): overlay.checked_base(self.candidate, pristine)

    def test_branch_encoder_handles_forward_backward_and_rejects_oversized_target(self):
        for source, target in ((0x1a1f78, 0x1a1dde), (0x1a1f78, 0x22d119), (0x22d100, 0x1a1eed)):
            self.assertEqual(decode_bl(source, overlay.thumb_bl(source, target)), target & ~1)
        for target in (0x10000000, 0xffffffffffff):
            with self.assertRaises(ValueError): overlay.thumb_bl(0x1a1f78, target)

    def test_same_c_ownership_logic_under_address_and_undefined_behavior_sanitizers(self):
        binary = self.output / 'ownership-host-test'
        subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-DOWNERSHIP_HOST_TEST',
                        str(overlay.HERE / 'ownership.c'), str(overlay.HERE / 'tsf_query.c'),
                        str(overlay.HERE / 'shm_query.c'), str(overlay.HERE / 'beacon_stamp.c'),
                        str(overlay.HERE / 'test_ownership.c'),
                        '-o', str(binary)], check=True, timeout=30)
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=5)
        self.assertIn('all invariants passed', result.stdout)


    def test_production_abi_and_native_microcode_builder_are_unchanged(self):
        self.assertNotIn('rom_diagnostic', self.receipt)
        self.assertNotIn('descriptor_diagnostic', self.receipt)
        self.assertEqual(self.receipt['clock']['command'], 433)
        self.assertEqual(self.receipt['noack']['command'], 432)
        self.assertEqual(set(self.receipt['source_sha256']), {'ownership.c', 'ownership.h', 'tsf_query.c', 'tsf_query.h',
                         'shm_query.c', 'shm_query.h', 'beacon_stamp.c', 'beacon_stamp.h'})
        noack = int(self.receipt['symbols']['noack_stats'], 16) - overlay.RAM_START
        self.assertEqual(struct.unpack_from('<10I', self.candidate, noack), (0x414e5243, 1, 40, 1, 0, 0, 0, 0, 0, 0))
        # All bytes outside four ranges already match base; this specifically
        # includes native descriptor construction and compressed microcode.
        self.assertEqual(self.candidate[0x1a547c-overlay.RAM_START:0x1a63ee-overlay.RAM_START],
                         self.base[0x1a547c-overlay.RAM_START:0x1a63ee-overlay.RAM_START])
        text = (self.output / 'ownership-disassembly.txt').read_text()
        self.assertNotIn('<read_rom>', text)
        self.assertNotIn('<descriptor_build>', text)
        self.assertIn('<tsf_query>', text)
        self.assertIn('<shm_query>', text)
        self.assertIn('<beacon_stamp_build>', text)
        self.assertEqual(self.receipt['shm_diagnostic']['command'], 435)
        self.assertEqual(self.receipt['beacon_stamp']['command'], 436)
        self.assertEqual(overlay.sha256(self.candidate), 'ce38c2e189815a0495615ab4ecad979be8f01b234cd5b3e9c1cfe90edf32677d')

    def test_release_previous_upgrades_and_reproducible_output(self):
        release = json.loads((overlay.HERE / 'manifest.json').read_text())
        self.assertEqual([branch['base_sha256'] for branch in release['upgrades']], list(overlay.SUPPORTED_UPGRADES))
        previous_paths, previous_images = [], []
        for branch in release['upgrades']:
            previous = bytearray(self.candidate)
            for patch in branch['patches']:
                start = patch['offset']; expected = bytes.fromhex(patch['expected'])
                previous[start:start + len(expected)] = expected
            self.assertEqual(overlay.sha256(previous), branch['base_sha256'])
            path = self.output / (branch['base_sha256'] + '.bin'); path.write_bytes(previous)
            previous_paths.append(path); previous_images.append(previous)
        rebuilt = self.output / 'reproducible'
        receipt = overlay.build(BASE, NEXMON, rebuilt, previous_paths)
        self.assertEqual((rebuilt / 'cyfmac43455-sdio-standard-owned.bin').read_bytes(), self.candidate)
        self.assertEqual(receipt, self.receipt)
        self.assertEqual(json.loads((rebuilt / 'manifest.json').read_text()), release)
        for previous, branch in zip(previous_images, release['upgrades']):
            for patch in branch['patches']:
                start = patch['offset']; before, after = bytes.fromhex(patch['expected']), bytes.fromhex(patch['replacement'])
                self.assertEqual(previous[start:start + len(before)], before)
                previous[start:start + len(after)] = after
            self.assertEqual(previous, self.candidate)
        bad = self.output / 'unknown.bin'; bad.write_bytes(self.base)
        with self.assertRaisesRegex(ValueError, 'supported previous'):
            overlay.build(BASE, NEXMON, self.output / 'reject', bad)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            overlay.build(BASE, NEXMON, self.output / 'duplicate', [previous_paths[0], previous_paths[0]])

    def test_same_beacon_stamp_and_fixed_shm_c_under_sanitizers(self):
        for helper in ('beacon_stamp', 'shm_query'):
            binary = self.output / (helper + '-host-test')
            subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1',
                            '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-DOWNERSHIP_HOST_TEST',
                            str(overlay.HERE / (helper + '.c')), str(overlay.HERE / ('test_' + helper + '.c')),
                            '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=5)



if __name__ == '__main__': unittest.main()
