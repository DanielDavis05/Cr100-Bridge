"""Package policy tests against the audited stock and lab-signed fixtures."""

from pathlib import Path
import struct
import sys
import zlib

from tempfile import TemporaryDirectory
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import workspace

from cr100_flash.package import (  # noqa: E402
    ROOTFS_CAPACITY,
    _CramFS,
    _records,
    validate_candidate,
    validate_combined_release,
)


STOCK = workspace.STOCK_UPD
CANDIDATE = workspace.RUNS / (
    "controller-deployment-2026-11-02/"
    "25.2-50130-1-2-settings-ui-lab-signed.upd"
)
LAB_KEY = workspace.RUNS / "controller-deployment-2026-11-02/lab-upgradekey.pub"
COMBINED = workspace.FIRMWARE / "build/release/cr100-custom-4.3.2.upd"
DEV_KEY = workspace.FIRMWARE / "build/release/development-upgradekey.pub"


def _mutated(tmp_path: Path, change, name: str = "mutated.upd") -> Path:
    data = bytearray(CANDIDATE.read_bytes())
    change(data)
    path = tmp_path / name
    path.write_bytes(data)
    return path


class PackageTests(unittest.TestCase):
    def setUp(self):
        for asset in (STOCK, CANDIDATE, LAB_KEY):
            if not asset.is_file(): self.skipTest(f"Audited update fixture unavailable: {asset}")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_audited_lab_package_passes(self):
        report = validate_candidate(CANDIDATE, STOCK, LAB_KEY)
        self.assertEqual(report['candidate_sha256'], '99d70a0b20d5ac0e192128debe202410d4fadb2c926fcc4bd8fd038d01a211a2')
        self.assertIs(report['signature_valid'], True)
        self.assertEqual(report['release'], '25.2-50130')
        self.assertEqual(report['protected_records_identical'], 9)
        self.assertEqual(report['rootfs_bytes'], 4169728)
        self.assertEqual(report['changed_paths'], ['/opt/swf/MainUI.swf'])
        self.assertEqual(report['filesystem_paths_verified'], 288)
        self.assertEqual(report['partition_table_sha256'], '94b7528474328b0e138145ed5121390e81e817fff0af081a1fea6ba1d2fd59d7')
        self.assertEqual(report['kernel_sha256'], 'f39c4c6c7b8ca14e8d8d467d5ccc6293bccb87183fbe3d361da8a7ab9069e4b5')
        self.assertEqual(report['mcu_sha256'], '0f96ddc311a81c5e13f8dd4af92dccba86e89770218d5b93e518d215b3d47747')

    def test_rejects_other_release_before_signature(self):
        def change(data):
            struct.pack_into("<I", data, 16 + 8, 50131)

        path = _mutated(self.tmp_path, change)
        with self.assertRaisesRegex(ValueError, "Candidate release"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_modified_install_script(self):
        def change(data):
            install = _records(data)[3]
            data[install.offset + 16] ^= 1

        path = _mutated(self.tmp_path, change)
        with self.assertRaisesRegex(ValueError, "Protected UPD record type 3"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_unknown_record_order(self):
        def change(data):
            marker = _records(data)[6]
            struct.pack_into("<I", data, marker.offset + 4, 8)

        path = _mutated(self.tmp_path, change)
        with self.assertRaisesRegex(ValueError, "record order"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_rootfs_over_bank_capacity(self):
        data = bytearray(CANDIDATE.read_bytes())
        root = _records(data)[5]
        extra = ROOTFS_CAPACITY - (root.length - 16) + 1
        data[root.offset + root.length:root.offset + root.length] = b"\0" * extra
        struct.pack_into("<I", data, root.offset + 8, root.length + extra)
        path = self.tmp_path / "oversize.upd"
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "Root filesystem exceeds"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_invalid_signature(self):
        path = _mutated(self.tmp_path, lambda data: data.__setitem__(-1, data[-1] ^ 1))
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_modified_non_ui_file_with_valid_cramfs(self):
        data = bytearray(CANDIDATE.read_bytes())
        root = _records(data)[5]
        root_start = root.offset + 16
        root_end = root.offset + root.length
        filesystem = _CramFS(bytes(data[root_start:root_end]))
        node = filesystem.nodes["/etc/host.conf"]
        original = filesystem.contents(node)
        old_blocks = filesystem.compressed_blocks(node)
        self.assertEqual(len(old_blocks), 1)
        changed = b"A" + original[1:]
        compressed = zlib.compress(changed, 9)
        self.assertEqual(len(compressed), len(old_blocks[0]))
        block_start = root_start + node["offset"] + 4
        data[block_start:block_start + len(compressed)] = compressed
        struct.pack_into("<I", data, root_start + 32, 0)
        crc = zlib.crc32(data[root_start:root_end]) & 0xFFFFFFFF
        struct.pack_into("<I", data, root_start + 32, crc)
        path = self.tmp_path / "non-ui-change.upd"
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "Non-UI file content changed: /etc/host.conf"):
            validate_candidate(path, STOCK, LAB_KEY)

    def test_rejects_wrong_stock_reference(self):
        stock = bytearray(STOCK.read_bytes())
        stock[-1] ^= 1
        wrong = self.tmp_path / "wrong-stock.upd"
        wrong.write_bytes(stock)
        with self.assertRaisesRegex(ValueError, "Stock reference"):
            validate_candidate(CANDIDATE, wrong, LAB_KEY)

    def test_exact_combined_release_passes(self):
        if not COMBINED.is_file() or not DEV_KEY.is_file():
            self.skipTest("Combined release artifacts are unavailable")
        report = validate_combined_release(COMBINED, STOCK, DEV_KEY)
        self.assertEqual(report['policy'], 'combined-release-exact')
        self.assertEqual(report['candidate_sha256'], '26d0bf0f0a1817e629336563c24204954f71f1d011e2545c00da76d4ddd33608')
        self.assertEqual(report['development_public_key_sha256'], 'de42331e3e9a22f57402c4d04114854b78fd2b2ec9a060f41028613cf8e8bef6')
        self.assertEqual(report['changed_paths'], ['/bin/upgrade', '/opt/swf/MainUI.swf'])
        self.assertEqual(report['updater_changed_offsets'], [28879])
        self.assertEqual(report['updater_sha256'], '5eb6f17a71b5998090a71f03d05af5a74a9326391aea21db0551fe0ef7c5168b')
        self.assertEqual(report['main_ui_sha256'], '57365e1f16c4c5c59be1b600bf352279b962e8e9913492f3d344c7785b590bf1')
        self.assertTrue(report['rootfs_bytes'] == 4182016 < ROOTFS_CAPACITY)
        self.assertEqual(report['protected_records_identical'], 9)
        self.assertIs(report['signature_valid'], True)

    def test_combined_release_does_not_relax_ui_only_default(self):
        if not COMBINED.is_file() or not DEV_KEY.is_file():
            self.skipTest("Combined release artifacts are unavailable")
        with self.assertRaisesRegex(ValueError, "Non-UI file content changed: /bin/upgrade"):
            validate_candidate(COMBINED, STOCK, DEV_KEY)

    def test_combined_release_rejects_other_public_key(self):
        if not COMBINED.is_file():
            self.skipTest("Combined release artifact is unavailable")
        with self.assertRaisesRegex(ValueError, "Development public key"):
            validate_combined_release(COMBINED, STOCK, LAB_KEY)

    def test_combined_release_rejects_one_byte_mutation(self):
        if not COMBINED.is_file() or not DEV_KEY.is_file():
            self.skipTest("Combined release artifacts are unavailable")
        data = bytearray(COMBINED.read_bytes())
        data[-1] ^= 1
        changed = self.tmp_path / "changed-combined.upd"
        changed.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "not the exact reviewed combined release"):
            validate_combined_release(changed, STOCK, DEV_KEY)


if __name__ == "__main__":
    unittest.main()
