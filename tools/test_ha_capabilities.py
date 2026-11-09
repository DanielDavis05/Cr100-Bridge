"""HA routing tests without an HA installation or contact with real speakers."""
from types import SimpleNamespace
import threading
from unittest.mock import patch
import unittest
import html

import content
import ha_backend as ha
from ha_content import Content
from ha_linein import SonosLineIn
from room_capabilities import RoomBinding
from backends import BackendError


SONOS, CAST = 'media_player.sonos', 'media_player.cast'
UUID = 'RINCON_00112233445501400'


class HomeAssistant:
    def __init__(self):
        self.listeners, self.calls, self.browses = [], [], []
        self.states = {
            SONOS: {'state': 'playing', 'attributes': {'supported_features': 8321599}},
            CAST: {'state': 'playing', 'attributes': {'friendly_name': 'Sonos-looking name',
                    'supported_features': ha.PLAY | ha.PAUSE | ha.PLAY_MEDIA | ha.BROWSE_MEDIA}},
        }
        self.entities = [
            {'entity_id': SONOS, 'platform': 'sonos', 'device_id': 's',
             'config_entry_id': 'sonos-house', 'unique_id': UUID},
            {'entity_id': CAST, 'platform': 'cast', 'device_id': 'c', 'config_entry_id': 'cast-house'}]
        self.devices = [{'id': 's', 'configuration_url': 'http://10.0.0.1:1400/support/review',
                         'model': 'Real Sonos'}]
        self.media = {None: {'children': [
            {'media_content_id': 'provider://album/123', 'media_content_type': 'custom-album',
             'title': 'Opaque album', 'media_class': 'album', 'can_expand': True, 'can_play': True}]},
            'provider://album/123': {'children': [
                {'media_content_id': 'provider://track/456', 'media_content_type': 'custom-track',
                 'title': 'Opaque track', 'media_class': 'track', 'can_expand': False, 'can_play': True}]}}

    def state(self, entity): return self.states.get(entity, {})

    def call(self, payload, timeout):
        if payload['type'] == 'config/entity_registry/list': return self.entities
        if payload['type'] == 'config/device_registry/list': return self.devices
        if payload['type'] == 'media_player/browse_media':
            self.browses.append(payload)
            return self.media[payload.get('media_content_id')]
        raise AssertionError(payload)

    def call_service(self, domain, service, entity, data=None, **kwargs):
        self.calls.append((domain, service, entity, data))
        if service == 'get_queue': return {entity: []}


class Inventory:
    def refresh(self, seeds): return set()
    def identity(self, entity): return None
    def bonded_devices(self, entity): return []


class Listener:
    def subscribe(self, *args): return SimpleNamespace(healthy=True)
    def unsubscribe(self, *args): pass
    def close(self): pass


class Capabilities(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistant()
        self.inputs = SonosLineIn(self.ha, lambda: None, lambda _: None,
                                 listener=Listener(), reader=lambda *args: {}, inventory=Inventory())
        self.assertTrue(self.inputs.refresh())
        self.backend = SimpleNamespace(ha=self.ha, binding=self.inputs.binding, line_in=self.inputs,
                                       content=None, line_in_sources=lambda: [], name='ha')
        self.sonos = ha.HARoom(self.backend, SONOS, lambda _: None)
        self.cast = ha.HARoom(self.backend, CAST, lambda _: None)

    def test_binding_is_cached_registry_identity_separate_from_about(self):
        with patch.object(self.ha, 'call', side_effect=AssertionError('request-thread discovery')):
            binding = self.inputs.binding(SONOS)
            self.assertTrue(binding.is_sonos)
            self.assertTrue(binding.native_sonos)
            self.assertEqual((binding.native_id, binding.native_url), (UUID, 'http://10.0.0.1:1400'))
            self.assertEqual(binding.group_key, ('sonos', 'sonos-house'))
            self.assertEqual(self.inputs.identity(SONOS)['model'], 'Real Sonos')
            self.assertFalse(self.inputs.binding(CAST).is_sonos)
            self.assertEqual(self.inputs.binding('media_player.unknown'), RoomBinding())

    def test_known_sonos_without_native_endpoint_keeps_about_and_ha_routes(self):
        self.ha.entities[0].pop('unique_id')
        self.ha.devices = []
        self.inputs.refresh()
        self.assertTrue(self.inputs.binding(SONOS).is_sonos)
        self.assertFalse(self.inputs.binding(SONOS).native_sonos)
        self.assertEqual(self.inputs.identity(SONOS)['model'], 'Sonos (details unavailable)')
        self.assertIn('queue_browse', self.sonos.operation_capabilities())

    def test_registry_failure_retains_binding_but_removal_withdraws_it(self):
        original = self.inputs.binding(SONOS)
        with patch.object(self.ha, 'call', side_effect=RuntimeError('offline')):
            self.assertFalse(self.inputs.refresh())
        self.assertEqual(self.inputs.binding(SONOS), original)
        self.ha.entities = [self.ha.entities[1]]
        self.inputs.refresh()
        self.assertFalse(self.inputs.binding(SONOS).is_sonos)
        self.assertIsNone(self.inputs.identity(SONOS))

    def test_bridge_synthetic_players_never_gain_a_native_binding(self):
        self.ha.entities[0]['unique_id'] = 'RINCON_02690000000101400'
        self.inputs.refresh()
        self.assertEqual(self.inputs.binding(SONOS), RoomBinding())

    def test_non_sonos_never_calls_sonos_services_or_claims_native_queue(self):
        node = content.Node('id', '', 'Track', 'https://music.invalid/song.mp3', 'music', False, content.TRACK_CLASS)
        actions = [self.cast.queue_length, lambda: self.cast.play_track(1),
                   lambda: self.cast.remove_track(1), lambda: self.cast.enqueue_item(node),
                   lambda: self.cast.play_provider('spotify:track:x', 'music'),
                   lambda: self.cast.enqueue_provider('spotify:track:x', 'music'),
                   lambda: self.cast.play_line_in(SONOS)]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(BackendError): action()
        self.assertEqual(self.ha.calls, [])
        self.assertFalse(self.cast.operation_capabilities() & {'queue_browse', 'queue_play', 'queue_add', 'line_in'})
        self.ha.states[CAST]['attributes']['media_content_id'] = 'x-rincon-stream:' + UUID
        self.assertIsNone(self.cast.line_in_source())

    def test_feature_gates_and_stop_pause_fallback(self):
        self.ha.states[CAST]['attributes']['supported_features'] = 0
        for action in (self.cast.play, self.cast.pause, self.cast.stop, self.cast.next,
                       self.cast.previous, lambda: self.cast.seek(1),
                       lambda: self.cast.send_volume(30), lambda: self.cast.send_mute(True)):
            with self.subTest(action=action), self.assertRaises(BackendError): action()
        self.assertEqual(self.ha.calls, [])
        self.ha.states[CAST]['attributes']['supported_features'] = ha.PAUSE
        self.cast.stop()
        self.assertEqual(self.ha.calls, [('media_player', 'media_pause', CAST, None)])
        self.assertEqual(self.cast.transport_actions(), 'Stop,Pause')

    def test_unavailable_player_has_no_operations_and_cannot_mutate(self):
        self.ha.states[SONOS]['state'] = 'unavailable'
        self.assertEqual(self.sonos.operation_capabilities(), frozenset())
        with self.assertRaises(BackendError): self.sonos.queue_length()
        self.assertEqual(self.ha.calls, [])

    def test_disconnected_ha_keeps_identity_but_disables_cached_playback(self):
        self.ha.connected = threading.Event()
        self.assertTrue(self.sonos.binding.is_sonos)
        self.assertEqual(self.sonos.operation_capabilities(), frozenset())
        self.assertEqual(self.inputs.identity(SONOS)['model'], 'Real Sonos')
        with self.assertRaises(BackendError): self.sonos.play()
        self.assertEqual(self.ha.calls, [])
        self.ha.connected.set()
        self.assertIn('play', self.sonos.operation_capabilities())

    def test_play_mode_does_not_make_partial_changes(self):
        self.ha.states[CAST]['attributes']['supported_features'] = ha.SHUFFLE_SET
        with self.assertRaises(BackendError): self.cast.set_play_mode(True, 'all')
        self.assertEqual(self.ha.calls, [])

    def test_enqueue_requires_its_feature_even_on_sonos(self):
        self.ha.states[SONOS]['attributes']['supported_features'] = ha.PLAY_MEDIA
        with self.assertRaises(BackendError): self.sonos.play_uri('uri', 'music', 'next')
        self.assertNotIn('queue_add', self.sonos.operation_capabilities())
        self.assertEqual(self.ha.calls, [])

    def test_grouping_requires_same_registry_provider_scope(self):
        self.ha.states[CAST]['attributes']['supported_features'] |= ha.GROUPING
        with self.assertRaises(BackendError): self.sonos.join(self.cast)
        self.ha.entities[1].update(platform='sonos', config_entry_id='other-house')
        self.inputs.refresh()
        with self.assertRaises(BackendError): self.sonos.join(self.cast)
        self.ha.entities[1]['config_entry_id'] = 'sonos-house'
        self.inputs.refresh()
        self.sonos.join(self.cast)
        self.assertEqual(self.ha.calls[-1], ('media_player', 'join', SONOS, {'group_members': [CAST]}))
        self.ha.entities[1].pop('config_entry_id')
        self.inputs.refresh()
        with self.assertRaises(BackendError): self.sonos.join(self.cast)

    def test_generic_browser_preserves_opaque_ids_without_sonos_roots(self):
        source = Content(self.ha, binding=self.inputs.binding)
        album = source.children(CAST, 'A:')[0]
        self.assertEqual((album.ref, album.kind), ('provider://album/123', 'custom-album'))
        self.assertNotEqual(album.object_id, album.ref)
        track = source.children(CAST, album.object_id)[0]
        self.assertEqual((track.ref, track.kind), ('provider://track/456', 'custom-track'))
        self.cast.play_item(track)
        self.assertEqual(self.ha.calls[-1], ('media_player', 'play_media', CAST,
            {'media_content_id': 'provider://track/456', 'media_content_type': 'custom-track'}))
        with self.assertRaises(content.ContentError): source.queue(CAST)
        with self.assertRaises(content.ContentError): source.children(CAST, 'SQ:')
        self.assertFalse(any(call[0] == 'sonos' for call in self.ha.calls))

    def test_sonos_catalog_reference_cannot_be_played_on_generic_player(self):
        node = content.Node('SQ:1', 'SQ:', 'Sonos Playlist', 'SQ:1', 'playlist', True, 'object.container')
        with self.assertRaises(BackendError): self.cast.play_item(node)
        self.assertEqual(self.ha.calls, [])

    def test_line_in_withdraws_play_modes_and_keeps_queue_preferences(self):
        self.ha.states[SONOS]['attributes'].update(media_content_id='x-rincon-stream:' + UUID,
                                                 shuffle=True, repeat='all')
        self.assertEqual(self.sonos.valid_play_modes(), 'NORMAL')
        self.assertFalse(self.sonos.operation_capabilities() & {'shuffle', 'repeat', 'seek'})
        with self.assertRaises(BackendError): self.sonos.set_play_mode(True, 'all')
        self.assertEqual(self.ha.calls, [])

    def test_line_in_pause_is_advertised_and_stops_without_losing_source(self):
        self.ha.states[SONOS]['attributes'].update(media_content_id='x-rincon-stream:' + UUID,
                                                 supported_features=ha.PLAY | ha.STOP)
        self.assertEqual(self.sonos.transport_actions(), 'Play,Stop,Pause')
        self.sonos.require('pause')
        self.sonos.pause()
        self.assertEqual(self.ha.calls, [('media_player', 'media_stop', SONOS, None)])
        self.assertEqual(self.sonos.line_in_source(), SONOS)


class ConnectionTransitions(unittest.TestCase):
    def test_identical_reconnect_notifies_capabilities_without_clearing_cached_identity(self):
        import asyncio
        import fake_ha
        from ha_client import HomeAssistant as Client, HAError
        from test_ha_backend import wait_for

        snapshot = fake_ha.sonos_state(SONOS, 'Sonos', 'playing', supported_features=ha.PLAY)
        fake = fake_ha.FakeHA([snapshot])
        client = Client(fake.url, fake_ha.TOKEN, lambda _: None)
        identity = {'id': UUID, 'model': 'Cached Sonos'}
        inputs = SimpleNamespace(start=lambda: None, close=lambda: None,
            binding=lambda entity: RoomBinding(platform='sonos', device_id='cached-device'),
            identity=lambda entity: identity)
        backend = ha.HABackend(client, lambda _: None, line_in=inputs)
        room = backend.room(SONOS)
        seen = []
        backend.listeners.append(lambda entity: seen.append((
            room.operation_capabilities(), client.state(entity), client.connected.is_set(),
            not client.pending, client.ws is None)))

        with patch('ha_client.RECONNECT_DELAY', 0.05):
            client.start()
            try:
                self.assertTrue(wait_for(lambda: any(connected for _, _, connected, _, _ in seen)))
                cached = client.state(SONOS)
                seen.clear()

                async def pending_call():
                    future = client.loop.create_future()
                    client.pending[-1] = future
                    return future
                pending = asyncio.run_coroutine_threadsafe(pending_call(), client.loop).result(5)

                fake.close(); fake = None
                self.assertTrue(wait_for(lambda: any(not connected for _, _, connected, _, _ in seen)))
                disconnected = next(event for event in seen if not event[2])
                self.assertEqual(disconnected[0], frozenset())
                self.assertIs(disconnected[1], cached)
                self.assertTrue(disconnected[3])  # outstanding calls settled before notifying
                self.assertTrue(disconnected[4])
                self.assertTrue(pending.done())
                self.assertIsInstance(pending.exception(), HAError)
                self.assertIs(backend.identity(SONOS), identity)
                seen.clear()

                fake = fake_ha.FakeHA([snapshot])
                client.url = fake.url
                self.assertTrue(wait_for(lambda: any(connected for _, _, connected, _, _ in seen)))
                reconnected = next(event for event in seen if event[2])
                self.assertIn('play', reconnected[0])
                self.assertIs(reconnected[1], cached)  # get_states was byte-for-byte unchanged
                self.assertIs(backend.identity(SONOS), identity)
            finally:
                backend.close()
                if fake is not None: fake.close()


class HTTPRouting(unittest.TestCase):
    """Real SOAP and fake HA WebSocket traffic, including rejection before writes."""

    def setUp(self):
        import fake_ha, zoneplayers
        from ha_client import HomeAssistant as Client
        from test_ha_backend import wait_for
        from test_zoneplayers import HOUSEHOLD, zp
        self.wait_for = wait_for
        other = 'media_player.cast_other'
        flags = ha.PLAY | ha.PAUSE | ha.VOLUME_SET | ha.PLAY_MEDIA | ha.BROWSE_MEDIA | ha.GROUPING
        self.fake = fake_ha.FakeHA([
            fake_ha.sonos_state(CAST, 'Generic speaker', 'playing', supported_features=flags, media_title='Cast track'),
            fake_ha.sonos_state(SONOS, 'Sonos'),
            fake_ha.sonos_state(other, 'Other Cast', 'playing', supported_features=flags)])
        self.fake.platforms.update({CAST: 'cast', other: 'cast'})
        self.fake.config_entries.update({CAST: 'cast-a', other: 'cast-b'})
        self.fake.add_media(fake_ha.media('Generic catalog', '', 'root', 'directory', False, children=[
            fake_ha.media('Opaque album', 'provider://album/123', 'custom-album', 'album', False, children=[
                fake_ha.media('Opaque track', 'provider://track/456', 'custom-track', 'track')])]))
        self.client = Client(self.fake.url, fake_ha.TOKEN, lambda _: None)
        self.client.start()
        self.assertTrue(self.client.connected.wait(5))
        self.backend = ha.HABackend(self.client, lambda _: None)
        provider = SimpleNamespace(
            media_servers=lambda: '<MediaServers/>',
            descriptor=lambda: {'AvailableServiceListVersion': '1', 'AvailableServiceTypeList': '',
                                'AvailableServiceDescriptorList': '<Services/>'},
            owns=lambda uri, metadata='': uri.startswith('test-provider:'),
            resolve_playback=lambda uri, metadata='': ('spotify:track:test', 'music'))
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Cast', 'Sonos', 'Other Cast'], ['127.0.0.1'] * 3,
            lambda _: None, port=0, backends=[self.backend],
            refs=[('ha', CAST), ('ha', SONOS), ('ha', other)], music_service=provider)
        self.house.start('127.0.0.1', ssdp=False)
        self.cast, self.sonos, self.other = self.house.players

    def tearDown(self):
        self.house.close()
        self.backend.close()
        self.fake.close()

    def request(self, player, service, action, args=''):
        from test_zoneplayers import soap
        path = {'AVTransport': '/MediaRenderer/AVTransport/Control',
                'RenderingControl': '/MediaRenderer/RenderingControl/Control',
                'ContentDirectory': '/MediaServer/ContentDirectory/Control'}[service]
        return soap(player, path, service, action, args)

    def test_generic_supported_play_volume_and_opaque_browser(self):
        from test_ha_backend import field
        from test_ha_content import browse, res
        status, body = self.request(self.cast, 'AVTransport', 'GetCurrentTransportActions', '<InstanceID>0</InstanceID>')
        self.assertEqual(status, 200)
        self.assertEqual(field(body, 'Actions'), 'Play,Stop,Pause')
        self.assertEqual(self.request(self.cast, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')[0], 200)
        self.assertEqual(self.request(self.cast, 'RenderingControl', 'SetVolume',
            '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>37</DesiredVolume>')[0], 200)
        self.assertTrue(self.wait_for(lambda: self.fake.states[CAST]['attributes']['volume_level'] == .37))
        _, _, albums = browse(self.cast, 'A:')
        _, _, tracks = browse(self.cast, albums[0].get('id'))
        status, body = self.request(self.cast, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI><CurrentURIMetaData/>' % html.escape(res(tracks[0])))
        self.assertEqual(status, 200, body)
        self.assertIn(('play_media', CAST, {'media_content_id': 'provider://track/456',
                                          'media_content_type': 'custom-track'}), self.fake.calls)
        self.assertFalse(any(domain == 'sonos' and entity == CAST
                             for domain, _, entity, _ in self.fake.service_requests))

    def test_generic_queue_and_sonos_provider_are_rejected_without_calls(self):
        from test_ha_content import browse
        before_uri = self.cast.state.uri
        self.assertEqual(browse(self.cast, 'Q:0')[0], 500)
        for uri in ('https://music.invalid/song.mp3', 'test-provider:track'):
            status, _ = self.request(self.cast, 'AVTransport', 'AddURIToQueue',
                '<InstanceID>0</InstanceID><EnqueuedURI>%s</EnqueuedURI><EnqueuedURIMetaData/>'
                '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>'
                % html.escape(uri))
            self.assertEqual(status, 500)
        status, _ = self.request(self.cast, 'AVTransport', 'SetAVTransportURI',
            '<InstanceID>0</InstanceID><CurrentURI>test-provider:track</CurrentURI><CurrentURIMetaData/>')
        self.assertEqual(status, 500)
        self.assertEqual(self.cast.state.uri, before_uri)
        self.assertFalse(self.fake.service_requests)

    def test_cross_platform_and_different_config_groups_are_rejected(self):
        for target in (self.sonos, self.other):
            status, body = self.request(self.cast, 'AVTransport', 'SetAVTransportURI',
                '<InstanceID>0</InstanceID><CurrentURI>x-rincon:%s</CurrentURI><CurrentURIMetaData/>'
                % target.config.udn)
            self.assertEqual(status, 500, body)
        self.assertFalse(self.fake.service_requests)


if __name__ == '__main__': unittest.main()
