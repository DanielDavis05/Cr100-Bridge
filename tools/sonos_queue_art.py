"""Read missing queue artwork from Sonos without changing its queue or transport.

HA's sonos.get_queue response omits albumArtURI. Its trusted registries identify
the speaker; a bounded ContentDirectory Browse supplies art for the visible page.
Both queue position and resource URI must match before artwork is used.
"""
from __future__ import annotations

import collections
import ipaddress
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

REGISTRY_TTL = 300
ART_TTL = 30
ERROR_TTL = 5
NETWORK_TIMEOUT = 2
MAX_PAGE = 100
MAX_RESPONSE = 1024 * 1024
MAX_CACHE = 32
SERVICE = 'urn:schemas-upnp-org:service:ContentDirectory:1'
SOAP_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
DIDL_NS = 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/'
UPNP_NS = 'urn:schemas-upnp-org:metadata-1-0/upnp/'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _device_base(url):
    """Accept the Sonos HTTP endpoint advertised by HA, without credentials."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
        address = ipaddress.IPv4Address(parts.hostname or '')
        if parts.scheme != 'http' or parts.port != 1400 or parts.username or parts.password \
                or parts.query or parts.fragment or address.is_unspecified or address.is_multicast \
                or address.is_loopback or address.is_link_local:
            return None
    except ValueError:
        return None
    return 'http://%s:1400' % address


def _art_url(base, value):
    """Return only /getaa on the approved Sonos origin, never an arbitrary URL."""
    if not isinstance(value, str) or not value or len(value) > 8192 \
            or any(ord(ch) < 32 or ch.isspace() for ch in value):
        return ''
    try:
        joined = urljoin(base + '/', value)
        parts = urlsplit(joined)
        approved = urlsplit(base)
        if (parts.scheme, parts.hostname, parts.port) != (approved.scheme, approved.hostname, approved.port) \
                or parts.username or parts.password or parts.fragment or parts.path != '/getaa':
            return ''
    except ValueError:
        return ''
    return joined


def _xml(data):
    if len(data) > MAX_RESPONSE or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise ValueError('Invalid Sonos XML response')
    root = ET.fromstring(data)
    stack, count = [(root, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 4096 or depth > 24:
            raise ValueError('Sonos XML response is too complex')
        stack.extend((child, depth + 1) for child in node)
    return root


class SonosQueueArt:
    def __init__(self, ha, log=print, opener=None):
        self.ha, self.log = ha, log
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.lock = threading.RLock()
        self.registry_until = 0
        self.bases = {}
        self.cache = collections.OrderedDict()
        self.fetching = {}

    def _base(self, entity):
        # HA lists the group coordinator first. Its Q:0 is the group's queue.
        state = self.ha.state(entity) or {}
        members = (state.get('attributes') or {}).get('group_members') or []
        if isinstance(members, list) and members and isinstance(members[0], str):
            entity = members[0]
        with self.lock:
            if time.monotonic() >= self.registry_until:
                try:
                    entities = self.ha.call({'type': 'config/entity_registry/list'}, NETWORK_TIMEOUT)
                    devices = self.ha.call({'type': 'config/device_registry/list'}, NETWORK_TIMEOUT)
                    if not isinstance(entities, list) or not isinstance(devices, list):
                        raise ValueError('Missing Sonos registry')
                    device_urls = {d.get('id'): _device_base(d.get('configuration_url'))
                                   for d in devices if isinstance(d, dict)}
                    self.bases = {e['entity_id']: device_urls[e.get('device_id')]
                                  for e in entities if isinstance(e, dict) and e.get('platform') == 'sonos'
                                  and isinstance(e.get('entity_id'), str) and device_urls.get(e.get('device_id'))}
                    self.registry_until = time.monotonic() + REGISTRY_TTL
                except Exception:
                    self.bases = {}
                    self.registry_until = time.monotonic() + ERROR_TTL
                    # Registry/URL content can include unrelated private data.
                    self.log({'event': 'queue-art-registry-unavailable'})
            return self.bases.get(entity)

    def lookup(self, entity, uris, start=0):
        """Return page-relative index -> art URL; failures never prevent browsing.

        uris must be the HA resource URIs at the requested zero-based start.
        The cache key includes them so queue edits cannot attach old art to a
        new row. One native Browse covers the whole visible page, not each row.
        """
        if not uris or not 0 <= start <= 1_000_000 or len(uris) > MAX_PAGE:
            return {}
        base = self._base(entity)
        if not base:
            return {}
        key = (base, start, tuple(uris))
        with self.lock:
            now = time.monotonic()
            # Reuse a previously browsed page for a later single-item hover.
            for cached_key, (until, images) in reversed(self.cache.items()):
                host, first, resources = cached_key
                offset = start - first
                if until > now and host == base and offset >= 0 \
                        and resources[offset:offset + len(uris)] == tuple(uris):
                    return {index - offset: image for index, image in images.items()
                            if offset <= index < offset + len(uris)}
            waiter = self.fetching.get(key)
            if waiter is None:
                self.fetching[key] = threading.Event()
        if waiter is not None:
            if not waiter.wait(NETWORK_TIMEOUT + 1): return {}
            with self.lock:
                return dict(self.cache.get(key, (0, {}))[1])
        art, ttl = {}, ART_TTL
        try:
            art = self._browse(base, uris, start)
        except Exception:
            ttl = ERROR_TTL
            self.log({'event': 'queue-art-unavailable', 'entity': entity})
        with self.lock:
            self.cache[key] = (time.monotonic() + ttl, art)
            self.cache.move_to_end(key)
            while len(self.cache) > MAX_CACHE:
                self.cache.popitem(last=False)
            self.fetching.pop(key).set()
            return dict(art)

    def _browse(self, base, uris, start):
        body = ('<s:Envelope xmlns:s="%s"><s:Body><u:Browse xmlns:u="%s">'
                '<ObjectID>Q:0</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag>'
                '<Filter>*</Filter><StartingIndex>%d</StartingIndex><RequestedCount>%d</RequestedCount>'
                '<SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>'
                % (SOAP_NS, SERVICE, start, len(uris)))
        request = urllib.request.Request(base + '/MediaServer/ContentDirectory/Control', data=body.encode(),
                                         headers={'Content-Type': 'text/xml; charset="utf-8"',
                                                  'SOAPACTION': '"%s#Browse"' % SERVICE})
        with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            data = response.read(MAX_RESPONSE + 1)
        outer = _xml(data)
        result = outer.find('./{%s}Body/{%s}BrowseResponse/Result' % (SOAP_NS, SERVICE))
        if result is None or result.text is None:
            raise ValueError('Missing native queue result')
        didl = _xml(result.text.encode('utf-8'))
        if didl.tag != '{%s}DIDL-Lite' % DIDL_NS:
            raise ValueError('Invalid native queue result')
        found = {}
        for offset, node in enumerate(didl):
            if offset >= len(uris):
                break
            if node.tag != '{%s}item' % DIDL_NS or node.get('id') != 'Q:0/%d' % (start + offset + 1):
                continue
            resource = node.find('{%s}res' % DIDL_NS)
            picture = node.find('{%s}albumArtURI' % UPNP_NS)
            if resource is None or resource.text != uris[offset] or picture is None:
                continue
            url = _art_url(base, picture.text)
            if url:
                found[offset] = url
        return found
