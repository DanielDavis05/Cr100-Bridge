#!/usr/bin/env python3
"""Offline tests for the Home Assistant backend: virtual players on loopback, real
HTTP/SOAP/GENA on the CR100 side, a fake Home Assistant (fake_ha.py) on the other.
Needs websockets and Pillow (present on the Pi)."""
import datetime
import http.client
import http.server
import io
import queue
import re
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import fake_ha
import ha_backend
import zoneplayers
from ha_client import HomeAssistant
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

AVT = '/MediaRenderer/AVTransport/Control'
RC = '/MediaRenderer/RenderingControl/Control'
GRC = '/MediaRenderer/GroupRenderingControl/Control'


def playing_kitchen():
    return fake_ha.sonos_state(
        'media_player.kitchen', 'Kitchen Sonos', 'playing', media_title='So What', media_artist='Miles Davis',
        media_album_name='Kind of Blue', media_duration=562, media_position=60,
        media_position_updated_at=(datetime.datetime.now(datetime.timezone.utc)
                                   - datetime.timedelta(seconds=5)).isoformat(),
        entity_picture='/api/media_player_proxy/media_player.kitchen?token=abc&cache=1',
        queue_position=3, queue_size=9)


def field(body, name):
    element = ET.fromstring(body).find('.//' + name)
    return element.text if element is not None else None


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(0.02)
    return False


class Events:
    """GENA callback server collecting NOTIFY bodies."""

    def __init__(self):
        self.queue = queue.Queue()
        events = self.queue

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                events.put(self.rfile.read(int(self.headers['Content-Length'])).decode())
                self.send_response(200); self.end_headers()
            def log_message(self, *a): pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def subscribe(self, player, path):
        c = http.client.HTTPConnection('127.0.0.1', player.config.port, timeout=5)
        c.request('SUBSCRIBE', path, headers={'CALLBACK': f'<http://127.0.0.1:{self.server.server_port}/>',
                                              'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
        status = c.getresponse().status; c.close()
        return status

    def get(self, timeout=5):
        return self.queue.get(timeout=timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if threading.current_thread() is not self.thread: self.thread.join(3)


class HABackend(unittest.TestCase):
    def setUp(self):
        self.fake = fake_ha.FakeHA([playing_kitchen(), fake_ha.sonos_state('media_player.patio', 'Patio')])
        self.logs = []
        self.ha = HomeAssistant(self.fake.url, fake_ha.TOKEN, self.logs.append)
        self.ha.start()
        self.assertTrue(self.ha.connected.wait(5), self.logs)
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio', 'Spare'], ['127.0.0.1'] * 3,
                                           self.logs.append, port=0, backends=[ha_backend.HABackend(self.ha, self.logs.append)],
                                           refs=[('ha', 'media_player.kitchen'), ('ha', 'media_player.patio'), None])
        self.house.start('127.0.0.1', ssdp=False)
        self.kitchen, self.patio, self.spare = self.house.players
        self.events = Events()

    def tearDown(self):
        self.events.close()
        self.house.close()
        self.house.backends['ha'].close()
        self.fake.close()

    def services(self):
        return [(service, entity) for service, entity, _ in self.fake.calls]

    def test_disconnected_home_assistant_exposes_bridge_until_connection_recovers(self):
        house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio'], ['127.0.0.1'] * 2,
            lambda _: None, port=0, backends=self.house.backends.values(),
            refs=[('ha', 'media_player.kitchen'), ('ha', 'media_player.patio')])
        try:
            self.assertNotIn(['CR100 Bridge'], groups(house.topology()))
            self.ha.connected.clear()
            house.refresh_identities()
            self.assertIn(['CR100 Bridge'], groups(house.topology()))
            self.ha.connected.set()
            house.refresh_identities()
            self.assertNotIn(['CR100 Bridge'], groups(house.topology()))
        finally:
            self.ha.connected.set()
            house.close()

    def test_now_playing_from_home_assistant(self):
        status, body = soap(self.kitchen, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual((field(body, 'Track'), field(body, 'TrackDuration')), ('3', '0:09:22'))
        rel = field(body, 'RelTime')
        self.assertTrue('0:01:04' <= rel <= '0:01:07', rel)   # 60 s + the 5 s since the update
        metadata = field(body, 'TrackMetaData')
        for text in ('So What', 'Miles Davis', 'Kind of Blue', '<upnp:albumArtURI>/art/'):
            self.assertIn(text, metadata)
        ET.fromstring(metadata)   # well-formed DIDL
        _, body = soap(self.kitchen, AVT, 'AVTransport', 'GetTransportInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'CurrentTransportState'), 'PLAYING')
        _, body = soap(self.kitchen, AVT, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'NrTracks'), '9')
        # An idle speaker shows nothing playing.
        _, body = soap(self.patio, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        self.assertEqual((field(body, 'TrackMetaData'), field(body, 'Track')), (None, '0'))

    def test_album_art_is_a_small_baseline_jpeg_served_by_the_player(self):
        from PIL import Image
        _, body = soap(self.kitchen, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
        path = re.search(r'<upnp:albumArtURI>([^<]+)<', field(body, 'TrackMetaData')).group(1)
        c = http.client.HTTPConnection('127.0.0.1', self.kitchen.config.port, timeout=5)
        c.request('GET', path); r = c.getresponse(); data = r.read(); c.close()
        self.assertEqual((r.status, r.getheader('Content-Type')), (200, 'image/jpeg'))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(max(image.size), 200)
            self.assertFalse(image.info.get('progressive') or image.info.get('progression'))
        # Cached: a second fetch does not go back to Home Assistant.
        requests = self.fake.picture_requests
        c = http.client.HTTPConnection('127.0.0.1', self.kitchen.config.port, timeout=5)
        c.request('GET', path); self.assertEqual(c.getresponse().status, 200); c.close()
        self.assertEqual(self.fake.picture_requests, requests)
        c = http.client.HTTPConnection('127.0.0.1', self.kitchen.config.port, timeout=5)
        c.request('GET', '/art/0000000000000000.jpg'); self.assertEqual(c.getresponse().status, 404); c.close()

    def test_external_track_changes_refresh_identity_and_stable_proxy_art(self):
        from PIL import Image
        # Connect can retain its session URI and HA image proxy while the song changes.
        session_uri = 'x-sonos-vli:connect-session'
        self.fake.set('media_player.kitchen', media_content_id=session_uri)
        self.assertTrue(wait_for(lambda: self.ha.state('media_player.kitchen')['attributes'].get('media_content_id')
                                == session_uri))

        def position():
            status, body = soap(self.kitchen, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
            self.assertEqual(status, 200)
            metadata = field(body, 'TrackMetaData')
            art = ET.fromstring(metadata).findtext('.//{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI')
            return field(body, 'TrackURI'), metadata, art

        def picture(path):
            c = http.client.HTTPConnection('127.0.0.1', self.kitchen.config.port, timeout=5)
            c.request('GET', path); response = c.getresponse(); data = response.read(); c.close()
            self.assertEqual(response.status, 200)
            with Image.open(io.BytesIO(data)) as image: return image.convert('RGB').getpixel((0, 0))

        first_uri, _, first_art = position()
        first_pixel = picture(first_art)
        self.assertGreater(first_pixel[0], first_pixel[2] + 100)
        self.assertEqual(self.events.subscribe(self.kitchen, '/MediaRenderer/AVTransport/Event'), 200)
        self.events.get()

        image = io.BytesIO()
        Image.new('RGB', (640, 640), (10, 20, 220)).save(image, 'JPEG')
        self.fake.picture = image.getvalue()
        self.fake.set('media_player.kitchen', media_title='Freddie Freeloader', queue_position=4)
        update = ET.fromstring(field(self.events.get(), 'LastChange'))
        values = {node.tag.rsplit('}', 1)[-1]: node.get('val') for node in update.iter() if 'val' in node.attrib}
        second_uri, second_metadata, second_art = position()
        self.assertNotEqual(second_uri, first_uri)
        self.assertNotEqual(second_art, first_art)
        self.assertIn('Freddie Freeloader', second_metadata)
        self.assertEqual(values['CurrentTrackURI'], second_uri)
        self.assertEqual(values['CurrentTrackMetaData'], second_metadata)
        second_pixel = picture(second_art)
        self.assertGreater(second_pixel[2], second_pixel[0] + 100)

        # Position reports must not produce new track IDs, artwork fetches, or NOTIFYs.
        requests = self.fake.picture_requests
        self.fake.set('media_player.kitchen', media_position=200)
        with self.assertRaises(queue.Empty): self.events.get(timeout=1)
        uri, _, art = position()
        self.assertEqual((uri, art), (second_uri, second_art))
        self.assertEqual(self.fake.picture_requests, requests)

        # Distinct resource IDs must also differ when their displayed metadata matches.
        self.fake.set('media_player.kitchen', media_content_id='spotify:track:another-resource')
        self.assertTrue(wait_for(lambda: self.ha.state('media_player.kitchen')['attributes'].get('media_content_id')
                                == 'spotify:track:another-resource'))
        third_uri, _, third_art = position()
        self.assertNotEqual(third_uri, second_uri)
        self.assertNotEqual(third_art, second_art)

    def test_transport_actions_become_service_calls(self):
        for action, args in (('Pause', '<InstanceID>0</InstanceID>'),
                             ('Play', '<InstanceID>0</InstanceID><Speed>1</Speed>'),
                             ('Next', '<InstanceID>0</InstanceID>'),
                             ('Previous', '<InstanceID>0</InstanceID>'),
                             ('Seek', '<InstanceID>0</InstanceID><Unit>REL_TIME</Unit><Target>0:01:05</Target>'),
                             ('SetPlayMode', '<InstanceID>0</InstanceID><NewPlayMode>SHUFFLE</NewPlayMode>')):
            status, _ = soap(self.kitchen, AVT, 'AVTransport', action, args)
            self.assertEqual(status, 200, action)
        self.assertEqual(self.services(), [(s, 'media_player.kitchen') for s in (
            'media_pause', 'media_play', 'media_next_track', 'media_previous_track', 'media_seek',
            'shuffle_set', 'repeat_set')])
        data = {service: data for service, _, data in self.fake.calls}
        self.assertEqual(data['media_seek'], {'seek_position': 65})
        self.assertEqual((data['shuffle_set'], data['repeat_set']), ({'shuffle': True}, {'repeat': 'all'}))
        self.assertTrue(wait_for(lambda: self.kitchen.state.backend.play_mode == 'SHUFFLE'))

    def test_failed_call_is_a_upnp_error(self):
        self.fake.fail_services = True
        status, body = soap(self.kitchen, AVT, 'AVTransport', 'Pause', '<InstanceID>0</InstanceID>')
        self.assertEqual((status, field(body, '{urn:schemas-upnp-org:control-1-0}errorCode')), (500, '501'))

    def test_volume_answers_at_once_and_sends_the_latest(self):
        for volume in range(10, 31):
            status, body = soap(self.kitchen, RC, 'RenderingControl', 'SetVolume',
                                f'<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>')
            self.assertEqual(status, 200)
            _, body = soap(self.kitchen, RC, 'RenderingControl', 'GetVolume',
                           '<InstanceID>0</InstanceID><Channel>Master</Channel>')
            self.assertEqual(field(body, 'CurrentVolume'), str(volume))
        self.assertTrue(wait_for(lambda: self.fake.states['media_player.kitchen']['attributes']['volume_level'] == 0.30))
        self.assertEqual(self.fake.calls[-1][2], {'volume_level': 0.30})
        soap(self.kitchen, RC, 'RenderingControl', 'SetMute',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>1</DesiredMute>')
        self.assertTrue(wait_for(lambda: self.fake.states['media_player.kitchen']['attributes']['is_volume_muted']))
        # A change made elsewhere shows up once the CR100's own change has settled.
        time.sleep(backends.VOLUME_HOLD + 0.1)
        self.fake.set('media_player.kitchen', volume_level=0.55)
        self.assertTrue(wait_for(lambda: self.kitchen.state.backend.volume == 55))

    def test_changes_reach_the_cr100_as_events_and_unchanged_ones_do_not(self):
        self.assertEqual(self.events.subscribe(self.kitchen, '/MediaRenderer/AVTransport/Event'), 200)
        self.assertIn('So What', self.events.get())
        self.fake.set('media_player.kitchen', media_title='Freddie Freeloader', queue_position=4)
        event = self.events.get()
        self.assertIn('Freddie Freeloader', event)
        self.assertIn('CurrentTrack val="4"', event)
        # A position-only update changes nothing the CR100 is evented about.
        self.fake.set('media_player.kitchen', media_position=200)
        with self.assertRaises(queue.Empty): self.events.get(timeout=1)
        self.fake.set('media_player.kitchen', 'paused')
        self.assertIn('PAUSED_PLAYBACK', self.events.get())

    def test_grouping_goes_through_home_assistant(self):
        self.assertEqual(self.events.subscribe(self.spare, '/ZoneGroupTopology/Event'), 200)
        self.assertEqual(groups(field(self.events.get(), 'ZoneGroupState')), [['Kitchen'], ['Patio'], ['Spare']])
        uri = 'x-rincon:' + self.kitchen.config.udn
        status, _ = soap(self.patio, AVT, 'AVTransport', 'SetAVTransportURI',
                         f'<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1], ('join', 'media_player.kitchen', {'group_members': ['media_player.patio']}))
        self.assertEqual(groups(field(self.events.get(), 'ZoneGroupState')), [['Kitchen', 'Patio'], ['Spare']])
        self.assertEqual(self.patio.state.group_id, self.kitchen.config.udn + ':1')
        # Group volume is the members' average; changing it moves every member.
        _, body = soap(self.kitchen, GRC, 'GroupRenderingControl', 'GetGroupVolume', '<InstanceID>0</InstanceID>')
        self.assertEqual(field(body, 'CurrentVolume'), '20')
        soap(self.kitchen, GRC, 'GroupRenderingControl', 'SetGroupVolume',
             '<InstanceID>0</InstanceID><DesiredVolume>30</DesiredVolume>')
        self.assertTrue(wait_for(lambda: [self.fake.states[e]['attributes']['volume_level']
                                          for e in ('media_player.kitchen', 'media_player.patio')] == [0.3, 0.3]))
        # A simulated room cannot join a Home Assistant group, nor the reverse.
        self.assertFalse(self.house.join(self.spare.config.udn, self.kitchen.config.udn))
        self.assertFalse(self.house.join(self.kitchen.config.udn, self.spare.config.udn))
        status, _ = soap(self.patio, AVT, 'AVTransport', 'BecomeCoordinatorOfStandaloneGroup', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1][:2], ('unjoin', 'media_player.patio'))
        self.assertEqual(groups(field(self.events.get(), 'ZoneGroupState')), [['Kitchen'], ['Patio'], ['Spare']])

    def test_simulated_room_still_works_alongside(self):
        soap(self.spare, RC, 'RenderingControl', 'SetVolume',
             '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>44</DesiredVolume>')
        self.assertEqual(self.spare.state.volume, 44)
        self.assertEqual(self.fake.calls, [])


class Client(unittest.TestCase):
    def test_close_releases_connection_thread_and_loop(self):
        fake = fake_ha.FakeHA([])
        ha = HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None)
        ha.start()
        try:
            self.assertTrue(ha.connected.wait(5))
        finally:
            ha.close(); fake.close()
        ha.close(); fake.close()
        self.assertFalse(ha.thread.is_alive())
        self.assertTrue(ha.loop.is_closed())
        self.assertFalse(fake.thread.is_alive())
        self.assertTrue(fake.loop.is_closed())

    def test_close_from_client_thread_and_before_start(self):
        unused = HomeAssistant('http://127.0.0.1', 'unused', lambda _: None)
        unused.close(); unused.close()
        self.assertTrue(unused.loop.is_closed())
        fake = fake_ha.FakeHA([])
        ha = HomeAssistant(fake.url, fake_ha.TOKEN, lambda _: None)
        ha.start()
        closed_from_client = threading.Event()
        def close_on_client():
            ha.close(); ha.close()
            closed_from_client.set()
        try:
            self.assertTrue(ha.connected.wait(5))
            ha.loop.call_soon_threadsafe(close_on_client)
            self.assertTrue(closed_from_client.wait(2))
            ha.thread.join(5)
            self.assertFalse(ha.thread.is_alive())
            self.assertTrue(ha.loop.is_closed())
        finally:
            ha.close(); fake.close()

    def test_bad_token_is_reported_and_not_connected(self):
        fake = fake_ha.FakeHA([])
        logs = []
        ha = HomeAssistant(fake.url, 'wrong', logs.append)
        ha.start()
        try:
            self.assertTrue(wait_for(lambda: any(l.get('event') == 'ha-auth-failed' for l in logs)))
            self.assertFalse(ha.connected.is_set())
        finally:
            ha.close(); fake.close()

    def test_reconnects_and_resyncs(self):
        fake = fake_ha.FakeHA([fake_ha.sonos_state('media_player.patio', 'Patio')])
        seen = []
        ha = HomeAssistant(fake.url, fake_ha.TOKEN, lambda record: None)
        ha.listeners.append(lambda entity, state: seen.append(state and state['state']))
        import ha_client
        ha_client.RECONNECT_DELAY, saved = 0.2, ha_client.RECONNECT_DELAY
        ha.start()
        try:
            self.assertTrue(ha.connected.wait(5))
            fake.close()
            self.assertTrue(wait_for(lambda: not ha.connected.is_set()))
            fake2 = fake_ha.FakeHA([fake_ha.sonos_state('media_player.patio', 'Patio', 'playing')])
            # Bind the new fake on the old port is not guaranteed; point the client at it instead.
            ha.url = fake2.url
            self.assertTrue(wait_for(ha.connected.is_set))
            self.assertEqual(ha.state('media_player.patio')['state'], 'playing')
            self.assertEqual(seen[-1], 'playing')
            fake2.close()
        finally:
            ha_client.RECONNECT_DELAY = saved
            ha.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
