"""Network changes must reconcile real discovery, saved settings and advertisements."""
import copy
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import backends
import fake_sonos
import sonos_backend
import sonos_upnp
import webui
import zoneplayers
from test_ha_backend import wait_for
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

TECHSERV = 'sonos:RINCON_000E5800000B01400'
BEDROOM = 'sonos:RINCON_000E5800000A01400'


def config():
    return {'backend': 'sonos', 'rooms': [
        {'backend': 'sonos', 'id': TECHSERV[6:], 'name': 'TechServ', 'last_known_name': 'TechServ', 'slot': 11}],
        'player_slots': {TECHSERV: 11, BEDROOM: 10}, 'other': {'keep': 'untouched'}}


class NetworkSelection(unittest.TestCase):
    def test_network_round_trip_replaces_rooms_and_keeps_each_identity(self):
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, {BEDROOM: 'Bedroom'})
        self.assertEqual([(webui.room_key(r), r['last_known_name'], r['slot']) for r in value['rooms']],
                         [(BEDROOM, 'Bedroom', 10)])
        self.assertIn(TECHSERV, value['sonos']['room_preferences'])
        self.assertEqual(value['other'], {'keep': 'untouched'})
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'TechServ'})
        self.assertEqual([(webui.room_key(r), r['slot']) for r in value['rooms']], [(TECHSERV, 11)])
        self.assertIn(BEDROOM, value['sonos']['room_preferences'])
        before = copy.deepcopy(value)
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'TechServ'})
        self.assertEqual(value, before)

    def test_disconnected_speakers_are_withdrawn_and_reappear_after_recovery(self):
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, {})
        self.assertEqual(value['rooms'], [])
        webui.Bridge.reconcile_sonos_rooms(value, {}, {TECHSERV: 'TechServ'})
        self.assertEqual(value['rooms'], [])  # stale topology alone cannot resurrect it
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'TechServ'})
        self.assertEqual(value['rooms'][0]['slot'], 11)

    def test_existing_speaker_finishing_its_initial_read_does_not_flap(self):
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, {}, {TECHSERV: 'TechServ'})
        self.assertEqual([webui.room_key(r) for r in value['rooms']], [TECHSERV])

    def test_new_speakers_appear_but_explicitly_hidden_speakers_stay_hidden(self):
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'TechServ'})
        available = {TECHSERV: 'TechServ', BEDROOM: 'Bedroom'}
        webui.Bridge.reconcile_sonos_rooms(value, available)
        self.assertEqual({webui.room_key(r) for r in value['rooms']}, set(available))
        webui.Bridge.apply_rooms(value, [{'key': TECHSERV}], None, 'sonos', available, {})
        webui.Bridge.reconcile_sonos_rooms(value, {BEDROOM: 'Bedroom'})
        self.assertEqual(value['rooms'], [])
        webui.Bridge.reconcile_sonos_rooms(value, available)
        self.assertEqual([webui.room_key(r) for r in value['rooms']], [TECHSERV])

    def test_custom_names_icons_and_library_return_with_their_speaker(self):
        value = config()
        value['rooms'][0].update(name='Office', icon='kitchen')
        value['music_menu'] = {'library': {'backend': 'sonos', 'id': TECHSERV[6:]}, 'other': True}
        webui.Bridge.reconcile_sonos_rooms(value, {BEDROOM: 'Bedroom'})
        self.assertNotIn('library', value['music_menu'])
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'TechServ'})
        self.assertEqual(value['rooms'][0]['name'], 'Office')
        self.assertEqual(value['rooms'][0]['icon'], 'kitchen')
        self.assertEqual(value['music_menu']['library']['id'], TECHSERV[6:])
        self.assertTrue(value['music_menu']['other'])

    def test_source_renames_and_duplicate_room_names_do_not_leave_stale_labels_or_crash(self):
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'Office'})
        self.assertEqual(value['rooms'][0]['last_known_name'], 'Office')
        self.assertNotIn('name', value['rooms'][0])
        webui.Bridge.reconcile_sonos_rooms(value, {TECHSERV: 'Office', BEDROOM: 'Office'})
        names = [r.get('name') or r['last_known_name'] for r in value['rooms']]
        self.assertEqual(len(set(names)), 2)

    def test_manual_source_and_opt_out_are_preserved(self):
        for override in ({'backend': 'ha'}, {'sonos': {'follow_network': False}}):
            value = config(); value.update(override); before = copy.deepcopy(value)
            webui.Bridge.reconcile_sonos_rooms(value, {BEDROOM: 'Bedroom'})
            self.assertEqual(value, before)

    def test_empty_selection_still_connects_sonos_and_detects_future_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            path.write_text(json.dumps({'backend': 'sonos', 'rooms': []}))
            backend = SimpleNamespace(selection_snapshot=lambda: ({BEDROOM: 'Bedroom'}, {}),
                                      display_name=lambda key: 'Bedroom')
            with patch.object(backends, 'load', return_value={'sonos': backend}) as load:
                names, refs, slots, _ = zoneplayers.load_rooms(path, lambda _: None)
            self.assertEqual(load.call_args.args[2], ['sonos'])
            self.assertEqual((names, refs), (['Bedroom'], [('sonos', BEDROOM[6:])]))
            self.assertFalse(zoneplayers.sonos_selection_changed(json.loads(path.read_text()), backend, names, refs, slots))
            backend.selection_snapshot = lambda: ({TECHSERV: 'TechServ'}, {})
            changed = zoneplayers.reconcile_sonos_selection(path, backend, lambda _: None)
            self.assertTrue(zoneplayers.sonos_selection_changed(changed, backend, names, refs, slots))

    def test_discovery_cannot_overwrite_concurrent_user_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'; path.write_text(json.dumps(config()))
            def edit(_):
                def increment(value): value['counter'] = value.get('counter', 0) + 1
                webui.update_config_file(path, increment)
            with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(edit, range(40)))
            value = json.loads(path.read_text())
            self.assertEqual(value['counter'], 40)
            self.assertEqual(value['other'], {'keep': 'untouched'})


class Rediscovery(unittest.TestCase):
    def test_cached_host_failure_falls_back_to_current_network_discovery(self):
        fault = sonos_upnp.UPnPFault('GetZoneGroupState', None, 'old network')
        with patch.object(sonos_upnp, 'topology', side_effect=[fault, ([], {'new': {}})]) as topology, \
             patch.object(sonos_upnp, 'discover', return_value=['http://new:1400']) as discover:
            base, _, zones = sonos_upnp.household(['http://old:1400'], discover_fallback=True)
        self.assertEqual(base, 'http://new:1400')
        self.assertIn('new', zones)
        discover.assert_called_once()
        self.assertEqual(topology.call_args_list[1].args[0], 'http://new:1400')

    def test_explicit_address_test_does_not_silently_test_another_speaker(self):
        with patch.object(sonos_upnp, 'topology', side_effect=sonos_upnp.UPnPFault('Test', None, 'offline')), \
             patch.object(sonos_upnp, 'discover') as discover:
            with self.assertRaises(sonos_upnp.UPnPFault): sonos_upnp.household(['http://old:1400'])
        discover.assert_not_called()

    def test_reload_sends_byebye_for_every_previously_advertised_device(self):
        stop = threading.Event()
        listener, sender = Mock(), Mock()
        def receive(_): stop.set(); raise socket.timeout()
        listener.recvfrom.side_effect = receive
        player = SimpleNamespace(config=SimpleNamespace(udn='old', advertise_ip='127.0.0.1'),
                                 request_context=lambda address: nullcontext(),
                                 ssdp_targets=lambda: ['uuid:old', 'upnp:rootdevice'],
                                 ssdp_packet=lambda target, alive: b'NTS: ssdp:alive\r\n' + target.encode())
        house = zoneplayers.Household.__new__(zoneplayers.Household)
        house.devices = lambda: [player]
        house.zp = SimpleNamespace(SSDP_GROUP='239.255.255.250')
        house.ssdp_port, house.discovery_generation = 1900, 0
        with patch.object(zoneplayers.socket, 'socket', side_effect=[listener, sender]):
            house.ssdp_loop('127.0.0.1', stop)
        packets = [call.args[0] for call in sender.sendto.call_args_list]
        self.assertEqual(sum(b'NTS: ssdp:byebye' in packet for packet in packets), 2)
        sender.close.assert_called_once()


class PhysicalRouting(unittest.TestCase):
    def test_slow_s1_position_read_keeps_the_selected_room_available(self):
        network = fake_sonos.FakeHousehold()
        self.addCleanup(network.close)
        real = network.add(BEDROOM[6:], 'Bedroom')
        original = real.do_GetPositionInfo

        def slow_position(args):
            time.sleep(5)
            return original(args)

        real.do_GetPositionInfo = slow_position
        backend = sonos_backend.SonosBackend(log=lambda _: None)
        backend.listener.start()
        self.addCleanup(backend.close)
        speaker = sonos_backend.Speaker(real.uuid, real.base, real.name)
        backend.speakers[real.uuid] = speaker
        backend.groups = [(real.uuid, [real.uuid])]
        backend.read(speaker)
        self.assertTrue(speaker.available)
        available, pending = backend.selection_snapshot()
        self.assertIn(BEDROOM, available)
        value = config()
        webui.Bridge.reconcile_sonos_rooms(value, available, pending)
        self.assertEqual([webui.room_key(room) for room in value['rooms']], [BEDROOM])
        self.assertEqual(webui.sonos_playing(real.base)[0], 'idle')

    def test_moving_networks_changes_advertised_topology_and_control_target(self):
        network_a, network_b = fake_sonos.FakeHousehold(), fake_sonos.FakeHousehold()
        self.addCleanup(network_a.close); self.addCleanup(network_b.close)
        techserv = network_a.add(TECHSERV[6:], 'TechServ', volume=12)
        bedroom = network_b.add(BEDROOM[6:], 'Bedroom', volume=35)
        backend = sonos_backend.SonosBackend([techserv.base], log=lambda _: None)
        backend.start(); self.addCleanup(backend.close)
        self.assertTrue(backend.ready.wait(5))
        self.assertTrue(wait_for(lambda: backend.speaker(TECHSERV[6:]).available is True))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'; path.write_text(json.dumps(config()))
            def load_house():
                with patch.object(backends, 'load', return_value={'sonos': backend}):
                    names, refs, slots, _ = zoneplayers.load_rooms(path, lambda _: None)
                house = zoneplayers.Household(zp, HOUSEHOLD, names, ['127.0.0.1']*len(names),
                                             lambda _: None, port=0, refs=refs, backends=[backend], slots=slots)
                house.start('127.0.0.1', ssdp=False)
                return house, names, refs, slots
            first, names, refs, slots = load_house()
            original_uuid = first.players[0].config.udn
            first.close()
            backend.apply_topology([(BEDROOM[6:], [BEDROOM[6:]])],
                                   {BEDROOM[6:]: {'base': bedroom.base, 'name': 'Bedroom', 'icon': ''}})
            backend.read(backend.speaker(BEDROOM[6:]))
            changed = zoneplayers.reconcile_sonos_selection(path, backend, lambda _: None)
            self.assertTrue(zoneplayers.sonos_selection_changed(changed, backend, names, refs, slots))
            current, _, _, _ = load_house()
            try:
                self.assertEqual(groups(current.topology()), [['Bedroom']])
                self.assertNotEqual(current.players[0].config.udn, original_uuid)
                status, _ = soap(current.players[0], '/MediaRenderer/RenderingControl/Control',
                                 'RenderingControl', 'SetVolume',
                                 '<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>42</DesiredVolume>')
                self.assertEqual(status, 200)
                self.assertTrue(wait_for(lambda: bedroom.volume == 42))
                self.assertEqual(techserv.volume, 12)
                self.assertTrue(any(action == 'SetVolume' for action, _ in bedroom.calls))
            finally: current.close()


if __name__ == '__main__': unittest.main()
