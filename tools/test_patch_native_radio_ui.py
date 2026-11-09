import hashlib
from pathlib import Path
import unittest

from patch_controller_settings_ui import inspect
from patch_native_radio_ui import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT/'cr100-emulator/rootfs/opt/swf/MainUI.swf',
           ROOT/'cr100-analysis/compare-4.3-to-4.3.2/system/rootfs-4.3.2/opt/swf/MainUI.swf']


@unittest.skipUnless(all(path.is_file() for path in SOURCES), 'Original CR100 firmware fixtures are available only in the research workspace')
class RadioPatchTests(unittest.TestCase):
    def test_both_exact_originals_rebuild_without_changing_resources_or_other_functions(self):
        for path in SOURCES:
            with self.subTest(path=path):
                original = path.read_bytes()
                result, receipt = patch(original)
                names, _ = inspect(result)
                self.assertEqual(sorted(receipt['modified_functions']), ['BrowsePanel_setRadioLocation', 'addInternetRadioRootNode', 'createCDNode'])
                self.assertTrue(receipt['unrelated_functions_and_resources_unchanged'])
                self.assertEqual(sum(name == 'RadioUI_originalCreateCDNode' for name, _ in names), 1)
                self.assertEqual(sum(name == 'RadioUI_applyChoice' for name, _ in names), 1)
                self.assertEqual(path.read_bytes(), original)
                self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(result).digest())
                self.assertIn(b'Favorites\0', result)
                self.assertIn(b'Radio Settings\0', result)
                self.assertIn(b'CR100_RadioAction\0', result)
                self.assertIn(b'setString\0', result)

    def test_unknown_or_already_patched_firmware_is_rejected(self):
        source = SOURCES[0].read_bytes()
        altered = bytearray(source); altered[-10] ^= 1
        for bad in [bytes(altered), patch(source)[0], b'not a SWF']:
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError): patch(bad)


if __name__ == '__main__': unittest.main()
