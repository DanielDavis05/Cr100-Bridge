import concurrent.futures
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from radio_store import RadioStore


def station(number=1):
    return dict(id='station:s%d' % number, title='Station %d' % number, kind='station',
                metadata={'guide_id': 's%d' % number})


def _process_writes(path, offset):
    store = RadioStore(path)
    for number in range(offset, offset + 5):
        store.add_favorite(station(number))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'radio.json'

    def test_restart_and_cross_instance_read(self):
        one, two = RadioStore(self.path), RadioStore(self.path)
        one.add_favorite(station())
        two.add_favorite(station(2))
        self.assertEqual([row['id'] for row in one.favorites()], ['station:s1', 'station:s2'])
        self.assertEqual(len(RadioStore(self.path).favorites()), 2)

    def test_concurrent_instances_do_not_lose_updates(self):
        stores = [RadioStore(self.path) for _ in range(6)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: stores[i % len(stores)].add_favorite(station(i)), range(30)))
        self.assertEqual(len(RadioStore(self.path).favorites()), 30)
        self.assertEqual(RadioStore(self.path).snapshot()['radio_favorites_update_id'], 30)

    def test_separate_web_and_player_process_writes_do_not_lose_updates(self):
        context = multiprocessing.get_context('spawn')
        processes = [context.Process(target=_process_writes, args=(str(self.path), number * 5)) for number in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len(RadioStore(self.path).favorites()), 20)

    def test_favorite_types_duplicates_reorder_and_local_rename(self):
        store = RadioStore(self.path)
        store.add_favorite(station())
        store.add_favorite(station())
        store.add_favorite(dict(id='show:p17', kind='show', title='Fresh Air'))
        self.assertEqual(len(store.favorites()), 2)
        self.assertEqual(store.snapshot()['radio_favorites_update_id'], 2)
        store.update_favorite('station:s1', title='My Station', index=1)
        self.assertEqual(store.favorites()[1]['title'], 'My Station')
        self.assertEqual(len(store.favorites('shows')), 1)
        with self.assertRaises(ValueError):
            store.reorder_favorites(['station:s1', 'station:s1'])

    def test_custom_creation_update_delete_is_atomic_with_favorite(self):
        store = RadioStore(self.path)
        item = store.add_custom('Custom', 'https://station.test/live.mp3', logo='https://station.test/logo.jpg')
        self.assertEqual(store.favorites('stations')[0]['id'], item['id'])
        store.update_custom(item['id'], {'url': 'https://station.test/new.mp3', 'name': 'New Name'})
        self.assertEqual(store.custom(item['id'])['title'], 'New Name')
        self.assertEqual(store.favorites()[0]['url'], 'https://station.test/new.mp3')
        store.delete_custom(item['id'])
        self.assertEqual(store.favorites(), [])
        self.assertEqual(store.custom_stations(), [])

    def test_invalid_settings_and_import_leave_existing_file_untouched(self):
        store = RadioStore(self.path)
        store.add_favorite(station())
        original = self.path.read_bytes()
        for data in ({'automatic_reconnect': 'false'}, {'unknown': True}, {'location': {'latlon': '91,0'}}):
            with self.assertRaises(ValueError):
                store.update_settings(data)
        invalid = store.export_data()
        invalid['favorites'].append(dict(id='bogus', title='Invalid', kind='station'))
        with self.assertRaises(ValueError):
            store.import_data(invalid)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_persisted_state_is_not_overwritten(self):
        self.path.write_text('{bad json')
        with self.assertRaises(ValueError):
            RadioStore(self.path)
        self.assertEqual(self.path.read_text(), '{bad json')

    def test_history_off_bounds_searches_and_bookmarks(self):
        store = RadioStore()
        for i in range(35):
            store.add_history(station(i))
            store.add_search('query%d' % i)
        self.assertEqual(len(store.history()), 30)
        self.assertEqual(len(store.searches()), 10)
        store.update_settings({'history': False})
        store.add_history(station(99))
        store.add_search('hidden query')
        self.assertEqual(store.history(), [])
        self.assertEqual(store.searches(), [])
        store.set_bookmark('episode:t1', 30, 100)
        self.assertEqual(store.bookmark('episode:t1')['position'], 30)
        with self.assertRaises(ValueError):
            store.set_bookmark('station:s1', 3)
        with self.assertRaises(ValueError):
            store.set_bookmark('episode:t1', 300, 100)
        store.update_settings({'episode_resume': False})
        self.assertIsNone(store.bookmark('episode:t1'))

    def test_export_import_reset_and_refresh_preserve_saved_data(self):
        store = RadioStore(self.path)
        store.add_favorite(station())
        store.set_location({'label': 'Boston', 'latlon': '42.36,-71.06'})
        exported = store.export_data()
        other = RadioStore()
        other.import_data(exported)
        other.reset_settings()
        self.assertEqual(other.favorites()[0]['id'], 'station:s1')
        before = store.snapshot()
        store.refresh()
        after = RadioStore(self.path).snapshot()
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertEqual(after['radio_directory_update_id'], before['radio_directory_update_id'] + 1)
        self.assertEqual(after['settings'], before['settings'])

    def test_failed_persistence_does_not_commit_memory(self):
        store = RadioStore()
        def fail(state):
            raise OSError('disk full')
        store._persist = fail
        with self.assertRaises(OSError):
            store.add_favorite(station())
        self.assertEqual(store.favorites(), [])

    def test_merge_import_applies_explicit_preferences_without_losing_sources(self):
        store = RadioStore(self.path)
        store.add_favorite(station(1))
        store.update_settings({'quality': 'low', 'history': False})
        store.import_data({'version': 1, 'settings': {'quality': 'best'}, 'favorites': [station(2)]}, merge=True)
        restarted = RadioStore(self.path)
        self.assertEqual({row['id'] for row in restarted.favorites()}, {'station:s1', 'station:s2'})
        self.assertEqual(restarted.settings()['quality'], 'best')
        self.assertFalse(restarted.settings()['history'])
        restarted.import_data({'version': 1, 'favorites': [station(3)]}, merge=True)
        self.assertEqual(restarted.settings()['quality'], 'best')
        self.assertFalse(restarted.settings()['history'])


if __name__ == '__main__':
    unittest.main()
