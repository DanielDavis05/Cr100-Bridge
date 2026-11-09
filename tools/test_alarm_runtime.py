"""Global alarm production bindings, including real Sonos HTTP/SOAP callbacks."""
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from alarm_runtime import (HouseholdAlarmExecutor, RadioAlarmProvider, attach_alarms,
                           chime_wav)
from alarms import AlarmError, AlarmMediaRef, AlarmProviderRegistry, AlarmService, AlarmStore, PlaybackRequest
from backends import BackendError
from radio_catalog import RadioItem
from radio_playback import RadioPlayback, radio_uri
from radio_store import RadioStore
from test_alarms import Clock


class Catalog:
    def __init__(self): self.resolutions = []
    def metadata(self, identity): return RadioItem(identity, 'Alarm station', 'station')
    def resolve(self, identity, formats=()):
        self.resolutions.append(identity)
        return [SimpleNamespace(url='https://station.example/live.mp3')]


class Room:
    def __init__(self, name, *, native=True):
        self.key = name
        self.backend = SimpleNamespace(name='sonos' if native else 'ha')
        self.ref = self.backend.name, name
        self.available = True
        self.binding = SimpleNamespace(is_sonos=native, native_url='http://192.0.2.1:1400')
        self.avt = {'AVTransportURI': 'queue:original'}
        self.calls, self.queue = [], ['saved song one', 'saved song two']
        self.operations = {'radio_play','volume','mute','stop','group','provider_play'}
    def require(self, operation):
        if operation not in self.operations: raise BackendError('Unavailable ' + operation)
    def supports(self, operation): return operation in self.operations
    def send_volume(self, value): self.calls.append(('volume', value))
    def send_mute(self, value): self.calls.append(('mute', value))
    def play_radio(self, uri, metadata):
        self.calls.append(('radio', uri)); self.avt['AVTransportURI'] = uri
    def play_provider(self, uri, kind):
        self.calls.append(('provider', uri, kind)); self.avt['AVTransportURI'] = uri
    def unjoin(self): self.calls.append(('unjoin',))
    def stop(self): self.calls.append(('stop',))
    def track_reference(self): return self.avt['AVTransportURI']
    def track(self): return None


class Household:
    def __init__(self, grouped=False, *, native=True):
        self.lock = threading.RLock(); self.logs = []; self.log = self.logs.append
        self.players = [SimpleNamespace(config=SimpleNamespace(udn=name), component_key='component:' + name,
            state=SimpleNamespace(backend=Room(name, native=native)), http_thread=None) for name in ('kitchen','patio')]
        self.by_udn = {p.config.udn:p for p in self.players}
        self.coordinator = {'kitchen':'kitchen','patio':'kitchen' if grouped else 'patio'}
    def members(self, udn):
        coordinator = self.coordinator[udn]
        return [p for p in self.players if self.coordinator[p.config.udn] == coordinator]


class Runtime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'bridge.json'; self.config.write_text('{}')
        self.clock = Clock(); self.house = Household(grouped=True)
        self.catalog, self.radio = Catalog(), RadioStore()
        self.radio.add_favorite(RadioItem('station:s1','Station one','station'))
        self.playback = RadioPlayback(self.house,self.catalog,self.radio,log=lambda _:None)
        self.playback.art.path_for = lambda *args,**kwargs:'/art/alarm-fixture.jpg'
        self.addCleanup(self.playback.close)
        self.house.radio_playback = self.playback
        profile = patch.object(self.playback,'refresh_profile')
        profile.start(); self.addCleanup(profile.stop)

    def setup_service(self, config=None):
        store = AlarmStore(Path(self.temp.name) / ('alarms-%d.sqlite' % len(list(Path(self.temp.name).glob('*.sqlite')))))
        runtime = HouseholdAlarmExecutor(self.house,store,config,clock=self.clock)
        registry = AlarmProviderRegistry(); registry.register('radio',RadioAlarmProvider(self.catalog,self.radio))
        service = AlarmService(store,providers=registry,executor=runtime.callbacks(),clock=self.clock,
            log=lambda _:None,room_resolver=runtime.udn,handle_sleep=True,bridge_sleep=runtime.bridge_sleep)
        self.addCleanup(service.close)
        return service,runtime

    def alarm(self,service,**values):
        return service.save({'media_ref':{'provider':'radio','content_id':'station:s1'},'room':'kitchen',
            'time':'12:00:00','duration':30,'include_linked':True,'volume':37,**values})

    def test_live_alarm_uses_actual_radio_path_and_group_volume_without_queue_clear(self):
        service,runtime = self.setup_service(); self.alarm(service)
        self.clock.advance(10); service.tick()
        for player in self.house.players:
            self.assertIn(('volume',37),player.state.backend.calls)
            self.assertIn(('mute',False),player.state.backend.calls)
            self.assertEqual(player.state.backend.queue,['saved song one','saved song two'])
        self.assertIn(('radio','https://station.example/live.mp3'),self.house.players[0].state.backend.calls)
        self.assertEqual(service.store.runs()[-1]['status'],'playing')

    def test_selected_room_is_isolated_when_linked_zones_disabled(self):
        service,runtime = self.setup_service(); self.alarm(service,room='patio',include_linked=False)
        self.clock.advance(10); service.tick()
        self.assertIn(('unjoin',),self.house.players[1].state.backend.calls)
        self.assertEqual(self.house.coordinator['patio'],'patio')
        self.assertFalse(any(c[0]=='radio' for c in self.house.players[0].state.backend.calls))
        self.assertIn(('radio','https://station.example/live.mp3'),self.house.players[1].state.backend.calls)

    def test_same_uri_manual_selection_revokes_alarm_duration(self):
        service,runtime = self.setup_service(); self.alarm(service)
        self.clock.advance(10); service.tick()
        room = self.house.players[0].state.backend
        runtime.before_action('component:patio','AVTransport','SetAVTransportURI',{'CurrentURI':room.avt['AVTransportURI']})
        self.clock.advance(30); service.tick()
        self.assertEqual(service.store.runs()[-1]['status'],'superseded')
        self.assertNotIn(('stop',),room.calls)

    def test_controlled_radio_reconnect_keeps_alarm_duration_ownership(self):
        service,runtime=self.setup_service();self.alarm(service)
        self.clock.advance(10);service.tick()
        run=service.store.runs()[-1]
        self.playback.play('kitchen','station:s1',retry_count=1)
        self.assertTrue(runtime.owns(run['context'],run['handle']))
        self.clock.advance(30);service.tick()
        self.assertIn(('stop',),self.house.players[0].state.backend.calls)

    def test_duration_stops_real_bound_backend_and_clears_radio_session(self):
        service,runtime = self.setup_service(); self.alarm(service)
        self.clock.advance(10); service.tick(); self.clock.advance(30); service.tick()
        self.assertIn(('stop',),self.house.players[0].state.backend.calls)
        self.assertIsNone(self.playback.session('kitchen'))

    def test_room_aliases_map_to_stable_virtual_identity(self):
        service,runtime = self.setup_service()
        for alias in ('kitchen','component:kitchen','sonos:kitchen'):
            self.assertEqual(runtime.udn(alias),'kitchen')
        self.assertEqual(runtime.player('component:patio').config.udn,'kitchen')

    def test_native_chime_fallback_starts_real_buzzer_transport(self):
        service,runtime = self.setup_service()
        context = {'run_id':'fixture-run','room':'patio','include_linked':True,'volume':45}
        handle = runtime.fallback(context)
        self.assertEqual(handle,'chime:fixture-run')
        self.assertIn(('radio','x-rincon-buzzer:0'),self.house.players[0].state.backend.calls)
        self.assertTrue(runtime.owns(context,handle))
        runtime.stop(context,handle)
        self.assertIn(('stop',),self.house.players[0].state.backend.calls)

    def test_generic_chime_uses_configured_renderer_reachable_wav(self):
        self.house.players[0].state.backend.binding.is_sonos = False
        self.house.players[0].state.backend.backend.name = 'ha'
        service,runtime = self.setup_service({'alarms':{'chime_url':'http://bridge.lan:8080/alarm-chime.wav'}})
        context = {'run_id':'generic','room':'kitchen','include_linked':False,'volume':20}
        runtime.fallback(context)
        self.assertIn(('radio','http://bridge.lan:8080/alarm-chime.wav'),self.house.players[0].state.backend.calls)

    def test_native_sleep_route_is_retained_and_generic_timer_is_real(self):
        service,runtime = self.setup_service()
        self.house.players[0].state.backend.operations.add('sleep_timer')
        self.assertIsNone(service.action('AVTransport','ConfigureSleepTimer',{'NewSleepTimerDuration':'0:01:00'},'kitchen'))
        self.house.players[0].state.backend.operations.remove('sleep_timer')
        self.assertEqual(service.action('AVTransport','ConfigureSleepTimer',{'NewSleepTimerDuration':'0:00:10'},'kitchen'),{})
        self.clock.advance(10); service.tick()
        self.assertIn(('stop',),self.house.players[0].state.backend.calls)

    def test_provider_native_formats_and_deleted_favorite_independence(self):
        provider = RadioAlarmProvider(self.catalog,self.radio)
        for uri in (radio_uri('station:s1'),'x-cr100-radio:station:s1','x-sonosapi-stream:s1?sid=254'):
            self.assertEqual(provider.from_native(uri).content_id,'station:s1')
        self.radio.remove_favorite('station:s1')
        self.assertEqual(provider.describe(AlarmMediaRef('radio','station:s1'))['title'],'Alarm station')

    def test_chime_is_valid_audible_pcm(self):
        data = chime_wav(1)
        with wave.open(io.BytesIO(data),'rb') as source:
            self.assertEqual((source.getnchannels(),source.getsampwidth(),source.getframerate()),(1,2,8000))
            self.assertEqual(source.getnframes(),8000)
            self.assertNotEqual(set(source.readframes(8000)),{0})


class AboutTopologyEvents(unittest.TestCase):
    """Real advertised About/bridge endpoints publish events, never play alarms."""
    def setUp(self):
        import zoneplayers
        from test_about_system import BUILD, IdentityBackend
        from test_zoneplayers import HOUSEHOLD, zp
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.backend = IdentityBackend()
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den', 'Hall'], ['127.0.0.1'] * 2,
            lambda _: None, port=0, backends=[self.backend],
            refs=[('memory', 'den'), ('memory', 'hall')], build=BUILD, bridge_ip='192.0.2.200')
        self.addCleanup(self.house.close)
        config = Path(self.temp.name) / 'bridge.json'; config.write_text('{}')
        self.service = attach_alarms(self.house, config, Catalog(), RadioStore())

    def test_clock_edits_publish_alarm_clock_events_for_subscribed_controllers(self):
        player = self.house.players[0]
        with patch.object(player, 'http_thread', True), patch.object(player, 'publish_changed') as publish:
            self.service.action('AlarmClock', 'SetFormat', {'DesiredTimeFormat':'12H','DesiredDateFormat':'MDY'})
            publish.assert_called_with('AlarmClock')
            self.service.action('AlarmClock', 'SetTimeZone', {'Index':'13','AutoAdjustDst':'1'})
            publish.assert_called_with('AlarmClock')
            self.service.action('AlarmClock', 'SetTimeServer', {'DesiredTimeServer':''})
            publish.assert_called_with('AlarmClock')

    def test_all_advertised_components_publish_topology_without_becoming_alarm_targets(self):
        self.assertGreaterEqual(len(self.house.about_players), 2)  # Bonded Sub and bridge.
        for player in self.house.devices():
            with self.subTest(component=player.component_key):
                values = player.state.event_values('ZoneGroupTopology')
                expected = '%s:%s:0' % (player.config.udn, player.config.boot_sequence)
                self.assertEqual(values['AlarmRunSequence'], expected)
                if player.state.informational:
                    with self.assertRaises(AlarmError): self.service.runtime.udn(player.config.udn)
                    with self.assertRaises(AlarmError): self.service.runtime.player(player.config.udn)
                    self.assertEqual(self.service.event_values('AVTransport', player.config.udn), {})
                    with self.assertRaises(AlarmError):
                        self.service.action('AlarmClock', 'CreateAlarm',
                            {'RoomUUID': player.config.udn}, player.config.udn)
        self.assertEqual(self.service.list(), [])

    def test_stale_alarm_source_uses_advertised_endpoint_and_removed_endpoints_are_safe(self):
        player = next(iter(self.house.about_players.values()))
        self.service.store.set_meta('last_alarm_room', 'removed-room')
        player.config.boot_sequence = 19
        self.assertEqual(self.service.event_values('ZoneGroupTopology', player.config.udn),
                         {'AlarmRunSequence': player.config.udn + ':19:0'})
        self.assertEqual(self.service.event_values('ZoneGroupTopology', 'removed-component'), {})
        room = self.house.players[0]
        self.service.store.set_meta('last_alarm_room', room.config.udn)
        self.service.store.set_meta('alarm_run_sequence', 7)
        self.assertEqual(self.service.event_values('ZoneGroupTopology', player.config.udn),
                         {'AlarmRunSequence': '%s:%s:7' % (room.config.udn, room.config.boot_sequence)})


class SonosHTTP(unittest.TestCase):
    """Actual Household + SonosBackend commands to the local Sonos fixture."""
    def setUp(self):
        from test_sonos_backend import SonosHousehold
        SonosHousehold.setUp(self)
        self.addCleanup(SonosHousehold.tearDown, self)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / 'bridge.json'; config.write_text('{}')
        self.catalog, self.radio = Catalog(), RadioStore()
        self.playback = RadioPlayback(self.house,self.catalog,self.radio,log=lambda _:None)
        self.house.radio_playback = self.playback
        profile = patch.object(self.playback,'refresh_profile'); profile.start(); self.addCleanup(profile.stop)
        self.service = attach_alarms(self.house,config,self.catalog,self.radio)

    def test_opaque_time_stamp_faults_instead_of_falling_through_to_peer_clock(self):
        from test_zoneplayers import soap
        import xml.etree.ElementTree as ET
        status, body = soap(self.vkitchen, '/AlarmClock/Control', 'AlarmClock',
                            'GetHouseholdTimeAtStamp', '<TimeStamp>0</TimeStamp>')
        self.assertEqual(status, 500, body)
        root = ET.fromstring(body)
        self.assertEqual(root.findtext('.//{urn:schemas-upnp-org:control-1-0}errorCode'), '501')
        self.assertIsNone(root.find('.//HouseholdUTCTime'))

    def test_alarm_reaches_sonos_transport_volume_and_preserves_saved_queue(self):
        from test_sonos_backend import calls
        from test_ha_backend import wait_for
        original = [('http://saved.example/song.mp3','')]
        self.kitchen.queue = list(original)
        alarm = self.service.save({'media_ref':{'provider':'radio','content_id':'station:s1'},
            'room':self.vkitchen.config.udn,'time':'12:00:00','duration':60,
            'include_linked':True,'volume':42})
        result = self.service.run_now(alarm['id'])
        self.assertEqual(result['status'],'playing')
        event = __import__('xml.etree.ElementTree',fromlist=['']).fromstring(self.vkitchen.state.last_change())
        rincon = '{urn:schemas-rinconnetworks-com:metadata-1-0/}'
        self.assertEqual(event.find('.//' + rincon + 'AlarmRunning').get('val'),'1')
        sequence = self.service.event_values('ZoneGroupTopology',self.vkitchen.config.udn)['AlarmRunSequence']
        self.assertTrue(sequence.startswith(self.vkitchen.config.udn + ':'))
        self.assertTrue(sequence.endswith(':1'))
        self.assertEqual(self.kitchen.uri,'https://station.example/live.mp3')
        self.assertEqual(self.kitchen.volume,42)
        self.assertEqual(self.kitchen.queue,original)
        self.assertEqual(calls(self.kitchen,'RemoveAllTracksFromQueue'),[])
        self.assertTrue(wait_for(lambda:self.backend.room(self.kitchen.uuid).avt.get('AVTransportURI')==self.kitchen.uri))
        self.assertTrue(self.service.stop(self.vkitchen.config.udn))
        self.assertTrue(calls(self.kitchen,'Stop'))


class HomeAssistantWebSocket(unittest.TestCase):
    def setUp(self):
        from test_ha_backend import HABackend
        HABackend.setUp(self)
        self.addCleanup(HABackend.tearDown,self)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / 'bridge.json'; config.write_text('{}')
        self.catalog,self.radio = Catalog(),RadioStore()
        self.playback = RadioPlayback(self.house,self.catalog,self.radio,log=lambda _:None)
        self.house.radio_playback = self.playback
        profile = patch.object(self.playback,'refresh_profile'); profile.start(); self.addCleanup(profile.stop)
        self.service = attach_alarms(self.house,config,self.catalog,self.radio)

    def test_alarm_routes_to_ha_play_media_without_enqueue_and_sets_actual_volume(self):
        alarm = self.service.save({'media_ref':{'provider':'radio','content_id':'station:s1'},
            'room':self.kitchen.config.udn,'time':'12:00:00','duration':60,
            'include_linked':True,'volume':38})
        result = self.service.run_now(alarm['id'])
        self.assertEqual(result['status'],'playing')
        plays = [(entity,data) for name,entity,data in self.fake.calls if name == 'play_media']
        self.assertEqual(len(plays),1)
        entity,data = plays[0]
        self.assertEqual(entity,'media_player.kitchen')
        self.assertEqual(data['media_content_id'],'https://station.example/live.mp3')
        self.assertNotIn('enqueue',data)
        volumes = [data['volume_level'] for name,entity,data in self.fake.calls if name == 'volume_set']
        self.assertIn(.38,volumes)


if __name__ == '__main__': unittest.main()
