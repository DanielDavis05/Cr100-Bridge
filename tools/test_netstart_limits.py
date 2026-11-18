"""Bound pairing work and memory while preserving valid retransmissions."""
import errno
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import netstart_pair as pair
from test_bridge_service import STATE


FIRST, SECOND, THIRD = ('02:00:00:00:00:02', '02:00:00:00:00:03', '02:00:00:00:00:04')


class PairingLimits(unittest.TestCase):
    def pairer(self, **settings):
        return pair.Pairer(pair.player_uuid(STATE['bssid']), STATE['household'].encode(),
                           STATE['freq'], log=lambda _: None, **settings)

    def query(self, pairer, txid):
        return pair.encode(pair.QUERY, [(0, pairer.uuid), (1, struct.pack('!I', txid)),
                                      (3, pair.PLAYER_PUBLIC_KEY)])

    def test_response_cache_is_bounded_and_retransmissions_keep_the_cached_ciphertext(self):
        pairer = self.pairer(response_limit=3)
        first = pairer.handle(FIRST, self.query(pairer, 0))
        for txid in (1, 2): pairer.handle(FIRST, self.query(pairer, txid))
        self.assertEqual(pairer.handle(FIRST, self.query(pairer, 0)), first)
        pairer.handle(FIRST, self.query(pairer, 3))
        self.assertEqual(len(pairer.responses), 3)
        self.assertNotIn((FIRST, struct.pack('!I', 1)), pairer.responses)
        self.assertEqual(pairer.handle(FIRST, self.query(pairer, 0)), first)
        self.assertEqual(pairer.cache_evictions, 1)

    def test_distinct_query_flood_cannot_grow_cache_or_controller_map_without_bound(self):
        pairer = self.pairer(response_limit=8, controller_limit=2)
        with patch.object(pair, 'rsa_encrypt_key', return_value=bytes(128)) as encrypt:
            for txid in range(1000): pairer.handle(FIRST, self.query(pairer, txid))
            pairer.handle(SECOND, self.query(pairer, 1))
            calls = encrypt.call_count
            self.assertEqual(pairer.handle(THIRD, self.query(pairer, 1)), [])
            self.assertEqual(encrypt.call_count, calls)
            self.assertTrue(pairer.handle(FIRST, self.query(pairer, 1001)))
        self.assertEqual(len(pairer.responses), 8)
        self.assertEqual(set(pairer.paired_controllers), {FIRST, SECOND})
        self.assertEqual(pairer.dropped_controllers, 1)

    def test_pending_queue_coalesces_duplicates_and_preserves_fifo_at_its_limit(self):
        pending = pair.PendingReplies(limit=2)
        pending.put_many([b'first', b'first', b'second', b'third'])
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending.dropped, 1)
        sent = []
        pending.drain(sent.append, 10, clock=lambda: 0)
        self.assertEqual(sent, [b'first', b'second'])
        self.assertEqual(len(pending), 0)

    def test_pending_payload_memory_is_bounded(self):
        pending = pair.PendingReplies(limit=3)
        pending.put_many([b'', b'x' * (pair.MAX_NETSTART_PAYLOAD + 1), 'not bytes'])
        self.assertEqual(pending.dropped, 3)
        self.assertEqual(len(pending), 0)
        pending.put_many([str(index).encode() for index in range(1000)])
        self.assertEqual(len(pending), 3)

    def test_replenished_queue_yields_after_a_fixed_number_of_attempts(self):
        pending = pair.PendingReplies(limit=2)
        pending.put_many([b'repeated'])
        sent = []
        def flood(payload):
            sent.append(payload)
            pending.put_many([payload])
        attempted = pending.drain(flood, 10, limit=3, clock=lambda: 0)
        self.assertEqual(attempted, 3)
        self.assertEqual(sent, [b'repeated'] * 3)
        self.assertEqual(len(pending), 1)

    def test_reply_drain_checks_global_or_linger_deadline_and_tick_budget(self):
        for deadline, budget in ((.04, 1), (10, .04)):
            with self.subTest(deadline=deadline, budget=budget):
                pending = pair.PendingReplies()
                pending.put_many([b'repeated'])
                now, sent = [0.0], []
                def send(payload):
                    sent.append(payload)
                    now[0] += .03
                    pending.put_many([payload])
                self.assertEqual(pending.drain(send, deadline, budget=budget, clock=lambda: now[0]), 2)
                self.assertEqual(len(sent), 2)
        pending = pair.PendingReplies()
        pending.put_many([b'old'])
        self.assertEqual(pending.drain(lambda _: self.fail('sent after deadline'), 1, clock=lambda: 1), 0)

    def test_netstart_reassembly_rejects_truncated_or_oversized_ie_payloads(self):
        valid = pair.frame_body(b'payload')
        self.assertEqual(pair.parse_body(valid), b'payload')
        self.assertIsNone(pair.parse_body(valid[:-1]))
        self.assertIsNone(pair.parse_body(valid + b'\xdd'))
        start = bytes((0, len(pair.NETSTART_SSID))) + pair.NETSTART_SSID
        oversized = start + (b'\x46\xff' + bytes(255)) * 5
        self.assertIsNone(pair.parse_body(oversized))


class PairingLoopDeadlines(unittest.TestCase):
    def test_sniffer_start_failure_closes_the_socket_once(self):
        sock, sniffer = Mock(), Mock()
        sniffer.start.side_effect = RuntimeError('synthetic sniffer start failure')
        scapy = types.ModuleType('scapy'); scapy.__path__ = []
        scapy_all = types.ModuleType('scapy.all')
        for name, value in {'AsyncSniffer': lambda **_: sniffer, 'Dot11': object,
                            'Dot11ProbeReq': object, 'RadioTap': object, 'Raw': object,
                            'conf': types.SimpleNamespace(L2socket=lambda **_: sock)}.items():
            setattr(scapy_all, name, value)
        args = types.SimpleNamespace(state='unused-synthetic-state', iface='synthetic0')
        with patch.dict(sys.modules, {'scapy': scapy, 'scapy.all': scapy_all}), \
                patch.object(pair, 'initial_state', return_value=dict(STATE)), \
                self.assertRaisesRegex(RuntimeError, 'sniffer start failure'):
            pair.run(args)
        sock.close.assert_called_once_with()
        sniffer.stop.assert_not_called()

    def run_flooded_pairing(self, *, seconds, linger, busy):
        """Run the real pairing loop with a replenished queue and no radio."""
        class Layer:
            def __init__(self, *args, **kwargs): pass
            def __truediv__(self, other): return self
        class Wifi(Layer): pass
        class Probe(Layer): pass
        query = pair.encode(pair.QUERY, [(0, pair.player_uuid(STATE['bssid'])),
            (1, struct.pack('!I', 5)), (3, pair.PLAYER_PUBLIC_KEY)])
        class Incoming:
            def haslayer(self, layer): return layer is Probe
            def __getitem__(self, layer):
                return types.SimpleNamespace(addr2=FIRST) if layer is Wifi else \
                    types.SimpleNamespace(payload=pair.frame_body(query))
        callbacks, now = {}, [0.0]
        sniffer, sock = Mock(), Mock()
        def sniffer_factory(**options):
            callbacks['receive'] = options['prn']
            sniffer.start.side_effect = lambda: callbacks['receive'](Incoming())
            return sniffer
        def transmit(_):
            now[0] += .003
            # Even after every send attempt, input keeps the queue nonempty.
            callbacks['receive'](Incoming())
            if busy: raise OSError(errno.EAGAIN, 'synthetic busy injection')
        sock.send.side_effect = transmit
        scapy = types.ModuleType('scapy')
        scapy.__path__ = []
        scapy_all = types.ModuleType('scapy.all')
        for name, value in {'AsyncSniffer': sniffer_factory, 'Dot11': Wifi, 'Dot11ProbeReq': Probe,
                            'RadioTap': Layer, 'Raw': Layer,
                            'conf': types.SimpleNamespace(L2socket=lambda **_: sock)}.items():
            setattr(scapy_all, name, value)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            args = types.SimpleNamespace(state=str(state_path), iface='synthetic0', seconds=seconds,
                                         linger=linger, alive_interval=.01)
            with patch.dict(sys.modules, {'scapy': scapy, 'scapy.all': scapy_all}), \
                    patch.object(pair, 'initial_state', return_value=dict(STATE)), \
                    patch.object(pair.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(pair.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)), \
                    patch('builtins.print'):
                pair.run(args)
            saved = json.loads(state_path.read_text())
            self.assertNotIn('network_key', saved)
            self.assertIn(FIRST, {record['mac'] for record in saved['controllers']})
            self.assertTrue(all(len(bytes.fromhex(record['key'])) == 16 for record in saved['controllers']))
        sock.outs.setblocking.assert_called_once_with(False)
        sock.close.assert_called_once_with()
        sniffer.stop.assert_called_once_with()
        return now[0], sock.send.call_count

    def test_input_flood_cannot_starve_the_global_pairing_deadline(self):
        for busy in (False, True):
            with self.subTest(busy=busy):
                stopped_at, attempts = self.run_flooded_pairing(seconds=.05, linger=1, busy=busy)
                self.assertLessEqual(stopped_at, .061)
                self.assertLessEqual(attempts, 20)

    def test_input_flood_cannot_starve_the_post_pairing_linger_deadline(self):
        for busy in (False, True):
            with self.subTest(busy=busy):
                stopped_at, attempts = self.run_flooded_pairing(seconds=1, linger=.02, busy=busy)
                self.assertLessEqual(stopped_at, .026)
                self.assertLessEqual(attempts, 8)


if __name__ == '__main__': unittest.main()
