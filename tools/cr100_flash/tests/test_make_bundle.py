"""Bundle input paths follow workspace overrides without building flash bundles."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cr100_flash import make_bundle
import workspace


class SourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / 'runs'
        self.firmware = self.root / 'firmware'
        self.stock = self.root / 'stock.upd'
        self.candidate = self.root / 'candidate.upd'
        self.paths = [self.stock, self.candidate,
            self.firmware / 'build/release/development-upgradekey.pub']
        self.paths += [self.runs / path for path in (
            'controller-deployment-2026-11-02/lab-upgradekey.pub',
            'physical-upgrade-preflight-2026-11-04/mcu_version',
            'physical-backup-tool-2026-11-04/mtd_stream',
            'physical-backup-tool-2026-11-04/file_stream',
            'physical-backup-tool-2026-11-04/receive_dump.py',
            'physical-backup-tool-2026-11-04/receive_file.py',
            'controller-native-install-2026-11-02/select_nand_boot.py',
            'physical-preflight-2026-11-04/run_rop_shell.sh',
            'physical-preflight-2026-11-04/wait_root_shell.py',
            'physical-rop-system-2026-11-04/warmup_rop_art_fixture.py',
            'physical-rop-system-2026-11-04/fixed-rop-system-root-shell.png')]
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        for name, value in (('RUNS', self.runs), ('STOCK_UPD', self.stock), ('FIRMWARE', self.firmware)):
            replacement = patch.object(workspace, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_workspace_overrides_choose_each_reviewed_input(self):
        mapping = make_bundle.sources(self.candidate)
        self.assertEqual(mapping['assets/stock.upd'], self.stock)
        self.assertEqual(mapping['assets/lab.pub'], self.runs / 'controller-deployment-2026-11-02/lab-upgradekey.pub')
        self.assertEqual(mapping['assets/mtd_stream'], self.runs / 'physical-backup-tool-2026-11-04/mtd_stream')
        self.assertTrue(all(path.is_file() for path in mapping.values()))
        combined = make_bundle.sources(self.candidate, 'combined-release')
        self.assertEqual(combined['assets/lab.pub'], self.firmware / 'build/release/development-upgradekey.pub')

    def test_each_missing_input_names_the_expected_location(self):
        for path in self.paths:
            profile = 'combined-release' if path.is_relative_to(self.firmware) else 'ui-only'
            with self.subTest(path=path):
                path.unlink()
                try:
                    with self.assertRaises(FileNotFoundError) as missing:
                        make_bundle.sources(self.candidate, profile)
                    self.assertIn(str(path), str(missing.exception))
                finally:
                    path.write_bytes(b'fixture')


if __name__ == '__main__':
    unittest.main()
