#!/usr/bin/env python3
"""A stub Sonos music service (SMAPI 1.1) for finding out how the CR100 uses one.

The virtual players advertise it in MusicServices.ListAvailableServices as an
anonymous service with a synthetic account entry for its Music menu
(MUSIC-SERVICES.md). The CR100's own SMAPI client then talks to this server directly. Everything it sends is logged
(SOAPAction, headers, credentials, arguments) and answered from a small fixed tree:
playlists, albums, an artist, a 250-track list to see how the CR100 pages, search,
and album art. Item IDs start with ID_PREFIX so play attempts can be recognised
when the CR100 hands them to a player.

Run inside zoneplayers.py (bridge-config.json "music_service_stub"), or alone for
a look: python3 smapi_stub.py --host 127.0.0.1 --port 8400
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SMAPI_NS = 'http://www.sonos.com/Services/1.1'
SOAP_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
ID_PREFIX = 'bt:'
SERVICE_TYPE_SUFFIX = 7   # service type = Id * 256 + 7 (SA_RINCON<type>_ account IDs)
LIST_VERSION = '2'        # MusicServices list version; the peer's empty list is '1'
ACCOUNT_NAME = 'bridge'   # placeholder account: the service is anonymous, nothing checks it


def track(number, title, artist='Stub Artist', album='Stub Album', album_id='bt:album:1', duration=200):
    return {'id': 'bt:track:%d' % number, 'itemType': 'track', 'title': title, 'mimeType': 'audio/mpeg',
            'artist': artist, 'album': album, 'albumId': album_id, 'duration': duration}


def collection(item_id, title, item_type='container', children=(), can_play=False, artist=''):
    return {'id': item_id, 'itemType': item_type, 'title': title, 'canPlay': can_play,
            'canEnumerate': True, 'artist': artist, 'children': list(children)}


def build_tree():
    album_tracks = [track(i, 'Stub Track %d' % i) for i in range(1, 6)]
    playlist_tracks = [track(10 + i, 'Playlist Song %d' % i, artist='Various') for i in range(1, 4)]
    album = collection('bt:album:1', 'Stub Album', 'album', album_tracks, can_play=True, artist='Stub Artist')
    playlist = collection('bt:playlist:1', 'Stub Playlist', 'playlist', playlist_tracks, can_play=True)
    many = collection('bt:many', 'Many Tracks (250)', 'container',
                      [track(100 + i, 'Long List %03d' % i, artist='Paging Test') for i in range(1, 251)])
    root = collection('root', 'Bridge Test', 'container', [
        collection('bt:playlists', 'Playlists', 'container', [playlist]),
        collection('bt:albums', 'Albums', 'container', [album]),
        collection('bt:artists', 'Artists', 'container',
                   [collection('bt:artist:1', 'Stub Artist', 'artist', [album])]),
        many,
        track(1000, 'A Single Stub Track')])
    search = collection('search', 'Search', 'container', [
        collection('artists', 'Artists', 'search'), collection('albums', 'Albums', 'search'),
        collection('tracks', 'Tracks', 'search'), collection('playlists', 'Playlists', 'search')])
    nodes = {}

    def index(node):
        nodes[node['id']] = node
        for child in node.get('children') or []: index(child)
    index(root); index(search)
    return nodes


def text(value):
    return html.escape(str(value), quote=False)


class SmapiStub:
    def __init__(self, host, port, service_id=240, name='Bridge Test', log=print, capabilities='1'):
        self.host, self.service_id, self.name, self.log = host, service_id, name, log
        self.capabilities = capabilities
        self.nodes = build_tree()
        self.requests = []   # (method, args) for tests
        self.art = self.make_art()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self): stub.handle_post(self)
            def do_GET(self): stub.handle_get(self)
            def log_message(self, fmt, *args): pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    # --- advertised to the CR100 --------------------------------------------------
    @property
    def uri(self):
        return 'http://%s:%d/smapi' % (self.host, self.port)

    @property
    def service_type(self):
        return self.service_id * 256 + SERVICE_TYPE_SUFFIX

    def descriptor(self):
        """MusicServices.ListAvailableServices out-arguments."""
        services = ('<Services><Service Id="%d" Name="%s" Version="1.1" Uri="%s" SecureUri="%s" '
                    'ContainerType="MService" Capabilities="%s"><Policy Auth="Anonymous" PollInterval="3600"/>'
                    '<Presentation/></Service></Services>'
                    % (self.service_id, html.escape(self.name), self.uri, self.uri, self.capabilities))
        return {'AvailableServiceDescriptorList': services, 'AvailableServiceTypeList': str(self.service_type),
                'AvailableServiceListVersion': LIST_VERSION}

    def media_servers(self):
        """ZoneGroupTopology ThirdPartyMediaServers: the CR100's Music menu lists media servers,
        and a music service only becomes one through a <Service> account entry here; the
        service list above just describes it (MainUI.swf Household.lookupMediaServer / isWebSvc).
        Wire attributes are UDN SA_RINCON<type>_<username>, Md, Password, NumAccounts,
        Username0, Md0, Password0 (native XML parser 0x58e480, CR100 4.3).
        zoneplayers encodes this XML for ThirdPartyMediaServersX: the plain event
        does not parse XML into the objects the Music menu requires. The placeholder
        account names the menu entry; anonymous SMAPI requests require no login."""
        user = ACCOUNT_NAME
        return ('<MediaServers><Service UDN="SA_RINCON%d_%s" Md="" Password="" NumAccounts="1" '
                'Username0="%s" Md0="" Password0=""/></MediaServers>' % (self.service_type, user, user))

    def owns(self, uri, metadata=''):
        """True when a URI or DIDL the CR100 hands a player came from this service."""
        return (('sid=%d' % self.service_id) in uri or ID_PREFIX in uri
                or 'SA_RINCON%d_' % self.service_type in (metadata or ''))

    # --- HTTP ---------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log({'event': 'smapi-stub', 'uri': self.uri, 'id': self.service_id, 'type': self.service_type})

    def close(self):
        self.server.shutdown(); self.server.server_close()

    def make_art(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return None
        image = Image.new('RGB', (160, 160), (30, 120, 60))
        ImageDraw.Draw(image).text((20, 70), 'Bridge Test', fill=(255, 255, 255))
        out = io.BytesIO(); image.save(out, 'JPEG', quality=85, progressive=False)
        return out.getvalue()

    def handle_get(self, request):
        self.log({'event': 'smapi-get', 'path': request.path, 'from': request.client_address[0],
                  'agent': request.headers.get('User-Agent')})
        if request.path.startswith('/art/') and self.art:
            self.reply(request, 200, self.art, 'image/jpeg')
        else:
            self.reply(request, 404, b'Not found', 'text/plain')

    def reply(self, request, status, body, content_type='text/xml; charset="utf-8"'):
        request.send_response(status)
        request.send_header('Content-Type', content_type)
        request.send_header('Content-Length', str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def handle_post(self, request):
        length = int(request.headers.get('Content-Length') or 0)
        raw = request.rfile.read(length) if 0 < length <= 1 << 20 else b''
        action = (request.headers.get('SOAPACTION') or '').strip('"').rpartition('#')[2]
        method, args, header = '', {}, ''
        try:
            envelope = ET.fromstring(raw)
            body = envelope.find('{%s}Body' % SOAP_NS)
            call = body[0]
            method = call.tag.rpartition('}')[2]
            args = {child.tag.rpartition('}')[2]: (child.text or '') for child in call}
            head = envelope.find('{%s}Header' % SOAP_NS)
            if head is not None: header = ''.join(ET.tostring(h, encoding='unicode') for h in head)
        except (ET.ParseError, IndexError, TypeError):
            pass
        self.requests.append((method or action, args))
        self.log({'event': 'smapi', 'method': method or action, 'soapaction': request.headers.get('SOAPACTION'),
                  'args': args, 'credentials': re.sub(r'\s+', ' ', header)[:600],
                  'agent': request.headers.get('User-Agent'), 'from': request.client_address[0]})
        try:
            result = self.dispatch(method, args)
            status, xml = 200, self.envelope('<%sResponse xmlns="%s">%s</%sResponse>' % (method, SMAPI_NS, result, method))
        except LookupError as error:
            status, xml = 500, self.fault('Client.ItemNotFound', str(error))
        except NotImplementedError:
            status, xml = 500, self.fault('Server.NotImplemented', '%s is not implemented by the stub' % (method or action))
        self.reply(request, status, xml.encode())

    def envelope(self, body):
        return '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="%s"><s:Body>%s</s:Body></s:Envelope>' % (SOAP_NS, body)

    def fault(self, code, message):
        return self.envelope('<s:Fault><faultcode>s:%s</faultcode><faultstring>%s</faultstring></s:Fault>'
                             % (code, text(message)))

    # --- SMAPI ----------------------------------------------------------------------
    def dispatch(self, method, args):
        if method == 'getMetadata': return self.get_metadata(args)
        if method == 'getMediaMetadata':
            return '<getMediaMetadataResult>%s</getMediaMetadataResult>' % self.entry(self.node(args.get('id')), wrap=False)
        if method == 'getExtendedMetadata':
            return '<getExtendedMetadataResult>%s</getExtendedMetadataResult>' % self.entry(self.node(args.get('id')))
        if method == 'getMediaURI':
            return '<getMediaURIResult>http://%s:%d/stream/%s.mp3</getMediaURIResult>' % (self.host, self.port, args.get('id'))
        if method == 'search': return self.search(args)
        if method == 'getLastUpdate':
            return ('<getLastUpdateResult><catalog>1</catalog><favorites>1</favorites>'
                    '<pollInterval>3600</pollInterval></getLastUpdateResult>')
        if method == 'getScrollIndices': return '<getScrollIndicesResult></getScrollIndicesResult>'
        if method == 'getSessionId': return '<getSessionIdResult>stub-session</getSessionIdResult>'
        raise NotImplementedError(method)

    def node(self, item_id):
        node = self.nodes.get(item_id or '')
        if node is None: raise LookupError('No such item: %s' % item_id)
        return node

    def page(self, items, args, wrapper):
        index, count = int(args.get('index') or 0), int(args.get('count') or 100)
        chosen = items[index:index + count]
        return ('<%s><index>%d</index><count>%d</count><total>%d</total>%s</%s>'
                % (wrapper, index, len(chosen), len(items), ''.join(self.entry(n) for n in chosen), wrapper))

    def get_metadata(self, args):
        return self.page(self.node(args.get('id')).get('children') or [], args, 'getMetadataResult')

    def search(self, args):
        kind, term = args.get('id', ''), (args.get('term') or '').lower()
        wanted = {'artists': 'artist', 'albums': 'album', 'tracks': 'track', 'playlists': 'playlist'}.get(kind)
        found = [n for n in self.nodes.values() if n['itemType'] == wanted and term in n['title'].lower()
                 and n['id'].startswith(ID_PREFIX)]
        if not found and wanted == 'track':   # any term finds something, so the results screen shows
            found = [track(9000, 'Search: %s' % args.get('term', ''))]
        return self.page(sorted(found, key=lambda n: n['title']), args, 'searchResult')

    def entry(self, node, wrap=True):
        art = 'http://%s:%d/art/%s.jpg' % (self.host, self.port, node['id'].replace(':', '_'))
        if node['itemType'] == 'track':
            fields = ('<id>%s</id><itemType>track</itemType><title>%s</title><mimeType>%s</mimeType>'
                      '<trackMetadata><artist>%s</artist><album>%s</album><albumId>%s</albumId>'
                      '<duration>%d</duration><albumArtURI>%s</albumArtURI><canPlay>true</canPlay>'
                      '<canSkip>true</canSkip><canAddToFavorites>false</canAddToFavorites></trackMetadata>'
                      % (text(node['id']), text(node['title']), node['mimeType'], text(node['artist']),
                         text(node['album']), text(node['albumId']), node['duration'], art))
            return '<mediaMetadata>%s</mediaMetadata>' % fields if wrap else fields
        fields = ('<id>%s</id><itemType>%s</itemType><title>%s</title>%s<canPlay>%s</canPlay>'
                  '<canEnumerate>true</canEnumerate><canAddToFavorites>false</canAddToFavorites>'
                  '<canScroll>false</canScroll><albumArtURI>%s</albumArtURI>'
                  % (text(node['id']), node['itemType'], text(node['title']),
                     '<artist>%s</artist>' % text(node['artist']) if node.get('artist') else '',
                     'true' if node['canPlay'] else 'false', art))
        return '<mediaCollection>%s</mediaCollection>' % fields if wrap else fields


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8400)
    parser.add_argument('--id', type=int, default=240)
    args = parser.parse_args()
    stub = SmapiStub(args.host, args.port, args.id,
                     log=lambda r: print(json.dumps({'time': round(time.time(), 3), **r}), flush=True))
    stub.start()
    print(json.dumps(stub.descriptor()))
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stub.close()


if __name__ == '__main__':
    main()
