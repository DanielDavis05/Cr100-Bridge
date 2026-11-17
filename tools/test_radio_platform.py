"""The board/radio registry the installer reads, and its agreement with install.sh."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import radio_platform

ROOT = Path(__file__).resolve().parents[1]


class RadioPlatform(unittest.TestCase):
    def test_both_boards_use_the_onboard_nexmon_radio(self):
        for board in ('raspberrypi,4-model-b', 'raspberrypi,3-model-b-plus'):
            name, radio = radio_platform.radio_for(board)
            self.assertEqual(name, 'bcm43455c0-nexmon')
            self.assertEqual(radio.interface, 'wlan0')
            self.assertEqual(radio.packages, ('brcmfmac-nexmon-dkms', 'firmware-nexmon'))

    def test_every_board_names_a_radio_whose_patch_script_exists(self):
        for board, (_, radio) in radio_platform.BOARDS.items():
            self.assertIn(radio, radio_platform.RADIOS, board)
        for name, radio in radio_platform.RADIOS.items():
            if radio.firmware_patch: self.assertTrue((ROOT / radio.firmware_patch).is_file(), name)

    def test_unsupported_boards_and_unknown_radios_are_rejected(self):
        for board in ('raspberrypi,3-model-b', 'raspberrypi,3-model-a-plus', 'raspberrypi,5-model-b', ''):
            with self.assertRaises(radio_platform.UnsupportedPlatform): radio_platform.radio_for(board)
        with self.assertRaises(radio_platform.UnsupportedPlatform):
            radio_platform.radio_for('raspberrypi,4-model-b', 'no-such-radio')

    def test_current_board_is_the_first_compatible_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'compatible'
            path.write_bytes(b'raspberrypi,3-model-b-plus\0brcm,bcm2837\0')
            self.assertEqual(radio_platform.current_board(path), 'raspberrypi,3-model-b-plus')
            self.assertEqual(radio_platform.current_board(Path(temp) / 'missing'), '')

    def test_installer_fields(self):
        def field(name, board='raspberrypi,3-model-b-plus'):
            return subprocess.run([sys.executable, radio_platform.__file__, '--board', board, '--field', name],
                                  capture_output=True, text=True)
        self.assertEqual(field('radio').stdout, 'bcm43455c0-nexmon\n')
        self.assertEqual(field('packages').stdout, 'brcmfmac-nexmon-dkms firmware-nexmon\n')
        self.assertEqual(field('dkms_module').stdout, 'brcmfmac-nexmon\n')
        self.assertEqual(field('firmware_patch').stdout, 'firmware/nexmon-ownership/apply.py\n')
        rejected = field('radio', 'raspberrypi,3-model-b')
        self.assertEqual((rejected.returncode, rejected.stdout), (1, ''))
        self.assertIn('Unsupported board', rejected.stderr)

    def test_installer_early_board_list_matches_the_registry(self):
        match = re.search(r"^SUPPORTED_BOARDS='([^']*)'$", (ROOT / 'install.sh').read_text(), re.M)
        self.assertIsNotNone(match, 'install.sh must gate on SUPPORTED_BOARDS')
        self.assertEqual(set(match[1].split()), set(radio_platform.BOARDS))


if __name__ == '__main__': unittest.main()
