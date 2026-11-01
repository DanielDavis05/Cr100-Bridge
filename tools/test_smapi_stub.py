#!/usr/bin/env python3
"""Offline tests for the stub music service (smapi_stub.py) and how the virtual players
advertise it and catch play attempts for its items."""
import base64
import hashlib
import http.client
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zoneplayers
from smapi_stub import SMAPI_NS, SmapiStub
from test_zoneplayers import soap, zp

NS = {'s': SMAPI_NS}
AVT = '/MediaRenderer/AVTransport/Control'
HOUSEHOLD = 'Sonos_SMAPI_TEST'
OTHER_HOUSEHOLD = 'Sonos_SMAPI_OTHER'
IV = bytes(range(16))
# Independent household-password values for these synthetic integration fixtures.
PASSWORDS = {HOUSEHOLD: bytes.fromhex('63ff353a869a05d5aff65cda2e9a8673'),
             OTHER_HOUSEHOLD: bytes.fromhex('91329390058f16fd8b81d5a448ca322e')}


def decode_accounts(wire, household):
    """Decode the emitted event independently of the production encoder."""
    version, payload = wire.split(':', 1)
    if version != '2': raise ValueError('Expected account-event version 2')
    blob = base64.b64decode(payload, validate=True)
    iv, ciphertext = blob[:16], blob[16:]
    key = hashlib.md5(iv + PASSWORDS[household]).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    data, checksum = plaintext[:-4], plaintext[-4:]
    if checksum != hashlib.md5(data).digest()[:4]: raise ValueError('Invalid account-event checksum')
    return data.decode('utf-8')


def smapi(stub, method, args):
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Header><credentials xmlns="%s"><deviceId>00-0E-58-02-6F-A9:4</deviceId>'
            '<deviceProvider>Sonos</deviceProvider></credentials></s:Header><s:Body><%s xmlns="%s">%s</%s>'
            '</s:Body></s:Envelope>' % (SMAPI_NS, method, SMAPI_NS,
                                         ''.join('<%s>%s</%s>' % (k, v, k) for k, v in args.items()), method))
    c = http.client.HTTPConnection('127.0.0.1', stub.port, timeout=5)
    c.request('POST', '/smapi', body.encode(), {'Content-Type': 'text/xml', 'SOAPACTION': '"%s#%s"' % (SMAPI_NS, method)})
    r = c.getresponse(); data = r.read(); c.close()
    return r.status, ET.fromstring(data)


class Stub(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.stub = SmapiStub('127.0.0.1', 0, log=self.logs.append)
        self.stub.start()

    def tearDown(self):
        self.stub.close()

    def test_browse_page_search_and_log(self):
        status, doc = smapi(self.stub, 'getMetadata', {'id': 'root', 'index': 0, 'count': 100})
        self.assertEqual(status, 200)
        titles = [e.text for e in doc.iterfind('.//s:title', NS)]
        self.assertEqual(titles, ['Playlists', 'Albums', 'Artists', 'Many Tracks (250)', 'A Single Stub Track'])
        self.assertEqual(doc.find('.//s:total', NS).text, '5')
        self.assertEqual(doc.find('.//s:mediaMetadata/s:mimeType', NS).text, 'audio/mpeg')
        _, doc = smapi(self.stub, 'getMetadata', {'id': 'bt:many', 'index': 240, 'count': 100})
        self.assertEqual([doc.find('.//s:%s' % k, NS).text for k in ('index', 'count', 'total')], ['240', '10', '250'])
        _, doc = smapi(self.stub, 'search', {'id': 'albums', 'term': 'stub', 'index': 0, 'count': 10})
        self.assertEqual([e.text for e in doc.iterfind('.//s:title', NS)], ['Stub Album'])
        _, doc = smapi(self.stub, 'search', {'id': 'tracks', 'term': 'zzz', 'index': 0, 'count': 10})
        self.assertEqual([e.text for e in doc.iterfind('.//s:title', NS)], ['Search: zzz'])
        status, doc = smapi(self.stub, 'getMetadata', {'id': 'bt:nothing', 'index': 0, 'count': 10})
        self.assertEqual((status, doc.find('.//faultcode').text), (500, 's:Client.ItemNotFound'))
        status, _ = smapi(self.stub, 'rateItem', {'id': 'bt:track:1', 'rating': 1})
        self.assertEqual(status, 500)
        logged = [l for l in self.logs if l.get('event') == 'smapi']
        self.assertEqual(logged[0]['method'], 'getMetadata')
        self.assertIn('00-0E-58-02-6F-A9', logged[0]['credentials'])

    def test_descriptor(self):
        values = self.stub.descriptor()
        service = ET.fromstring(values['AvailableServiceDescriptorList']).find('Service')
        self.assertEqual((service.get('Id'), service.get('ContainerType'), service.find('Policy').get('Auth')),
                         ('240', 'MService', 'Anonymous'))
        self.assertEqual(service.get('Uri'), 'http://127.0.0.1:%d/smapi' % self.stub.port)
        self.assertEqual(values['AvailableServiceTypeList'], str(240 * 256 + 7))


class Advertised(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.stub = SmapiStub('127.0.0.1', 0, log=self.logs.append)
        with patch('music_service_events.secrets.token_bytes', return_value=IV):
            self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], self.logs.append, port=0,
                                               music_service=self.stub)
        self.house.start('127.0.0.1', ssdp=False)
        self.den = self.house.players[0]

    def tearDown(self):
        self.house.close()
        self.stub.server.server_close()

    def test_players_list_the_service_and_refuse_its_items(self):
        status, body = soap(self.den, '/MusicServices/Control', 'MusicServices', 'ListAvailableServices')
        self.assertEqual(status, 200)
        self.assertIn('Bridge Test', ET.fromstring(body).find('.//AvailableServiceDescriptorList').text)
        # Only the X path constructs the account objects used by the CR100 Music menu.
        topology = ET.fromstring(self.den.state.propertyset('ZoneGroupTopology'))
        self.assertIsNone(topology.find('.//ThirdPartyMediaServers'))
        decoded = decode_accounts(topology.find('.//ThirdPartyMediaServersX').text, HOUSEHOLD)
        self.assertEqual(decoded, self.stub.media_servers())
        servers = ET.fromstring(decoded)
        account = servers.find('Service')
        self.assertEqual((account.get('UDN'), account.get('NumAccounts'), account.get('Username0')),
                         ('SA_RINCON%d_bridge' % (240 * 256 + 7), '1', 'bridge'))
        uri = 'x-sonos-http:bt%3atrack%3a1.mp3?sid=240&amp;flags=32'
        status, _ = soap(self.den, AVT, 'AVTransport', 'AddURIToQueue',
                         f'<InstanceID>0</InstanceID><EnqueuedURI>{uri}</EnqueuedURI><EnqueuedURIMetaData></EnqueuedURIMetaData>'
                         '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>')
        self.assertEqual(status, 500)
        played = [l for l in self.logs if l.get('event') == 'service-play']
        self.assertEqual(played[0]['args']['EnqueuedURI'], 'x-sonos-http:bt%3atrack%3a1.mp3?sid=240&flags=32')
        # Anything else still works as before.
        status, _ = soap(self.den, AVT, 'AVTransport', 'SetAVTransportURI',
                         '<InstanceID>0</InstanceID><CurrentURI>x-rincon-queue:%s#0</CurrentURI>'
                         '<CurrentURIMetaData></CurrentURIMetaData>' % self.den.config.udn)
        self.assertEqual(status, 200)

    def test_account_event_is_stable_and_bound_to_its_household(self):
        def event(player):
            topology = ET.fromstring(player.state.propertyset('ZoneGroupTopology'))
            self.assertIsNone(topology.find('.//ThirdPartyMediaServers'))
            return topology.find('.//ThirdPartyMediaServersX').text

        first = event(self.den)
        self.assertEqual(event(self.den), first)
        # Fix the IV in both households so a changed ciphertext proves the household
        # affects the key, rather than merely exercising random-IV generation.
        with patch('music_service_events.secrets.token_bytes', return_value=IV):
            other = zoneplayers.Household(zp, OTHER_HOUSEHOLD, ['Other'], ['127.0.0.1'],
                                          self.logs.append, port=0, music_service=self.stub)
        other.start('127.0.0.1', ssdp=False)
        try:
            second = event(other.players[0])
            self.assertEqual(base64.b64decode(first[2:])[:16], IV)
            self.assertEqual(base64.b64decode(second[2:])[:16], IV)
            self.assertNotEqual(second, first)
            self.assertEqual(decode_accounts(second, OTHER_HOUSEHOLD), self.stub.media_servers())
            with self.assertRaises(ValueError): decode_accounts(second, HOUSEHOLD)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
