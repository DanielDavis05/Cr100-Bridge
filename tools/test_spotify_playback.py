"""Playback identifier contracts, including the original CR100's captured shapes."""
import html
import unittest
from urllib.parse import quote

from spotify_playback import MAX_METADATA, owns, resolve_playback

TRACK = 'spotify:track:0123456789ABCDEFGHIJKL'
ALBUM = 'spotify:album:ABCDEFGHIJKL0123456789'
PLAYLIST = 'spotify:playlist:abcdefghijkl0123456789'
OTHER_TRACK = 'spotify:track:abcdefghijkl0123456789'
ARTIST = 'spotify:artist:0123456789ABCDEFGHIJKL'
ACCOUNT = 'SA_RINCON61447_bridge'


def didl(item, account=ACCOUNT, res=None, parent='0004004cspotify%3aalbum%3aABCDEFGHIJKL0123456789'):
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="%s" parentID="%s" restricted="true">'
            '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">%s</desc>%s'
            '</item></DIDL-Lite>' % (html.escape(item, quote=True), html.escape(parent, quote=True),
                                    html.escape(account),
                                    '<res>%s</res>' % html.escape(res) if res is not None else ''))


def track_uri(item=TRACK, sid='240'):
    return 'x-sonos-http:%s.mp3?sid=%s&flags=0' % (quote(item, safe=''), sid)


class Playback(unittest.TestCase):
    def test_native_track_shape(self):
        # Native frame6890: bt:track:1 -> x-sonos-http:bt%3atrack%3a1.mp3?sid=240&flags=0,
        # DIDL id00030000bt%3atrack%3a1; the service substitutes a canonical Spotify id.
        uri = track_uri()
        metadata = didl('00030000' + quote(TRACK, safe=''))
        self.assertEqual(resolve_playback(uri, metadata), (TRACK, 'music'))
        self.assertTrue(owns(uri, metadata))

    def test_native_album_shape_has_no_sid(self):
        # Native frame6757: x-rincon-cpcontainer:0004004cbt%3aalbum%3a1.
        item = '0004004c' + quote(ALBUM, safe='')
        self.assertEqual(resolve_playback('x-rincon-cpcontainer:' + item, didl(item)), (ALBUM, 'album'))
        self.assertTrue(owns('x-rincon-cpcontainer:' + item, didl(item)))

    def test_playlist_and_unwrapped_container(self):
        # Native Spotify probe frame9450 uses 0006004c before the escaped playlist id.
        for item in (PLAYLIST, quote(PLAYLIST, safe=''), '0006004c' + quote(PLAYLIST, safe=''),
                     '1006206c' + quote(PLAYLIST, safe='')):
            with self.subTest(item=item):
                self.assertEqual(resolve_playback('x-rincon-cpcontainer:' + item, didl(item)),
                                 (PLAYLIST, 'playlist'))

    def test_bare_canonical_requires_ownership(self):
        self.assertFalse(owns(TRACK))
        with self.assertRaises(ValueError):
            resolve_playback(TRACK)
        self.assertEqual(resolve_playback(TRACK, didl(TRACK)), (TRACK, 'music'))

    def test_optional_track_extension_and_escaped_query(self):
        self.assertEqual(resolve_playback(track_uri().replace('&', '&amp;')), (TRACK, 'music'))
        self.assertEqual(resolve_playback(track_uri().replace('.mp3', '')), (TRACK, 'music'))

    def test_nested_encoding_is_bounded(self):
        item = TRACK
        for _ in range(3):
            item = quote(item, safe='')
        self.assertEqual(resolve_playback('x-sonos-http:' + item + '.mp3?sid=240'), (TRACK, 'music'))
        with self.assertRaises(ValueError):
            resolve_playback('x-sonos-http:' + quote(item, safe='') + '.mp3?sid=240')

    def test_ownership_uses_exact_fields(self):
        for uri in (track_uri(sid='2400'), track_uri(sid='0240'),
                    'http://example.test/sid=240', 'http://example.test/?other=sid%3D240',
                    'http://example.test/?sid=240'):
            with self.subTest(uri=uri):
                self.assertFalse(owns(uri))
        self.assertFalse(owns(TRACK, didl(TRACK, 'SA_RINCON614470_bridge')))
        self.assertFalse(owns(TRACK, didl(TRACK, 'SA_RINCON061447_bridge')))
        self.assertFalse(owns(TRACK, didl(TRACK).replace('id="cdudn"', 'id="title"')))
        self.assertFalse(owns(TRACK, '<DIDL-Lite><item id="%s"><title>%s</title></item></DIDL-Lite>'
                              % (TRACK, ACCOUNT)))

    def test_foreign_sid_and_metadata_are_rejected(self):
        for uri, metadata in ((track_uri(sid='12'), didl(TRACK)),
                              (track_uri(), didl(TRACK, 'SA_RINCON3079_other')),
                              (track_uri(), didl(TRACK, res=track_uri(sid='12')))):
            with self.subTest(uri=uri, metadata=metadata), self.assertRaises(ValueError):
                resolve_playback(uri, metadata)

    def test_didl_id_and_resource_must_match(self):
        for metadata in (didl(OTHER_TRACK), didl(TRACK, res=track_uri(OTHER_TRACK)),
                         didl(TRACK, res='https://example.test/track.mp3')):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                resolve_playback(track_uri(), metadata)
        self.assertEqual(resolve_playback(track_uri(), didl(TRACK, res=track_uri())), (TRACK, 'music'))

    def test_didl_parent_is_not_playback_target(self):
        self.assertEqual(resolve_playback(track_uri(), didl(TRACK, parent=ALBUM)), (TRACK, 'music'))

    def test_strict_spotify_identifiers(self):
        bad = [ARTIST, 'spotify:track:short', TRACK + 'x', TRACK + '/', TRACK + '?sid=12',
               TRACK + '\n', TRACK + '#fragment', TRACK.replace('A', '-'),
               'prefix' + TRACK, 'https://open.spotify.com/track/' + TRACK.rsplit(':', 1)[1],
               TRACK.replace('spotify:', 'Spotify:'), 'bt:track:1', TRACK + '%', TRACK + '%xx']
        for item in bad:
            uri = 'x-sonos-http:' + quote(item, safe='%') + '.mp3?sid=240'
            with self.subTest(item=item), self.assertRaises(ValueError):
                resolve_playback(uri)

    def test_bad_queries_and_encoded_query_injection(self):
        path = track_uri().split('?', 1)[0]
        for query in ('sid=240&sid=12', 'sid=240&sid=240', 'sid=240&flags=0&flags=1',
                      'sid=240&uri=https://evil.test/', 'sid=240&flags=-1', 'sid=240&flags=',
                      'sid=240;flags=0', 'sid=240&flags=0%26sid%3D12', 'sid=240&flags=4294967296',
                      'sid=%GG', 'sid=240&flags=0&', ''):
            with self.subTest(query=query), self.assertRaises(ValueError):
                resolve_playback(path + '?' + query)
        for payload in (quote(TRACK + '?sid=12', safe=''), quote(TRACK + '&sid=12', safe='')):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                resolve_playback('x-sonos-http:' + payload + '.mp3?sid=240')

    def test_scheme_and_extension_must_match_item(self):
        for uri in ('x-sonos-http:' + quote(ALBUM, safe='') + '.mp3?sid=240',
                    track_uri().replace('.mp3', '.flac'), 'x-sonos-http://' + TRACK + '?sid=240',
                    'x-rincon-cpcontainer:arbitrary' + TRACK + '?sid=240'):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                resolve_playback(uri)

    def test_metadata_is_bounded_safe_xml(self):
        malformed = ['not XML', '<DIDL-Lite>', '<wrong/>', '<DIDL-Lite/>',
                     '<!DOCTYPE DIDL-Lite [<!ENTITY x "expansion">]><DIDL-Lite/>',
                     '<!DOCTYPE DIDL-Lite SYSTEM "file:///etc/passwd"><DIDL-Lite/>',
                     '<DIDL-Lite><item id="%s"/><item id="%s"/></DIDL-Lite>' % (TRACK, TRACK),
                     'x' * (MAX_METADATA + 1),
                     '<DIDL-Lite><item id="%s">%s%s</item></DIDL-Lite>'
                     % (TRACK, '<x>' * 20, '</x>' * 20),
                     '<DIDL-Lite><item id="%s">%s</item></DIDL-Lite>' % (TRACK, '<x/>' * 130)]
        for metadata in malformed:
            with self.subTest(metadata=metadata[:100]), self.assertRaises(ValueError):
                resolve_playback(track_uri(), metadata)

    def test_bad_account_marker_does_not_claim_service(self):
        for account in ('junk' + ACCOUNT, ACCOUNT + ' extra', 'SA_RINCON61447_', 'SA_RINCON61447x_bridge'):
            with self.subTest(account=account):
                metadata = didl(TRACK, account)
                self.assertFalse(owns(TRACK, metadata))
                with self.assertRaises(ValueError):
                    resolve_playback(TRACK, metadata)

    def test_malformed_owned_items_are_claimed_then_rejected(self):
        uri = 'x-rincon-cpcontainer:not-a-spotify-id'
        metadata = didl('not-a-spotify-id')
        self.assertTrue(owns(uri, metadata))
        with self.assertRaises(ValueError):
            resolve_playback(uri, metadata)

    def test_service_id_validation_and_custom_id(self):
        for service_id in (0, -1, 65536, True, '240', None):
            with self.subTest(service_id=service_id):
                with self.assertRaises(ValueError):
                    resolve_playback(track_uri(), service_id=service_id)
                with self.assertRaises(ValueError):
                    owns(track_uri(), service_id=service_id)
        self.assertEqual(resolve_playback(track_uri(sid='241'), service_id=241), (TRACK, 'music'))
        self.assertTrue(owns(track_uri(sid='241'), service_id=241))

    def test_non_string_and_oversized_inputs(self):
        for uri in (None, 240, 'x' * 4097):
            with self.subTest(uri=repr(uri)[:50]), self.assertRaises(ValueError):
                resolve_playback(uri)
        with self.assertRaises(ValueError):
            resolve_playback(track_uri(), None)


if __name__ == '__main__':
    unittest.main()
