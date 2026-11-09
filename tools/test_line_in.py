#!/usr/bin/env python3
"""Line-in through the virtual household's real SOAP and event surfaces."""
import html
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import zoneplayers
from test_ha_backend import AVT, Events, field
from test_zoneplayers import HOUSEHOLD, soap, zp

CD = '/MediaServer/ContentDirectory/Control'
AI = '/AudioIn/Control'
PHYSICAL = 'RINCON_PHYSICAL_SOURCE01400'
HIDDEN = 'RINCON_HIDDEN_SOURCE01400'


class LineInBackend(backends.Backend):
    name = 'line-in-test'

    def __init__(self):
        super().__init__(lambda record: None)
        self.calls, self.rooms = [], {}
        self.sources = [
            {'id': 'source', 'name': 'Turntable & CD', 'room_name': 'Den',
             'icon': 'AudioComponent', 'uri': 'x-rincon-stream:' + PHYSICAL},
            {'id': 'hidden', 'name': 'Portable', 'room_name': 'Studio',
             'icon': 'PortablePlayer', 'uri': 'x-rincon-stream:' + HIDDEN},
        ]

    def line_in_sources(self):
        return list(self.sources)

    def room(self, key):
        return self.rooms.setdefault(key, LineInRoom(self, key))


class LineInRoom(backends.Room):
    reported_volume = 20
    reported_mute = False
    transport = None

    def __init__(self, backend, key):
        super().__init__(backend, key)
        self.source, self.transport = None, 'STOPPED'

    def track(self):
        return None

    def line_in_source(self):
        return self.source

    def play_line_in(self, source):
        self.source, self.transport = source, 'PLAYING'
        self.backend.calls.append((self.key, 'play_line_in', source))
        self.backend.changed(self.key)

    def play(self):
        self.transport = 'PLAYING'
        self.backend.calls.append((self.key, 'play'))

    def stop(self):
        self.transport = 'STOPPED'
        self.backend.calls.append((self.key, 'stop'))


class LineInHousehold(unittest.TestCase):
    def setUp(self):
        self.backend = LineInBackend()
        self.house = zoneplayers.Household(
            zp, HOUSEHOLD, ['Den', 'Kitchen'], ['127.0.0.1'] * 2,
            lambda record: None, port=0, backends=[self.backend],
            refs=[(self.backend.name, 'source'), (self.backend.name, 'destination')])
        self.house.start('127.0.0.1', ssdp=False)
        self.source, self.destination = self.house.players

    def tearDown(self):
        self.house.close()

    def select(self, player, uri):
        return soap(player, AVT, 'AVTransport', 'SetAVTransportURI',
                    '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>'
                    '<CurrentURIMetaData></CurrentURIMetaData>' % html.escape(uri))

    def test_audioin_advertises_only_the_rooms_connected_input(self):
        values = self.source.state.event_values('AudioIn')
        self.assertEqual(values['LineInConnected'], '1')
        self.assertEqual(values['AudioInputName'], 'Turntable & CD')
        self.assertEqual(values['Icon'], 'AudioComponent')
        self.assertEqual(self.destination.state.event_values('AudioIn')['LineInConnected'], '0')
        status, body = soap(self.source, AI, 'AudioIn', 'GetAudioInputAttributes')
        self.assertEqual(status, 200)
        self.assertEqual(field(body, 'CurrentName'), 'Turntable & CD')
        self.assertEqual(field(body, 'CurrentIcon'), 'AudioComponent')

    def browse(self, player, object_id='AI:', flag='BrowseDirectChildren'):
        return soap(player, CD, 'ContentDirectory', 'Browse',
                            '<ObjectID>%s</ObjectID><BrowseFlag>%s</BrowseFlag>'
                            '<Filter>*</Filter><StartingIndex>0</StartingIndex>'
                            '<RequestedCount>10</RequestedCount><SortCriteria></SortCriteria>' % (object_id, flag))

    def test_linein_browse_lists_only_its_own_input_without_a_content_provider(self):
        status, body = self.browse(self.source)
        self.assertEqual((status, field(body, 'NumberReturned'), field(body, 'TotalMatches')),
                         (200, '1', '1'))
        result = ET.fromstring(field(body, 'Result'))
        uris = [node.text for node in result.iter() if node.tag.endswith('}res')]
        self.assertEqual(uris, ['x-rincon-stream:' + self.source.config.udn])
        self.assertIn('Turntable &amp; CD', field(body, 'Result'))
        titles = [node.text for node in result.iter() if node.tag.endswith('}title')]
        self.assertEqual(titles, ['Turntable & CD'])  # native CR100 appends the room itself
        status, body = self.browse(self.destination)
        self.assertEqual((status, field(body, 'NumberReturned'), field(body, 'TotalMatches')),
                         (200, '0', '0'))
        status, body = self.browse(self.source, 'AI:0', 'BrowseMetadata')
        self.assertEqual((status, field(body, 'NumberReturned')), (200, '1'))
        self.assertIn('x-rincon-stream:' + self.source.config.udn, field(body, 'Result'))

    def test_virtual_source_selection_resolves_to_backend_room(self):
        status, _ = self.select(self.destination, 'x-rincon-stream:' + self.source.config.udn)
        self.assertEqual(status, 200)
        self.assertIn(('destination', 'play_line_in', 'source'), self.backend.calls)
        self.assertEqual(self.destination.state.backend.line_in_source(), 'source')

    def test_source_can_play_its_own_input(self):
        status, _ = self.select(self.source, 'x-rincon-stream:' + self.source.config.udn)
        self.assertEqual(status, 200)
        self.assertIn(('source', 'play_line_in', 'source'), self.backend.calls)

    def test_selection_uses_requested_room_when_virtual_group_cache_is_stale(self):
        # A real ungroup has happened, while the batched virtual topology still
        # points at the former coordinator. The backend owns current grouping.
        self.house.coordinator[self.destination.config.udn] = self.source.config.udn
        status, _ = self.select(self.destination, 'x-rincon-stream:' + self.source.config.udn)
        self.assertEqual(status, 200)
        self.assertIn(('destination', 'play_line_in', 'source'), self.backend.calls)
        self.assertNotIn(('source', 'play_line_in', 'source'), self.backend.calls)

    def test_source_without_a_virtual_room_can_still_be_selected(self):
        status, _ = self.select(self.destination, 'x-rincon-stream:' + HIDDEN)
        self.assertEqual(status, 200)
        self.assertIn(('destination', 'play_line_in', 'hidden'), self.backend.calls)

    def test_external_self_and_remote_playback_have_real_linein_transport(self):
        for player in (self.source, self.destination):
            with self.subTest(room=player.config.room):
                player.state.backend.source = 'source'
                player.state.backend.transport = 'PLAYING'
                status, body = soap(player, AVT, 'AVTransport', 'GetMediaInfo', '<InstanceID>0</InstanceID>')
                self.assertEqual(status, 200)
                self.assertEqual(field(body, 'CurrentURI'), 'x-rincon-stream:' + self.source.config.udn)
                self.assertEqual(field(body, 'NrTracks'), '1')
                # The legacy UI uses the transport title for its Source label.
                # Empty physical-player metadata falls back to "Internet Radio".
                metadata = ET.fromstring(field(body, 'CurrentURIMetaData'))
                titles = [node.text for node in metadata.iter() if node.tag.endswith('}title')]
                self.assertEqual(titles, ['Turntable & CD'])
                self.assertIn('Turntable &amp; CD', player.state.backend_values()['AVTransportURIMetaData'])
                _, body = soap(player, AVT, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
                self.assertEqual(field(body, 'TrackURI'), 'x-rincon-stream:' + self.source.config.udn)
                self.assertEqual(field(body, 'Track'), '1')
                track_metadata = ET.fromstring(field(body, 'TrackMetaData'))
                self.assertEqual([node.text for node in track_metadata.iter() if node.tag.endswith('}title')],
                                 ['Turntable & CD'])
                self.assertIn('Turntable &amp; CD', player.state.backend_values()['CurrentTrackMetaData'])
                self.assertEqual(field(body, 'TrackDuration'), 'NOT_IMPLEMENTED')
                self.assertEqual(field(body, 'RelTime'), 'NOT_IMPLEMENTED')
                self.assertNotIn('bridge.invalid', body.decode())

    def test_audioin_playing_tracks_use_of_the_source_by_another_room(self):
        self.destination.state.backend.source = 'source'
        self.destination.state.backend.transport = 'PLAYING'
        self.assertEqual(self.source.state.event_values('AudioIn')['Playing'], '1')
        self.assertEqual(self.destination.state.event_values('AudioIn')['Playing'], '0')
        self.destination.state.backend.transport = 'STOPPED'
        self.assertEqual(self.source.state.event_values('AudioIn')['Playing'], '0')

    def test_unknown_linein_source_is_rejected_without_backend_call(self):
        status, _ = self.select(self.destination, 'x-rincon-stream:RINCON_UNKNOWN01400')
        self.assertEqual(status, 500)
        self.assertEqual(self.backend.calls, [])

    def test_disconnect_and_reconnect_notify_audioin_and_invalidate_browse(self):
        audio, content = Events(), Events()
        try:
            self.assertEqual(audio.subscribe(self.source, '/AudioIn/Event'), 200)
            self.assertEqual(field(audio.get().encode(), 'LineInConnected'), '1')
            self.assertEqual(content.subscribe(self.source, '/MediaServer/ContentDirectory/Event'), 200)
            before = field(content.get().encode(), 'ContainerUpdateIDs')
            source = self.backend.sources.pop(0)
            self.backend.changed('source')
            self.assertEqual(field(audio.get().encode(), 'LineInConnected'), '0')
            disconnected = field(content.get().encode(), 'ContainerUpdateIDs')
            self.assertNotEqual(before, disconnected)
            self.assertEqual(field(self.browse(self.source)[1], 'TotalMatches'), '0')
            self.backend.sources.insert(0, dict(source, name='Renamed Turntable'))
            self.backend.changed('source')
            event = audio.get().encode()
            self.assertEqual(field(event, 'LineInConnected'), '1')
            self.assertEqual(field(event, 'AudioInputName'), 'Renamed Turntable')
            self.assertNotEqual(disconnected, field(content.get().encode(), 'ContainerUpdateIDs'))
            self.assertIn('Renamed Turntable', field(self.browse(self.source)[1], 'Result'))
        finally:
            audio.close()
            content.close()
            audio.server.server_close()
            content.server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
