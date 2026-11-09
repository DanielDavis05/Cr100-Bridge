import copy
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from radiotime import RadioTimeClient, RadioTimeError, merge_metadata


class ClientTests(unittest.TestCase):
    def client(self, body=None, head=None, headers=None):
        calls = []
        def transport(url, **kwargs):
            calls.append((url, kwargs))
            return 200, headers or {}, dict(head=head or {'status': '200'}, body=copy.deepcopy(body or []))
        return RadioTimeClient(transport, min_request_interval=0), calls

    def test_api_fault_inside_http_200_is_not_empty(self):
        client, _ = self.client(head={'status': '400', 'fault': 'Invalid category', 'fault_code': 'id'})
        with self.assertRaises(RadioTimeError) as result:
            client.browse(id='cbad')
        self.assertEqual(result.exception.code, 'id')
        self.assertEqual(result.exception.status, 400)

    def test_valid_empty_response(self):
        client, _ = self.client()
        self.assertEqual(client.browse().body, [])

    def test_nowplaying_http_ttl_wins_and_is_respected(self):
        client, calls = self.client(head={'status': '200', 'ttl': '1'}, headers={'Cache-Control': 'private, max-age=600'})
        now = [10]
        client.clock = lambda: now[0]
        self.assertEqual(client.now_playing('s123').ttl, 600)
        now[0] = 609
        client.now_playing('s123')
        self.assertEqual(len(calls), 1)
        now[0] = 610
        client.now_playing('s123')
        self.assertEqual(len(calls), 2)

    def test_no_store_is_not_cached(self):
        client, calls = self.client(headers={'Cache-Control': 'no-store'})
        client.browse()
        client.browse()
        self.assertEqual(len(calls), 2)

    def test_one_fetch_for_concurrent_requests(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            entered.set()
            release.wait(2)
            return {'head': {'status': '200'}, 'body': []}
        client = RadioTimeClient(transport, min_request_interval=0)
        threads = [threading.Thread(target=client.browse) for _ in range(4)]
        for thread in threads:
            thread.start()
        entered.wait(1)
        release.set()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(calls), 1)

    def test_navigation_is_public_and_host_scoped(self):
        client, calls = self.client()
        client.request('http://opml.radiotime.com/Browse.ashx?id=s3&partnerId=OtherDevice&serial=secret')
        self.assertNotIn('partner', calls[0][0].lower())
        self.assertNotIn('secret', calls[0][0])
        with self.assertRaises(RadioTimeError):
            client.request('http://localhost/anything')
        with self.assertRaises(RadioTimeError):
            client.request('Browse.ashx', partnerId='OtherDevice')

    def test_search_query_unicode_and_mode(self):
        client, calls = self.client()
        client.search('José & Ana', 'hosts')
        query = parse_qs(urlsplit(calls[0][0]).query)
        self.assertEqual(query['query'], ['José & Ana'])
        self.assertEqual(query['filter'], ['p'])

    def test_streams_refetched_ordered_and_placeholders_rejected(self):
        client, calls = self.client([
            dict(element='audio', url='http://cdn-cms.tunein.com/service/Audio/notcompatible.enUS.mp3', media_type='mp3'),
            dict(element='audio', url='https://station.test/main.aac', media_type='aac', bitrate=128, reliability='99'),
            dict(element='audio', url='https://station.test/backup.mp3', media_type='mp3', bitrate=64, reliability=90),
            dict(element='audio', url='https://station.test/main.aac', media_type='aac'),
            dict(element='audio', url='https://station.test/unsupported.wma', media_type='wma'),
        ])
        result = client.resolve_streams('s1', formats='mp3,aac')
        self.assertEqual([stream.format for stream in result], ['aac', 'mp3'])
        self.assertEqual(result[0].bitrate, 128)
        client.resolve_streams('s1', formats=['mp3', 'aac'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(parse_qs(urlsplit(calls[0][0]).query)['formats'], ['mp3,aac'])

    def test_unavailable_is_an_error(self):
        client, _ = self.client([dict(element='audio', url='https://cdn-cms.tunein.com/service/Audio/nostream.enUS.mp3')])
        with self.assertRaises(RadioTimeError) as result:
            client.resolve_streams('s1')
        self.assertEqual(result.exception.code, 'unavailable')

    def test_wtmc_mp3_label_does_not_hide_hls_container(self):
        # Actual public WTMC Tune shape: MP3 is the codec inside the HLS
        # playlist, not a continuous MP3 URL accepted by legacy renderers.
        row = dict(element='audio',
                   url='https://video.deldot.gov/live/WTMC1380.stream/playlist.m3u8',
                   media_type='mp3', bitrate=128, reliability=100,
                   is_hls_advanced='false', live_seek_stream='false')
        client, _ = self.client([row])
        with self.assertRaises(RadioTimeError) as result:
            client.resolve_streams('s342087', formats=('mp3', 'aac', 'wma'))
        self.assertEqual(result.exception.code, 'unavailable')
        stream = client.resolve_streams('s342087')[0]
        self.assertEqual((stream.format, stream.bitrate, stream.reliability), ('hls', 128, 100))
        self.assertEqual(stream.metadata['container'], 'hls')
        self.assertEqual(stream.metadata['codec'], 'mp3')
        self.assertEqual(stream.metadata['media_type'], 'mp3')
        self.assertEqual(stream.metadata['provider_media_type'], 'mp3')

    def test_hls_only_fallback_requests_provider_codecs_then_filters_container(self):
        calls = []
        def transport(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            calls.append(query)
            if 'mp3' not in query['formats'][0].split(','):
                return dict(head={'status': '200'}, body=[dict(element='audio', media_type='mp3',
                    url='https://cdn-cms.tunein.com/service/Audio/notcompatible.enUS.mp3')])
            return dict(head={'status': '200'}, body=[
                dict(element='audio', media_type='mp3', url='https://radio.test/live.m3u8?token=abc'),
                dict(element='audio', media_type='mp3', url='https://radio.test/direct.mp3'),
                dict(element='audio', media_type='aac', url='https://radio.test/direct.aac')])
        client = RadioTimeClient(transport, min_request_interval=0)
        self.assertEqual([stream.format for stream in client.resolve_streams('s342087', formats=('hls',))], ['hls'])
        self.assertEqual(calls[0]['formats'], ['mp3,aac'])
        self.assertEqual(calls[0]['id'], ['s342087'])
        self.assertEqual([stream.format for stream in client.resolve_streams('s342087', formats=('wma', 'hls'))], ['hls'])
        self.assertEqual(calls[1]['formats'], ['wma,mp3,aac'])

    def test_hls_m3u_is_not_expanded_into_independent_audio_segments(self):
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            if urlsplit(url).hostname == 'opml.radiotime.com':
                return dict(head={'status': '200'}, body=[dict(element='audio', media_type='mp3',
                    url='https://radio.test/live.m3u')])
            return 200, {}, b'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment.mp3\n'
        client = RadioTimeClient(transport, min_request_interval=0)
        stream = client.resolve_streams('s1', formats=('hls',))[0]
        self.assertEqual((stream.url, stream.format), ('https://radio.test/live.m3u', 'hls'))
        self.assertEqual(stream.metadata['codec'], 'mp3')
        self.assertEqual(len(calls), 2)
        with self.assertRaises(RadioTimeError):
            client.resolve_url('https://radio.test/live.m3u', formats=('mp3',))

    def test_m3u_pls_alternates_and_hls_are_distinct(self):
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            if url.endswith('.pls'):
                return 200, {}, b'[playlist]\nFile1=main.mp3\nFile2=backup.aac\nNumberOfEntries=2\n'
            return 200, {}, b'#EXTM3U\nother.pls\n'
        client = RadioTimeClient(transport, min_request_interval=0)
        self.assertEqual([stream.url for stream in client.resolve_url('https://station.test/list.pls')],
                         ['https://station.test/main.mp3', 'https://station.test/backup.aac'])
        before = len(calls)
        self.assertEqual(client.resolve_url('https://station.test/live.m3u8', ['hls'])[0].format, 'hls')
        self.assertEqual(len(calls), before)
        with self.assertRaises(RadioTimeError):
            client.resolve_url('https://station.test/live.m3u8', ['mp3'])

    def test_xml_envelope_and_malformed_response(self):
        client = RadioTimeClient(lambda url, **kw: (200, {}, b'<opml><head><status>200</status></head><body><outline text="Stations"><outline text="One" type="audio" guide_id="s1"/></outline></body></opml>'), min_request_interval=0)
        self.assertEqual(client.browse().body[0]['children'][0]['guide_id'], 's1')
        bad = RadioTimeClient(lambda url, **kw: (200, {}, b'bad json and xml'), min_request_interval=0)
        with self.assertRaises(RadioTimeError):
            bad.browse()

    def test_legacy_xml_profile_wrapper_is_normalized(self):
        client = RadioTimeClient(lambda url, **kw: (200, {}, b'<opml><head><status>200</status></head><body><outline type="object"><station guide_id="s1"><name>Station</name><logo>logo.jpg</logo></station></outline></body></opml>'), min_request_interval=0)
        row = client.describe('s1').body[0]
        self.assertEqual(row['element'], 'station')
        self.assertEqual(row['name'], 'Station')

    def test_url_probe_falls_back_from_head_and_rejects_html(self):
        calls = []
        def transport(url, **kwargs):
            calls.append(kwargs)
            if kwargs.get('method') == 'HEAD':
                return 405, {}, b''
            return 206, {'Content-Type': 'audio/mpeg'}, b'ID3'
        client = RadioTimeClient(transport, min_request_interval=0)
        self.assertTrue(client.test_url('https://radio.test/live.mp3')[0].metadata['verified'])
        self.assertEqual(calls[-1]['headers']['Range'], 'bytes=0-4095')
        html = RadioTimeClient(lambda *args, **kw: (200, {'Content-Type': 'text/html'}, b''), min_request_interval=0)
        with self.assertRaises(RadioTimeError):
            html.test_url('https://radio.test/live.mp3')

    def test_asx_and_mms_windows_media_are_not_lost(self):
        def transport(url, **kwargs):
            if url.endswith('.asx'):
                return 200, {}, b'<ASX><ENTRY><REF HREF="mms://radio.test/live"/></ENTRY></ASX>'
            return {'head': {'status': '200'}, 'body': [dict(element='audio', url='https://radio.test/list.asx', media_type='wma')]}
        client = RadioTimeClient(transport, min_request_interval=0)
        stream = client.resolve_streams('s1', formats=['wma'])[0]
        self.assertEqual((stream.url, stream.format), ('mms://radio.test/live', 'wma'))
        with self.assertRaises(RadioTimeError):
            client.resolve_streams('s1', formats=['mp3'])

    def test_metadata_preserves_cover_when_profile_has_null_art(self):
        result = merge_metadata({'image': 'station.jpg', 'playing_image': 'album.jpg', 'playing': 'Artist - Title'},
                                {'logo': 'canonical-station.jpg', 'current_song': 'Title', 'current_artist': 'Artist', 'current_album_art': None})
        self.assertEqual(result['song_image'], 'album.jpg')
        self.assertEqual(result['station_image'], 'canonical-station.jpg')
        self.assertEqual(result['song_title'], 'Title')
        self.assertEqual(result['playing'], 'Artist - Title')


if __name__ == '__main__':
    unittest.main()
