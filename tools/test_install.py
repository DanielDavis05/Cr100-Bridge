"""Exercise installer release retention only, without running installation."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


class ReleaseRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.releases = self.root / 'releases'; self.releases.mkdir()
        self.new = self.release('release-new', 1)
        (self.root / 'current').symlink_to(self.new)

    def release(self, name, modified):
        path = self.releases / name; path.mkdir()
        (path / 'payload').write_text(name)
        os.utime(path, (modified, modified))
        return path

    def prune(self, old=''):
        installer = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
        match = re.search(r'# Retain recent releases.*?\npython3 - .*?<<\'PY\'\n(.*?)\nPY', installer, re.S)
        self.assertIsNotNone(match, 'Installer must prune old releases after switching current')
        subprocess.run([sys.executable, '-', str(self.new), str(old)], input=match[1], text=True, check=True)

    def test_keeps_current_and_two_newest_others(self):
        stale = self.release('release-stale', 2)
        older = self.release('release-older', 3)
        newest = self.release('release-newest', 4)
        self.prune()
        self.assertTrue(self.new.is_dir()); self.assertTrue(older.is_dir()); self.assertTrue(newest.is_dir())
        self.assertFalse(stale.exists())

    def test_keeps_rollback_target_even_if_older_than_recent_releases(self):
        old = self.release('release-rollback', 0)
        self.release('release-recent-one', 3); self.release('release-recent-two', 4)
        self.prune(old)
        self.assertEqual((old / 'payload').read_text(), old.name)

    def test_leaves_other_names_files_and_symlinks_untouched(self):
        other = self.release('unrelated', 0)
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'payload').write_text('keep')
        link = self.releases / 'release-link'; link.symlink_to(outside, target_is_directory=True)
        file = self.releases / 'release-file'; file.write_text('keep')
        for i in range(4): self.release('release-%s' % i, i + 3)
        self.prune()
        self.assertTrue(other.is_dir()); self.assertTrue(link.is_symlink())
        self.assertEqual((outside / 'payload').read_text(), 'keep'); self.assertEqual(file.read_text(), 'keep')

    def test_never_deletes_a_different_current_target(self):
        current = self.release('release-current', 0)
        (self.root / 'current').unlink(); (self.root / 'current').symlink_to(current)
        for i in range(4): self.release('release-%s' % i, i + 3)
        self.prune()
        self.assertTrue(current.is_dir()); self.assertTrue(self.new.is_dir())


class NetworkManagerSetup(unittest.TestCase):
    """The installer's NetworkManager block, run against fake systemctl and nmcli."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.conf_dir = root / 'conf.d'
        self.calls = root / 'nmcli.log'
        bin_dir = root / 'bin'; bin_dir.mkdir()
        for name, body in (('systemctl', 'exit 0'), ('nmcli', 'echo "$*" >> "$NMCLI_LOG"')):
            tool = bin_dir / name
            tool.write_text('#!/bin/sh\n' + body + '\n'); tool.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', NMCLI_LOG=str(self.calls))
        installer = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
        match = re.search(r'^# Keep NetworkManager off.*?^# End NetworkManager setup\.$', installer, re.S | re.M)
        self.assertIsNotNone(match, 'Installer must set up NetworkManager in a marked block')
        self.block = match[0].replace('NM_CONF_DIR=/etc/NetworkManager/conf.d', f'NM_CONF_DIR={self.conf_dir}')
        self.assertIn(str(self.conf_dir), self.block)

    def run_block(self):
        self.calls.unlink(missing_ok=True)
        subprocess.run(['bash', '-Eeuo', 'pipefail', '-c', self.block], env=self.env, check=True)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_first_install_writes_drop_in_and_reloads_only_configuration(self):
        self.assertEqual(self.run_block(), ['general reload conf', 'device set wlan0 managed no'])
        self.assertEqual((self.conf_dir / '99-cr100-bridge.conf').read_text(),
                         '[device-cr100-bridge]\n'
                         'match-device=interface-name:wlan0;interface-name:mon0;interface-name:sonos0\n'
                         'managed=0\n')

    def test_reinstall_with_same_drop_in_does_not_reload(self):
        self.run_block()
        self.assertEqual(self.run_block(), ['device set wlan0 managed no'])

    def test_changed_drop_in_is_rewritten_and_reloaded(self):
        self.run_block()
        (self.conf_dir / '99-cr100-bridge.conf').write_text('[device-cr100-bridge]\nmanaged=1\n')
        self.assertEqual(self.run_block(), ['general reload conf', 'device set wlan0 managed no'])
        self.assertIn('managed=0', (self.conf_dir / '99-cr100-bridge.conf').read_text())

    def test_installer_never_runs_a_full_reload(self):
        # A bare `nmcli general reload` rewrites /etc/resolv.conf and breaks DNS owned by Tailscale or dhcpcd.
        installer = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
        self.assertIsNone(re.search(r'nmcli general reload\s*$', installer, re.M))


if __name__ == '__main__': unittest.main()
