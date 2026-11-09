"""Cross-provider HTTP validation and extension checks for the SMAPI transport."""
from email.message import Message
import http.client
import io
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smapi_service import MAX_REQUEST_BYTES, SMAPI_NS, SOAP_NS, SmapiFault, SmapiService, text
from smapi_stub import SmapiStub
from speaker_settings import SpeakerSettingsService
from spotify_client import SpotifyError
from spotify_service import SpotifyService


def envelope(call='<getLastUpdate xmlns="%s"/>' % SMAPI_NS):
    return ('<s:Envelope xmlns:s="%s"><s:Header><credentials xmlns="%s">'
            '<token>do-not-log-this</token></credentials></s:Header><s:Body>%s</s:Body></s:Envelope>'
            % (SOAP_NS, SMAPI_NS, call)).encode()


def post(service, payload, path='/smapi'):
    connection = http.client.HTTPConnection('127.0.0.1', service.port, timeout=3)
    try:
        connection.request('POST', path, payload, {'Content-Type': 'text/xml'})
        response = connection.getresponse()
        return response.status, ET.fromstring(response.read())
    finally:
        connection.close()


class Framing(unittest.TestCase):
    def test_invalid_framing_is_rejected_without_reading_untrusted_body(self):
        payload = envelope()
        cases = ([], [('Content-Length', '0')], [('Content-Length', '-1')],
                 [('Content-Length', '+1')], [('Content-Length', str(MAX_REQUEST_BYTES + 1))],
                 [('Content-Length', '1'), ('Content-Length', '1')],
                 [('Content-Length', str(len(payload))), ('Transfer-Encoding', 'chunked')])
        for fields in cases:
            with self.subTest(headers=fields):
                headers = Message()
                for key, value in fields: headers[key] = value
                stream = io.BytesIO(payload)
                request = SimpleNamespace(path='/smapi', headers=headers, rfile=stream, close_connection=False)
                with self.assertRaises(SmapiFault) as caught:
                    SmapiService.parse_request(None, request)
                self.assertEqual(caught.exception.code, 'Client.InvalidRequest')
                self.assertEqual(stream.tell(), 0)
                self.assertTrue(request.close_connection)

    def test_truncated_body_is_a_fault(self):
        headers = Message()
        headers['Content-Length'] = '100'
        request = SimpleNamespace(path='/smapi', headers=headers, rfile=io.BytesIO(b'<x/>'), close_connection=False)
        with self.assertRaises(SmapiFault): SmapiService.parse_request(None, request)
        self.assertTrue(request.close_connection)


class SharedTransport(unittest.TestCase):
    def setUp(self):
        self.logs = {}
        self.services = []
        constructors = (
            ('stub', lambda log: SmapiStub('127.0.0.1', 0, log=log)),
            ('spotify', lambda log: SpotifyService('127.0.0.1', 0, SimpleNamespace(), log=log)),
            ('settings', lambda log: SpeakerSettingsService('127.0.0.1', 0, lambda: (), log=log)),
        )
        for name, create in constructors:
            self.logs[name] = []
            service = create(self.logs[name].append)
            self.addCleanup(service.close)
            service.start()
            self.services.append((name, service))

    def test_native_namespaced_calls_and_provider_outputs_remain_available(self):
        for name, service in self.services:
            with self.subTest(service=name):
                status, document = post(service, envelope())
                self.assertEqual(status, 200)
                self.assertIsNotNone(document.find('.//{%s}getLastUpdateResult' % SMAPI_NS))
                self.assertIsNone(getattr(service.request, 'address', None))
        for name in ('spotify', 'settings'):
            self.assertNotIn('do-not-log-this', repr(self.logs[name]))

    def test_malformed_calls_never_reach_any_provider_dispatch(self):
        def call(arguments, namespace=SMAPI_NS):
            return '<getMetadata xmlns="%s">%s</getMetadata>' % (namespace, arguments)

        malformed = (
            b'<wrong/>',
            envelope('<getLastUpdate/>'),
            envelope(call('<id>root</id>', 'urn:wrong')),
            envelope(call('<id xmlns="">root</id>')),
            envelope(call('<id xmlns="urn:wrong">root</id>')),
            envelope(call('<id>root</id><id>other</id>')),
            envelope(call('<id><value>root</value></id>')),
            envelope('<getLastUpdate xmlns="%s"/><getLastUpdate xmlns="%s"/>' % (SMAPI_NS, SMAPI_NS)),
            envelope().replace(b'</s:Body>', b'</s:Body><s:Body/>'),
            b'<!DOCTYPE x [<!ENTITY secret "value">]>' + envelope(),
            ('<!DOCTYPE x [<!ENTITY secret "value">]>' + envelope().decode()).encode('utf-16'),
        )
        for name, service in self.services:
            for payload in malformed:
                with self.subTest(service=name, payload=payload[:100]):
                    with patch.object(service, 'dispatch') as dispatch:
                        status, document = post(service, payload)
                        dispatch.assert_not_called()
                    self.assertEqual(status, 500)
                    self.assertEqual(document.findtext('.//faultcode'), 's:Client.InvalidRequest')
                    self.assertIsNone(getattr(service.request, 'address', None))
            with self.subTest(service=name, endpoint=True), patch.object(service, 'dispatch') as dispatch:
                status, document = post(service, envelope(), '/unrelated')
                dispatch.assert_not_called()
                self.assertEqual((status, document.findtext('.//faultcode')), (500, 's:Client.InvalidRequest'))

    def test_context_and_safe_provider_error_mapping_share_the_same_pipeline(self):
        failures = {'stub': (LookupError('missing diagnostic item'), 's:Client.ItemNotFound'),
                    'spotify': (SpotifyError(401, 'private-token'), 's:Server.ServiceUnavailable'),
                    'settings': (SmapiFault('Client.ItemNotFound', 'Setting changed.'), 's:Client.ItemNotFound')}
        for name, service in self.services:
            error, code = failures[name]

            def fail(method, args):
                self.assertEqual(service.request.address, '127.0.0.1')
                raise error

            with self.subTest(service=name), patch.object(service, 'dispatch', side_effect=fail):
                status, document = post(service, envelope())
                self.assertEqual((status, document.findtext('.//faultcode')), (500, code))
                self.assertNotIn('private-token', ET.tostring(document).decode())
                self.assertIsNone(getattr(service.request, 'address', None))


class IndependentProvider(unittest.TestCase):
    def test_new_provider_uses_production_transport_without_diagnostic_catalogue(self):
        class Provider(SmapiService):
            def dispatch(self, method, args):
                if method == 'echo':
                    return '<value>%s</value>' % text(args.get('value', ''))
                return super().dispatch(method, args)

        service = Provider('127.0.0.1', 0, 245, 'Another Service', log=lambda record: None)
        self.addCleanup(service.close)
        service.start()
        status, document = post(service, envelope('<echo xmlns="%s"><value>A &amp; B</value></echo>' % SMAPI_NS))
        self.assertEqual(status, 200)
        self.assertEqual(document.findtext('.//{%s}value' % SMAPI_NS), 'A & B')
        self.assertIn('Name="Another Service"', service.descriptor()['AvailableServiceDescriptorList'])
        self.assertNotIn('bt:', service.media_servers())
        self.assertEqual(service.requests, [])
        self.assertFalse(hasattr(service, 'nodes'))


if __name__ == '__main__':
    unittest.main()
