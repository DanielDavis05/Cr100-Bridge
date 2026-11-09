"""Accountless public RadioTime OPML client, with bounded requests and honest errors.

The transport is injectable for fixtures. No partner credentials are borrowed from
another device. A successful HTTP response can still carry an OPML API fault.
"""
from __future__ import annotations

import collections
import copy
import dataclasses
import json
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

BASE_URL = 'https://opml.radiotime.com/'
USER_AGENT = 'cr100-bridge-radio/1.0'
PLACEHOLDER_NAMES = ('notcompatible.', 'nostream.', 'notavailable.', 'georestricted.')
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class RadioTimeError(Exception):
    def __init__(self, message, code='', status=502):
        super().__init__(message)
        self.code, self.status = code, status


@dataclasses.dataclass
class RadioResponse:
    head: dict
    body: list
    url: str = ''
    ttl: float = 0
    headers: dict = dataclasses.field(default_factory=dict)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def as_dict(self):
        return dict(head=copy.deepcopy(self.head), body=copy.deepcopy(self.body),
                    url=self.url, ttl=self.ttl, headers=dict(self.headers))


@dataclasses.dataclass
class RadioStream:
    url: str
    format: str = ''
    bitrate: int = 0
    reliability: int = 0
    metadata: dict = dataclasses.field(default_factory=dict)

    @property
    def media_type(self):
        return self.format

    def as_dict(self):
        return dataclasses.asdict(self)


def outlines(rows):
    """Yield the tree without treating section wrappers as stations."""
    for row in rows or []:
        if isinstance(row, dict):
            yield row
            yield from outlines(row.get('children') or [])


def unavailable_url(url):
    path = urlsplit(str(url or '')).path.lower()
    return any(name in path for name in PLACEHOLDER_NAMES)


def _integer(value, default=0):
    try:
        return max(0, int(float(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def _xml_element(element):
    row = dict(element.attrib)
    row['element'] = element.tag.rsplit('}', 1)[-1]
    children = list(element)
    if children:
        if all(child.tag.rsplit('}', 1)[-1] == 'outline' for child in children):
            row['children'] = [_xml_element(child) for child in children]
        else:
            for child in children:
                tag = child.tag.rsplit('}', 1)[-1]
                row[tag] = _xml_element(child) if list(child) else child.text or ''
    return row


def _parse(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        payload = payload.encode()
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        try:
            root = ET.fromstring(payload)
            head, body = root.find('head'), root.find('body')
            rows = [_xml_element(child) for child in body] if body is not None else []
            normalized = []
            for row in rows:
                wrapped = next((row.get(kind) for kind in ('station', 'show', 'topic')
                                if isinstance(row.get(kind), dict)), None)
                normalized.append(wrapped if row.get('type') == 'object' and wrapped else row)
            return dict(head={child.tag: child.text or '' for child in head} if head is not None else {},
                        body=normalized)
        except ET.ParseError as error:
            raise RadioTimeError('Radio directory returned an unreadable response', 'malformed') from error


class RadioTimeClient:
    def __init__(self, transport=None, timeout=12, clock=time.monotonic,
                 min_request_interval=0.1, max_cache_entries=512):
        self.transport, self.timeout, self.clock = transport, timeout, clock
        self.min_request_interval = max(0, min_request_interval)
        self.max_cache_entries = max_cache_entries
        self._cache = collections.OrderedDict()
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._flights = {}
        self._last_request = float('-inf')

    @staticmethod
    def url(path='Browse.ashx', params=None):
        url = urljoin(BASE_URL, path)
        parsed = urlsplit(url)
        if parsed.hostname != 'opml.radiotime.com' or parsed.scheme not in ('http', 'https') or parsed.username:
            raise RadioTimeError('The directory supplied an unsupported navigation URL', 'navigation_url', 400)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        # Accountless means accountless, including links returned by upstream.
        for key in list(query):
            if key.lower() in ('partnerid', 'serial', 'username', 'password', 'token'):
                del query[key]
        for key, value in (params or {}).items():
            if value is not None:
                if key.lower() in ('partnerid', 'serial', 'username', 'password', 'token'):
                    raise RadioTimeError('Account credentials are not used by public Radio', 'account', 400)
                query[key] = str(value)
        query['render'] = 'json'
        return urlunsplit(('https', 'opml.radiotime.com', parsed.path, urlencode(sorted(query.items())), ''))

    def _raw(self, url, max_bytes=MAX_RESPONSE_BYTES, headers=None, method=None, truncate=False):
        request_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json, text/xml;q=0.8'}
        request_headers.update(headers or {})
        with self._request_lock:
            wait = self.min_request_interval - (self.clock() - self._last_request)
            if wait > 0:
                time.sleep(min(wait, self.min_request_interval))
            self._last_request = self.clock()
        try:
            if self.transport:
                kwargs = dict(headers=request_headers, timeout=self.timeout)
                if method:
                    kwargs['method'] = method
                result = self.transport(url, **kwargs)
                if isinstance(result, RadioResponse):
                    return 200, result.headers, result.as_dict()
                if isinstance(result, tuple) and len(result) == 3:
                    return result
                return 200, {}, result
            with urlopen(Request(url, headers=request_headers, method=method), timeout=self.timeout) as response:
                payload = response.read(max_bytes if truncate else max_bytes + 1)
                if len(payload) > max_bytes and not truncate:
                    raise RadioTimeError('Radio response exceeded the size limit', 'response_size')
                return response.status, dict(response.headers.items()), payload
        except HTTPError as error:
            raise RadioTimeError('Radio server returned HTTP %s' % error.code, 'http', error.code) from error
        except RadioTimeError:
            raise
        except Exception as error:
            raise RadioTimeError('Cannot reach the Radio directory: %s' % error, 'transport') from error

    def request(self, path='Browse.ashx', params=None, cache=True, default_ttl=60, **query):
        params = dict(params or {}, **query)
        url = self.url(path, params)
        while True:
            with self._lock:
                cached = self._cache.get(url)
                if cached and self.clock() < cached[0] and cache:
                    self._cache.move_to_end(url)
                    return copy.deepcopy(cached[1])
                flight = self._flights.get(url)
                if flight is None:
                    flight = self._flights[url] = threading.Event()
                    break
            # Bound waiting even if an injected transport misbehaves.
            if not flight.wait(self.timeout + 1):
                raise RadioTimeError('Timed out waiting for Radio response', 'timeout')
        try:
            status, headers, payload = self._raw(url)
            if not 200 <= int(status) < 300:
                raise RadioTimeError('Radio directory HTTP error %s' % status, 'http', int(status))
            data = _parse(payload)
            if not isinstance(data, dict):
                raise RadioTimeError('Invalid Radio response envelope', 'malformed')
            head, body = data.get('head') or {}, data.get('body', [])
            if not isinstance(head, dict) or not isinstance(body, list):
                raise RadioTimeError('Invalid Radio response contents', 'malformed')
            code = str(head.get('status', '200'))
            if head.get('fault') or code not in ('200', '0'):
                raise RadioTimeError(head.get('fault') or 'Radio request failed (%s)' % code,
                                     head.get('fault_code', 'api'), _integer(code, 502))
            lower_headers = {str(k).lower(): str(v) for k, v in headers.items()}
            control = lower_headers.get('cache-control', '')
            age = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', control, re.I)
            ttl = float(age.group(1)) if age else float(_integer(head.get('ttl'), default_ttl))
            if any(token in control.lower() for token in ('no-store', 'no-cache')):
                ttl = 0
            ttl = max(0, min(ttl, 7 * 86400))
            result = RadioResponse(head, body, url, ttl, lower_headers)
            if cache and ttl:
                with self._lock:
                    self._cache[url] = (self.clock() + ttl, copy.deepcopy(result))
                    self._cache.move_to_end(url)
                    while len(self._cache) > self.max_cache_entries:
                        self._cache.popitem(last=False)
            return result
        finally:
            with self._lock:
                self._flights.pop(url, None)
                flight.set()

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def browse(self, url='Browse.ashx', **params):
        return self.request(url, params)

    def search(self, query, kind='stations', **params):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError('Enter a search query of up to 500 characters')
        mode = {'station': 'stations', 'show': 'shows', 'host': 'hosts'}.get(kind, kind)
        if mode not in ('stations', 'shows', 'hosts', 'all'):
            raise ValueError('Unknown Radio search type')
        if mode == 'stations':
            params['filter'] = 's'
        elif mode in ('shows', 'hosts'):
            params['filter'] = 'p'
        return self.request('Search.ashx', dict(query=query.strip(), **params))

    def describe(self, identity, **params):
        return self.request('Describe.ashx', dict(id=identity, **params), default_ttl=300)

    def now_playing(self, identity):
        # The original API explicitly disallows frequent polling: cache headers win.
        return self.request('Describe.ashx', dict(c='nowplaying', id=identity), default_ttl=300)

    def options(self, identity):
        return self.request('Options.ashx', dict(id=identity), default_ttl=300)

    def schedule(self, identity, **params):
        return self.request('Browse.ashx', dict(c='schedule', id=identity, **params), default_ttl=300)

    def local(self, latlon=None, **params):
        return self.request('Browse.ashx', dict(c='local', latlon=latlon, **params))

    def resolve_streams(self, identity=None, formats=None, url=None, sid=None):
        if isinstance(formats, str):
            formats = tuple(part.strip() for part in formats.split(',') if part.strip())
        formats = tuple(formats or ('mp3', 'aac', 'wma', 'ogg', 'hls'))
        # Tune's format filter describes audio codecs, not the transport
        # container. In particular, WTMC advertises its MP3-in-HLS URL only
        # when MP3 is requested, and returns a placeholder for formats=hls.
        upstream_formats = [format for format in formats if format != 'hls']
        if 'hls' in formats:
            upstream_formats.extend(format for format in ('mp3', 'aac') if format not in upstream_formats)
        params = dict(formats=','.join(upstream_formats))
        if identity:
            params['id'] = identity
        if sid:
            params['sid'] = sid
        response = self.request(url or 'Tune.ashx', params, cache=False, default_ttl=0)
        streams, seen = [], set()
        for row in outlines(response.body):
            target = row.get('url') or row.get('URL')
            if row.get('element') != 'audio' and row.get('type') != 'audio':
                continue
            if not isinstance(target, str) or urlsplit(target).scheme not in ('http', 'https', 'mms', 'mmsh', 'rtsp') or unavailable_url(target):
                continue
            # A Tune response can itself contain OPML listening options, not audio.
            if urlsplit(target).hostname == 'opml.radiotime.com':
                continue
            provider_type = str(row.get('media_type', row.get('formats', ''))).lower()
            path = urlsplit(target).path.lower()
            is_hls = path.endswith('.m3u8') or provider_type in (
                'hls', 'm3u8', 'application/x-mpegurl', 'application/vnd.apple.mpegurl',
                'audio/mpegurl', 'audio/x-mpegurl')
            media_type = 'hls' if is_hls else provider_type
            if target not in seen:
                seen.add(target)
                if path.endswith(('.asx', '.pls', '.m3u')):
                    try:
                        expanded = self.resolve_url(target, formats)
                    except RadioTimeError:
                        continue
                    for stream in expanded:
                        stream.bitrate = stream.bitrate or _integer(row.get('bitrate'))
                        stream.reliability = stream.reliability or _integer(row.get('reliability'))
                        stream.metadata = dict(row, **stream.metadata)
                        if stream.format == 'hls':
                            stream.metadata['provider_media_type'] = provider_type
                            if provider_type in ('mp3', 'aac', 'wma', 'ogg', 'opus'):
                                stream.metadata['codec'] = provider_type
                        if not any(previous.url == stream.url for previous in streams):
                            streams.append(stream)
                else:
                    if (media_type and media_type not in formats and ',' not in media_type
                            or not media_type and set(formats) == {'hls'}):
                        continue
                    metadata = copy.deepcopy(row)
                    if is_hls:
                        metadata.update(container='hls', provider_media_type=provider_type)
                        if provider_type in ('mp3', 'aac', 'wma', 'ogg', 'opus'):
                            metadata['codec'] = provider_type
                    streams.append(RadioStream(target, media_type, _integer(row.get('bitrate')),
                                               _integer(row.get('reliability')), metadata))
        if not streams:
            raise RadioTimeError('No compatible public stream is currently available', 'unavailable', 404)
        return streams

    def resolve_url(self, url, formats=None, depth=0, seen=None):
        """Expand ordinary M3U/PLS alternatives; leave HLS playlists to the renderer."""
        parsed = urlsplit(str(url or ''))
        if parsed.scheme not in ('http', 'https', 'mms', 'mmsh', 'rtsp') or not parsed.hostname or unavailable_url(url):
            raise RadioTimeError('Invalid or unavailable station URL', 'stream_url', 400)
        formats = tuple(formats.split(',')) if isinstance(formats, str) else tuple(formats or ('mp3', 'aac', 'wma', 'ogg', 'hls'))
        if parsed.scheme in ('mms', 'mmsh'):
            if 'wma' not in formats:
                raise RadioTimeError('This renderer does not support Windows Media streams', 'format', 415)
            return [RadioStream(url, 'wma')]
        seen = set() if seen is None else seen
        if depth > 4 or url in seen:
            raise RadioTimeError('Station playlist contains a loop', 'playlist_loop')
        seen.add(url)
        suffix = parsed.path.lower().rsplit('.', 1)[-1]
        if suffix in ('m3u', 'pls', 'asx'):
            _, _, raw = self._raw(url, max_bytes=1024 * 1024)
            if not isinstance(raw, (str, bytes)):
                raise RadioTimeError('Unreadable station playlist', 'playlist')
            text = raw.decode('utf-8-sig', errors='replace') if isinstance(raw, bytes) else raw
            # HLS playlists can also use the .m3u suffix. Their segment list is
            # one continuous stream, not independent alternate station URLs.
            if suffix == 'm3u' and any(line.lstrip().upper().startswith('#EXT-X-') for line in text.splitlines()):
                if 'hls' not in formats:
                    raise RadioTimeError('This renderer does not support the station format', 'format', 415)
                return [RadioStream(url, 'hls', metadata={'container': 'hls'})]
            targets = []
            if suffix == 'asx':
                try:
                    document = ET.fromstring(text)
                    targets = [element.attrib.get('href') or element.attrib.get('HREF')
                               for element in document.iter() if element.tag.lower().rsplit('}', 1)[-1] == 'ref']
                    targets = [urljoin(url, target) for target in targets if target]
                except ET.ParseError as error:
                    raise RadioTimeError('Unreadable ASX station playlist', 'playlist') from error
            for line in text.splitlines():
                if suffix == 'asx':
                    break
                line = line.strip()
                if not line or line.startswith(('#', '[', ';')):
                    continue
                if suffix == 'pls':
                    match = re.match(r'File\d+\s*=\s*(.+)', line, re.I)
                    if not match:
                        continue
                    line = match.group(1)
                targets.append(urljoin(url, line))
            result = []
            for target in targets[:32]:
                try:
                    result.extend(self.resolve_url(target, formats, depth + 1, seen))
                except RadioTimeError:
                    continue
            if not result:
                raise RadioTimeError('Station playlist has no compatible streams', 'playlist')
            return list({stream.url: stream for stream in result}.values())
        media_type = {'m3u8': 'hls', 'mp3': 'mp3', 'aac': 'aac', 'aacp': 'aac',
                      'ogg': 'ogg', 'opus': 'opus', 'wma': 'wma', 'asx': 'wma'}.get(suffix, '')
        if media_type and media_type not in formats:
            raise RadioTimeError('This renderer does not support the station format', 'format', 415)
        return [RadioStream(url, media_type, metadata={'container': 'hls'} if media_type == 'hls' else {})]

    def test_url(self, url, formats=None):
        """Probe bounded HTTP headers/bytes without sending anything to a room."""
        streams = self.resolve_url(url, formats)
        verified, failures = [], []
        for stream in streams[:8]:
            try:
                try:
                    status, headers, _ = self._raw(stream.url, max_bytes=0,
                                                   headers={'Accept': 'audio/*, application/octet-stream'}, method='HEAD')
                    if status in (405, 501):
                        raise RadioTimeError('HEAD unavailable', 'http', status)
                except RadioTimeError as error:
                    if error.status not in (405, 501):
                        raise
                    status, headers, _ = self._raw(stream.url, max_bytes=4096, truncate=True,
                                                   headers={'Accept': 'audio/*, application/octet-stream', 'Range': 'bytes=0-4095'})
                if not 200 <= int(status) < 400:
                    raise RadioTimeError('Station returned HTTP %s' % status, 'http', int(status))
                content_type = next((str(value).split(';')[0].strip().lower()
                                     for key, value in headers.items() if key.lower() == 'content-type'), '')
                if content_type in ('text/html', 'application/json', 'text/xml'):
                    raise RadioTimeError('URL returned a page rather than an audio stream', 'format', 415)
                stream.metadata.update(http_status=status, content_type=content_type, verified=True)
                verified.append(stream)
            except RadioTimeError as error:
                failures.append(str(error))
        if not verified:
            raise RadioTimeError('No stream could be verified: %s' % '; '.join(failures), 'unavailable', 404)
        return verified

    def zip_location(self, zipcode):
        """Public ZIP lookup is separate from RadioTime's coordinate-only local API."""
        if not re.fullmatch(r'\d{5}', str(zipcode)):
            raise ValueError('Enter a five-digit U.S. ZIP code')
        _, _, payload = self._raw('https://api.zippopotam.us/us/' + str(zipcode), max_bytes=65536)
        data = _parse(payload)
        places = data.get('places') or []
        if not places:
            raise ValueError('No location found for that ZIP code')
        place = places[0]
        return dict(zip=str(zipcode), country='US', city=place.get('place name', ''),
                    label=', '.join(filter(None, (place.get('place name'), place.get('state abbreviation')))),
                    latlon='%s,%s' % (place['latitude'], place['longitude']))


def merge_metadata(browse=None, profile=None, now_playing=None):
    """Preserve raw fields and add canonical fields; callers guard playback generation.

    Missing/null profile fields must not erase a useful browse logo or song cover.
    A caller with fresher stream metadata must reconcile identity before applying
    this snapshot; the public API may cache its view for a considerable interval.
    """
    result = copy.deepcopy(browse or {})
    for source in (profile, now_playing):
        if isinstance(source, RadioResponse):
            source = next((row for row in source.body if row.get('element') in ('station', 'show', 'topic')), {})
        if isinstance(source, dict):
            result.update({key: value for key, value in source.items() if value is not None and value != ''})
    result['station_name'] = result.get('name') or result.get('station_name') or result.get('text', '')
    result['song_title'] = result.get('current_song') or result.get('song_title', '')
    result['song_artist'] = result.get('current_artist') or result.get('song_artist', '')
    result['song_album'] = result.get('current_album') or result.get('song_album', '')
    result['station_image'] = result.get('logo') or result.get('station_image') or result.get('image', '')
    result['song_image'] = result.get('current_album_art') or result.get('playing_image') or result.get('song_image', '')
    result['program'] = result.get('current_track') or result.get('program', '')
    result['artwork'] = result['song_image'] or result['station_image']
    return result
