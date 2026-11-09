"""Offline native playback contract: account boundaries, finite starts and secrets."""
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse

import spotify_connect as connect

SEED = 'spotify:track:' + 'a' * 22
NEXT = 'spotify:track:' + 'b' * 22
OTHER = 'spotify:track:' + 'c' * 22
BASE = 'http://10.0.0.130:1400'
RECEIVER = '9' * 40
ACCOUNT = 'canonical-user'
OAUTH = 'OAUTH_SECRET'
TOKEN = 'NATIVE_TOKEN_SECRET'


class Folders:
    def __init__(self):
        self.connected, self.account, self.token, self.calls = True, ACCOUNT, OAUTH, []
    def _connected(self):
        return self.connected
    def _canonical_account(self):
        self.calls.append('account')
        return self.account
    def _access_token(self):
        self.calls.append('token')
        return self.token


class Native:
    def __init__(self, account, token, device):
        self.arguments, self.closed = (account, token, device), False
    def token(self):
        return TOKEN
    def close(self):
        self.closed = True


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class Opener:
    def __init__(self):
        self.calls, self.responses = [], {}
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        value = self.responses[request.full_url]
        if isinstance(value, Exception):
            raise value
        return Response(value)


class Adapter(unittest.TestCase):
    def setUp(self):
        self.folders, self.opener, self.logs, self.sessions = Folders(), Opener(), [], []
        self.source = 'x-sonos-vli:opaque'
        self.soap_calls = []
        def factory(*args):
            session = Native(*args)
            self.sessions.append(session)
            return session
        def soap(*args, **kwargs):
            self.soap_calls.append((args, kwargs))
            return {'CurrentURI': self.source}
        self.adapter = connect.SpotifyConnect(object(), self.folders, log=self.logs.append,
            session_factory=factory, opener=self.opener, soap=soap)
        self.opener.responses['https://apresolve.spotify.com/?type=spclient'] = {'spclient': ['native.spotify.com:443']}
        self.origin = 'https://native.spotify.com:443'
        self.command = self.origin + '/connect-state/v1/player/command/from/' + self.adapter.device_id + '/to/' + RECEIVER
        self.opener.responses[self.command] = {'ack_id': 'opaque'}
        self.info = {'deviceID': RECEIVER, 'clientID': '8' * 32, 'brandDisplayName': 'Sonos',
                     'version': '2.7.0', 'tokenType': 'authorization_code'}
        self.opener.responses[BASE + '/spotifyzc?action=getInfo'] = self.info

    def tearDown(self):
        self.adapter.close()

    def test_constructor_and_local_preflight_never_authenticate_or_request(self):
        self.assertEqual((self.folders.calls, self.sessions, self.opener.calls), ([], [], []))
        self.assertTrue(self.adapter.available())
        self.adapter.check_config()
        self.assertEqual((self.folders.calls, self.sessions, self.opener.calls), ([], [], []))
        self.folders.connected = False
        self.assertFalse(self.adapter.available())
        with self.assertRaisesRegex(connect.SpotifyConnectError, 'Connect Spotify playback'):
            self.adapter.check_config()

    def test_prepare_reuses_verified_session_and_replaces_it_for_rotated_grant(self):
        self.adapter.prepare()
        self.adapter.prepare()
        self.assertEqual(len(self.sessions), 1)
        first = self.sessions[0]
        self.assertEqual(first.arguments, (ACCOUNT, OAUTH, self.adapter.device_id))
        self.folders.token = 'ROTATED_SECRET'
        self.adapter.prepare()
        self.assertTrue(first.closed)
        self.assertEqual(len(self.sessions), 2)
        self.assertNotIn('SECRET', repr(self.logs))

    def test_metadata_only_recommendations_validate_filter_and_deduplicate(self):
        url = self.origin + '/context-resolve/v1/autoplay'
        self.opener.responses[url] = {'pages': [{'tracks': [
            {'uri': SEED}, {'uri': NEXT}, {'uri': OTHER}, {'uri': NEXT},
            {'uri': 'spotify:episode:' + 'd' * 22}, {'uri': 'https://invalid'}, None]}]}
        self.assertEqual(self.adapter.recommendations(SEED, [OTHER]), [NEXT])
        request, timeout = self.opener.calls[-1]
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(request.headers['Content-type'], 'application/x-protobuf')
        self.assertEqual(request.data, b'\x0a' + bytes([len(SEED)]) + SEED.encode()
                         + b'\x12' + bytes([len(OTHER)]) + OTHER.encode())
        self.assertEqual(self.soap_calls, [])
        self.assertEqual(timeout, connect.HTTP_TIMEOUT)
        self.assertFalse(any('accounts.spotify.com' in r.full_url for r, _ in self.opener.calls))

    def test_finite_play_never_delegates_autoplay_or_seeks_or_mutates_preferences(self):
        result = self.adapter.play_track(BASE, SEED)
        self.assertEqual(result, {'device_id': RECEIVER, 'activated': False})
        request = self.opener.calls[-1][0]
        body = json.loads(request.data)
        command = body['command']
        self.assertEqual(command['endpoint'], 'play')
        self.assertEqual(command['context'], {'uri': '', 'url': '', 'metadata': {},
                         'pages': [{'tracks': [{'uri': SEED}]}]})
        self.assertNotIn('seek_to', command['options'])
        self.assertFalse(command['options']['player_options_override']['repeating_context'])
        self.assertEqual([args[2] for args, _ in self.soap_calls], ['GetMediaInfo'])
        self.assertFalse(any('preferences' in r.full_url or 'attributes' in r.full_url
                             or '/api/token' in r.full_url for r, _ in self.opener.calls))

    def test_inactive_receiver_gets_scoped_code_not_long_lived_credential(self):
        self.source = 'x-rincon-queue:room#0'
        self.opener.responses['https://accounts.spotify.com/api/token'] = {'access_token': 'RECEIVER_CODE_SECRET'}
        self.opener.responses[BASE + '/spotifyzc'] = {'status': 101, 'spotifyError': 0}
        self.assertTrue(self.adapter.play_track(BASE, SEED)['activated'])
        exchange = next(r for r, _ in self.opener.calls if r.full_url.endswith('/api/token'))
        fields = urllib.parse.parse_qs(exchange.data.decode())
        self.assertEqual(fields['subject_token'], [OAUTH])
        self.assertEqual(fields['audience'], [self.info['clientID']])
        activation = next(r for r, _ in self.opener.calls if r.full_url == BASE + '/spotifyzc')
        fields = urllib.parse.parse_qs(activation.data.decode(), keep_blank_values=True)
        self.assertEqual(fields['blob'], ['RECEIVER_CODE_SECRET'])
        self.assertEqual(fields['loginId'], [ACCOUNT])
        self.assertNotIn('Authorization', activation.headers)
        self.assertNotIn(OAUTH, activation.data.decode())
        self.assertNotIn(TOKEN, activation.data.decode())
        self.assertNotIn('SECRET', repr(self.logs))

    def test_invalid_inputs_and_untrusted_receiver_do_not_send_credentials(self):
        for base in ('http://127.0.0.1:1400', 'http://10.0.0.130:80', 'http://user:secret@10.0.0.130:1400',
                     'http://public.example:1400', 'http://10.0.0.130:1400/untrusted', 'https://10.0.0.130:1400'):
            with self.subTest(base=base), self.assertRaises(connect.SpotifyConnectError):
                self.adapter.play_track(base, SEED)
        with self.assertRaises(connect.SpotifyConnectError):
            self.adapter.recommendations('spotify:album:' + 'a' * 22)
        self.assertEqual(self.sessions, [])
        self.info['deviceID'] = 'bad'
        with self.assertRaises(connect.SpotifyConnectError):
            self.adapter.play_track(BASE, SEED)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertNotIn('Authorization', self.opener.calls[0][0].headers)

    def test_resolver_cannot_redirect_bearer_outside_spotify(self):
        self.opener.responses['https://apresolve.spotify.com/?type=spclient'] = {'spclient': ['evil.example:443']}
        with self.assertRaises(connect.SpotifyConnectError):
            self.adapter.recommendations(SEED)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertNotIn('Authorization', self.opener.calls[0][0].headers)

    def test_error_bodies_and_auth_exceptions_are_never_exposed(self):
        self.opener.responses[BASE + '/spotifyzc?action=getInfo'] = urllib.error.HTTPError(
            BASE, 403, 'RECEIVER_CODE_SECRET', {}, io.BytesIO(b'OAUTH_SECRET NATIVE_TOKEN_SECRET'))
        with self.assertRaises(connect.SpotifyConnectError) as caught:
            self.adapter.play_track(BASE, SEED)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('SECRET', repr(self.logs))
        self.folders._access_token = mock.Mock(side_effect=RuntimeError(OAUTH))
        with self.assertRaises(connect.SpotifyConnectError) as caught:
            self.adapter.prepare()
        self.assertNotIn('SECRET', str(caught.exception))

    def test_close_disposes_session_and_prevents_restart(self):
        self.adapter.prepare()
        self.adapter.close()
        self.assertTrue(self.sessions[0].closed)
        with self.assertRaises(connect.SpotifyConnectError):
            self.adapter.prepare()

    def test_login5_failure_happens_before_receiver_or_playback_requests(self):
        self.adapter._session_factory = lambda *args: Native(*args)
        with mock.patch.object(Native, 'token', side_effect=RuntimeError(TOKEN)):
            with self.assertRaises(connect.SpotifyConnectError) as caught:
                self.adapter.play_track(BASE, SEED)
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(self.soap_calls, [])
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_failed_helper_is_replaced_on_next_attempt(self):
        self.adapter.prepare()
        first = self.sessions[0]
        first.token = mock.Mock(side_effect=RuntimeError(TOKEN))
        with self.assertRaises(connect.SpotifyConnectError):
            self.adapter.play_track(BASE, SEED)
        self.assertTrue(first.closed)
        self.assertEqual(self.opener.calls, [])
        self.adapter.play_track(BASE, SEED)
        self.assertEqual(len(self.sessions), 2)

    def test_close_during_login5_does_not_leave_late_helper_or_play_request(self):
        started, finish = threading.Event(), threading.Event()
        result = []
        class SlowNative(Native):
            def token(self):
                started.set()
                finish.wait(2)
                return TOKEN
        session = SlowNative(ACCOUNT, OAUTH, self.adapter.device_id)
        self.adapter._session_factory = lambda *args: session
        def prepare():
            try:
                self.adapter.prepare()
            except connect.SpotifyConnectError as error:
                result.append(str(error))
        thread = threading.Thread(target=prepare)
        thread.start()
        self.assertTrue(started.wait(1))
        self.adapter.close()
        finish.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(session.closed)
        self.assertTrue(result)
        self.assertIsNone(self.adapter._session)


class SessionProcess(unittest.TestCase):
    def spawn(self, code):
        original = subprocess.Popen
        def replacement(args, **kwargs):
            return original([sys.executable, '-u', '-c', code], **kwargs)
        return mock.patch.object(connect.subprocess, 'Popen', replacement)

    def test_private_pipe_protocol_and_bounded_close(self):
        code = ('import sys,json\n'
                'for line in sys.stdin:\n'
                ' request=json.loads(line)\n'
                ' print(json.dumps({"ok":True,**({"access_token":"NATIVE_TOKEN_SECRET"} if request["op"]=="token" else {})}),flush=True)\n')
        with self.spawn(code):
            session = connect._SessionProcess(ACCOUNT, OAUTH, connect.DEFAULT_DEVICE_ID)
            self.assertEqual(session.token(), TOKEN)
            started = time.monotonic()
            session.close()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNotNone(session.process.poll())

    def test_worker_timeout_is_bounded_and_does_not_echo_credentials(self):
        with self.spawn('import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n'), \
                mock.patch.object(connect, 'SESSION_TIMEOUT', .05):
            with self.assertRaises(connect.SpotifyConnectError) as caught:
                connect._SessionProcess(ACCOUNT, OAUTH, connect.DEFAULT_DEVICE_ID)
        self.assertNotIn(OAUTH, str(caught.exception))


class RecommendationPaging(unittest.TestCase):
    """Exercise the real cursor independently of the runtime's candidate queue."""
    setUp = Adapter.setUp
    tearDown = Adapter.tearDown
    @staticmethod
    def uri(number):
        return 'spotify:track:%022d' % number

    @staticmethod
    def page_link(number):
        return 'hm://radio-apollo/v3/tracks/spotify:station:track:' + 'a' * 22 + '?pageNum=%d' % number

    def response(self, numbers, next_page=None, page_url=None):
        result = {'tracks': [{'uri': self.uri(number)} for number in numbers]}
        if next_page is not None:
            result['next_page_url'] = next_page
        if page_url is not None:
            result['page_url'] = page_url
        return result

    def initial(self, page):
        self.opener.responses[self.origin + '/context-resolve/v1/autoplay'] = {'pages': [page]}

    def page_response(self, link, response):
        self.opener.responses[connect._page_target(self.origin, link)] = response

    def test_station_pages_advance_beyond_fifty_without_resolving_again(self):
        one, two = self.page_link(1), self.page_link(2)
        self.initial(self.response(range(1, 51), one))
        self.page_response(one, self.response(range(40, 101), two))
        self.page_response(two, self.response(range(101, 151)))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(stream.next_batch(), [self.uri(i) for i in range(1, 51)])
        self.assertEqual(stream.next_batch(), [self.uri(i) for i in range(51, 101)])
        self.assertEqual(stream.next_batch(), [self.uri(i) for i in range(101, 151)])
        self.assertTrue(stream.exhausted)
        count = len(self.opener.calls)
        self.assertEqual(stream.next_batch(), [])
        self.assertEqual(len(self.opener.calls), count)
        posts = [r for r, _ in self.opener.calls if r.method == 'POST']
        self.assertEqual(len(posts), 1)

    def test_full_history_filters_locally_but_only_last_hundred_are_sent(self):
        history = [self.uri(i) for i in range(1, 151)]
        self.initial(self.response([1, 140, 151]))
        stream = self.adapter.recommendation_stream(SEED, history)
        self.assertEqual(stream.next_batch(), [self.uri(151)])
        body = self.opener.calls[-1][0].data
        self.assertEqual(body.count(b'\x12'), connect.MAX_HISTORY)
        self.assertIn(connect._proto_string(2, history[-1]), body)
        self.assertNotIn(connect._proto_string(2, history[0]), body)

    def test_newly_played_queue_history_filters_future_page(self):
        one = self.page_link(1)
        self.initial(self.response([1], one))
        self.page_response(one, self.response([1, 2, 3]))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        self.assertEqual(stream.next_batch(recent=[self.uri(2)]), [self.uri(3)])

    def test_empty_final_page_is_explicit_exhaustion_not_a_new_station(self):
        one = self.page_link(1)
        self.initial(self.response([1], one))
        self.page_response(one, self.response([1]))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        self.assertEqual(stream.next_batch(), [])
        self.assertTrue(stream.exhausted)
        self.assertEqual(sum(r.method == 'POST' for r, _ in self.opener.calls), 1)

    def test_inline_pages_and_large_page_remainder_are_not_dropped(self):
        self.opener.responses[self.origin + '/context-resolve/v1/autoplay'] = {'pages': [
            self.response(range(1, 252)), self.response([252, 253])]}
        stream = self.adapter.recommendation_stream(SEED)
        result = []
        while not stream.exhausted:
            result += stream.next_batch()
        self.assertEqual(result, [self.uri(i) for i in range(1, 254)])
        self.assertEqual(len(self.opener.calls), 2)  # AP resolution and a single context POST.

    def test_page_url_placeholder_resolves_without_losing_later_inline_pages(self):
        one = self.page_link(1)
        self.opener.responses[self.origin + '/context-resolve/v1/autoplay'] = {'pages': [
            {'page_url': one}, self.response([2])]}
        self.page_response(one, self.response([1], self.page_link(2)))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        self.assertEqual(stream.next_batch(), [self.uri(2)])
        self.assertTrue(stream.exhausted)

    def test_supported_link_forms_stay_on_pinned_origin(self):
        path = '/radio-apollo/v3/tracks/station?pageNum=1'
        forms = ('hm://radio-apollo/v3/tracks/station?pageNum=1',
                 'hm:/radio-apollo/v3/tracks/station?pageNum=1', path, path.lstrip('/'),
                 self.origin + path, 'https://native.spotify.com' + path)
        for link in forms:
            with self.subTest(link=link):
                self.assertEqual(connect._page_target(self.origin, link), self.origin + path)
        one = self.page_link(1)
        self.initial(self.response([1], one))
        self.page_response(one, self.response([2]))
        stream = self.adapter.recommendation_stream(SEED)
        stream.next_batch()
        self.adapter._origin = 'https://other.spotify.com:443'
        self.assertEqual(stream.next_batch(), [self.uri(2)])
        self.assertTrue(self.opener.calls[-1][0].full_url.startswith(self.origin + '/'))

    def test_apollo_page_can_continue_through_native_radio_router_alias(self):
        one = self.page_link(1)
        router = 'hm://radio-router/v3/tracks/spotify:station:track:' + 'a' * 22 + '?pageNum=2'
        self.initial(self.response(range(1, 51), one))
        self.page_response(one, self.response(range(51, 101), router))
        self.page_response(router, self.response(range(101, 151)))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual([len(stream.next_batch()) for _ in range(3)], [50, 50, 50])
        self.assertEqual(self.opener.calls[-1][0].full_url,
                         self.origin + '/radio-router/v3/tracks/spotify:station:track:' + 'a' * 22 + '?pageNum=2')
        self.assertEqual(sum(r.method == 'POST' for r, _ in self.opener.calls), 1)

    def test_radio_router_404_tries_apollo_once_with_same_cursor_and_origin(self):
        router = 'hm://radio-router/v3/tracks/station?pageNum=2&salt=123&prev_tracks=opaque'
        self.initial(self.response([1], router))
        router_url = connect._page_target(self.origin, router)
        apollo_url = router_url.replace('/radio-router/v3/', '/radio-apollo/v3/', 1)
        self.opener.responses[router_url] = urllib.error.HTTPError('private', 404, TOKEN, {}, io.BytesIO())
        self.opener.responses[apollo_url] = self.response([2])
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        self.assertEqual(stream.next_batch(), [self.uri(2)])
        self.assertEqual([r.full_url for r, _ in self.opener.calls[-2:]], [router_url, apollo_url])
        self.assertEqual(sum(r.method == 'POST' for r, _ in self.opener.calls), 1)
        self.assertTrue(stream.exhausted)

    def test_radio_router_auth_failure_never_tries_fallback(self):
        router = 'hm://radio-router/v3/tracks/station?pageNum=2'
        self.initial(self.response([1], router))
        router_url = connect._page_target(self.origin, router)
        self.opener.responses[router_url] = urllib.error.HTTPError('private', 403, TOKEN, {}, io.BytesIO())
        stream = self.adapter.recommendation_stream(SEED)
        stream.next_batch()
        with self.assertRaises(connect.SpotifyConnectError):
            stream.next_batch()
        self.assertEqual(self.opener.calls[-1][0].full_url, router_url)

    def test_radio_router_and_apollo_404_remains_bounded_error(self):
        router = 'hm://radio-router/v3/tracks/station?pageNum=2'
        self.initial(self.response([1], router))
        router_url = connect._page_target(self.origin, router)
        apollo_url = router_url.replace('/radio-router/v3/', '/radio-apollo/v3/', 1)
        for url in (router_url, apollo_url):
            self.opener.responses[url] = urllib.error.HTTPError('private', 404, TOKEN, {}, io.BytesIO())
        stream = self.adapter.recommendation_stream(SEED)
        stream.next_batch()
        with self.assertRaises(connect.SpotifyConnectError):
            stream.next_batch()
        self.assertEqual([r.full_url for r, _ in self.opener.calls[-2:]], [router_url, apollo_url])
        count = len(self.opener.calls)
        with self.assertRaises(connect.SpotifyConnectError):
            stream.next_batch()
        self.assertEqual(len(self.opener.calls), count)

    def test_bad_pagination_never_sends_bearer_to_response_supplied_host(self):
        for link in ('https://evil.example/radio-apollo/v3/tracks/a',
                     'https://other.spotify.com/radio-apollo/v3/tracks/a',
                     '//evil.example/radio-apollo/v3/tracks/a',
                     'http://native.spotify.com/radio-apollo/v3/tracks/a',
                     'https://secret@native.spotify.com/radio-apollo/v3/tracks/a',
                     'hm://evil.example/radio-apollo/v3/tracks/a',
                     'hm://radio-apollo/v3/../login',
                     '/radio-apollo/v3/%2e%2e/login',
                     '/radio-apollo/v3/a#secret', '/login5/v3/login', '/radio-apollo/v3/a\r\nsecret'):
            with self.subTest(link=link):
                self.initial(self.response([1], link))
                stream = self.adapter.recommendation_stream(SEED)
                with self.assertRaises(connect.SpotifyConnectError):
                    stream.next_batch()
        allowed = ('https://apresolve.spotify.com/', self.origin + '/')
        self.assertTrue(all(r.full_url.startswith(allowed) for r, _ in self.opener.calls))

    def test_cycle_fails_without_replaying_a_page(self):
        one = self.page_link(1)
        self.initial(self.response([1], one, page_url=one))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        requests = len(self.opener.calls)
        with self.assertRaisesRegex(connect.SpotifyConnectError, 'repeated'):
            stream.next_batch()
        self.assertEqual(len(self.opener.calls), requests)
        self.assertFalse(stream.exhausted)
        with self.assertRaises(connect.SpotifyConnectError):
            stream.next_batch()

    def test_unique_page_url_indirections_are_bounded_too(self):
        self.initial({'page_url': self.page_link(1)})
        for number in range(1, connect.MAX_EMPTY_PAGES + 2):
            self.page_response(self.page_link(number), {'page_url': self.page_link(number + 1)})
        stream = self.adapter.recommendation_stream(SEED)
        with self.assertRaisesRegex(connect.SpotifyConnectError, 'too many'):
            stream.next_batch()
        gets = [r for r, _ in self.opener.calls if '/radio-apollo/' in r.full_url]
        self.assertLessEqual(len(gets), connect.MAX_EMPTY_PAGES)

    def test_duplicate_only_page_chain_is_bounded(self):
        self.initial(self.response([1], self.page_link(1)))
        for number in range(1, connect.MAX_EMPTY_PAGES + 2):
            self.page_response(self.page_link(number), self.response([1], self.page_link(number + 1)))
        stream = self.adapter.recommendation_stream(SEED)
        self.assertEqual(stream.next_batch(), [self.uri(1)])
        with self.assertRaisesRegex(connect.SpotifyConnectError, 'too many'):
            stream.next_batch()
        gets = [r for r, _ in self.opener.calls if '/radio-apollo/' in r.full_url]
        self.assertEqual(len(gets), connect.MAX_EMPTY_PAGES)

    def test_page_http_failure_is_not_reported_as_exhaustion(self):
        one = self.page_link(1)
        self.initial(self.response([1], one))
        self.page_response(one, urllib.error.HTTPError('private', 403, TOKEN, {}, io.BytesIO(TOKEN.encode())))
        stream = self.adapter.recommendation_stream(SEED)
        stream.next_batch()
        with self.assertRaises(connect.SpotifyConnectError) as caught:
            stream.next_batch()
        self.assertFalse(stream.exhausted)
        self.assertNotIn(TOKEN, str(caught.exception))
        requests = len(self.opener.calls)
        with self.assertRaises(connect.SpotifyConnectError):
            stream.next_batch()
        self.assertEqual(len(self.opener.calls), requests)


if __name__ == '__main__':
    unittest.main()
