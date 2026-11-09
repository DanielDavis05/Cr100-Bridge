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
import io
import json
import re
import time
import xml.etree.ElementTree as ET

from smapi_service import (ACCOUNT_NAME, LIST_VERSION, SERVICE_TYPE_SUFFIX, SMAPI_NS, SOAP_NS,
                           SmapiService, collection, text)

ID_PREFIX = 'bt:'


def track(number, title, artist='Stub Artist', album='Stub Album', album_id='bt:album:1', duration=200):
    return {'id': 'bt:track:%d' % number, 'itemType': 'track', 'title': title, 'mimeType': 'audio/mpeg',
            'artist': artist, 'album': album, 'albumId': album_id, 'duration': duration}


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


class SmapiStub(SmapiService):
    startup_event = 'smapi-stub'

    def __init__(self, host, port, service_id=240, name='Bridge Test', log=print, capabilities='1'):
        self.nodes = build_tree()
        self.art = self.make_art()
        super().__init__(host, port, service_id, name, log, capabilities)

    def owns(self, uri, metadata=''):
        """Recognize diagnostic items before the player can try to play them."""
        return (('sid=%d' % self.service_id) in uri or ID_PREFIX in uri
                or 'SA_RINCON%d_' % self.service_type in (metadata or ''))

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

    def on_request(self, request, method, args, header):
        # This explicitly diagnostic service retains its capture-style logging.
        self.requests.append((method, args))
        credentials = '' if header is None else ''.join(ET.tostring(h, encoding='unicode') for h in header)
        self.log({'event': 'smapi', 'method': method, 'soapaction': request.headers.get('SOAPACTION'),
                  'args': args, 'credentials': re.sub(r'\s+', ' ', credentials)[:600],
                  'agent': request.headers.get('User-Agent'), 'from': request.client_address[0]})

    def log_response(self, method, status):
        pass  # on_request already records each diagnostic call.

    def error_fault(self, error):
        if isinstance(error, LookupError):
            return 'Client.ItemNotFound', str(error)
        if isinstance(error, NotImplementedError):
            return 'Server.NotImplemented', '%s is not implemented by the stub' % error
        return super().error_fault(error)

    # --- SMAPI ----------------------------------------------------------------------
    def dispatch(self, method, args):
        if method == 'getMediaURI':
            return '<getMediaURIResult>http://%s:%d/stream/%s.mp3</getMediaURIResult>' % (self.host, self.port, args.get('id'))
        if method == 'getLastUpdate':
            return ('<getLastUpdateResult><catalog>1</catalog><favorites>1</favorites>'
                    '<pollInterval>3600</pollInterval></getLastUpdateResult>')
        if method == 'getSessionId': return '<getSessionIdResult>stub-session</getSessionIdResult>'
        return super().dispatch(method, args)

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
