import functools
import http.client
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from radio_transcode import RadioTranscoder, RadioTranscodeError
from radiotime import RadioTimeClient


class _PipeProcess:
    """A real readable pipe with a controllable process lifecycle."""
    def __init__(self, data=b'ID3fake-mp3', keep_open=False):
        reader, self.writer = os.pipe()
        self.stdout = os.fdopen(reader, 'rb', buffering=0)
        self.returncode = None
        self.terminated = False
        os.write(self.writer, data)
        if not keep_open:
            os.close(self.writer)
            self.writer = None
            self.returncode = 0
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = -15
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
    def kill(self):
        self.terminate()
    def wait(self, timeout=None):
        return self.returncode or 0


class TranscodeTests(unittest.TestCase):
    def make(self, **kwargs):
        calls, processes = [], []
        keep_open = kwargs.pop('hold', False)
        def factory(command, **options):
            calls.append((command, options))
            process = _PipeProcess(keep_open=keep_open)
            processes.append(process)
            return process
        proxy = RadioTranscoder('127.0.0.1', host='127.0.0.1', ffmpeg=shutil.which('python3'),
                                process_factory=factory, read_timeout=1, **kwargs)
        self.addCleanup(proxy.close)
        return proxy, calls, processes

    def get(self, url, method='GET'):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def test_lazy_opaque_url_head_and_no_arbitrary_source_parameter(self):
        proxy, calls, _ = self.make()
        uri = proxy.prepare('https://station.test/private/path?token=secret', identity='room-one')
        self.assertNotIn('secret', uri)
        self.assertNotIn('station.test', uri)
        self.assertEqual(calls, [])
        self.assertEqual(self.get(uri, 'HEAD')[0], 200)
        self.assertEqual(calls, [])
        self.assertEqual(self.get(uri + '?url=https://another.test/source')[0], 404)
        status, headers, body = self.get(uri)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'audio/mpeg')
        self.assertEqual(body, b'ID3fake-mp3')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('shell', calls[0][1])
        self.assertIn('https://station.test/private/path?token=secret', calls[0][0])
        self.assertNotIn('file', calls[0][0][calls[0][0].index('-protocol_whitelist') + 1].split(','))

    def test_invalid_sources_and_unavailable_ffmpeg(self):
        proxy, _, _ = self.make()
        for url in ('file:///etc/passwd', 'pipe:0', 'ftp://station.test/audio', 'https://station.test/\nInjected'):
            with self.assertRaises(RadioTranscodeError):
                proxy.prepare(url)
        missing = RadioTranscoder('127.0.0.1', ffmpeg='/does/not/exist')
        self.assertFalse(missing.available)
        with self.assertRaises(RadioTranscodeError):
            missing.prepare('https://station.test/stream.wma')
        missing.close()

    def test_capacity_stop_revokes_token_and_preserves_other_room(self):
        proxy, calls, processes = self.make(max_streams=1, hold=True)
        one = proxy.prepare('https://station.test/live.wma', identity='room-one')
        two = proxy.prepare('https://station.test/live.wma', identity='room-two')
        parsed = urlsplit(one)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request('GET', parsed.path)
        response = connection.getresponse()
        self.assertEqual(response.read(3), b'ID3')
        self.assertEqual(self.get(two)[0], 503)
        proxy.stop('room-one')
        self.assertTrue(processes[0].terminated)
        self.assertEqual(self.get(one)[0], 404)
        self.assertEqual(self.get(two, 'HEAD')[0], 200)
        connection.close()

    def test_reprepare_same_identity_replaces_source_and_bounded_registry(self):
        proxy, _, _ = self.make(max_sources=4)
        old = proxy.prepare('https://station.test/old.wma', identity='room-one')
        current = proxy.prepare('https://station.test/new.wma', identity='room-one')
        self.assertEqual(self.get(old)[0], 404)
        self.assertEqual(proxy.prepare('https://station.test/new.wma', identity='room-one'), current)
        for number in range(20):
            proxy.prepare('https://station.test/%d.wma' % number, identity='room-%d' % number)
        self.assertLessEqual(len(proxy.sources), 4)

    def test_source_exit_before_audio_is_reported_as_502(self):
        proxy, _, _ = self.make()
        proxy.process_factory = lambda *args, **kwargs: _PipeProcess(data=b'')
        uri = proxy.prepare('https://station.test/broken.wma')
        self.assertEqual(self.get(uri)[0], 502)


class RealWmaFixture(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe are optional runtime dependencies')
    def test_local_asx_wma_is_converted_to_mp3_without_a_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'fixture.wma'
            subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.35', '-c:a', 'wmav2', str(fixture)],
                           check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *args): pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=directory))
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            proxy = RadioTranscoder('127.0.0.1', host='127.0.0.1', read_timeout=3)
            try:
                source = 'http://127.0.0.1:%d/fixture.wma' % server.server_address[1]
                (root / 'fixture.asx').write_text('<ASX version="3.0"><ENTRY><REF HREF="%s"/></ENTRY></ASX>' % source)
                playlist = 'http://127.0.0.1:%d/fixture.asx' % server.server_address[1]
                streams = RadioTimeClient().resolve_url(playlist, formats=['wma'])
                self.assertEqual(streams[0].url, source)
                target = urlsplit(proxy.prepare(streams[0].url, identity='test-room'))
                connection = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
                connection.request('GET', target.path)
                response = connection.getresponse()
                data = response.read(100000)
                self.assertEqual(response.status, 200)
                self.assertGreater(len(data), 1000)
                decoded = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_entries', 'stream=codec_name',
                                          '-of', 'csv=p=0', 'pipe:0'], input=data, capture_output=True, check=True, timeout=5)
                self.assertEqual(decoded.stdout.strip(), b'mp3')
                connection.close()
            finally:
                proxy.close()
                server.shutdown()
                server.server_close()
                thread.join(2)


class RealHlsFixture(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe are optional runtime dependencies')
    def test_mp3_hls_master_and_segments_are_served_as_continuous_mp3(self):
        # WTMC advertises MP3 through Tune but wraps MP3 segments in HLS, which
        # older Sonos cannot render. Exercise the real HTTP and FFmpeg route.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'sine=frequency=523:duration=0.6', '-c:a', 'libmp3lame',
                '-b:a', '128k', str(root/'audio.mp3')], check=True, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            (root/'chunklist.m3u8').write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n'
                '#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:0.6,\naudio.mp3\n#EXT-X-ENDLIST\n')
            (root/'playlist.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.34"\nchunklist.m3u8\n')
            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self,*args): pass
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=directory))
            thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True)
            thread.start()
            proxy=RadioTranscoder('127.0.0.1',host='127.0.0.1',read_timeout=4)
            try:
                source='http://127.0.0.1:%d/playlist.m3u8'%server.server_port
                converted=urlsplit(proxy.prepare(source,identity='hls-test-room'))
                connection=http.client.HTTPConnection(converted.hostname,converted.port,timeout=8)
                connection.request('GET',converted.path)
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                self.assertEqual(response.getheader('Content-Type'),'audio/mpeg')
                audio=response.read();connection.close()
                self.assertGreater(len(audio),1000)
                probe=subprocess.run([shutil.which('ffprobe'),'-v','error','-f','mp3',
                    '-show_entries','stream=codec_name','-of','default=nw=1:nk=1','pipe:0'],
                    input=audio,capture_output=True,check=True,timeout=5)
                self.assertEqual(probe.stdout.strip(),b'mp3')
            finally:
                proxy.close();server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':
    unittest.main()
