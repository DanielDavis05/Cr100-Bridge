#!/usr/bin/env python3
"""Offline tests for the provider-neutral bridge queue."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_queue import BridgeQueue, QueueEntry


def entries(*titles):
    return [QueueEntry('spotify', 'spotify:track:%022d' % i, title, 'Artist %s' % title)
            for i, title in enumerate(titles)]


class BridgeQueueTests(unittest.TestCase):
    def test_add_reports_sonos_enqueue_results(self):
        queue = BridgeQueue()
        self.assertEqual(queue.numbers(), (0, 0))
        self.assertEqual(queue.add(entries('a', 'b')),
                         {'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '2', 'NewQueueLength': '2'})
        queue.select(1)
        result = queue.add(entries('c'), as_next=True)
        self.assertEqual(result['FirstTrackNumberEnqueued'], '2')
        self.assertEqual([e.title for e in queue.snapshot()], ['a', 'c', 'b'])
        self.assertEqual(queue.numbers(), (1, 3))

    def test_insert_before_cursor_keeps_current_track(self):
        queue = BridgeQueue()
        queue.add(entries('a', 'b', 'c'))
        queue.select(3)
        queue.remove(1)
        self.assertEqual(queue.current.title, 'c')
        self.assertEqual(queue.numbers(), (2, 2))
        queue.remove(2)
        self.assertEqual(queue.current.title, 'b')
        queue.clear()
        self.assertIsNone(queue.current)
        self.assertEqual(queue.numbers(), (0, 0))

    def test_advance_and_repeat(self):
        queue = BridgeQueue()
        queue.replace(entries('a', 'b'))
        self.assertEqual(queue.advance().title, 'b')
        self.assertFalse(queue.has_next())
        self.assertIsNone(queue.advance())
        queue.set_mode(False, 'all')
        self.assertEqual(queue.advance().title, 'a')
        queue.set_mode(False, 'one')
        self.assertEqual(queue.advance(finished=True).title, 'a')
        self.assertEqual(queue.advance().title, 'b')     # an explicit Next still moves on

    def test_back_stays_on_first(self):
        queue = BridgeQueue()
        queue.replace(entries('a', 'b'))
        self.assertEqual(queue.back().title, 'a')
        queue.select(2)
        self.assertEqual(queue.back().title, 'a')

    def test_shuffle_visits_every_track_once(self):
        queue = BridgeQueue(rng=random.Random(4))
        queue.replace(entries(*'abcdef'))
        queue.set_mode(True, 'off')
        seen = [queue.current.title]
        while (entry := queue.advance()) is not None: seen.append(entry.title)
        self.assertEqual(sorted(seen), list('abcdef'))
        self.assertEqual([e.title for e in queue.snapshot()], list('abcdef'))   # display order kept

    def test_locate_follows_speaker_forward(self):
        queue = BridgeQueue()
        queue.replace(entries('Intro', 'Song', 'Intro'))
        queue.select(2)
        self.assertTrue(queue.locate('intro', 'Artist Intro'))
        self.assertEqual(queue.numbers()[0], 3)
        self.assertFalse(queue.locate('Unknown'))
        self.assertEqual(queue.numbers()[0], 3)
        # Bose SoundTouch through Home Assistant reports "Artist - Title".
        self.assertTrue(queue.locate('Artist Song - Song', 'Artist Song'))
        self.assertEqual(queue.numbers()[0], 2)

    def test_invalid_numbers_rejected(self):
        queue = BridgeQueue()
        queue.add(entries('a'))
        for number in (0, 2, '1'):
            with self.assertRaises(ValueError): queue.select(number)
        with self.assertRaises(ValueError): queue.add([])


if __name__ == '__main__':
    unittest.main()
