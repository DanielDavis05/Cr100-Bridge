"""Build identities survive releases that deliberately omit .git."""
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_info
import distribution


class BuildInfo(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def repo(self):
        self.git('init', '-q')
        self.git('config', 'user.email', 'build-tests@example.invalid')
        self.git('config', 'user.name', 'Build identity test')
        (self.root / 'tracked.txt').write_text('first\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'first')
        return self.git('rev-parse', 'HEAD')

    def test_untagged_clean_build_and_virtual_about_fields(self):
        commit = self.repo()
        info = build_info.read(self.root)
        self.assertEqual(info, {'version': 'dev', 'commit': commit, 'short_commit': commit[:8],
                                'description': commit[:8], 'dirty': False, 'tag': ''})
        about = build_info.about(self.root)
        self.assertEqual(about['software'], commit[:8])
        self.assertEqual(about['display'], 'dev')
        self.assertIn(commit, about['extra'])

    def test_exact_tag_nearest_tag_and_dirty_tree(self):
        self.repo()
        self.git('tag', 'v0.4.0')
        self.assertEqual(build_info.read(self.root)['description'], 'v0.4.0')
        (self.root / 'tracked.txt').write_text('second\n')
        self.git('commit', '-qam', 'second')
        info = build_info.read(self.root)
        self.assertEqual(info['version'], 'v0.4.0')
        self.assertEqual(info['tag'], 'v0.4.0')
        self.assertEqual(info['description'], 'v0.4.0-1-g' + info['short_commit'])
        (self.root / 'new_feature.py').write_text('# not committed yet\n')
        dirty = build_info.read(self.root)
        self.assertTrue(dirty['dirty'])
        self.assertEqual(dirty['description'], info['description'] + '-dirty')
        self.assertIn(dirty['description'], build_info.about(self.root)['extra'])

    def test_child_directory_does_not_report_parent_repository(self):
        self.repo()
        child = self.root / 'installed'
        child.mkdir()
        self.assertEqual(build_info.read(child)['commit'], '')
        (child / '.git').mkdir()    # even a misleading marker cannot borrow parent metadata
        self.assertEqual(build_info.read(child)['commit'], '')

    def test_invalid_stamp_does_not_fabricate_an_identity(self):
        (self.root / build_info.STAMP).write_text(json.dumps({'commit': 'not a commit'}))
        self.assertEqual(build_info.read(self.root)['short_commit'], 'unknown')
        (self.root / build_info.STAMP).write_text('{broken')
        self.assertEqual(build_info.read(self.root)['description'], 'unknown')

    def test_copy_archive_and_redistribution_keep_the_original_stamp(self):
        distribution.copy(self.root)
        self.repo()
        self.git('tag', 'v1.2.3')
        expected = build_info.read(self.root)
        installed = self.base / 'installed'
        distribution.copy(installed, self.root)
        self.assertFalse((installed / '.git').exists())
        self.assertTrue((installed / 'tools/build_info.py').is_file())
        with patch('build_info.subprocess.run', side_effect=AssertionError('Git is unavailable')):
            self.assertEqual(build_info.read(installed), expected)
            copied = self.base / 'recopied'
            distribution.copy(copied, installed)
            self.assertEqual(build_info.read(copied), expected)
            archive = self.base / 'bridge.tar.gz'
            distribution.archive(archive, copied)
        with tarfile.open(archive, 'r:gz') as bundle:
            self.assertIsNotNone(bundle.getmember('cr100-bridge/tools/build_info.py'))
            with bundle.extractfile('cr100-bridge/' + build_info.STAMP) as stamp:
                self.assertEqual(json.load(stamp), expected)


if __name__ == '__main__': unittest.main()
