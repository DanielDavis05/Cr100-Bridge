"""Production advertisements and beacon scheduling share the radio clock."""
import json
import errno
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import sonosnet_link as sl
from nexmon_clock import ClockUnavailable
from test_link import STATE
from test_multi_controller_link import Radio


class RadioClock:
    def __init__(self, tsf=900_000_123):
        self.value, self.ready, self.alive = tsf, True, True
        self.invalidations = 0
        self.next_tbtt = Mock(return_value=100.25)

    def tsf_us(self):
        if not self.ready: raise ClockUnavailable('Synthetic stale clock')
        return self.value

    def status(self):
        return {'ready': self.ready, 'worker_alive': self.alive}

    def invalidate(self):
        self.invalidations += 1
        self.ready = False


class HardwareClockLink(unittest.TestCase):
    def link(self, clock):
        return sl.Link(STATE, Radio(), -1, lambda _: None, hardware_clock=clock)

    def test_beacon_timestamp_and_next_grid_ignore_process_epoch(self):
        clock = RadioClock()
        link = self.link(clock)
        link.start = time.monotonic() - 12345
        self.assertEqual(link.tsf(), clock.value)
        self.assertEqual(link.next_tbtt(100.1), 100.25)
        clock.next_tbtt.assert_called_once_with(100.1, 102400)
        link.send_beacon()
        self.assertEqual(struct.unpack_from('<Q', link.radio.sent[-1], 24)[0],
                         clock.value + sl.TSF_LEAD_US)
        newer = self.link(clock)
        self.assertEqual(newer.tsf(), link.tsf())

    def test_slightly_early_beacon_is_not_reported_one_interval_late(self):
        clock = RadioClock(100 * 102400 - 50)
        link = self.link(clock)
        self.assertEqual(link.send_beacon(), -50)

    def test_failed_clock_worker_stops_before_injection(self):
        clock = RadioClock()
        clock.alive = False
        link = self.link(clock)
        with self.assertRaises(ClockUnavailable): link.run_main(.1)
        self.assertEqual(link.radio.sent, [])

    def test_stale_clock_withholds_beacons_then_fails_after_grace(self):
        clock, records = RadioClock(), []
        clock.ready = False
        link = self.link(clock)
        link.log = records.append
        link.run_main(.1)  # inside the grace: keep running, transmit nothing timestamped
        self.assertEqual(link.radio.sent, [])
        self.assertEqual([r['event'] for r in records if 'clock' in r['event']], ['clock-stale'])
        with patch.object(sl, 'CLOCK_STALE_GRACE', .05):
            link.start = time.monotonic()
            with self.assertRaises(ClockUnavailable): link.run_main(.5)
        self.assertEqual(link.radio.sent, [])
        self.assertEqual([r['event'] for r in records if 'clock' in r['event']],
                         ['clock-stale', 'clock-unavailable'])

    def test_transient_stale_clock_keeps_association_and_resumes_beacons(self):
        clock, records = RadioClock(), []
        clock.next_tbtt.side_effect = lambda now, interval: now + .01
        link = self.link(clock)
        link.log = records.append
        station = next(iter(link.stations.values()))
        station.associated, station.last_rx = True, time.monotonic()
        station.queue.append((time.monotonic(), b'\x00' * 14))
        clock.ready = False
        link.run_main(.05)
        self.assertEqual(link.stats['beacon_sent'], 0)
        clock.ready = True
        link.start = time.monotonic()
        link.run_main(.1)
        self.assertGreater(link.stats['beacon_sent'], 0)
        self.assertTrue(station.associated)
        events = [r['event'] for r in records if 'clock' in r['event']]
        self.assertEqual(events, ['clock-stale', 'clock-recovered'])

    def test_withheld_beacons_are_not_evidence_that_a_dozing_cr100_is_searching(self):
        clock = RadioClock()
        clock.next_tbtt.side_effect = lambda now, interval: now + .01
        link = self.link(clock)
        station = next(iter(link.stations.values()))
        start = time.monotonic()
        station.associated, station.dozing, station.last_rx = True, True, start - 60
        clock.ready = False
        self.assertFalse(link.check_workers())
        # Silent and holding an unpolled frame well past both windows, with no beacon sent.
        later = start + sl.CHECK_SILENCE + sl.CHECK_WINDOW + 1
        station.queue.append((start, b'\x00' * 14))
        with link.lock: link.check_hearing(station, later)
        self.assertFalse(station.searching)
        self.assertIsNone(station.check_from)
        clock.ready = True
        self.assertTrue(link.check_workers())
        link.beacon_deadline(later)                 # recovery: beacons resume from here
        with link.lock: link.check_hearing(station, later + .1)
        self.assertFalse(station.searching)
        self.assertIsNone(station.check_from)
        # A full silence and check window of real beacons still finds a lost CR100.
        with link.lock:
            link.expire_queue(station, later + .1)
            self.assertFalse(station.queue)
            link.check_hearing(station, later + sl.CHECK_SILENCE + .1)
            self.assertIsNotNone(station.check_from)
            link.check_hearing(station, later + sl.CHECK_SILENCE + sl.CHECK_WINDOW + .3)
        self.assertTrue(station.searching)

    def test_tsf_reset_during_link_still_requires_supervised_restart(self):
        clock, records = RadioClock(), []
        resets = [0]
        clock.status = lambda: {'ready': True, 'worker_alive': True, 'resets': resets[0]}
        link = self.link(clock)
        link.log = records.append
        self.assertTrue(link.check_workers())
        resets[0] = 1
        with self.assertRaisesRegex(ClockUnavailable, 'reset'):
            link.check_workers()
        self.assertEqual([r['event'] for r in records], ['clock-unavailable'])

    def test_stale_clock_withholds_probe_responses_without_failing_receive(self):
        clock = RadioClock()
        link = self.link(clock)
        station = next(iter(link.stations.values()))
        station.auto_reconnect = True
        clock.ready = False
        now = time.monotonic()
        link.queue_mgmt_reply(station, 5, now)       # radio thread path
        link.service_stations(now)                   # unsolicited search probe
        self.assertEqual(link.radio.sent, [])
        self.assertIn('probe', station.management_replies)
        clock.ready = True
        link.service_mgmt_replies(station, now)
        self.assertEqual(len(link.radio.sent), 1)
        self.assertNotIn('probe', station.management_replies)

    def test_clock_fault_is_logged_once_with_public_status_before_cleanup(self):
        clock, records = RadioClock(), []
        clock.ready = False
        clock.status = Mock(return_value={'ready': False, 'worker_alive': True,
                                         'last_error': 'Synthetic sample timeout',
                                         'helper': {'pid': 123, 'alive': False,
                                                    'last_fault': 'Synthetic IPC timeout'}})
        link = self.link(clock)
        link.log = records.append
        with patch.object(sl, 'CLOCK_STALE_GRACE', 0):
            for call in (lambda: link.run_main(.1), link.tsf):
                with self.assertRaises(ClockUnavailable): call()
        faults = [record for record in records if record['event'] == 'clock-unavailable']
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]['clock']['last_error'], 'Synthetic sample timeout')
        self.assertEqual(faults[0]['clock']['helper']['last_fault'], 'Synthetic IPC timeout')
        self.assertNotIn(STATE['controllers'][0]['key'], json.dumps(faults))
        self.assertNotIn('key', json.dumps(faults))
        self.assertEqual(link.radio.sent, [])

    def test_next_grid_failure_is_logged_before_any_injection(self):
        clock, records = RadioClock(), []
        clock.next_tbtt.side_effect = ClockUnavailable('Synthetic grid failure')
        link = self.link(clock)
        link.log = records.append
        with patch.object(sl, 'CLOCK_STALE_GRACE', 0), self.assertRaises(ClockUnavailable):
            link.run_main(.1)
        fault, = [record for record in records if record['event'] == 'clock-unavailable']
        self.assertEqual(fault['error'], 'Synthetic grid failure')
        self.assertTrue(fault['clock']['ready'])
        self.assertEqual(link.radio.sent, [])

    def test_radio_loss_cannot_resume_from_background_recalibration(self):
        clock = RadioClock()
        link = self.link(clock)
        link.reopen = Mock(return_value=Radio())
        link.radio_lost(OSError(errno.ENETDOWN, 'Synthetic radio loss'))
        link.radio_lost(OSError(errno.ENETDOWN, 'Repeated radio loss'))
        self.assertEqual(clock.invalidations, 1)
        clock.ready = True  # Even a newly ready background model cannot reopen this link.
        self.assertEqual(link.send(b'frame'), sl.FAILED)
        with self.assertRaisesRegex(ClockUnavailable, 'supervised'):
            link.run_main(.1)
        link.reopen.assert_not_called()
        self.assertEqual(link.radio.sent, [])

    def test_radio_loss_after_worker_check_still_cannot_reopen_in_place(self):
        clock = RadioClock()
        clock.next_tbtt.side_effect = lambda now, interval: now + .1024
        link = self.link(clock)
        link.reopen = Mock(return_value=Radio())

        def lose_between_checks(now):
            link.radio_lost(OSError(errno.ENETDOWN, 'Synthetic concurrent radio loss'))
            clock.ready = True

        link.reload_reconnect_policy = lose_between_checks
        with self.assertRaisesRegex(ClockUnavailable, 'supervised'):
            link.run_main(.1)
        link.reopen.assert_not_called()
        self.assertEqual(link.radio.sent, [])

    def test_process_clock_harness_retains_in_place_reopen(self):
        link = self.link(None)
        old_radio, replacement = link.radio, Radio()
        old_radio.close = Mock()
        link.radio_down = True
        link.reopen = Mock(return_value=replacement)
        link.run_main(.01)
        link.reopen.assert_called_once()
        old_radio.close.assert_called_once()
        self.assertFalse(link.radio_down)
        self.assertIs(link.radio, replacement)

    def test_failed_startup_calibration_closes_resources_without_starting_link(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state.json'
            state.write_text(json.dumps(STATE))
            tap, writer = os.pipe()
            self.addCleanup(os.close, writer)
            radio, clock = Mock(), Mock()
            clock.bootstrap.return_value = False
            with patch.object(sys, 'argv', ['link', '--state', str(state)]), \
                    patch.object(sl, 'open_tap', return_value=tap), \
                    patch.object(sl, 'open_verified_radio', return_value=radio), \
                    patch.object(sl.subprocess, 'run'), \
                    patch.object(sl, 'HardwareClock', return_value=clock), \
                    patch.object(sl, 'Link') as make_link:
                with self.assertRaises(ClockUnavailable): sl.main()
            make_link.assert_not_called()
            clock.close.assert_called_once()
            radio.close.assert_called_once()
            with self.assertRaises(OSError): os.fstat(tap)


if __name__ == '__main__': unittest.main()
