"""Spotify as a bridge media source: catalogue expansion and relay audio.

Expansion uses the bridge's read-only Web API client. Relay audio comes from an
isolated librespot helper process (``--audio-worker``) that signs in with the
streaming authorization already used for Connect control, and writes one
track's Ogg Vorbis stream to stdout. Credentials travel only over its stdin;
they never appear in argv, logs or errors.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from backends import BackendError
from bridge_queue import MAX_ENTRIES, QueueEntry
from media_sources import AudioInput

URI = re.compile(r'spotify:(track|album|playlist):([A-Za-z0-9]{22})\Z')
TRACK = re.compile(r'spotify:track:[A-Za-z0-9]{22}\Z')
PAGE = 50
AUDIO_DEVICE_ID = hashlib.sha1(b'cr100-bridge-relay-audio').hexdigest()
IMAGE_HOSTS = frozenset(('i.scdn.co', 'mosaic.scdn.co', 'image-cdn-ak.spotifycdn.com',
                         'image-cdn-fa.spotifycdn.com'))


def _image(images):
    candidates = [i for i in images or () if isinstance(i, dict) and isinstance(i.get('url'), str)]
    candidates.sort(key=lambda i: i.get('width') or 10000)
    for candidate in candidates:
        if re.match(r'https://([a-z0-9.-]+)/', candidate['url']) and \
                candidate['url'].split('/')[2] in IMAGE_HOSTS:
            return candidate['url']
    return ''


def _entry(item, album=None):
    """A QueueEntry for a Web API track object, or None when it cannot be played."""
    if not isinstance(item, dict) or item.get('type', 'track') != 'track' or item.get('is_local'):
        return None
    if item.get('is_playable') is False: return None
    ident = item.get('id')
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9]{22}', ident): return None
    title = item.get('name')
    if not isinstance(title, str) or not title: return None
    album = item.get('album') or album or {}
    artists = ', '.join(a['name'] for a in item.get('artists') or ()
                        if isinstance(a, dict) and isinstance(a.get('name'), str))
    duration = item.get('duration_ms')
    return QueueEntry('spotify', 'spotify:track:' + ident, title, artists,
                      album.get('name') if isinstance(album.get('name'), str) else '',
                      _image(album.get('images')),
                      duration / 1000 if isinstance(duration, (int, float)) and duration > 0 else None)


class SpotifySource:
    name = 'spotify'

    def __init__(self, client, connect, *, log=print, python=None, process_factory=None):
        self.client, self.connect, self.log = client, connect, log
        self.python = python or sys.executable
        self.process_factory = process_factory or subprocess.Popen

    def owns(self, uri):
        return isinstance(uri, str) and URI.fullmatch(uri) is not None

    def available(self):
        return self.client is not None

    def audio_available(self):
        """Relay audio needs the streaming sign-in and the librespot package."""
        try:
            return self.connect is not None and self.connect.available()
        except Exception:
            return False

    # --- catalogue -----------------------------------------------------------------
    def _get(self, path, params=None):
        try:
            result = self.client.get(path, params)
        except Exception:
            raise BackendError('Spotify could not load this selection. Try again.') from None
        if not isinstance(result, dict): raise BackendError('Spotify returned an unreadable selection')
        return result

    def expand(self, uri, kind=None):
        match = URI.fullmatch(uri or '')
        if not match: raise BackendError('Unsupported Spotify item')
        what, ident = match.groups()
        if what == 'track':
            entry = _entry(self._get('/tracks/' + ident))
            if entry is None: raise BackendError('This Spotify track cannot be played')
            return [entry]
        if what == 'album':
            album = self._get('/albums/' + ident)
            page = album.get('tracks') or {}
            entries = self._collect(page, '/albums/%s/tracks' % ident, lambda row: _entry(row, album))
        else:
            page = self._get('/playlists/%s/items' % ident, {'offset': 0, 'limit': PAGE})
            entries = self._collect(page, '/playlists/%s/items' % ident,
                                    lambda row: _entry((row.get('item') or row.get('track'))
                                                       if isinstance(row, dict) else None))
        if not entries: raise BackendError('This Spotify selection has no playable tracks')
        return entries

    def _collect(self, page, path, convert):
        entries, offset = [], 0
        total = page.get('total') if type(page.get('total')) is int else None
        while True:
            rows = page.get('items')
            if not isinstance(rows, list): raise BackendError('Spotify returned an unreadable track list')
            for row in rows:
                entry = convert(row)
                if entry is not None: entries.append(entry)
            offset += len(rows)
            if not rows or len(entries) >= MAX_ENTRIES or total is None or offset >= total: break
            page = self._get(path, {'offset': offset, 'limit': PAGE})
        return entries[:MAX_ENTRIES]

    # --- relay audio --------------------------------------------------------------
    def open_audio(self, entry):
        if not TRACK.fullmatch(entry.ref or ''): raise BackendError('Unsupported Spotify track')
        if not self.audio_available():
            raise BackendError('Spotify relay playback needs the bridge Spotify playback sign-in')
        account, token = self.connect.credentials()
        process = self.process_factory(
            [self.python, str(Path(__file__).resolve()), '--audio-worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            close_fds=True)
        try:
            process.stdin.write(json.dumps({'account': account, 'oauth_token': token,
                                            'device_id': AUDIO_DEVICE_ID, 'track': entry.ref}).encode() + b'\n')
            process.stdin.close()
        except (OSError, ValueError):
            AudioInput(process=process).close()
            raise BackendError('Spotify relay audio could not start') from None
        return AudioInput(process=process, format='ogg')


def _audio_worker():
    """Sign in once and write one track's decrypted Ogg Vorbis audio to stdout."""
    import contextlib
    import logging
    import socket
    logging.disable(logging.CRITICAL)
    socket.setdefaulttimeout(15)
    raw = sys.stdin.buffer.readline(65537)
    if len(raw) > 65536: return 2
    request = json.loads(raw)
    if not TRACK.fullmatch(request.get('track') or ''): return 2
    out = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    sink = open(os.devnull, 'w')
    session = None
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
            from librespot.core import Session
            from librespot.metadata import TrackId
            from librespot.proto import Authentication_pb2 as Authentication
            configuration = Session.Configuration.Builder().set_store_credentials(False) \
                .set_cache_enabled(False).build()
            builder = Session.Builder(configuration).set_device_id(request['device_id']) \
                .set_device_name('CR100 bridge relay')
            builder.login_credentials = Authentication.LoginCredentials(
                username=request['account'], typ=Authentication.AuthenticationType.AUTHENTICATION_SPOTIFY_TOKEN,
                auth_data=request['oauth_token'].encode())
            session = builder.create()
            if session.username() != request['account']: return 3
            loaded = session.content_feeder().load(TrackId.from_uri(request['track']),
                                                   VorbisOnlyAudioQuality(AudioQuality.VERY_HIGH), False, None)
            stream = loaded.input_stream.stream()
        while True:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                data = stream.read(32768)
            if not data or data == -1: break
            out.write(bytes(data))
        return 0
    except BrokenPipeError:
        return 0
    except Exception:
        return 4
    finally:
        if session is not None:
            with contextlib.suppress(Exception), contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                session.close()
        sink.close()
        with contextlib.suppress(Exception): out.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--audio-worker']:
        raise SystemExit(_audio_worker())
    raise SystemExit('This module is used by the bridge.')
