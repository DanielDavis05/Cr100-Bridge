import http.client
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
import xml.etree.ElementTree as ET

from content import ContentError
from native_radio import NativeRadioService, canonical_id, is_radio_uri, radio_uri
from radio_catalog import RadioCatalog
from radio_store import RadioStore
from radiotime import RadioTimeClient
from smapi_service import SMAPI_NS, SOAP_NS


class NativeRadioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'radio.json'
        self.events, self.calls = [], []
        def transport(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            self.calls.append((urlsplit(url).path, query))
            guide = query.get('id', ['s1'])[0]
            if 'Describe' in url:
                body = [dict(element='show' if guide.startswith('p') else 'station', guide_id=guide, name='Program One' if guide.startswith('p') else 'Station One',
                             type='link' if guide.startswith('p') else 'audio', item='show' if guide.startswith('p') else 'station')]
            else:
                body = [dict(type='audio', text='Station One', guide_id='s1', item='station',
                             URL='http://opml.radiotime.com/Tune.ashx?id=s1', image='https://images.test/cover.jpg')]
            return dict(head={'status': '200'}, body=body)
        self.catalog = RadioCatalog(RadioTimeClient(transport, min_request_interval=0), RadioStore(self.path),
            zip_resolver=lambda zipcode: dict(zip=zipcode, label='New York, NY', latlon='40.7484,-73.9967'))
        self.service = NativeRadioService('127.0.0.1', 0, self.catalog, log=lambda row: None, on_change=self.events.append)
        self.addCleanup(self.service.close)

    def test_native_discovery_has_no_generic_music_account_or_routing(self):
        descriptor = ET.fromstring(self.service.descriptor()['AvailableServiceDescriptorList'])[0]
        self.assertEqual(descriptor.get('Id'), '254')
        self.assertEqual(descriptor.get('Capabilities'), '0')
        self.assertEqual(descriptor.find('Policy').get('Auth'), 'Anonymous')
        self.assertEqual(self.service.media_servers(), '<MediaServers/>')
        self.assertFalse(self.service.owns(radio_uri('station:s1')))

    def test_root_omits_firmware_generated_entries_without_eager_directory_request(self):
        result = ET.fromstring(self.service.get_metadata({'id': 'root', 'index': '0', 'count': '499'}))
        titles = [node.findtext('title') for node in result.findall('mediaCollection')]
        self.assertEqual(titles, ['Music', 'Talk', 'Sports', 'Location', 'Podcasts', 'Radio Settings'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.catalog.store.snapshot()['revision'], 0)

    def test_native_favorite_add_duplicate_browse_delete_survives_restart(self):
        elements = '<DIDL-Lite><item><title>untrusted label</title><res>%s</res></item></DIDL-Lite>' % radio_uri('station:s1')
        added = self.service.native_action('ContentDirectory', 'CreateObject', {'ContainerID': 'R:0/0', 'Elements': elements})
        self.assertEqual(added['ObjectID'], 'R:0/0/station%3As1')
        self.assertEqual(RadioStore(self.path).favorites()[0]['id'], 'station:s1')
        with self.assertRaises(ContentError) as error:
            self.service.native_action('ContentDirectory', 'CreateObject', {'ContainerID': 'R:0/0', 'Elements': elements})
        self.assertEqual(error.exception.code, 803)
        listing = self.service.native_action('ContentDirectory', 'Browse', {'ObjectID': 'R:0/0'})
        self.assertEqual(listing['NumberReturned'], '1')
        self.assertIn('audioBroadcast', listing['Result'])
        self.assertIn('Station One', listing['Result'])
        self.service.native_action('ContentDirectory', 'DestroyObject', {'ObjectID': added['ObjectID']})
        self.assertEqual(RadioStore(self.path).favorites(), [])
        self.assertGreater(int(self.service.event_values()['RadioFavoritesUpdateID']), 0)

    def test_native_episode_http_suffix_preserves_canonical_identity(self):
        self.assertEqual(canonical_id('x-sonos-http:episode%3At88.mp3?sid=254&flags=0'), 'episode:t88')
        self.assertEqual(canonical_id('x-sonos-http:t88.mp3?sid=254'), 'episode:t88')

    def test_show_favorite_uses_original_native_show_resource(self):
        result = self.service.native_action('ContentDirectory', 'CreateObject', {'ContainerID': 'R:0/1',
            'Elements': '<DIDL-Lite><item><res>x-sonosapi-show:show:p17</res></item></DIDL-Lite>'})
        self.assertEqual(result['ObjectID'], 'R:0/1/show%3Ap17')
        self.assertIn('object.container.radioShow', result['Result'])

    def test_native_zip_wire_is_validated_resolved_and_durable(self):
        wire = r'F00080000z10001,New York\, NY'
        self.assertEqual(self.service.native_action('SystemProperties', 'SetString',
            {'VariableName': 'R_RadioLocation', 'StringValue': wire}), {})
        location = RadioStore(self.path).settings()['location']
        self.assertEqual(location['latlon'], '40.7484,-73.9967')
        self.assertEqual(location['zip'], '10001')
        self.assertEqual(self.service.native_action('SystemProperties', 'GetString',
            {'VariableName': 'R_RadioLocation'}), {'StringValue': wire})
        with self.assertRaises(ValueError): self.service._set_location_wire('bad,unescaped,location')
        self.assertEqual(RadioStore(self.path).settings()['location'], location)

    def test_browse_is_read_only_and_explicit_setting_operation_applies(self):
        choices = self.catalog.children('settings:quality')
        chosen = next(item for item in choices if item.metadata.get('value') == 'low')
        before = self.catalog.store.snapshot()
        self.service.get_metadata({'id': 'settings:quality'})
        self.assertEqual(self.catalog.store.snapshot(), before)
        choice = ET.fromstring(self.service.entry(chosen))
        self.assertEqual(choice.findtext('canAddToFavorites'), 'false')
        self.service.dispatch('createItem', {'favorite': chosen.id})
        self.assertEqual(self.catalog.store.settings()['quality'], 'low')
        self.assertEqual(self.catalog.store.favorites(), [])

    def test_explicit_native_settings_writer_is_validated_and_durable(self):
        chosen = next(item for item in self.catalog.children('settings:quality') if item.metadata.get('value') == 'low')
        encoded = 'F00080000' + quote(chosen.id, safe='')
        self.assertEqual(self.service.native_action('SystemProperties', 'SetString',
            {'VariableName': 'CR100_RadioAction', 'StringValue': encoded}), {})
        self.assertEqual(RadioStore(self.path).settings()['quality'], 'low')
        self.assertEqual(self.catalog.store.favorites(), [])
        with self.assertRaises(ValueError):
            self.service.native_action('SystemProperties', 'SetString',
                {'VariableName': 'CR100_RadioAction', 'StringValue': 'station:s1'})

    def test_stock_picker_root_retains_region_context(self):
        self.assertEqual(canonical_id('r0:browse'), 'r0')
        class Page:
            body = [dict(type='link', text='North America', guide_id='r100', URL='https://opml.radiotime.com/Browse.ashx?id=r100')]
        original = self.catalog.client.browse
        self.catalog.client.browse = lambda **kwargs: Page()
        try:
            root = ET.fromstring(self.service.get_metadata({'id': 'r0:browse'}))
            self.assertEqual(root.findtext('mediaCollection/id'), 'r100')
            self.assertEqual(root.findtext('mediaCollection/title'), 'North America')
        finally:
            self.catalog.client.browse = original

    def test_city_picker_only_fetches_opened_region_and_saves_region_selection(self):
        result = ET.fromstring(self.service.get_metadata({'id': 'r102'}))
        collection = result.find('mediaCollection')
        self.assertEqual(collection.findtext('id'), 'r102')
        self.assertEqual(collection.findtext('canEnumerate'), 'false')
        self.assertEqual(collection.findtext('canPlay'), 'false')
        self.service._set_location_wire('F00080004r102,Boston')
        self.assertEqual(self.catalog.store.settings()['location']['region_id'], 'r102')
        self.assertIn('bridge-local,Boston', self.service.location_wire())
        self.assertEqual([path for path, _ in self.calls], ['/Browse.ashx'])

    def test_native_encoded_show_resource_retains_canonical_provider_identity(self):
        result = self.service.native_action('ContentDirectory', 'CreateObject', {'ContainerID': 'R:0/1',
            'Elements': '<DIDL-Lite><item><res>x-sonosapi-show:F00080004show%3ap17:browse</res></item></DIDL-Lite>'})
        self.assertEqual(result['ObjectID'], 'R:0/1/show%3Ap17')

    def test_media_uri_stays_in_radio_playback_route_without_tuning_during_browse(self):
        value = self.service.dispatch('getMediaURI', {'id': 'station:s1'})
        self.assertIn('x-rincon-mp3radio://bridge-radio/station%3As1', value)
        self.assertFalse(any('Tune' in path for path, _ in self.calls))
        self.assertTrue(is_radio_uri('x-sonosapi-stream:station%3As1?sid=254&flags=32'))
        self.assertFalse(is_radio_uri('x-sonosapi-stream:s1?sid=240'))
        self.assertEqual(canonical_id('R:0/0/station%3As1'), 'station:s1')

    def test_real_namespaced_soap_round_trip_and_xml_escaping(self):
        self.service.start()
        document = ('<s:Envelope xmlns:s="%s"><s:Body><getMetadata xmlns="%s">'
                    '<id>root</id><index>0</index><count>1</count></getMetadata></s:Body></s:Envelope>') % (SOAP_NS, SMAPI_NS)
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('POST', '/smapi', document.encode(), {'Content-Type': 'text/xml'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = ET.fromstring(response.read()).find('.//{%s}getMetadataResult' % SMAPI_NS)
        self.assertEqual(result.findtext('{%s}count' % SMAPI_NS), '1')
        self.assertEqual(result.findtext('{%s}total' % SMAPI_NS), '6')


if __name__ == '__main__': unittest.main()
