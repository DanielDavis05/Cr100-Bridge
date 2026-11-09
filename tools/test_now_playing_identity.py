"""Regression checks for constant-URI Connect tracks and reused artwork proxies."""
import io
import unittest
import xml.etree.ElementTree as ET

from PIL import Image

from backends import ArtStore, Room, track_revision


class Backend:
    name = 'memory'
    content = None

    def __init__(self):
        self.color, self.fetches = 'red', []

    def fetch(self, source):
        self.fetches.append(source)
        stream = io.BytesIO()
        Image.new('RGB', (4, 4), self.color).save(stream, 'PNG')
        return stream.getvalue()


class ConnectRoom(Room):
    transport = 'PLAYING'
    reported_volume = 20
    reported_mute = False

    def __init__(self):
        super().__init__(Backend(), 'speaker')
        self.title, self.elapsed = 'First song', 10

    def track_reference(self):
        return 'speaker:x-sonos-vli:one-unchanging-session'

    def track(self):
        return self.title, 'Artist', 'Album', '/art/cover.jpg', 180

    def queue_numbers(self):
        return 1, 1

    def position(self):
        return self.elapsed


class NowPlayingIdentity(unittest.TestCase):
    def test_position_reconciliation_is_reflected_in_the_same_response(self):
        room = ConnectRoom()
        def reconcile():
            room.title = 'Song discovered by position read'
            return 42
        room.position = reconcile
        values = room.values('VIRTUAL')
        self.assertIn('Song discovered by position read', values['TrackMetaData'])
        self.assertEqual(values['RelTime'], '0:00:42')

    def test_connect_song_changes_have_matching_didl_and_action_event_identities(self):
        room = ConnectRoom()
        first = room.values('VIRTUAL')
        room.title = 'Second song'
        second = room.values('VIRTUAL')
        self.assertNotEqual(first['CurrentTrackURI'], second['CurrentTrackURI'])
        self.assertEqual(second['CurrentTrackURI'], second['TrackURI'])
        self.assertEqual(second['CurrentTrackMetaData'], second['TrackMetaData'])
        root = ET.fromstring(second['CurrentTrackMetaData'])
        item = root[0]
        self.assertNotEqual(ET.fromstring(first['TrackMetaData'])[0].get('id'), item.get('id'))
        self.assertEqual(item.find('{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res').text,
                         second['TrackURI'])
        room.elapsed = 90
        self.assertEqual(room.values('VIRTUAL')['TrackURI'], second['TrackURI'])

    def test_reused_proxy_fetches_new_image_for_new_song_and_keeps_previous_image(self):
        backend = Backend()
        art = ArtStore(backend)
        source = '/api/media_player_proxy/speaker'
        first = art.path_for(source, prefetch=False, revision=track_revision('session', 'First'))
        red = art.get(first.rsplit('/', 1)[1][:-4])
        backend.color = 'blue'
        second = art.path_for(source, prefetch=False, revision=track_revision('session', 'Second'))
        blue = art.get(second.rsplit('/', 1)[1][:-4])
        self.assertNotEqual(first, second)
        self.assertNotEqual(red, blue)
        self.assertEqual(art.get(first.rsplit('/', 1)[1][:-4]), red)
        self.assertEqual(backend.fetches, [source, source])
        self.assertEqual(art.path_for(source, prefetch=False,
                                    revision=track_revision('session', 'Second')), second)
        self.assertEqual(art.get(second.rsplit('/', 1)[1][:-4]), blue)
        self.assertEqual(len(backend.fetches), 2)


if __name__ == '__main__':
    unittest.main()
