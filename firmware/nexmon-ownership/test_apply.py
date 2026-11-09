"""Exact firmware patch verification and safe atomic installation on temp files."""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('ownership_apply', Path(__file__).with_name('apply.py'))
apply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply)


class FirmwarePatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'firmware.bin'
        self.base = bytes(range(64))
        self.changed = self.base[:8] + b'OWNR' + self.base[12:]
        self.target.write_bytes(self.base)
        self.target.chmod(0o644)
        self.manifest = self.root / 'manifest.json'
        self.value = {'format': 1, 'base_sha256': apply.digest(self.base),
                      'patched_sha256': apply.digest(self.changed), 'firmware_name': 'firmware.bin',
                      'ram_base': '0x198000', 'stats': {'command': 430, 'magic': '0x4f4e5243', 'version': 1, 'size': 48},
                      'patches': [
                          {'offset': 8, 'expected': self.base[8:12].hex(), 'replacement': b'OWNR'.hex()}]}
        self.save_manifest()
        self.backups = self.root / 'backups'

    def save_manifest(self): self.manifest.write_text(json.dumps(self.value))
    def run_patch(self, path=None, write=False):
        return apply.patch_file(path or self.target, self.manifest, self.backups, write)

    def add_upgrade(self):
        self.previous = self.base[:8] + b'OLD!' + self.base[12:]
        upgrade = {'base_sha256': apply.digest(self.previous), 'patches': [
            {'offset': 8, 'expected': b'OLD!'.hex(), 'replacement': b'OWNR'.hex()}]}
        self.value['upgrades'] = [upgrade]
        self.save_manifest()
        return upgrade

    def test_check_is_read_only_and_apply_is_idempotent(self):
        before = self.target.stat()
        self.assertEqual(self.run_patch()['state'], 'patch_available')
        self.assertEqual(self.target.read_bytes(), self.base)
        self.assertEqual(self.target.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.backups.exists())
        result = self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), self.changed)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(Path(result['backup_path']).read_bytes(), self.base)
        self.assertEqual(Path(result['backup_path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(result['audit_path']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(Path(result['audit_path']).read_text())['state'], 'applied')
        files = sorted(self.backups.iterdir())
        self.assertEqual(self.run_patch(write=True)['state'], 'already_patched')
        self.assertEqual(sorted(self.backups.iterdir()), files)

    def test_symlink_chain_is_preserved_and_final_target_is_replaced(self):
        first, second = self.root / 'board.bin', self.root / 'alternative.bin'
        first.symlink_to(second.name); second.symlink_to(self.target.name)
        result = self.run_patch(first, write=True)
        self.assertTrue(first.is_symlink()); self.assertTrue(second.is_symlink())
        self.assertEqual(first.readlink(), Path(second.name))
        self.assertEqual(result['resolved_path'], str(self.target))
        self.assertEqual(first.read_bytes(), self.changed)

    def test_unknown_image_precondition_failure_and_wrong_output_hash_refuse_writes(self):
        for change, message in (({'base_sha256': '0' * 64}, 'Unknown firmware'),
                                ({'patches': [{'offset': 8, 'expected': '00000000', 'replacement': '4f574e52'}]}, 'precondition'),
                                ({'patched_sha256': '0' * 64}, 'checksum')):
            original = dict(self.value)
            self.value.update(change); self.save_manifest()
            with self.assertRaisesRegex(ValueError, message): self.run_patch(write=True)
            self.assertEqual(self.target.read_bytes(), self.base)
            self.assertFalse(self.backups.exists())
            self.value = original

    def test_atomic_replacement_failure_preserves_firmware_and_original_backup(self):
        replace = apply.os.replace
        def fail_firmware(source, destination):
            if Path(destination) == self.target: raise OSError('simulated disk error')
            return replace(source, destination)
        with patch.object(apply.os, 'replace', side_effect=fail_firmware):
            with self.assertRaisesRegex(OSError, 'disk error'): self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), self.base)
        self.assertEqual(next(self.backups.glob('base-*.bin')).read_bytes(), self.base)
        self.assertFalse(list(self.root.glob('.*.cr100-*')))

    def test_overlap_and_size_changing_manifests_are_rejected(self):
        for bad in ({'offset': 8, 'expected': '0809', 'replacement': 'abcd'},
                    {'offset': 20, 'expected': '14', 'replacement': 'abcd'}):
            original = list(self.value['patches'])
            self.value['patches'].append(bad); self.save_manifest()
            with self.assertRaises(ValueError): self.run_patch(write=True)
            self.assertEqual(self.target.read_bytes(), self.base)
            self.value['patches'] = original

    def test_incompatible_diagnostic_manifest_is_rejected_before_writes(self):
        self.value['stats']['version'] = 2
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'diagnostic ABI'): self.run_patch(write=True)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.target.read_bytes(), self.base)

    def test_canonical_and_reviewed_upgrade_routes_converge_to_one_target(self):
        self.add_upgrade()
        manifest = apply.read_manifest(self.manifest)
        self.assertEqual(apply.patched_image(self.base, manifest), self.changed)
        self.assertEqual(apply.patched_image(self.previous, manifest), self.changed)
        self.assertEqual(len(apply.patched_image(self.previous, manifest)), len(self.previous))

    def test_upgrade_backs_up_the_actual_recognized_image_and_is_idempotent(self):
        self.add_upgrade()
        self.target.write_bytes(self.previous)
        self.assertEqual(self.run_patch()['state'], 'patch_available')
        self.assertFalse(self.backups.exists())
        result = self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), self.changed)
        self.assertEqual(result['base_sha256'], apply.digest(self.previous))
        self.assertEqual(Path(result['backup_path']).name, 'base-' + apply.digest(self.previous) + '.bin')
        self.assertEqual(Path(result['backup_path']).read_bytes(), self.previous)
        self.assertEqual(Path(result['backup_path']).stat().st_mode & 0o777, 0o600)
        receipt = json.loads(Path(result['audit_path']).read_text())
        self.assertEqual(receipt['base_sha256'], apply.digest(self.previous))
        self.assertEqual(receipt['patched_sha256'], apply.digest(self.changed))
        self.assertEqual(receipt['manifest_sha256'], apply.digest(self.manifest.read_bytes()))
        files = sorted(self.backups.iterdir())
        self.assertEqual(self.run_patch(write=True)['state'], 'already_patched')
        self.assertEqual(sorted(self.backups.iterdir()), files)

    def test_upgrade_entry_hashes_are_valid_unique_and_never_the_final_target(self):
        upgrade = self.add_upgrade()
        cases = [None, {}, [None], [{}],
                 [{**upgrade, 'base_sha256': 'not-a-hash'}],
                 [{**upgrade, 'base_sha256': 1}],
                 [{**upgrade, 'base_sha256': self.value['base_sha256']}],
                 [{**upgrade, 'base_sha256': self.value['patched_sha256']}],
                 [upgrade, copy.deepcopy(upgrade)],
                 [{**upgrade, 'patched_sha256': self.value['patched_sha256']}]]
        for upgrades in cases:
            with self.subTest(upgrades=upgrades):
                self.value['upgrades'] = upgrades
                self.save_manifest()
                with self.assertRaises(ValueError): self.run_patch(write=True)
                self.assertEqual(self.target.read_bytes(), self.base)
                self.assertFalse(self.backups.exists())

    def test_every_upgrade_patch_list_is_validated_even_when_not_selected(self):
        upgrade = self.add_upgrade()
        valid = copy.deepcopy(upgrade['patches'])
        cases = [None, [], [None],
                 [{'offset': -1, 'expected': '00', 'replacement': '01'}],
                 [{'offset': True, 'expected': '00', 'replacement': '01'}],
                 [{'offset': 1 << 32, 'expected': '00', 'replacement': '01'}],
                 [{'offset': len(self.base), 'expected': '00', 'replacement': '01'}],
                 [{'offset': 1, 'expected': 'not-hex', 'replacement': '01'}],
                 [{'offset': 1, 'expected': '', 'replacement': ''}],
                 [{'offset': 1, 'expected': '00', 'replacement': '0102'}],
                 valid + [{'offset': 9, 'expected': '00', 'replacement': '01'}]]
        for patches in cases:
            with self.subTest(patches=patches):
                upgrade['patches'] = patches
                self.save_manifest()
                with self.assertRaises(ValueError): self.run_patch(write=True)
                self.assertEqual(self.target.read_bytes(), self.base)
                self.assertFalse(self.backups.exists())

    def test_upgrade_preconditions_ranges_and_whole_image_hash_are_required(self):
        upgrade = self.add_upgrade()
        self.target.write_bytes(self.previous)
        valid = copy.deepcopy(upgrade['patches'])
        for patches, target_hash, message in (
                ([{**valid[0], 'expected': '00000000'}], self.value['patched_sha256'], 'precondition'),
                ([{**valid[0], 'offset': 63}], self.value['patched_sha256'], 'range'),
                (valid, '0' * 64, 'checksum')):
            with self.subTest(message=message):
                upgrade['patches'] = patches
                self.value['patched_sha256'] = target_hash
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, message): self.run_patch(write=True)
                self.assertEqual(self.target.read_bytes(), self.previous)
                self.assertFalse(self.backups.exists())

    def test_final_image_is_checked_against_canonical_replacements_for_every_route(self):
        self.add_upgrade()
        self.value['patches'][0]['replacement'] = b'FAIL'.hex()
        self.save_manifest()
        for image in (self.previous, self.changed):
            with self.subTest(already_patched=image == self.changed):
                self.target.write_bytes(image)
                with self.assertRaisesRegex(ValueError, 'byte verification'): self.run_patch(write=True)
                self.assertEqual(self.target.read_bytes(), image)
                self.assertFalse(self.backups.exists())

    def test_upgrade_atomic_replace_failure_retains_the_recognized_input_and_backup(self):
        self.add_upgrade()
        self.target.write_bytes(self.previous)
        replace = apply.os.replace
        def fail_firmware(source, destination):
            if Path(destination) == self.target: raise OSError('simulated upgrade disk error')
            return replace(source, destination)
        with patch.object(apply.os, 'replace', side_effect=fail_firmware):
            with self.assertRaisesRegex(OSError, 'upgrade disk error'): self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), self.previous)
        self.assertEqual(next(self.backups.glob('base-*.bin')).read_bytes(), self.previous)
        self.assertFalse(list(self.root.glob('.*.cr100-*')))

    def test_upgrade_obeys_the_existing_target_lock(self):
        self.add_upgrade()
        self.target.write_bytes(self.previous)
        self.backups.mkdir()
        lock_path = self.backups / (apply.digest(os.fsencode(str(self.target))) + '.lock')
        with lock_path.open('w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), self.previous)
        self.assertFalse(list(self.backups.glob('base-*.bin')))
        self.assertFalse(list(self.backups.glob('apply-*.json')))

    def test_unrecognized_nearby_image_still_refuses_every_upgrade_route(self):
        self.add_upgrade()
        unknown = self.previous[:-1] + b'X'
        self.target.write_bytes(unknown)
        with self.assertRaisesRegex(ValueError, 'Unknown firmware'): self.run_patch(write=True)
        self.assertEqual(self.target.read_bytes(), unknown)
        self.assertFalse(self.backups.exists())

    def test_duplicate_json_keys_are_rejected_in_upgrade_entries(self):
        upgrade = self.add_upgrade()
        entry = '"base_sha256": "' + upgrade['base_sha256'] + '"'
        self.manifest.write_text(json.dumps(self.value).replace(entry, entry + ', ' + entry))
        with self.assertRaisesRegex(ValueError, 'Duplicate firmware manifest key'): self.run_patch(write=True)
        self.assertFalse(self.backups.exists())


if __name__ == '__main__': unittest.main()
