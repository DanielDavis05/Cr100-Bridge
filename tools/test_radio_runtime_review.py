"""First-setup and changing-LAN regressions for the source composition layer."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from radio_runtime import configure_sources, renderer_stream_host
from radio_transcode import RadioTranscoder, RadioTranscodeError


class SourceRuntimeReview(unittest.TestCase):
    def test_transcoder_uses_lan_refresh_and_tracks_later_address_changes(self):
        house=SimpleNamespace(bridge_ip='')
        def refresh(**kwargs):house.bridge_ip='192.0.2.10'
        house.refresh_identities=refresh
        reader=renderer_stream_host(house,{},'192.168.69.1')
        converter=RadioTranscoder(reader,ffmpeg='/missing')
        self.assertEqual(converter._public_url('opaque'),'http://192.0.2.10:0/radio/audio/opaque.mp3')
        house.bridge_ip='192.0.2.20'
        self.assertIn('192.0.2.20',converter._public_url('opaque'))

    def test_controller_tap_is_not_advertised_to_real_renderers(self):
        house=SimpleNamespace(bridge_ip='192.168.69.1')
        with self.assertRaises(RadioTranscodeError):renderer_stream_host(house,{},'192.168.69.1')()
        self.assertEqual(renderer_stream_host(house,{'stream_host':'bridge.lan'},'192.168.69.1')(),'bridge.lan')

    def test_no_rooms_first_setup_keeps_web_state_without_native_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=Path(temporary)/'bridge.json'
            config.write_text(json.dumps({'rooms':[],'alarms':{'enabled':False}}))
            house=SimpleNamespace(players=[],by_udn={},coordinator={},music_service=None,
                bridge_ip='',alarms=None,log=lambda _:None)
            with patch('radio_runtime.NativeRadioService') as native:
                catalog=configure_sources(house,config,'192.168.69.1',client=SimpleNamespace(zip_location=lambda _:None),native_port=0)
                self.assertIs(catalog,house.radio_catalog)
                native.assert_not_called()
                self.assertTrue(house.radio_store.settings()['show_podcasts'])
                house.radio_playback.close();house.radio_transcoder.close()


if __name__=='__main__':unittest.main()
