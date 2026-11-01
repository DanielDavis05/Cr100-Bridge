"""Read-only native queue metadata, matching and origin boundary contracts."""
import html
import io
import unittest
import xml.etree.ElementTree as ET

from sonos_queue_art import (DIDL_NS, MAX_PAGE, MAX_RESPONSE, NETWORK_TIMEOUT,
                             SERVICE, SOAP_NS, SonosQueueArt, _art_url, _device_base)

ENTITY = 'media_player.kitchen'
OTHER = 'media_player.den'
BASE = 'http://192.168.1.164:1400'
URIS = ['x-sonos-spotify:spotify%%3atrack%%3a%d?sid=12&flags=0' % n for n in range(3)]


def response(items):
    didl = ('<DIDL-Lite xmlns="%s" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">%s</DIDL-Lite>'
            % (DIDL_NS, ''.join('<item id="Q:0/%d"><res>%s</res><upnp:albumArtURI>%s</upnp:albumArtURI></item>'
                               % (position, html.escape(uri), html.escape(art))
                               for position, uri, art in items)))
    return ('<s:Envelope xmlns:s="%s"><s:Body><u:BrowseResponse xmlns:u="%s">'
            '<Result>%s</Result></u:BrowseResponse></s:Body></s:Envelope>'
            % (SOAP_NS, SERVICE, html.escape(didl))).encode()


class Answer(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Opener:
    def __init__(self, data): self.data, self.requests = data, []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.data, Exception): raise self.data
        return Answer(self.data)


class HA:
    def __init__(self):
        self.calls, self.states = [], {}
        self.entities = [{'entity_id': ENTITY, 'platform': 'sonos', 'device_id': 'one'},
                         {'entity_id': OTHER, 'platform': 'sonos', 'device_id': 'two'}]
        self.devices = [{'id': 'one', 'configuration_url': BASE + '/support/review'},
                        {'id': 'two', 'configuration_url': 'http://192.168.1.20:1400/support/review'}]
    def call(self, payload, timeout):
        self.calls.append((payload, timeout))
        return self.entities if payload['type'] == 'config/entity_registry/list' else self.devices
    def state(self, entity): return self.states.get(entity, {})


class QueueArt(unittest.TestCase):
    def setUp(self):
        self.ha, self.logs = HA(), []
        self.opener = Opener(response([(n + 1, uri, '/getaa?u=%d&v=1' % n) for n, uri in enumerate(URIS)]))
        self.helper = SonosQueueArt(self.ha, self.logs.append, self.opener)

    def test_page_is_one_browse_and_later_hover_reuses_it(self):
        art = self.helper.lookup(ENTITY, URIS)
        self.assertEqual(art, {n: BASE + '/getaa?u=%d&v=1' % n for n in range(3)})
        self.assertEqual(self.helper.lookup(ENTITY, [URIS[1]], start=1), {0: art[1]})
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(len(self.ha.calls), 2)
        request, timeout = self.opener.requests[0]
        self.assertEqual(timeout, NETWORK_TIMEOUT)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, BASE + '/MediaServer/ContentDirectory/Control')
        self.assertNotIn('Authorization', request.headers)
        body = ET.fromstring(request.data)
        args = body.find('{%s}Body' % SOAP_NS)[0]
        self.assertEqual({a.tag: a.text or '' for a in args},
                         {'ObjectID': 'Q:0', 'BrowseFlag': 'BrowseDirectChildren', 'Filter': '*',
                          'StartingIndex': '0', 'RequestedCount': '3', 'SortCriteria': ''})

    def test_nonzero_page_and_exact_resource_and_index_matching(self):
        self.opener.data = response([(101, URIS[0], '/getaa?u=0'), (103, URIS[1], '/getaa?u=1'),
                                     (103, URIS[0], '/getaa?u=2')])
        self.assertEqual(self.helper.lookup(ENTITY, URIS, start=100), {0: BASE + '/getaa?u=0'})
        body = ET.fromstring(self.opener.requests[0][0].data)
        self.assertEqual(body.find('.//StartingIndex').text, '100')

    def test_queue_edits_do_not_reuse_art_for_a_different_uri(self):
        self.helper.lookup(ENTITY, URIS)
        self.assertEqual(self.helper.lookup(ENTITY, ['different-resource'], start=1), {})
        self.assertEqual(len(self.opener.requests), 2)

    def test_group_uses_current_coordinator_registry_address(self):
        self.ha.states[ENTITY] = {'attributes': {'group_members': [OTHER, ENTITY]}}
        self.helper.lookup(ENTITY, URIS)
        self.assertTrue(self.opener.requests[0][0].full_url.startswith('http://192.168.1.20:1400/'))
        self.ha.states[ENTITY] = {'attributes': {'group_members': [ENTITY]}}
        self.helper.lookup(ENTITY, URIS)
        self.assertTrue(self.opener.requests[1][0].full_url.startswith(BASE + '/'))

    def test_failure_is_cached_and_does_not_break_queue_browsing(self):
        self.opener.data = TimeoutError('private-response-must-not-be-logged')
        self.assertEqual(self.helper.lookup(ENTITY, URIS), {})
        self.assertEqual(self.helper.lookup(ENTITY, URIS), {})
        self.assertEqual(len(self.opener.requests), 1)
        self.assertNotIn('private-response', repr(self.logs))

    def test_bad_registry_and_non_sonos_entities_are_not_contacted(self):
        self.ha.entities[0]['platform'] = 'other'
        self.assertEqual(self.helper.lookup(ENTITY, URIS), {})
        self.assertEqual(self.opener.requests, [])
        self.helper.registry_until = 0
        self.ha.entities[0]['platform'] = 'sonos'
        self.ha.devices[0]['configuration_url'] = 'http://192.168.1.164:1400.evil.test/getaa'
        self.assertEqual(self.helper.lookup(ENTITY, URIS), {})
        self.assertEqual(self.opener.requests, [])

    def test_art_never_leaves_sonos_getaa_origin(self):
        for value in ('/getaa?u=x&v=1', BASE + '/getaa?u=x'):
            self.assertTrue(_art_url(BASE, value).startswith(BASE + '/getaa'))
        for value in ('https://i.scdn.co/image/x', 'http://192.168.1.164:1400.evil/getaa',
                      '//192.168.1.20:1400/getaa', 'http://user:pass@192.168.1.164:1400/getaa',
                      '/getaa#fragment', '/support/review', '/getaa\n?u=x', '/getaa?u=x y'):
            with self.subTest(value=value): self.assertEqual(_art_url(BASE, value), '')

    def test_registry_only_accepts_literal_http_sonos_addresses(self):
        self.assertEqual(_device_base(BASE + '/support/review'), BASE)
        for value in ('https://192.168.1.164:1400/', 'http://example.test:1400/',
                      'http://127.0.0.1:1400/', 'http://169.254.1.1:1400/', 'http://0.0.0.0:1400/',
                      'http://239.1.1.1:1400/', 'http://192.168.1.164:8123/',
                      BASE + '/?token=private', 'http://user@192.168.1.164:1400/'):
            with self.subTest(value=value): self.assertIsNone(_device_base(value))

    def test_bad_xml_oversized_and_untrusted_native_art_are_ignored(self):
        for data in (b'x' * (MAX_RESPONSE + 1), b'<!DOCTYPE x><x/>', b'<bad>', b'<wrong/>',
                     response([(1, URIS[0], 'http://other.test/getaa')])):
            with self.subTest(data=data[:50]):
                helper = SonosQueueArt(self.ha, self.logs.append, Opener(data))
                self.assertEqual(helper.lookup(ENTITY, [URIS[0]]), {})

    def test_empty_or_excessive_range_has_no_network_work(self):
        self.assertEqual(self.helper.lookup(ENTITY, []), {})
        self.assertEqual(self.helper.lookup(ENTITY, URIS, -1), {})
        self.assertEqual(self.helper.lookup(ENTITY, ['x'] * (MAX_PAGE + 1)), {})
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.opener.requests, [])


if __name__ == '__main__':
    unittest.main()
