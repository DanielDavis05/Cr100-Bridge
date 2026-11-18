#!/usr/bin/env python3
"""Offline tests for the continuous relay stream (no FFmpeg: a fake process supplies MP3 bytes)."""
import http.client
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_sources import AudioInput
from relay_stream import RelayStreamer, Segment


class FakeProcess:
    """Writes fixed bytes to a pipe, like FFmpeg's stdout, then closes it."""
    def __init__(self, payload):
        read, write = os.pipe()
        self.stdout = os.fdopen(read, 'rb', buffering=0)
        self.returncode = None
        def run():
            with os.fdopen(write, 'wb') as out: out.write(payload)
            self.returncode = 0
        threading.Thread(target=run, daemon=True).start()

    def poll(self): return self.returncode
    def terminate(self): pass
    def kill(self): pass
    def wait(self, timeout=None): return 0


class Feed:
    def __init__(self, segments):
        self.generation, self.segments = 0, list(segments)
        self.events, self.positions = [], []

    def segment(self):
        if not self.segments: return None
        self.segments.pop(0)
        self.generation += 1
        return Segment(self.generation, AudioInput(url='http://example.invalid/a'), 0)

    def progress(self, generation, seconds): self.positions.append(seconds)
    def finished(self, generation): self.events.append(('finished', generation))
    def failed(self, generation, message): self.events.append(('failed', generation))
    def disconnected(self): self.events.append(('disconnected',))


class RelayStreamTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        def factory(command, **kwargs):
            self.commands.append(command)
            return FakeProcess(b'ID3' + b'x' * 5000)
        self.streamer = RelayStreamer('127.0.0.1', host='127.0.0.1', port=0, ffmpeg='sh',
                                      process_factory=factory, sleep=lambda seconds: None)

    def tearDown(self):
        self.streamer.close()

    def get(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.streamer.port, timeout=5)
        connection.request('GET', path)
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read()

    def test_segments_are_joined_into_one_stream(self):
        feed = Feed([1, 2])
        token = self.streamer.register(feed)
        url = self.streamer.url(token)
        self.assertTrue(url.startswith('http://127.0.0.1:%d/relay/' % self.streamer.port))
        status, kind, body = self.get('/relay/%s.mp3' % token)
        self.assertEqual((status, kind), (200, 'audio/mpeg'))
        self.assertEqual(len(body), 2 * 5003)
        self.assertEqual(feed.events, [('finished', 1), ('finished', 2), ('disconnected',)])
        self.assertTrue(all('-i' in command and 'libmp3lame' in command for command in self.commands))

    def test_unknown_token_is_not_found(self):
        self.streamer.start()
        self.assertEqual(self.get('/relay/nope.mp3')[0], 404)
        token = self.streamer.register(Feed([]))
        self.streamer.unregister(token)
        self.assertEqual(self.get('/relay/%s.mp3' % token)[0], 404)

    def test_piped_input_uses_format_hint_and_seek(self):
        process = FakeProcess(b'')
        command = self.streamer.command(Segment(1, AudioInput(process=process, format='ogg'), 61.5))
        self.assertEqual(command[command.index('-f') + 1], 'ogg')
        self.assertEqual(command[command.index('-ss') + 1], '61.500')
        self.assertEqual(command[command.index('-i') + 1], 'pipe:0')
        self.assertGreater(command.index('-ss'), command.index('-i'))     # pipes cannot seek on input
        self.assertEqual(command[command.index('-id3v2_version') + 1], '0')
        url = self.streamer.command(Segment(1, AudioInput(url='http://example.invalid/a'), 5))
        self.assertLess(url.index('-ss'), url.index('-i'))
        self.assertNotIn('-nostdin', command)
        process.stdout.close()


if __name__ == '__main__':
    unittest.main()
