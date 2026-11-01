"""Resolve this service's CR100 playback identifiers without passing through URLs.

Native CR100 4.3 captures use ``x-sonos-http:<escaped-id>.mp3?sid=N&flags=0``
for a track and ``x-rincon-cpcontainer:<8-hex-flags><escaped-id>`` for an album.
The latter has no sid: its DIDL ``desc id="cdudn"`` identifies the service.
The DIDL item id has the same eight-hex header; parentID identifies its parent
and is deliberately not used as a playable identifier. The header is the native
``%08x%s`` object-id format, not part of the SMAPI/Spotify item id.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, unquote

MAX_URI = 4096
MAX_METADATA = 65536
MAX_XML_NODES = 128
MAX_XML_DEPTH = 16
_CANONICAL = re.compile(r'spotify:(track|album|playlist):([A-Za-z0-9]{22})\Z')
_HEADER = re.compile(r'[0-9A-Fa-f]{8}(?=spotify:)')
_ACCOUNT = re.compile(r'SA_RINCON([0-9]+)_([^\s<>]+)\Z')
_BAD_PERCENT = re.compile(r'%(?![0-9A-Fa-f]{2})')
_MEDIA_TYPE = {'track': 'music', 'album': 'album', 'playlist': 'playlist'}
_DIDL_NS = 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/'


def _service_id(service_id):
    if type(service_id) is not int or not 1 <= service_id <= 65535:
        raise ValueError('Invalid music service id')
    return service_id


def _bounded(value, limit, name):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('Invalid or oversized %s' % name)
    return value


def _decode_id(value, header=False):
    """Decode at most three escaping layers, then match the complete item id."""
    value = _bounded(value, MAX_URI, 'item id')
    for _ in range(3):
        if '%' not in value:
            break
        if _BAD_PERCENT.search(value):
            raise ValueError('Malformed item id escaping')
        try:
            value = unquote(value, encoding='utf-8', errors='strict')
        except UnicodeError as error:
            raise ValueError('Invalid item id encoding') from error
    if header:
        value = _HEADER.sub('', value, count=1)
    match = _CANONICAL.fullmatch(value)
    if not match:
        raise ValueError('Unsupported Spotify item id')
    return value, match.group(1)


def _query(query):
    if not query:
        return {}
    if _BAD_PERCENT.search(query):
        raise ValueError('Malformed playback query')
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=4)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Malformed playback query') from error
    result = {}
    for key, value in pairs:
        if key not in ('sid', 'flags') or key in result or not re.fullmatch(r'[0-9]+', value):
            raise ValueError('Invalid playback query parameter')
        if len(value) > 10 or int(value) > 0xffffffff:
            raise ValueError('Invalid playback query parameter')
        result[key] = value
    return result


def _split_uri(uri):
    uri = _bounded(uri, MAX_URI, 'playback URI')
    # SOAP callers normally already unescape &, but accept the literal XML form
    # as well. Decode payload escaping separately so %3f cannot inject a query.
    uri = uri.replace('&amp;', '&')
    if not uri or any(c.isspace() or ord(c) < 32 for c in uri) or '#' in uri:
        raise ValueError('Invalid playback URI')
    path, marker, query = uri.partition('?')
    if marker and not query:
        raise ValueError('Empty playback query')
    return path, _query(query)


def _parse_uri(uri):
    path, query = _split_uri(uri)
    if path.startswith('x-sonos-http:'):
        payload = path[len('x-sonos-http:'):]
        # The CR100's audio/mpeg path adds .mp3 to the SMAPI item id.
        if payload.endswith('.mp3'):
            payload = payload[:-4]
        item, kind = _decode_id(payload)
        if kind != 'track':
            raise ValueError('Only tracks use x-sonos-http')
    elif path.startswith('x-rincon-cpcontainer:'):
        item, kind = _decode_id(path[len('x-rincon-cpcontainer:'):], header=True)
    elif path.startswith('spotify:'):
        item, kind = _decode_id(path)
    else:
        raise ValueError('Unsupported playback URI')
    return item, kind, query.get('sid')


def _didl_item(metadata):
    metadata = _bounded(metadata, MAX_METADATA, 'playback metadata')
    if not metadata:
        return None
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', metadata, flags=re.I):
        raise ValueError('DTD and entity declarations are unsupported')
    try:
        root = ET.fromstring(metadata)
    except (ET.ParseError, ValueError) as error:
        raise ValueError('Malformed playback metadata') from error
    if root.tag not in ('DIDL-Lite', '{%s}DIDL-Lite' % _DIDL_NS):
        raise ValueError('Expected DIDL-Lite metadata')
    stack, count = [(root, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
            raise ValueError('Playback metadata is too complex')
        stack.extend((child, depth + 1) for child in node)
    objects = [node for node in root if node.tag.rsplit('}', 1)[-1] in ('item', 'container')]
    if len(objects) != 1:
        raise ValueError('Expected exactly one playback item')
    return objects[0]


def _accounts(obj):
    if obj is None:
        return []
    accounts = []
    for node in obj:
        if node.tag.rsplit('}', 1)[-1] == 'desc' and node.attrib.get('id') == 'cdudn':
            account = _ACCOUNT.fullmatch((node.text or '').strip())
            if not account:
                raise ValueError('Invalid music service account')
            accounts.append(account.group(1))
    return accounts


def _metadata(metadata):
    obj = _didl_item(metadata)
    if obj is None:
        return [], []
    identities, accounts = [], _accounts(obj)
    if 'id' in obj.attrib:
        identities.append((_decode_id(obj.attrib['id'], header=True)[0], None))
    for node in obj:
        name = node.tag.rsplit('}', 1)[-1]
        if name == 'res':
            item, _, sid = _parse_uri((node.text or '').strip())
            identities.append((item, sid))
    if not identities:
        raise ValueError('Playback metadata has no item identifier')
    return identities, accounts


def owns(uri: str, metadata: str = '', service_id: int = 240) -> bool:
    """Whether exact sid/account fields claim this service, even for a bad item.

    Resolution validates the identifier separately. Keeping ownership separate
    lets callers reject malformed items belonging to this service rather than
    inadvertently handing them to another backend.
    """
    _service_id(service_id)
    try:
        path, query = _split_uri(uri)
        if path.startswith(('x-sonos-http:', 'x-rincon-cpcontainer:', 'spotify:')) \
                and query.get('sid') == str(service_id):
            return True
    except ValueError:
        pass
    try:
        accounts = _accounts(_didl_item(metadata))
        return str(service_id * 256 + 7) in accounts
    except ValueError:
        return False


def resolve_playback(uri: str, metadata: str = '', service_id: int = 240) -> tuple[str, str]:
    """Return a validated Spotify URI and HA type (music, album or playlist).

    An exact sid or DIDL account must identify this service. Every supplied sid,
    account and item/resource identifier must agree; title and parentID do not
    identify the playable object. Artists and arbitrary web URLs are unsupported.
    """
    _service_id(service_id)
    item, kind, sid = _parse_uri(uri)
    identities, accounts = _metadata(metadata)
    expected_sid, expected_account = str(service_id), str(service_id * 256 + 7)
    sids = [value for _, value in identities if value is not None]
    if sid is not None:
        sids.append(sid)
    if any(value != expected_sid for value in sids) or any(value != expected_account for value in accounts):
        raise ValueError('Playback belongs to a different music service')
    if not sids and not accounts:
        raise ValueError('Playback has no music service identity')
    if any(value != item for value, _ in identities):
        raise ValueError('Playback URI and metadata identify different items')
    return item, _MEDIA_TYPE[kind]
