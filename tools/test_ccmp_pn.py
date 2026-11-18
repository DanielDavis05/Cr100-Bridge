"""Durable CCMP reservations prevent counter reuse across restarts and clock rollback."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import ccmp_pn as pn

BSSID = bytes.fromhex('020000000001')
FINGERPRINT = hashlib.sha256(b'cr100-bridge-tx:' + BSSID).hexdigest()


class PacketNumberReservations(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'bridge-state.pn.json'

    def allocator(self, **kwargs):
        options = {'minimum': 0, 'block_size': 4, **kwargs}
        return pn.PacketNumbers(self.path, BSSID, **options)

    def state(self): return json.loads(self.path.read_text())

    def test_restart_abandons_the_entire_previously_reserved_block(self):
        with self.allocator() as first:
            self.assertEqual(first.initial, 0)
            first.ensure(1)
            self.assertEqual(self.state()['high_water'][FINGERPRINT], 4)
        with self.allocator() as second:
            self.assertEqual(second.initial, 4)
            second.ensure(5)
        with self.allocator() as third:
            self.assertEqual(third.initial, 8)
        # Even a lifetime that emits no frames must not reuse its reservation.
        with self.allocator() as fourth: self.assertEqual(fourth.initial, 12)

    def test_default_clock_bootstrap_cannot_lower_an_existing_high_water_mark(self):
        with patch.object(pn.time, 'time', return_value=1000):
            with self.allocator(minimum=None) as first:
                self.assertEqual(first.initial, 1000000)
                first.ensure(first.initial + 1)
        with patch.object(pn.time, 'time', return_value=1):
            with self.allocator(minimum=None) as after_rollback:
                self.assertEqual(after_rollback.initial, 1000004)
                after_rollback.ensure(after_rollback.initial + 1)
        with patch.object(pn.time, 'time', return_value=2000):
            with self.allocator(minimum=None) as after_forward_jump:
                self.assertEqual(after_forward_jump.initial, 2000000)

    def test_in_range_packets_need_no_write_and_crossing_range_reserves_first(self):
        with self.allocator() as allocator:
            with patch.object(pn.os, 'fsync', side_effect=AssertionError('In-range PN performed disk I/O')):
                for value in range(1, 5): allocator.ensure(value)
            allocator.ensure(5)
            self.assertEqual(self.state()['high_water'][FINGERPRINT], 8)
            allocator.ensure(14)
            self.assertEqual(self.state()['high_water'][FINGERPRINT], 16)

    def test_changing_bssids_preserves_previous_reservations(self):
        with self.allocator() as allocator: allocator.ensure(1)
        other_bssid, third_bssid = bytes.fromhex('020000000099'), bytes.fromhex('0200000000aa')
        with pn.PacketNumbers(self.path, other_bssid, minimum=10, block_size=4) as allocator:
            self.assertEqual(allocator.initial, 10)
            allocator.ensure(11)
        with pn.PacketNumbers(self.path, third_bssid, minimum=20, block_size=4) as allocator:
            self.assertEqual(allocator.initial, 20)
            allocator.ensure(21)
        before = self.state()['high_water']
        self.assertEqual(len(before), 3)
        with self.allocator() as original:
            self.assertEqual(original.initial, 4)
            original.ensure(5)
        after = self.state()['high_water']
        self.assertEqual(set(after), set(before))
        for fingerprint, value in before.items():
            self.assertEqual(after[fingerprint], 8 if fingerprint == FINGERPRINT else value)

    def test_lifetime_lock_survives_state_replacement_and_releases_on_close(self):
        first = self.allocator()
        self.addCleanup(first.close)
        first.ensure(5)  # Atomic replacement must not replace the locked inode.
        with self.assertRaises(BlockingIOError): self.allocator()
        first.close()
        first.close()
        with self.allocator() as second: self.assertEqual(second.initial, 8)

    def test_state_and_lock_are_private_even_after_existing_permissions_widen(self):
        with self.allocator(): pass
        lock = Path(str(self.path) + '.lock')
        self.path.chmod(0o666)
        lock.chmod(0o666)
        with self.allocator():
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_corrupt_state_is_not_reset_or_overwritten_and_releases_its_lock(self):
        corrupt = [b'', b'{broken', b'[]', b'null', b'{"version":2,"high_water":{}}',
                   b'{"version":true,"high_water":{}}', b'{"version":1,"high_water":[]}',
                   b'{"version":1,"high_water":{},"high_water":{}}',
                   b'{"version":1,"high_water":{},"unexpected":0}', b'\xff']
        for value in (-1, pn.MAX_PN + 1, True, 1.5, '12'):
            corrupt.append(json.dumps({'version': 1, 'high_water': {FINGERPRINT: value}}).encode())
        corrupt.append(json.dumps({'version': 1, 'high_water': {'not-a-fingerprint': 1}}).encode())
        corrupt.append(('{"version":1,"high_water":{"' + FINGERPRINT + '":4,"' +
                        FINGERPRINT + '":0}}').encode())
        for data in corrupt:
            self.path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(pn.PacketNumberError): self.allocator()
            self.assertEqual(self.path.read_bytes(), data)
        self.path.unlink()
        with self.allocator(): pass

    def test_every_truncation_of_valid_state_fails_closed(self):
        with self.allocator(): pass
        complete = self.path.read_bytes().rstrip(b'\n')
        for length in range(len(complete)):
            self.path.write_bytes(complete[:length])
            with self.subTest(length=length), self.assertRaises(pn.PacketNumberError): self.allocator()
            self.assertEqual(self.path.read_bytes(), complete[:length])
        self.path.write_bytes(complete)
        with self.allocator() as restored: self.assertEqual(restored.initial, 4)

    def test_file_fsync_failure_keeps_old_state_and_poisoned_instance_denies_pns(self):
        with self.allocator() as allocator:
            allocator.ensure(1)
            previous = self.path.read_bytes()
            with patch.object(pn.os, 'fsync', side_effect=OSError('Disk failure')):
                with self.assertRaisesRegex(OSError, 'Disk failure'): allocator.ensure(5)
            self.assertEqual(self.path.read_bytes(), previous)
            with self.assertRaises(pn.PacketNumberError): allocator.ensure(6)
            self.assertEqual(list(self.path.parent.glob('.*.tmp')), [])
        with self.allocator() as restarted: self.assertEqual(restarted.initial, 4)

    def test_directory_fsync_failure_never_authorizes_new_range(self):
        real_fsync = os.fsync
        calls = []
        def fsync(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            calls.append(directory)
            if directory: raise OSError('Directory sync failure')
            real_fsync(fd)
        with self.allocator() as allocator:
            allocator.ensure(1)
            with patch.object(pn.os, 'fsync', side_effect=fsync):
                with self.assertRaisesRegex(OSError, 'Directory sync failure'): allocator.ensure(5)
            self.assertEqual(calls, [False, True])
            with self.assertRaises(pn.PacketNumberError): allocator.ensure(6)
            self.assertEqual(list(self.path.parent.glob('.*.tmp')), [])
        # If rename survived, the next process skips the range it finds on disk.
        with self.allocator() as restarted: self.assertEqual(restarted.initial, 8)

    def test_final_partial_block_and_48_bit_exhaustion_never_wrap(self):
        with self.allocator(minimum=pn.MAX_PN - 2) as allocator:
            self.assertEqual(self.state()['high_water'][FINGERPRINT], pn.MAX_PN)
            allocator.ensure(pn.MAX_PN - 1)
            allocator.ensure(pn.MAX_PN)
            with self.assertRaises(pn.PacketNumberError): allocator.ensure(pn.MAX_PN + 1)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(pn.PacketNumberError, 'exhausted'): self.allocator()
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_arguments_reuse_and_use_after_close_are_rejected(self):
        for minimum in (-1, pn.MAX_PN + 1, True, 1.5):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError): self.allocator(minimum=minimum)
        for block_size in (0, -1, True, 1.5):
            with self.subTest(block_size=block_size), self.assertRaises(ValueError): self.allocator(block_size=block_size)
        for bssid in (b'', BSSID[:5], 'bssid'):
            with self.assertRaises(ValueError): pn.PacketNumbers(self.path, bssid)
        allocator = self.allocator()
        self.addCleanup(allocator.close)
        allocator.ensure(1)
        for value in (0, 1, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(pn.PacketNumberError): allocator.ensure(value)
        allocator.close()
        with self.assertRaises(pn.PacketNumberError): allocator.ensure(2)


if __name__ == '__main__': unittest.main()
