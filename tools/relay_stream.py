"""Continuous MP3 relay streams for speakers that can only play a URL.

Each room using the relay route registers a *feed* (bridge_player.RelayRoute)
under an unguessable token. A speaker fetching /relay/<token>.mp3 receives one
endless MP3 stream: the feed supplies segments (an entry's audio from a start
offset), FFmpeg converts each to constant-bitrate MP3, and this module paces
output to real time after a short burst so position, Next and Seek stay close
to what the speaker is playing. A changed feed generation (Next, Seek, a new
queue) switches the audio mid-stream without the speaker reconnecting.
"""
from __future__ import annotations

import http.server
import os
import secrets
import selectors
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

BITRATE = 192000
BYTES_PER_SECOND = BITRATE // 8
BURST_SECONDS = 3.0
CHUNK = 8192


class RelayError(Exception):
    pass


class Segment:
    """One stretch of audio: an AudioInput from start seconds, for one feed generation."""
    def __init__(self, generation, audio, start=0.0):
        self.generation, self.audio, self.start = generation, audio, float(start or 0)


class RelayStreamer:
    def __init__(self, advertised_host, host='0.0.0.0', port=0, ffmpeg=None, max_streams=4,
                 read_timeout=20, process_factory=None, clock=time.monotonic, sleep=time.sleep, log=None):
        self.advertised_host, self.host, self.port = advertised_host, host, int(port)
        self.ffmpeg = shutil.which('ffmpeg') if ffmpeg is None else shutil.which(str(ffmpeg))
        self.capacity = threading.BoundedSemaphore(max(1, int(max_streams)))
        self.read_timeout = float(read_timeout)
        self.process_factory = process_factory or subprocess.Popen
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda event: None)
        self.lock = threading.RLock()
        self.feeds = {}                    # token -> feed
        self.clients = {}                  # token -> client serial (newest wins)
        self.server = self.thread = None
        self.closed = False

    @property
    def available(self):
        return bool(self.ffmpeg)

    # --- feeds --------------------------------------------------------------------
    def register(self, feed):
        token = secrets.token_urlsafe(18)
        with self.lock: self.feeds[token] = feed
        return token

    def unregister(self, token):
        with self.lock:
            self.feeds.pop(token, None)
            self.clients.pop(token, None)

    def url(self, token):
        self.start()
        host = str(self.advertised_host() if callable(self.advertised_host) else self.advertised_host)
        if ':' in host and not host.startswith('['): host = '[' + host + ']'
        return 'http://%s:%d/relay/%s.mp3' % (host, self.port, token)

    # --- server ---------------------------------------------------------------------
    def start(self):
        with self.lock:
            if self.closed: raise RelayError('Relay streaming has stopped')
            if not self.available: raise RelayError('FFmpeg is not installed; relay playback is unavailable')
            if self.server is not None: return self
            owner = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.0'
                def log_message(self, *args): pass

                def _feed(self):
                    path = urlsplit(self.path).path
                    if not (path.startswith('/relay/') and path.endswith('.mp3')): return None, None
                    token = path[len('/relay/'):-len('.mp3')]
                    with owner.lock: return token, owner.feeds.get(token)

                def _headers(self):
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('icy-name', 'CR100 Bridge')
                    self.end_headers()

                def do_HEAD(self):
                    token, feed = self._feed()
                    if feed is None: self.send_error(404); return
                    self._headers()

                def do_GET(self):
                    token, feed = self._feed()
                    if feed is None: self.send_error(404); return
                    if not owner.capacity.acquire(blocking=False): self.send_error(503); return
                    try:
                        self._headers()
                        owner.serve(token, feed, self.wfile)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                        pass
                    finally:
                        owner.capacity.release()

            server = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
            server.daemon_threads = True
            self.server, self.port = server, server.server_address[1]
            self.thread = threading.Thread(target=server.serve_forever, name='relay-stream', daemon=True)
            self.thread.start()
            return self

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
            self.feeds.clear()
            server = self.server
        if server:
            server.shutdown(); server.server_close()

    # --- streaming --------------------------------------------------------------------
    def command(self, segment):
        audio = segment.audio
        command = [self.ffmpeg, '-hide_banner', '-loglevel', 'error']
        if audio.url:
            command += ['-nostdin', '-protocol_whitelist', 'http,https,tcp,tls,crypto',
                        '-rw_timeout', str(int(self.read_timeout * 1000000))]
        elif audio.format:
            command += ['-f', audio.format]
        seek = ['-ss', '%.3f' % segment.start] if segment.start > 0 else []
        # A URL can seek on input; a pipe cannot, so decode and discard up to the offset.
        if audio.url: command += seek
        command += ['-i', audio.url or 'pipe:0']
        if not audio.url: command += seek
        # Segments are concatenated into one stream: no ID3/Xing headers mid-stream.
        command += ['-vn', '-sn', '-dn', '-map_metadata', '-1',
                    '-acodec', 'libmp3lame', '-b:a', str(BITRATE // 1000) + 'k', '-ar', '44100', '-ac', '2',
                    '-id3v2_version', '0', '-write_xing', '0', '-f', 'mp3', 'pipe:1']
        return command

    def _spawn(self, segment):
        stdin = segment.audio.process.stdout if segment.audio.process is not None else subprocess.DEVNULL
        return self.process_factory(self.command(segment), stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)

    @staticmethod
    def _terminate(process):
        try:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=1)
        except (OSError, ProcessLookupError):
            pass
        finally:
            if process.stdout: process.stdout.close()

    def serve(self, token, feed, out):
        """Write the feed's audio to out until the feed ends or this client is replaced."""
        with self.lock:
            serial = self.clients.get(token, 0) + 1
            self.clients[token] = serial
        def current():
            with self.lock: return self.clients.get(token) == serial and self.feeds.get(token) is feed
        try:
            self._serve(feed, out, current)
        finally:
            gone = getattr(feed, 'disconnected', None)
            if callable(gone) and current(): gone()

    def _serve(self, feed, out, current):
        sent_total, started = 0, self.clock()
        while current():
            segment = feed.segment()
            if segment is None: return
            process = None
            try:
                process = self._spawn(segment)
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                sent, ended = 0, False
                last_data = self.clock()
                while current() and feed.generation == segment.generation:
                    if not selector.select(.25):
                        if self.clock() - last_data > self.read_timeout:
                            raise RelayError('Relay audio stopped arriving')
                        continue
                    data = os.read(process.stdout.fileno(), CHUNK)
                    if not data: ended = True; break
                    last_data = self.clock()
                    out.write(data)
                    sent += len(data); sent_total += len(data)
                    feed.progress(segment.generation,
                                  segment.start + max(0.0, sent / BYTES_PER_SECOND - BURST_SECONDS))
                    # Pace to real time once the speaker has its initial buffer.
                    ahead = sent_total / BYTES_PER_SECOND - BURST_SECONDS - (self.clock() - started)
                    if ahead > 0: self.sleep(min(ahead, 1.0))
                selector.close()
                if ended and current() and feed.generation == segment.generation:
                    feed.finished(segment.generation)
            except RelayError as error:
                self.log({'event': 'relay-audio-failed', 'error': str(error)})
                feed.failed(segment.generation, str(error))
            finally:
                if process is not None: self._terminate(process)
                segment.audio.close()
