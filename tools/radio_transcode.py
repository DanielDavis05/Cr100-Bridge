"""Optional bounded FFmpeg → MP3 proxy for a renderer lacking a source codec.

Only prepared opaque tokens are served. The HTTP request never supplies an input
URL, and no process starts until a renderer actually requests a prepared stream.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import http.server
import os
import secrets
import selectors
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit


class RadioTranscodeError(Exception):
    pass


@dataclasses.dataclass
class _Source:
    url: str
    identity: str
    cancelled: threading.Event = dataclasses.field(default_factory=threading.Event)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RadioTranscoder:
    def __init__(self, advertised_host, host='0.0.0.0', port=0, ffmpeg=None,
                 max_streams=4, read_timeout=15, max_sources=256, process_factory=None,
                 log=None):
        self.advertised_host, self.host, self.port = advertised_host, host, int(port)
        self.ffmpeg = shutil.which('ffmpeg') if ffmpeg is None else shutil.which(str(ffmpeg))
        self.max_streams = max(1, int(max_streams))
        self.read_timeout = max(.1, float(read_timeout))
        self.max_sources = max(self.max_streams, int(max_sources))
        self.process_factory = process_factory or subprocess.Popen
        self.log = log or (lambda event: None)
        self.lock, self.capacity = threading.RLock(), threading.BoundedSemaphore(self.max_streams)
        self.sources = collections.OrderedDict()
        self.processes = {}
        self.server = self.thread = None
        self.closed = False

    @property
    def available(self):
        return bool(self.ffmpeg)

    @staticmethod
    def validate_source(url):
        if not isinstance(url, str) or len(url) > 16384 or any(c in url for c in '\r\n\x00'):
            raise RadioTranscodeError('Invalid Radio stream URL')
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https', 'mms', 'mmsh') and parsed.hostname
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise RadioTranscodeError('Only HTTP, HTTPS, MMS, or MMSH Radio sources can be converted')
        return url

    def start(self):
        with self.lock:
            if self.closed:
                raise RadioTranscodeError('Radio conversion service is closed')
            if not self.available:
                raise RadioTranscodeError('FFmpeg is not installed; this stream format cannot be converted')
            if self.server is not None:
                return self
            owner = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.0'
                server_version = 'CR100Radio'
                sys_version = ''

                def log_message(self, *args):
                    pass

                def _source(self):
                    parsed = urlsplit(self.path)
                    prefix, suffix = '/radio/audio/', '.mp3'
                    if parsed.query or not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
                        return None, None
                    token = parsed.path[len(prefix):-len(suffix)]
                    with owner.lock:
                        source = owner.sources.get(token)
                    return (token, source) if source and not source.cancelled.is_set() else (None, None)

                def _headers(self):
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Accept-Ranges', 'none')
                    self.send_header('Connection', 'close')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()

                def do_HEAD(self):
                    _, source = self._source()
                    if source is None:
                        self.send_error(404)
                    else:
                        self._headers()

                def do_GET(self):
                    self.connection.settimeout(owner.read_timeout)
                    token, source = self._source()
                    if source is None:
                        self.send_error(404)
                        return
                    if not owner.capacity.acquire(blocking=False):
                        self.send_error(503, 'Radio conversion capacity is in use')
                        return
                    process, headers_sent = None, False
                    try:
                        with owner.lock:
                            if source.cancelled.is_set() or owner.closed:
                                self.send_error(410)
                                return
                            process = owner._spawn(source.url)
                            owner.processes.setdefault(token, set()).add(process)
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            data = owner._chunk(process, selector, source)
                            if not data:
                                raise RadioTranscodeError('The station could not be decoded')
                            self._headers()
                            headers_sent = True
                            self.wfile.write(data)
                            while not source.cancelled.is_set():
                                data = owner._chunk(process, selector, source)
                                if not data:
                                    break
                                self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    except Exception as error:
                        owner.log({'event': 'radio-conversion-error', 'identity': source.identity,
                                   'error': str(error)})
                        if not headers_sent:
                            self.send_error(502, 'The station could not be converted')
                    finally:
                        if process is not None:
                            owner._terminate(process)
                            with owner.lock:
                                active = owner.processes.get(token)
                                if active is not None:
                                    active.discard(process)
                                    if not active:
                                        owner.processes.pop(token, None)
                        owner.capacity.release()

            try: self.server = _Server((self.host, self.port), Handler)
            except OSError as error:
                raise RadioTranscodeError('Radio conversion listener is unavailable: %s' % error) from error
            self.port = self.server.server_address[1]
            self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.1),
                                           name='radio-conversion-http', daemon=True)
            self.thread.start()
        return self

    def prepare(self, source_url, identity=None):
        source_url = self.validate_source(source_url)
        identity = str(identity) if identity is not None else 'source:' + hashlib.sha256(source_url.encode()).hexdigest()
        self.start()
        with self.lock:
            for token, source in self.sources.items():
                if source.identity == identity and source.url == source_url and not source.cancelled.is_set():
                    self.sources.move_to_end(token)
                    return self._public_url(token)
            superseded = [token for token, source in self.sources.items() if source.identity == identity]
        for token in superseded:
            self._revoke(token)
        with self.lock:
            token = secrets.token_urlsafe(24)
            self.sources[token] = _Source(source_url, identity)
            evicted = []
            while len(self.sources) > self.max_sources:
                candidate = next((key for key in self.sources if key not in self.processes), None)
                if candidate is None:
                    # Sources is bounded independently from active decoders.
                    candidate = next(iter(self.sources))
                evicted.append(candidate)
                self.sources[candidate].cancelled.set()
                self.sources.pop(candidate)
            result = self._public_url(token)
        for candidate in evicted:
            self._revoke(candidate)
        return result

    url = prepare

    def _public_url(self, token):
        host = str(self.advertised_host() if callable(self.advertised_host) else self.advertised_host)
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        return 'http://%s:%d/radio/audio/%s.mp3' % (host, self.port, token)

    def command(self, source_url):
        # No shell interpolation and no file/pipe/concat protocols from remote
        # playlists. Output is a finite-sized pipe buffer consumed incrementally.
        if source_url.startswith('mms://'):
            # FFmpeg exposes the HTTP transport under its explicit MMSH name.
            source_url = 'mmsh://' + source_url[len('mms://'):]
        return [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-protocol_whitelist', 'http,https,tcp,tls,crypto,mmsh,mmst',
                '-rw_timeout', str(int(self.read_timeout * 1000000)), '-i', source_url,
                '-vn', '-sn', '-dn', '-map_metadata', '-1', '-acodec', 'libmp3lame',
                '-b:a', '128k', '-ar', '44100', '-ac', '2', '-f', 'mp3', 'pipe:1']

    def _spawn(self, source_url):
        return self.process_factory(self.command(source_url), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _chunk(self, process, selector, source):
        deadline = time.monotonic() + self.read_timeout
        while not source.cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RadioTranscodeError('Radio decoder stopped producing audio')
            if selector.select(min(remaining, .25)):
                return os.read(process.stdout.fileno(), 32768)
        return b''

    @staticmethod
    def _terminate(process):
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        except (OSError, ProcessLookupError):
            pass
        finally:
            if process.stdout:
                process.stdout.close()

    def _revoke(self, token):
        with self.lock:
            source = self.sources.pop(token, None)
            if source:
                source.cancelled.set()
            processes = list(self.processes.get(token, ()))
        for process in processes:
            self._terminate(process)

    def stop(self, identity=None):
        with self.lock:
            tokens = [token for token, source in self.sources.items()
                      if identity is None or source.identity == str(identity)]
        for token in tokens:
            self._revoke(token)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(2)


# Both spellings are useful at the call site; there is one lifecycle owner.
RadioTranscode = RadioTranscoder
