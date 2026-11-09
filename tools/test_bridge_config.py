"""Shared configuration persistence must retain permissions and survive failed edits."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge_config import update_config_file, write_json


class Persistence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'bridge-config.json'
        self.path.write_text('{"rooms": [], "other": "kept"}\n')
        self.path.chmod(0o600)

    def test_replace_preserves_private_mode_and_requested_mode_is_respected(self):
        write_json(self.path, {'rooms': [], 'other': 'updated'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text())['other'], 'updated')
        write_json(self.path, {'rooms': []}, mode=0o640)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_serialization_failure_keeps_original_and_removes_partial_temporary_file(self):
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            write_json(self.path, {'rooms': [], 'invalid': object()})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failed_edit_is_not_persisted_and_next_edit_can_acquire_lock(self):
        before = self.path.read_bytes()

        def invalid_edit(value):
            value['other'] = 'partial edit'
            raise ValueError('Rejected selection')

        with self.assertRaisesRegex(ValueError, 'Rejected selection'):
            update_config_file(self.path, invalid_edit)
        self.assertEqual(self.path.read_bytes(), before)
        result = update_config_file(self.path, lambda value: value.update(rooms=[{'name': 'Den'}]))
        self.assertEqual(result, {'rooms': [{'name': 'Den'}], 'other': 'kept'})
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
