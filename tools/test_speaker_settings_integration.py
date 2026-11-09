"""Music-menu settings reach real provider SOAP through both backend adapters."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from fake_sonos import FakeHousehold
from ha_backend import HABackend
from music_services import MusicServices
from room_capabilities import RoomBinding
from sonos_backend import SonosBackend
from speaker_settings import SpeakerSettingsService
from spotify_service import SpotifyService
from test_ha_backend import wait_for
from test_settings_backends import HA, COORDINATOR, MEMBER, GENERIC
from test_smapi_stub import smapi, NS, decode_accounts, HOUSEHOLD
from test_spotify_bridge import Catalogue
from test_zoneplayers import zp
import zoneplayers

A, B = 'RINCON_000E58AAAAAA01400', 'RINCON_000E58BBBBBB01400'


class SettingsIntegration(unittest.TestCase):
    def setup_backend(self, mode):
        fake = FakeHousehold()
        self.addCleanup(fake.close)
        leader = fake.add(A, 'Coordinator', settings=True, line_in_connected=False)
        member = fake.add(B, 'Member', settings=True, line_in_connected=False)
        fake.coordinator[B] = A
        if mode == 'sonos':
            backend = SonosBackend([leader.base], log=lambda _: None)
            backend.start()
            self.addCleanup(backend.close)
            self.assertTrue(backend.ready.wait(5))
            refs = [('sonos', A), ('sonos', B)]
            self.assertTrue(wait_for(lambda: all(s.healthy and s.read_at for s in backend.speakers.values())))
            names = ['Coordinator', 'Member']
            ha = None
        else:
            ha = HA()
            bindings = {
                COORDINATOR: RoomBinding(platform='sonos', native_id=A, native_url=leader.base),
                MEMBER: RoomBinding(platform='sonos', native_id=B, native_url=member.base),
                GENERIC: RoomBinding(platform='cast')}
            inputs = SimpleNamespace(start=lambda: None, close=lambda: None,
                binding=lambda key: bindings.get(key, RoomBinding()), sources=lambda: [],
                identity=lambda key: {}, bonded_devices=lambda key: [], on_audio=lambda *args: None)
            backend = HABackend(ha, log=lambda _: None, line_in=inputs)
            self.addCleanup(backend.close)
            refs = [('ha', COORDINATOR), ('ha', MEMBER), ('ha', GENERIC)]
            names = ['Coordinator', 'Member', 'Generic']
        settings = SpeakerSettingsService('127.0.0.1', 0, lambda: house.settings_rooms(), log=lambda _: None)
        spotify = SpotifyService('127.0.0.1', 0, Catalogue(), log=lambda _: None)
        services = MusicServices([spotify, settings])
        self.addCleanup(services.close)
        house = zoneplayers.Household(zp, HOUSEHOLD, names, ['127.0.0.1'] * len(names),
            log=lambda _: None, port=0, backends=[backend], refs=refs, music_service=services)
        self.addCleanup(house.close)
        backend.settings.refresh()
        self.assertTrue(wait_for(lambda: 'bass' in house.players[1].state.backend.operation_capabilities()))
        services.start()
        house.start('127.0.0.1', ssdp=False)
        return SimpleNamespace(fake=fake, leader=leader, member=member, backend=backend,
                               house=house, settings=settings, services=services, ha=ha)

    def rows(self, service, ident):
        status, doc = smapi(service, 'getMetadata', {'id': ident, 'index': 0, 'count': 100})
        self.assertEqual(status, 200, ET.tostring(doc))
        return {child.find('s:title', NS).text: child.find('s:id', NS).text
                for child in doc.find('.//s:getMetadataResult', NS)
                if child.tag.endswith(('mediaCollection', 'mediaMetadata'))}

    def choice(self, fixture, name, setting, value):
        rooms = self.rows(fixture.settings, 'root')
        entries = self.rows(fixture.settings, rooms[name])
        setting_id = next(ident for title, ident in entries.items() if title.startswith(setting + ':'))
        return next(ident for title, ident in self.rows(fixture.settings, setting_id).items()
                    if title.removesuffix(' (current)') == value)

    @staticmethod
    def writes(player):
        return [(name, args) for name, args in player.calls if name.startswith(('Set', 'Configure'))]

    def test_both_backends_apply_member_eq_and_coordinator_timer_without_playback(self):
        for mode in ('sonos', 'ha'):
            with self.subTest(mode=mode):
                f = self.setup_backend(mode)
                before = (f.member.uri, list(f.member.queue), f.member.volume, f.member.mute)
                action = self.choice(f, 'Member', 'Bass', 'Bass +2')
                self.assertEqual(self.writes(f.member), [])
                self.assertEqual(smapi(f.settings, 'createItem', {'favorite': action})[0], 200)
                self.assertEqual(f.member.bass, 2)
                self.assertEqual(self.writes(f.member)[0][0], 'SetBass')
                self.assertEqual(self.writes(f.leader), [])
                self.assertEqual((f.member.uri, f.member.queue, f.member.volume, f.member.mute), before)
                timer = self.choice(f, 'Member', 'Sleep timer', 'Sleep after 15 minutes')
                self.assertEqual(smapi(f.settings, 'createItem', {'favorite': timer})[0], 200)
                self.assertIsNotNone(f.leader.sleep_deadline)
                self.assertIsNone(f.member.sleep_deadline)
                if f.ha:
                    self.assertEqual(f.ha.calls, [])
                    generic = self.rows(f.settings, self.rows(f.settings, 'root')['Generic'])
                    self.assertEqual(list(generic), ['No supported speaker settings available'])

    def test_accounts_coexist_and_fixed_mode_rejects_cached_eq_action(self):
        f = self.setup_backend('sonos')
        types = f.services.descriptor()['AvailableServiceTypeList'].split(',')
        self.assertEqual(types, ['61447', '62471'])
        wire = ET.fromstring(f.house.players[0].state.propertyset('ZoneGroupTopology')).find('.//ThirdPartyMediaServersX').text
        accounts = ET.fromstring(decode_accounts(wire, HOUSEHOLD)).findall('Service')
        self.assertEqual(len(accounts), 2)
        old_eq = self.choice(f, 'Member', 'Bass', 'Bass +2')
        fixed = self.choice(f, 'Member', 'Line-out', 'Line-out Fixed')
        self.assertEqual(smapi(f.settings, 'createItem', {'favorite': fixed})[0], 200)
        self.assertTrue(f.member.output_fixed)
        previous = self.writes(f.member)
        self.assertEqual(smapi(f.settings, 'createItem', {'favorite': old_eq})[0], 500)
        self.assertEqual(self.writes(f.member), previous)
        room = self.rows(f.settings, self.rows(f.settings, 'root')['Member'])
        self.assertFalse(any(title.startswith(('Bass:', 'Balance:', 'Loudness:')) for title in room))
        variable = self.choice(f, 'Member', 'Line-out', 'Line-out Variable')
        self.assertEqual(smapi(f.settings, 'createItem', {'favorite': variable})[0], 200)
        self.assertFalse(f.member.output_fixed)


class SettingsConfiguration(unittest.TestCase):
    def test_stock_settings_are_opt_in_and_independent_of_spotify(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text('{}')
            self.assertIsNone(zoneplayers.load_music_service(path, '127.0.0.1', lambda _: None))
            path.write_text(json.dumps({'speaker_settings': {'enabled': True, 'host': '127.0.0.1', 'port': 0}}))
            service = zoneplayers.load_music_service(path, '127.0.0.1', lambda _: None)
            try:
                self.assertIsInstance(service, SpeakerSettingsService)
                self.assertEqual(service.name, 'Speaker Settings')
            finally:
                service.close()
            path.write_text(json.dumps({'speaker_settings': {'enabled': False}}))
            self.assertIsNone(zoneplayers.load_music_service(path, '127.0.0.1', lambda _: None))


if __name__ == '__main__': unittest.main()
