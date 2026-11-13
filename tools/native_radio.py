"""Native CR100 Radio adapter for the household RadioTime catalog.

Radio uses the firmware's preexisting Household.radioUDN root (object 0), not
another Music-menu service. Native favorite writes go to the associated player's
ContentDirectory CreateObject/DestroyObject, while local-location changes use
SystemProperties R_RadioLocation. The SMAPI listener only supplies the directory.
"""
from __future__ import annotations

import copy
import html
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit
import xml.etree.ElementTree as ET

from smapi_service import SmapiService, text

SERVICE_ID = 254
SERVICE_TYPE = 65031
RADIO_UDN = 'SA_RINCON65031_'
RADIO_URI_PREFIX = 'x-rincon-mp3radio://bridge-radio/'
DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">')
ALIASES = {'root': '0', 'R:0': 'favorites', 'R:0/0': 'favorites:stations',
           'R:0/1': 'favorites:shows', 'R:0/recent': 'recent', 'L': 'local', 'S': 'search', 'bridge-local': 'local', 'r0:browse': 'r0'}
NATIVE_IDS = {'favorites': 'R:0', 'favorites:stations': 'R:0/0', 'favorites:shows': 'R:0/1',
              'recent': 'R:0/recent', 'local': 'L', 'search': 'search'}


def canonical_id(value):
    value = str(value or '')
    if value.startswith(RADIO_URI_PREFIX):
        return unquote(value[len(RADIO_URI_PREFIX):])
    if value.startswith('x-cr100-radio:'):
        return unquote(value[len('x-cr100-radio:'):])
    if value.startswith(('x-sonosapi-stream:', 'x-sonosapi-radio:', 'x-sonos-http:', 'x-sonosapi-rtrecent:', 'x-sonosapi-show:')):
        scheme, value = value.split(':', 1)
        value = value.split('?', 1)[0]
        if scheme == 'x-sonos-http' and value.endswith('.mp3'):
            value = value[:-4]
        value = unquote(value)
        # The stock Radio provider sometimes passes a raw guide ID.
        if re.fullmatch(r'[spt]\d+', value):
            return {'s': 'station:', 'p': 'show:', 't': 'episode:'}[value[0]] + value
        return value
    for prefix, kind in (('R:0/0/', 'station'), ('R:0/1/', 'show')):
        if value.startswith(prefix):
            return unquote(value[len(prefix):])
    return ALIASES.get(value, value)


def radio_uri(identity):
    return RADIO_URI_PREFIX + quote(canonical_id(identity), safe='')


def is_radio_uri(uri):
    if uri.startswith((RADIO_URI_PREFIX, 'x-cr100-radio:')):
        return True
    if not uri.startswith(('x-sonosapi-stream:', 'x-sonosapi-radio:', 'x-sonos-http:', 'x-sonosapi-rtrecent:')):
        return False
    return parse_qs(urlsplit(uri).query).get('sid') == [str(SERVICE_ID)]


def _value(item, key, default=''):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _dictionary(item):
    return copy.deepcopy(item if isinstance(item, dict) else item.as_dict())


def _identity(item):
    return str(_value(item, 'id'))


def _kind(item):
    return _value(item, 'kind')


def _native_id(identity, parent=''):
    if parent in ('R:0/0', 'R:0/1'):
        return parent + '/' + quote(identity, safe='')
    return NATIVE_IDS.get(identity, identity)


class NativeRadioService(SmapiService):
    startup_event = 'native-radio'
    response_event = 'radio-smapi'

    def __init__(self, host, port, catalog, *, art=None, art_base_url=None, log=print, on_change=None):
        self.catalog, self.store, self.art = catalog, catalog.store, art
        self.art_base_url = art_base_url
        self.on_change = on_change or (lambda services: None)
        self._location_wire = ''
        self._picker_labels = {'r0': 'Location'}
        super().__init__(host, port, SERVICE_ID, 'TuneIn', log, capabilities='0')

    def media_servers(self):
        # TuneIn is firmware-special-cased. An account would create a second
        # Music-menu entry and is deliberately never advertised by this adapter.
        return '<MediaServers/>'

    def owns(self, uri, metadata=''):
        # Playback is owned by the household RadioPlayback manager, not by the
        # generic music-service routing registry.
        return False

    def image_uri(self, item):
        image = _value(item, 'image') or ''
        if not image:
            return ''
        if self.art:
            path = self.art.path_for(image, prefetch=False)
            if path.startswith(('http://', 'https://')):
                return path
            return (self.art_base_url or 'http://%s:%d' % (self.host, self.port)).rstrip('/') + '/' + path.lstrip('/')
        return image

    def handle_get(self, request):
        path = urlsplit(request.path).path
        match = re.fullmatch(r'/art/([0-9a-f]{16})\.jpg', path)
        if match and self.art:
            data = self.art.get(match.group(1))
            if data:
                self.reply(request, 200, data, 'image/jpeg')
                return
        self.reply(request, 404, b'Not found', 'text/plain')

    def node(self, identity):
        identity = self._catalog_identity(identity)
        if re.fullmatch(r'z[0-9]{5}', identity):
            from types import SimpleNamespace
            location = self.catalog.zip_resolver(identity[1:])
            return SimpleNamespace(id=identity, title=location.get('label', identity[1:]), kind='folder', image='', metadata={'location': location})
        return self.catalog.metadata(identity)

    @staticmethod
    def _paging(args):
        try:
            index, count = int(args.get('index', 0)), int(args.get('count', 100))
        except (TypeError, ValueError):
            raise ValueError('Invalid Radio page') from None
        if index < 0 or count < 0 or index > 2**31 - 1:
            raise ValueError('Invalid Radio page')
        return index, min(count or 100, 500)

    def page(self, page, wrapper, index):
        return ('<%s><index>%d</index><count>%d</count><total>%d</total>%s</%s>' %
                (wrapper, index, len(page.items), page.total,
                 ''.join(self.entry(item) for item in page.items), wrapper))

    def get_metadata(self, args):
        index, count = self._paging(args)
        identity = canonical_id(args.get('id') or '0')
        if identity == '0':
            # The native Radio provider injects Favorites Stations/Shows, Search
            # and Local Radio itself. The guarded SWF adapter groups them into
            # the original Favorites menu. Never duplicate those upstream.
            from types import SimpleNamespace
            items = [item for item in self.catalog.root() if item.id not in ('favorites', 'search', 'local')]
            page = SimpleNamespace(items=items[index:index+count], total=len(items))
        elif re.fullmatch(r'r[0-9]+', identity):
            page = self.picker_page(identity, index, count)
        elif re.fullmatch(r'z[0-9]{5}', identity):
            page = self.catalog.browse('local', start=index, count=count)
        else:
            page = self.catalog.browse(identity, start=index, count=count)
        return self.page(page, 'getMetadataResult', index)

    def search(self, args):
        index, count = self._paging(args)
        kind = canonical_id(args.get('id') or 'stations')
        kind = kind.removeprefix('search:')
        page = self.catalog.search(args.get('term', ''), kind, start=index, count=count)
        return self.page(page, 'searchResult', index)

    def picker_page(self, region, index=0, count=100):
        from types import SimpleNamespace
        from radio_catalog import RadioItem
        from radiotime import outlines
        response = self.catalog.client.browse(id=region)
        regions, seen, stations = [], set(), False
        for row in outlines(response.body):
            guide = str(row.get('guide_id') or parse_qs(urlsplit(row.get('URL', '')).query).get('id', [''])[0])
            if re.fullmatch(r'r[0-9]+', guide) and guide != region and guide not in seen:
                seen.add(guide)
                label = str(row.get('text') or row.get('name') or guide)
                self._picker_labels[guide] = label
                regions.append(RadioItem(guide, label, 'folder'))
            if row.get('type') == 'audio' and str(row.get('guide_id', '')).startswith('s'):
                stations = True
        if not regions and stations and region != 'r0':
            # The original picker closes on a non-generic-container class.
            # A nonplayable, nonenumerable genre collection supplies that stock
            # selection contract without fabricating an audio station.
            regions = [RadioItem(region, self._picker_labels.get(region, region), 'location')]
        return SimpleNamespace(items=regions[index:index+count], total=len(regions))

    def entry(self, item, wrap=True):
        kind, identity, metadata = _kind(item), _identity(item), _value(item, 'metadata', {}) or {}
        item_id = _native_id(identity)
        title = _value(item, 'title')
        art = self.image_uri(item)
        art_field = '<albumArtURI>%s</albumArtURI>' % text(art) if art else ''
        if kind in ('station', 'custom'):
            fields = ('<id>%s</id><itemType>stream</itemType><title>%s</title><mimeType>audio/mpeg</mimeType>'
                      '<streamMetadata><canPlay>true</canPlay><canSkip>false</canSkip>'
                      '<canAddToFavorites>true</canAddToFavorites><hasOutOfBandMetadata>true</hasOutOfBandMetadata>'
                      '<currentShow>%s</currentShow><currentHost>%s</currentHost><logo>%s</logo>%s</streamMetadata>' %
                      (text(item_id), text(title), text(metadata.get('current_show') or metadata.get('current_track') or metadata.get('program') or metadata.get('show') or ''),
                       text(metadata.get('current_host') or metadata.get('host') or metadata.get('hosts') or ''), text(art), art_field))
            return '<mediaMetadata>%s</mediaMetadata>' % fields if wrap else fields
        if kind == 'episode':
            fields = ('<id>%s</id><itemType>track</itemType><title>%s</title><mimeType>audio/mpeg</mimeType>'
                      '<trackMetadata><artist>%s</artist><album>%s</album><duration>%d</duration>%s'
                      '<canPlay>true</canPlay><canSkip>true</canSkip><canAddToFavorites>false</canAddToFavorites>'
                      '</trackMetadata>' % (text(item_id), text(title), text(_value(item, 'artist')),
                                           text(_value(item, 'album')), max(0, int(_value(item, 'duration', 0) or 0)), art_field))
            return '<mediaMetadata>%s</mediaMetadata>' % fields if wrap else fields
        item_type = ('show' if kind == 'show' else 'genre' if kind == 'location' else
                     'search' if identity.startswith('search:') else 'container')
        fields = ('<id>%s</id><itemType>%s</itemType><title>%s</title><canPlay>false</canPlay>'
                  '<canEnumerate>%s</canEnumerate><canScroll>false</canScroll><canAddToFavorites>%s</canAddToFavorites>%s' %
                  (text(item_id), item_type, text(title), 'false' if item_type == 'search' or kind in ('action', 'location') else 'true',
                   'true' if kind == 'show' else 'false', art_field))
        return '<mediaCollection>%s</mediaCollection>' % fields if wrap else fields

    def dispatch(self, method, args):
        if method == 'getLastUpdate':
            state = self.store.snapshot()
            return ('<getLastUpdateResult><catalog>%s</catalog><favorites>%s</favorites><pollInterval>60</pollInterval>'
                    '</getLastUpdateResult>' % (text(state.get('radio_directory_update_id', 0) + state.get('radio_location_update_id', 0)),
                                               text(state.get('radio_favorites_update_id', 1))))
        if method == 'getMediaURI':
            item = self.node(args.get('id'))
            if _kind(item) not in ('station', 'custom', 'episode'):
                raise LookupError('Choose a station or episode')
            return '<getMediaURIResult>%s</getMediaURIResult>' % text(radio_uri(_identity(item)))
        if method in ('createItem', 'deleteItem'):
            identity = canonical_id(args.get('favorite') if method == 'createItem' else args.get('id'))
            if method == 'createItem' and not identity:
                identity = canonical_id(args.get('id'))
            if method == 'createItem':
                if identity.startswith(('settings-apply:', 'settings-action:')):
                    self.catalog.apply_action(identity)
                else:
                    self.catalog.favorite(identity)
            else:
                self.catalog.remove_favorite(identity)
            self.on_change({'ContentDirectory'})
            return '<%sResult>%s</%sResult>' % (method, text(identity), method)
        return super().dispatch(method, args)

    def didl(self, items, parent='0', favorite=False):
        result = []
        for item in items:
            identity, kind = _identity(item), _kind(item)
            container = kind in ('folder', 'show', 'action', 'message')
            object_id = _native_id(identity, parent if favorite else '')
            upnp_class = ('object.container.radioShow' if kind == 'show' else
                          'object.container.sonos-searchTypes' if identity == 'search' else
                          'object.container.sonos-localRadio' if identity == 'local' else
                          'object.container' if container else
                          'object.item.audioItem.musicTrack' if kind == 'episode' else
                          'object.item.audioItem.audioBroadcast')
            fields = ['<dc:title>%s</dc:title>' % text(_value(item, 'title')), '<upnp:class>%s</upnp:class>' % upnp_class]
            if _value(item, 'artist'): fields.append('<dc:creator>%s</dc:creator>' % text(_value(item, 'artist')))
            if _value(item, 'album'): fields.append('<upnp:album>%s</upnp:album>' % text(_value(item, 'album')))
            art = self.image_uri(item)
            if art: fields.append('<upnp:albumArtURI>%s</upnp:albumArtURI>' % text(art))
            if not container:
                uri = radio_uri(identity)
                fields.append('<res protocolInfo="x-rincon-mp3radio:*:*:*">%s</res>' % text(uri))
            tag = 'container' if container else 'item'
            result.append('<%s id="%s" parentID="%s" restricted="true">%s</%s>' %
                          (tag, html.escape(object_id, quote=True), html.escape(parent, quote=True), ''.join(fields), tag))
        return DIDL_OPEN + ''.join(result) + '</DIDL-Lite>'

    def _favorite_from_elements(self, args):
        raw = args.get('Elements', '')
        if len(raw) > 65536 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I):
            raise ValueError('Invalid favorite metadata')
        root = ET.fromstring(raw)
        resources = [element.text or '' for element in root.iter() if element.tag.rsplit('}', 1)[-1] == 'res']
        if len(resources) != 1:
            raise ValueError('Expected one favorite resource')
        identity = self._catalog_identity(canonical_id(resources[0]))
        item = self.catalog.metadata(identity)
        expected = 'show' if args.get('ContainerID') == 'R:0/1' else 'station'
        if (expected == 'show') != (_kind(item) == 'show'):
            raise ValueError('Favorite type does not match its folder')
        existing = {entry['id'] for entry in self.store.favorites()}
        if identity in existing:
            from content import ContentError
            raise ContentError(803, 'This Radio favorite already exists')
        self.catalog.favorite(identity)
        self.on_change({'ContentDirectory'})
        folder = args['ContainerID']
        return {'ObjectID': _native_id(identity, folder), 'Result': self.didl([item], folder, favorite=True)}

    def upnp_action(self, service, action, args, *, udn='', room_key=''):
        """Return (SOAP outputs, changed-services) or None for unrelated actions."""
        if service == 'ContentDirectory':
            object_id = str(args.get('ObjectID', ''))
            if action == 'Browse' and object_id.startswith('R:0'):
                identity = canonical_id(object_id)
                flag = args.get('BrowseFlag', 'BrowseDirectChildren')
                index = max(0, int(args.get('StartingIndex', 0)))
                count = min(max(1, int(args.get('RequestedCount', 100) or 100)), 100)
                if flag == 'BrowseMetadata':
                    items = [self.catalog.metadata(identity)]
                    total = 1
                else:
                    page = self.catalog.browse(identity, index, count)
                    items, total = page.items, page.total
                state = self.store.snapshot()
                values = {'Result': self.didl(items, object_id, favorite=object_id in ('R:0/0', 'R:0/1')),
                          'NumberReturned': str(len(items)), 'TotalMatches': str(total),
                          'UpdateID': str(state.get('radio_favorites_update_id', 1))}
                return values, set()
            if action == 'CreateObject' and args.get('ContainerID') in ('R:0/0', 'R:0/1'):
                return self._favorite_from_elements(args), {'ContentDirectory'}
            if action == 'DestroyObject' and object_id.startswith(('R:0/0/', 'R:0/1/')):
                self.catalog.remove_favorite(canonical_id(object_id))
                self.on_change({'ContentDirectory'})
                return {}, {'ContentDirectory'}
        return None

    @staticmethod
    def _catalog_identity(identity):
        identity = str(identity or '')
        if identity.startswith('F'):
            identity = identity[1:]
        if identity.startswith('S') and re.match(r'S[0-9a-fA-F]{8}', identity):
            identity = identity[1:]
        identity = re.sub(r'^[0-9a-fA-F]{8}', '', identity)
        identity = unquote(identity.removesuffix(':browse'))
        return canonical_id(identity)

    @staticmethod
    def _wire_parts(value):
        parts, current, escaped = [], [], False
        for char in value:
            if escaped:
                if char not in ('\\', ','):
                    raise ValueError('Invalid native Radio location escape')
                current.append(char); escaped = False
            elif char == '\\': escaped = True
            elif char == ',': parts.append(''.join(current)); current = []
            else: current.append(char)
        if escaped: raise ValueError('Invalid native Radio location escape')
        parts.append(''.join(current))
        if len(parts) != 2: raise ValueError('Invalid native Radio location')
        return parts

    def location_wire(self):
        location = self.store.settings().get('location') or {}
        if location.get('_native_wire') and not location.get('region_id'):
            return location['_native_wire']
        identity = 'z' + str(location['zip']) if location.get('zip') else 'bridge-local'
        if not identity:
            identity = 'bridge-local'
        label = location.get('label') or location.get('name') or 'Local Area'
        escape = lambda value: str(value).replace('\\', '\\\\').replace(',', '\\,')
        return escape('F00080000' + identity) + ',' + escape(label)

    def _set_location_wire(self, wire):
        if not isinstance(wire, str) or len(wire) > 16384:
            raise ValueError('Invalid native Radio location')
        raw_id, label = self._wire_parts(wire)
        identity = self._catalog_identity(raw_id)
        if re.fullmatch(r'z[0-9]{5}', identity):
            location = self.catalog.zip_resolver(identity[1:])
        else:
            if re.fullmatch(r'r[0-9]+', identity):
                region = identity
            else:
                item = self.catalog.metadata(identity)
                query = parse_qs(urlsplit(_value(item, 'url')).query)
                region = query.get('id', [''])[0]
            if not re.fullmatch(r'r[0-9]+', region):
                raise ValueError('Choose a city from the Radio directory')
            location = dict(region_id=region, label=label)
        location.update(label=label, _native_id=identity, _native_wire=wire)
        self.catalog.set_location(location)
        self.on_change({'ContentDirectory', 'SystemProperties'})

    def native_action(self, service, action, args, state=None):
        if service == 'SystemProperties' and args.get('VariableName') == 'CR100_RadioAction' and action == 'SetString':
            identity = self._catalog_identity(args.get('StringValue', ''))
            if not identity.startswith(('settings-apply:', 'settings-action:')):
                raise ValueError('Choose an explicit Radio setting action')
            self.catalog.apply_action(identity)
            self.on_change({'ContentDirectory'})
            return {}
        if service == 'SystemProperties' and args.get('VariableName') == 'R_RadioLocation':
            if action == 'GetString': return {'StringValue': self.location_wire()}
            if action == 'SetString':
                self._set_location_wire(args.get('StringValue', ''))
                return {}
        result = self.upnp_action(service, action, args,
                                  udn=getattr(state, 'uuid', ''),
                                  room_key=getattr(state, 'key', ''))
        return None if result is None else result[0]

    def event_values(self):
        state = self.store.snapshot()
        return {'RadioFavoritesUpdateID': str(state.get('radio_favorites_update_id', 1)),
                'RadioLocationUpdateID': str(state.get('radio_location_update_id', 1))}
