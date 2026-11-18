"""Independent CR100 stations share one radio, without sharing traffic or sleep state."""
import socket
import struct
import time
import unittest
from unittest.mock import patch

import ccmp
import frames
import sonosnet_link as sl


STATE = {'bssid': '02:00:00:00:00:01', 'household': 'Sonos_test', 'freq': 2437,
         'controllers': [{'mac': '02:00:00:00:00:02', 'aid': 1, 'key': '0123456789abcdef0123456789abcdef'},
                         {'mac': '02:00:00:00:00:03', 'aid': 65, 'key': 'fedcba9876543210fedcba9876543210'}]}
AP = frames.mac_bytes(STATE['bssid'])
FIRST, SECOND = (frames.mac_bytes(record['mac']) for record in STATE['controllers'])
# Each controller's own link key, by its MAC.
KEYS = {frames.mac_bytes(record['mac']): bytes.fromhex(record['key']) for record in STATE['controllers']}
OTHER = frames.mac_bytes('02:00:00:00:00:99')


class Radio:
    def __init__(self): self.sent = []
    def send(self, packet, flags=0):
        if not flags & socket.MSG_DONTWAIT:
            raise AssertionError('Radio writes must not block the main loop')
        self.sent.append(packet[len(frames.TX_RADIOTAP):])


def ethernet(destination, marker=b'payload', source=AP):
    return destination + source + b'\x08\x06' + marker


def management(mac, subtype, body=b'', receiver=AP, bssid=AP):
    return bytes((subtype << 4, 0)) + b'\0\0' + receiver + mac + bssid + b'\x10\x00' + body


def poll(mac, aid, receiver=AP):
    return b'\xa4\x11' + struct.pack('<H', 0xc000 | aid) + receiver + mac


def null(mac, pm=True, receiver=AP, bssid=AP):
    return bytes((0x48, 0x01 | (0x10 if pm else 0))) + b'\0\0' + receiver + mac + bssid + b'\x20\x00'


def data(mac, inner, pn, receiver=AP, bssid=AP, key=None):
    header = b'\x08\x51\0\0' + receiver + mac + bssid + b'\x30\x00'
    return ccmp.encrypt(header, ccmp.SNAP_PREFIX + b'\x69\x69' + inner, key or KEYS[mac], pn)


def decode(frame, key=None):
    """Decrypt a frame the link sent, with its destination's key unless one is given."""
    pn, key_id, plaintext = ccmp.decrypt(frame, key or KEYS[frame[4:10]])
    if key_id != 2 or plaintext[:8] != ccmp.SNAP_PREFIX + b'\x69\x69':
        raise AssertionError('Unexpected SonosNet data envelope')
    return frame[4:10], pn, bool(frame[1] & 0x20), plaintext[8:]


def tim_aids(beacon_body):
    """Decode the TIM, including a nonzero partial-virtual-bitmap offset."""
    offset = 12
    while offset + 2 <= len(beacon_body):
        tag, size = beacon_body[offset:offset + 2]
        value = beacon_body[offset + 2:offset + 2 + size]
        if tag == 5:
            first_byte = value[2] & 0xfe
            return {(first_byte + index) * 8 + bit
                    for index, byte in enumerate(value[3:]) for bit in range(8) if byte & (1 << bit)}
        offset += 2 + size
    raise AssertionError('Beacon has no TIM')


class MultiControllerLink(unittest.TestCase):
    def setUp(self):
        tap, self.tap = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(tap.close)
        self.addCleanup(self.tap.close)
        self.tap.setblocking(False)
        self.radio, self.logs = Radio(), []
        self.link = sl.Link(STATE, self.radio, tap.fileno(), self.logs.append)
        # Existing link tests exercise real pacing; these exercise station behavior
        # deterministically, including several polls at the same test-clock instant.
        self.pacer = patch.object(sl, 'PACE_WINDOW', 0)
        self.pacer.start()
        self.addCleanup(self.pacer.stop)

    def associate(self, mac):
        self.link.on_frame(management(mac, 0, bytes.fromhex('1003f401dd06000e58020101')))
        return self.link.stations[mac]

    def doze_both(self):
        for mac in (FIRST, SECOND):
            self.associate(mac)
            self.link.on_frame(null(mac))
        self.radio.sent.clear()

    def test_association_ids_and_tim_cover_both_stations(self):
        for mac, aid in ((FIRST, 1), (SECOND, 65)):
            station = self.associate(mac)
            response = self.radio.sent[-1]
            self.assertEqual(response[4:10], mac)
            self.assertEqual(struct.unpack_from('<H', response, 28)[0], 0xc000 | aid)
            self.assertTrue(station.associated)
            self.link.on_frame(null(mac))
        self.link.from_tap(ethernet(frames.BROADCAST))
        self.link.send_beacon()
        self.assertEqual(tim_aids(self.radio.sent[-1][24:]), {1, 65})
        self.link.on_frame(poll(FIRST, 1))
        self.link.send_beacon()
        self.assertEqual(tim_aids(self.radio.sent[-1][24:]), {65})
        associated = [record['mac'] for record in self.logs if record['event'] == 'associated']
        self.assertEqual(associated, [frames.mac_text(FIRST), frames.mac_text(SECOND)])

    def test_unicast_is_routed_and_group_traffic_is_replicated(self):
        self.doze_both()
        a, b = self.link.stations[FIRST], self.link.stations[SECOND]
        first = ethernet(FIRST, b'first-only')
        second = ethernet(SECOND, b'second-only')
        broadcast = ethernet(frames.BROADCAST, b'broadcast')
        multicast = ethernet(bytes.fromhex('01005e7ffffa'), b'SSDP')
        self.link.from_tap(first)
        self.link.from_tap(second)
        self.link.from_tap(ethernet(OTHER, b'unknown-unicast'))
        self.link.from_tap(broadcast)
        self.link.from_tap(multicast)
        self.assertEqual([inner for _, inner in a.queue], [first, broadcast, multicast])
        self.assertEqual([inner for _, inner in b.queue], [second, broadcast, multicast])
        self.assertEqual(self.radio.sent, [])
        self.link.on_frame(null(FIRST, pm=False))
        self.link.on_frame(null(SECOND, pm=False))
        sent = [decode(frame) for frame in self.radio.sent]
        self.assertEqual([inner for dst, _, _, inner in sent if dst == FIRST], [first, broadcast, multicast])
        self.assertEqual([inner for dst, _, _, inner in sent if dst == SECOND], [second, broadcast, multicast])

    def test_unassociated_controller_gets_no_group_traffic(self):
        self.associate(FIRST)
        self.link.on_frame(null(FIRST))
        self.radio.sent.clear()
        self.link.from_tap(ethernet(frames.BROADCAST))
        self.assertEqual(len(self.link.stations[FIRST].queue), 1)
        self.assertEqual(len(self.link.stations[SECOND].queue), 0)
        self.assertFalse(self.link.stations[SECOND].associated)

    def test_ps_poll_and_more_data_are_local_to_the_polling_station(self):
        self.doze_both()
        first = [ethernet(FIRST, marker) for marker in (b'first-0', b'first-1')]
        second = ethernet(SECOND, b'second-0')
        for inner in (*first, second): self.link.from_tap(inner)
        for mac, aid in ((FIRST, 1), (SECOND, 65), (FIRST, 1)):
            self.link.on_frame(poll(mac, aid))
        sent = [decode(frame) for frame in self.radio.sent]
        self.assertEqual([(dst, more, inner) for dst, _, more, inner in sent],
                         [(FIRST, True, first[0]), (SECOND, False, second), (FIRST, False, first[1])])
        for station in self.link.stations.values():
            self.assertTrue(station.dozing, 'PS-Poll must not wake a sleeping controller')
            self.assertFalse(station.queue)

    def test_stock_bare_aid_polls_and_standard_polls_release_only_their_own_queue(self):
        self.doze_both()
        first, second = self.link.stations[FIRST], self.link.stations[SECOND]
        first_packets = [ethernet(FIRST, marker) for marker in (b'first-0', b'first-1')]
        second_packets = [ethernet(SECOND, marker) for marker in (b'second-0', b'second-1')]
        for inner in (*first_packets, *second_packets): self.link.from_tap(inner)

        def bare_poll(mac, aid):
            # Stock CR100 captures carry 0x0001, rather than standard 0xc001,
            # in Duration/AID. Higher association IDs must work the same way.
            return poll(mac, aid)[:2] + struct.pack('<H', aid) + poll(mac, aid)[4:]

        first.last_rx, second.last_rx = 12.0, 13.0
        first.check_from = second.check_from = 10.0
        first.searching = second.searching = True
        # A controller presenting the other station's bare AID cannot consume
        # either station's traffic or count as a valid hearing-check response.
        self.link.on_frame(bare_poll(FIRST, 65))
        self.link.on_frame(bare_poll(SECOND, 1))
        self.assertEqual(self.radio.sent, [])
        self.assertEqual((first.last_rx, second.last_rx), (12.0, 13.0))
        for station in (first, second):
            self.assertEqual(len(station.queue), 2)
            self.assertEqual(station.check_from, 10.0)
            self.assertTrue(station.searching)

        for frame in (bare_poll(FIRST, 1), bare_poll(SECOND, 65), poll(FIRST, 1), poll(SECOND, 65)):
            self.link.on_frame(frame)
        sent = [decode(frame) for frame in self.radio.sent]
        self.assertEqual([(dst, more, inner) for dst, _, more, inner in sent],
                         [(FIRST, True, first_packets[0]), (SECOND, True, second_packets[0]),
                          (FIRST, False, first_packets[1]), (SECOND, False, second_packets[1])])
        for station in (first, second):
            self.assertFalse(station.queue)
            self.assertFalse(station.searching)
            self.assertIsNone(station.check_from)
            self.assertTrue(station.dozing)

    def test_waking_one_controller_does_not_release_the_others_queue(self):
        self.doze_both()
        packet = ethernet(frames.BROADCAST)
        self.link.from_tap(packet)
        self.link.on_frame(null(FIRST, pm=False))
        self.assertEqual([decode(frame)[0] for frame in self.radio.sent], [FIRST])
        self.assertFalse(self.link.stations[FIRST].queue)
        self.assertEqual([inner for _, inner in self.link.stations[SECOND].queue], [packet])
        self.assertTrue(self.link.stations[SECOND].dozing)

    def test_receive_replay_counters_and_restart_resume_are_per_controller(self):
        # Both controllers can already be associated when the process restarts, and
        # can independently use the same incoming PN under their distinct MAC nonce.
        packets = {mac: ethernet(AP, mac, source=mac) for mac in (FIRST, SECOND)}
        for mac in (FIRST, SECOND): self.link.on_frame(data(mac, packets[mac], 100))
        self.assertEqual(self.tap.recv(4096), packets[FIRST])
        self.assertEqual(self.tap.recv(4096), packets[SECOND])
        for mac in (FIRST, SECOND):
            self.assertTrue(self.link.stations[mac].associated)
            self.assertEqual(self.link.stations[mac].rx_last_pn, 100)
        self.link.on_frame(data(FIRST, packets[FIRST], 100))
        self.link.on_frame(data(SECOND, packets[SECOND], 101))
        self.assertEqual(self.tap.recv(4096), packets[SECOND])
        with self.assertRaises(BlockingIOError): self.tap.recv(4096)
        self.assertEqual(self.link.stations[FIRST].rx_last_pn, 100)
        self.assertEqual(self.link.stations[SECOND].rx_last_pn, 101)
        self.assertEqual(self.link.stats['rx_replay'], 1)

    def test_transmit_packet_numbers_are_global_under_the_shared_ap_key(self):
        self.doze_both()
        self.link.tx_pn = 1000
        for mac, aid in ((FIRST, 1), (SECOND, 65), (FIRST, 1), (SECOND, 65)):
            self.link.from_tap(ethernet(mac))
            self.link.on_frame(poll(mac, aid))
        sent = [decode(frame) for frame in self.radio.sent]
        self.assertEqual([dst for dst, _, _, _ in sent], [FIRST, SECOND, FIRST, SECOND])
        self.assertEqual([pn for _, pn, _, _ in sent], [1001, 1002, 1003, 1004])

    def test_other_ap_frames_and_wrong_aid_do_not_touch_station_state(self):
        self.doze_both()
        station = self.link.stations[FIRST]
        station.last_rx, station.check_from, station.searching = 12.0, 10.0, True
        self.link.from_tap(ethernet(FIRST))
        invalid = [poll(FIRST, 65), poll(FIRST, 1, receiver=OTHER),
                   null(FIRST, pm=False, receiver=OTHER),
                   management(FIRST, 10, b'\x03\0', receiver=OTHER, bssid=OTHER),
                   data(FIRST, ethernet(AP, source=FIRST), 100, receiver=OTHER)]
        for frame in invalid: self.link.on_frame(frame)
        self.assertEqual(self.radio.sent, [])
        self.assertEqual(len(station.queue), 1)
        self.assertTrue(station.associated)
        self.assertTrue(station.dozing)
        self.assertEqual(station.last_rx, 12.0)
        self.assertEqual(station.check_from, 10.0)
        self.assertTrue(station.searching)
        self.assertEqual(station.rx_last_pn, -1)
        with self.assertRaises(BlockingIOError): self.tap.recv(4096)

    def test_tods_destination_address_is_not_mistaken_for_bssid(self):
        # For ToDS data, Addr1 identifies the AP; Addr3 identifies the destination.
        # Captured CR100 tunnels use the AP in both fields, but it need not be so.
        packet = ethernet(OTHER, source=FIRST)
        self.link.on_frame(data(FIRST, packet, 100, bssid=OTHER))
        self.assertEqual(self.tap.recv(4096), packet)
        self.assertTrue(self.link.stations[FIRST].associated)
        self.link.from_tap(ethernet(FIRST))
        self.link.on_frame(null(FIRST, pm=False, bssid=OTHER))
        self.assertEqual(decode(self.radio.sent[-1])[0], FIRST)

    def test_departure_and_hearing_checks_do_not_change_the_other_station(self):
        self.doze_both()
        first, second = self.link.stations[FIRST], self.link.stations[SECOND]
        now = time.monotonic()
        first.last_rx, first.check_from = now - 20, now - sl.CHECK_WINDOW - 1
        second.last_rx = now
        with self.link.lock:
            self.link.check_hearing(first, now)
            self.link.check_hearing(second, now)
        self.assertTrue(first.searching)
        self.assertFalse(second.searching)
        self.assertEqual(self.logs[-1]['mac'], frames.mac_text(FIRST))
        self.link.on_frame(management(FIRST, 10, b'\x03\0'))
        self.assertFalse(first.associated)
        self.assertTrue(second.associated)
        self.link.from_tap(ethernet(SECOND))
        self.link.on_frame(poll(SECOND, 65))
        self.assertEqual(decode(self.radio.sent[-1])[0], SECOND)

    def test_transmit_pacing_is_shared_between_controller_queues(self):
        self.doze_both()
        self.link.tx_times.clear()
        with patch.object(sl, 'PACE_WINDOW', .01), patch.object(sl.time, 'monotonic', return_value=100) as clock:
            self.link.from_tap(ethernet(frames.BROADCAST))
            self.link.on_frame(poll(FIRST, 1))
            self.link.on_frame(poll(SECOND, 65))
            self.assertEqual([decode(frame)[0] for frame in self.radio.sent], [FIRST])
            self.assertEqual(len(self.link.stations[SECOND].queue), 1)
            clock.return_value = 100.02
            self.link.on_frame(poll(SECOND, 65))
        self.assertEqual([decode(frame)[0] for frame in self.radio.sent], [FIRST, SECOND])

    def test_awake_queues_take_turns_on_the_shared_radio(self):
        self.doze_both()
        self.link.tx_times.clear()
        with patch.object(sl, 'PACE_WINDOW', .0015), patch.object(sl.time, 'monotonic', return_value=100) as clock:
            for _ in range(4): self.link.from_tap(ethernet(frames.BROADCAST))
            for station in self.link.stations.values():
                station.dozing = False
                station.last_pm_rx = station.last_rx = 100
            for tick in range(4):
                clock.return_value = 100 + .002 * tick
                with self.link.lock: self.link.service_stations(clock.return_value)
        self.assertEqual([decode(frame)[0] for frame in self.radio.sent], [FIRST, SECOND, FIRST, SECOND])
        self.assertEqual([len(station.queue) for station in self.link.stations.values()], [2, 2])

    def test_stats_report_every_controller_independently(self):
        self.doze_both()
        self.link.on_frame(null(FIRST, pm=False))
        self.link.start = time.monotonic()
        with patch.object(self.link, 'radio_loop'), patch.object(self.link, 'tap_loop'):
            self.link.run(seconds=.02)
        stats = next(record for record in self.logs if record['event'] == 'stats')
        stations = {station['mac']: station for station in stats['stations']}
        self.assertEqual(set(stations), {frames.mac_text(FIRST), frames.mac_text(SECOND)})
        self.assertFalse(stations[frames.mac_text(FIRST)]['dozing'])
        self.assertTrue(stations[frames.mac_text(SECOND)]['dozing'])


class MultiControllerTIM(unittest.TestCase):
    def test_bitmap_covers_low_and_high_aids_without_setting_group_bit(self):
        self.assertEqual(tim_aids(frames.beacon_body(0, 6, [1, 65, 2007])), {1, 65, 2007})
        self.assertEqual(tim_aids(frames.beacon_body(0, 6, 1)), {1})
        self.assertEqual(tim_aids(frames.beacon_body(0, 6)), set())


if __name__ == '__main__': unittest.main()
