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


if __name__ == '__main__': unittest.main()
