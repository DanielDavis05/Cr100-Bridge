"""Real HTTP/SOAP and real backend adapters against isolated fake Sonos renderers."""
import html
import http.client
import json
from pathlib import Path
import tempfile
import unittest
import re
from urllib.parse import urlsplit

from radio_playback import radio_uri
from radio_runtime import configure_sources
from radiotime import RadioTimeClient
import test_sonos_backend as sonos_fixture
from test_sonos_backend import calls, browse
from test_ha_backend import AVT, field, wait_for
from test_zoneplayers import soap


class NativeRadioIntegration(unittest.TestCase):
    def setUp(self):
        sonos_fixture.SonosHousehold.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'radio': {'port': 0}, 'alarms': {'timezone': 'America/New_York'}}))
        def transport(url, **kwargs):
            parsed = urlsplit(url)
            if parsed.path.endswith('Describe.ashx'):
                body = [{'element': 'station', 'guide_id': 's1', 'name': 'Test Jazz',
                         'current_song': 'Blue Train', 'current_artist': 'John Coltrane',
                         'current_album': 'Blue Train', 'logo': '',
                         'current_album_art': self.kitchen.base + '/getaa?album=blue-train'}]
            elif parsed.path.endswith('Tune.ashx'):
                body = [{'element': 'audio', 'url': 'https://broadcast.test/live.mp3', 'media_type': 'mp3'}]
            else: body = []
            return {'head': {'status': '200'}, 'body': body}
        configure_sources(self.house, config, '127.0.0.1', client=RadioTimeClient(transport, min_request_interval=0), native_port=0)
        self.house.radio_store.update_settings({'cover_lookup': False})
        self.house.radio_service.start()
        self.house.radio_playback.start()
        self.house.alarms.start()
    def tearDown(self):
        sonos_fixture.SonosHousehold.tearDown(self)
        self.temp.cleanup()

    def test_radio_direct_playback_preserves_queue_and_native_metadata(self):
        self.kitchen.queue = [('https://saved.test/one.mp3', ''), ('https://saved.test/two.mp3', '')]
        before = list(self.kitchen.queue)
        uri = radio_uri('station:s1')
        status, body = soap(self.vkitchen, AVT, 'AVTransport', 'SetAVTransportURI',
                            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>' % html.escape(uri))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.kitchen.queue, before)
        self.assertEqual(calls(self.kitchen, 'RemoveAllTracksFromQueue'), [])
        self.assertEqual(self.kitchen.uri, 'https://broadcast.test/live.mp3')
        self.assertTrue(wait_for(lambda: self.house.radio_playback.session(self.vkitchen.config.udn).artist == 'John Coltrane'))
        status, body = soap(self.vkitchen, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200, body)
        metadata = field(body, 'TrackMetaData')
        self.assertIn('John Coltrane', metadata)
        self.assertIn('albumArtURI', metadata)
        art = re.search(r'<upnp:albumArtURI>([^<]+)<', metadata).group(1)
        parsed = urlsplit(art)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=8)
        connection.request('GET', parsed.path)
        response = connection.getresponse(); image = response.read(); connection.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(image.startswith(b'\xff\xd8'))
        status, body = soap(self.vkitchen, AVT, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertIn('audioBroadcast', field(body, 'CurrentURIMetaData'))
        self.assertIn('Test Jazz', field(body, 'CurrentURIMetaData'))

    def test_native_favorites_and_alarm_mutations_are_persistent(self):
        uri = radio_uri('station:s1')
        elements = '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item><res>%s</res></item></DIDL-Lite>' % uri
        status, body = soap(self.vkitchen, '/MediaServer/ContentDirectory/Control', 'ContentDirectory', 'CreateObject',
                            '<ContainerID>R:0/0</ContainerID><Elements>%s</Elements>' % html.escape(elements))
        self.assertEqual(status, 200, body)
        status, duplicate = soap(self.vkitchen, '/MediaServer/ContentDirectory/Control', 'ContentDirectory', 'CreateObject',
                                 '<ContainerID>R:0/0</ContainerID><Elements>%s</Elements>' % html.escape(elements))
        self.assertEqual(status, 500)
        self.assertIn(b'803', duplicate)
        status, body = browse(self.vkitchen, 'R:0/0')
        self.assertEqual(status, 200, body)
        self.assertIn('Test Jazz', field(body, 'Result'))
        args = dict(StartLocalTime='07:00:00', Duration='01:00:00', Recurrence='DAILY', Enabled='1',
                    RoomUUID=self.vkitchen.config.udn, ProgramURI=uri, ProgramMetaData='',
                    PlayMode='NORMAL', Volume='25', IncludeLinkedZones='0')
        xml = ''.join('<%s>%s</%s>' % (key, html.escape(str(value)), key) for key, value in args.items())
        status, body = soap(self.vkitchen, '/AlarmClock/Control', 'AlarmClock', 'CreateAlarm', xml)
        self.assertEqual(status, 200, body)
        alarm_id = field(body, 'AssignedID')
        self.assertTrue(alarm_id)
        status, body = soap(self.vkitchen, '/AlarmClock/Control', 'AlarmClock', 'ListAlarms')
        self.assertEqual(status, 200, body)
        self.assertIn('Test Jazz', field(body, 'CurrentAlarmList'))
        self.assertEqual(self.house.alarms.list()[0]['media_ref']['content_id'], 'station:s1')

    def test_settings_refresh_changes_native_event_without_editing_favorites(self):
        state = self.vkitchen.state
        before = state.propertyset('ContentDirectory')
        favorites = self.house.radio_store.favorites()
        self.house.radio_store.update_settings({'quality': 'low'})
        self.assertNotEqual(state.propertyset('ContentDirectory'), before)
        before = state.propertyset('ContentDirectory')
        self.house.radio_store.refresh()
        self.assertNotEqual(state.propertyset('ContentDirectory'), before)
        self.assertEqual(self.house.radio_store.favorites(), favorites)


if __name__ == '__main__': unittest.main()
