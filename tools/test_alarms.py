"""Behavioral alarm tests: persistence, scheduler, native adapters and ownership."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

from alarms import (AlarmError, AlarmMediaRef, AlarmProviderRegistry, AlarmService,
                    AlarmStore, CallbackAlarmExecutor, ExecutionUncertain,
                    NativeAlarmProgram, PlaybackRequest, next_occurrence)


def stamp(value): return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class Clock:
    def __init__(self, value='2026-11-03T11:59:50Z'): self.now = stamp(value)
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


class Provider:
    def __init__(self, name='radio', kind='live'):
        self.name, self.kind, self.calls, self.failure = name, kind, [], None
        self.on_resolve = None
    def describe(self, reference): return {'title': 'Station & Show' if self.name == 'radio' else 'Morning playlist'}
    def sources(self): return [{'content_id': 'station:s1' if self.name == 'radio' else 'spotify:playlist:fixture', 'account_ref': 'fixture-account' if self.name != 'radio' else None}]
    def from_native(self, uri, metadata):
        if self.name == 'radio' and uri.startswith('x-sonosapi-stream:s'):
            return AlarmMediaRef('radio', 'station:' + uri.split(':', 1)[1].partition('?')[0])
    def resolve(self, reference, context):
        self.calls.append((reference, dict(context)))
        if self.on_resolve: self.on_resolve()
        if self.failure: raise self.failure
        return PlaybackRequest(self.kind, 'https://stream.example/live?access_token=DO-NOT-PERSIST',
                               metadata='secret playback metadata', preserve_queue=self.kind == 'live')
    def native_program(self, reference, alarm):
        return NativeAlarmProgram('http://bridge.lan/station/' + reference.content_id,
                                  stable=True, renderer_reachable=True)


class Player:
    def __init__(self): self.current, self.events, self.failure, self.fallback_failure = {}, [], None, None
    def execute(self, request, context):
        self.events.append(('play', request.kind, request.preserve_queue, dict(context)))
        if self.failure: raise self.failure
        handle = context['run_id']; self.current[context['room']] = handle
        return handle
    def owns(self, context, handle): return self.current.get(context['room']) == handle
    def stop(self, context, handle):
        if not self.owns(context, handle): raise AssertionError('Stopped a newer playback generation')
        self.events.append(('stop', handle)); self.current.pop(context['room'], None)
    def fallback(self, context):
        self.events.append(('chime', dict(context)))
        if self.fallback_failure: raise self.fallback_failure
        handle = 'fallback:' + context['run_id']; self.current[context['room']] = handle
        return handle
    def recover(self, context):
        current = self.current.get(context['room'])
        return current if current and context['run_id'] in current else None
    def capture(self, context): return self.current.get(context['room'])
    def executor(self):
        return CallbackAlarmExecutor(self.execute, self.owns, self.stop, fallback=self.fallback,
                                     recover=self.recover, capture=self.capture)


class Native:
    def __init__(self): self.items, self.events, self.refuse_disable = {}, [], False
    def supports(self, alarm, program): return True
    def install(self, alarm, program, *, enabled, idempotency_key):
        self.events.append(('install', enabled, idempotency_key))
        native_id = str(alarm['id'])
        self.items[native_id] = {'enabled': enabled}
        return native_id
    def set_enabled(self, native_id, enabled):
        self.events.append(('enabled', native_id, enabled))
        if enabled or not self.refuse_disable: self.items[native_id]['enabled'] = enabled
    def inspect(self, native_id): return dict(self.items[native_id])
    def remove(self, native_id): self.items.pop(native_id); return True


class Alarms(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'alarms.sqlite'
        self.clock, self.player, self.provider = Clock(), Player(), Provider()
        self.registry = AlarmProviderRegistry(); self.registry.register('radio', self.provider)
        self.services = []
        self.service = self.make()

    def make(self, *, executor=True, **kwargs):
        service = AlarmService(self.path, providers=self.registry,
            executor=self.player.executor() if executor else None,
            clock=self.clock, log=lambda x: None, **kwargs)
        self.services.append(service)
        self.addCleanup(lambda: self.services.remove(service) or service.close() if service in self.services else None)
        return service

    def close_service(self, service):
        service.close(); self.services.remove(service)

    def save(self, **values):
        return self.service.save({'media_ref': {'provider': 'radio', 'content_id': 'station:s1'},
            'room': 'kitchen', 'time': '12:00:00', 'duration': 60, **values})

    def fire(self, **values):
        alarm = self.save(**values); self.clock.advance(10); self.service.tick()
        return alarm, self.service.store.runs()[-1]

    def test_stale_active_room_does_not_hide_another_rooms_active_alarm(self):
        self.fire(room='removed', duration=None)
        second = self.save(room='patio', duration=None)
        self.service.run_now(second['id'])
        def resolve(room):
            if room == 'removed': raise AlarmError('Alarm room is not currently configured: removed', 701)
            return room
        self.service.room_resolver = resolve
        self.assertEqual([run['alarm_id'] for run in self.service.active('patio')], [second['id']])
        with self.assertRaises(AlarmError): self.service.active('removed')
        self.assertEqual(self.service.action('AVTransport', 'GetRunningAlarmProperties', {}, 'patio')['AlarmID'], str(second['id']))

    def test_unavailable_room_does_not_hide_other_native_alarms_or_delete_it(self):
        first = self.save()
        hidden = self.save(room='removed')
        messages = []
        self.service.log = messages.append
        def resolve(room):
            if room == 'removed': raise AlarmError('Alarm room is not currently configured: removed', 701)
            return room
        self.service.native_room = resolve
        native = self.service.action('AlarmClock', 'ListAlarms', {})
        self.assertEqual([int(item.get('ID')) for item in ET.fromstring(native['CurrentAlarmList'])], [first['id']])
        self.assertEqual(messages, [{'event': 'alarm-room-unavailable', 'alarm_id': hidden['id']}])
        self.assertEqual([item['id'] for item in self.service.list()], [first['id'], hidden['id']])
        self.assertTrue(self.service.store.get(hidden['id'])['enabled'])
        self.service.native_room = str
        self.assertEqual(len(ET.fromstring(self.service.native_list()['CurrentAlarmList'])), 2)

    def test_due_alarm_in_unavailable_room_is_recorded_as_failed(self):
        alarm = self.save(room='removed')
        def resolve(room): raise AlarmError('Alarm room is not currently configured: removed', 701)
        self.service.room_resolver = resolve
        self.clock.advance(10); self.service.tick(); self.service.tick()
        run = self.service.store.runs()[-1]
        self.assertEqual((run['alarm_id'], run['status']), (alarm['id'], 'failed'))
        self.assertIn('not currently configured: removed', run['error'])
        self.assertEqual(self.player.events, [])

    def test_running_alarm_room_removal_does_not_break_tick_or_restart(self):
        alarm, run = self.fire()
        def resolve(room): raise AlarmError('Alarm room is not currently configured: kitchen', 701)
        self.service.room_resolver = resolve
        self.clock.advance(1); self.service.tick()
        self.assertEqual(self.service.store.run(run['id'])['status'], 'failed')
        # Recovery also tolerates a room removed while the bridge was stopped.
        self.service.store.update_run(run['id'], status='playing')
        self.close_service(self.service)
        self.service = self.make(room_resolver=resolve)
        self.assertEqual(self.service.store.run(run['id'])['status'], 'uncertain')

    def test_informational_component_has_no_running_alarm(self):
        def resolve(room): raise AlarmError('Not an alarm room', 701)
        self.service.room_resolver = resolve
        self.assertEqual(self.service.action('AVTransport', 'GetRunningAlarmProperties', {}, 'bridge'),
                         {'AlarmID': '0', 'GroupID': '', 'LoggedStartTime': ''})

    def test_pause_stops_playing_or_uncertain_alarm_and_clears_running_event(self):
        for status in ('playing', 'uncertain'):
            with self.subTest(status=status):
                alarm, run = self.fire()
                self.service.store.update_run(run['id'], status=status)
                before = sum(event[0] == 'stop' for event in self.player.events)
                self.assertEqual(self.service.action('AVTransport', 'Pause', {}, 'kitchen'), {})
                self.assertEqual(sum(event[0] == 'stop' for event in self.player.events), before + 1)
                self.assertEqual(self.service.store.run(run['id'])['status'], 'stopped')
                self.assertEqual(self.service.event_values('AVTransport', 'kitchen')['AlarmRunning'], '0')

    def test_pause_without_ringing_alarm_is_forwarded_to_speaker(self):
        self.assertIsNone(self.service.action('AVTransport', 'Pause', {}, 'kitchen'))
        alarm, run = self.fire(); self.service.snooze('kitchen')
        self.assertIsNone(self.service.action('AVTransport', 'Pause', {}, 'kitchen'))
        self.assertEqual(self.service.store.run(run['id'])['status'], 'snoozed')

    def test_pause_does_not_stop_playback_after_manual_takeover(self):
        alarm, run = self.fire()
        self.player.current['kitchen'] = 'manual-selection'
        self.assertEqual(self.service.action('AVTransport', 'Pause', {}, 'kitchen'), {})
        self.assertEqual(self.service.store.run(run['id'])['status'], 'superseded')
        self.assertFalse(any(event[0] == 'stop' for event in self.player.events))

    def test_duplicate_due_alarms_ring_once_without_changing_stored_alarms(self):
        first, duplicate = self.save(), self.save()
        self.clock.advance(10); self.service.tick()
        runs = self.service.store.runs()
        self.assertEqual([run['status'] for run in runs], ['playing', 'superseded'])
        self.assertEqual(runs[1]['error'], 'Another alarm is already ringing in this room')
        self.assertEqual([run['alarm_id'] for run in runs], [first['id'], duplicate['id']])
        self.assertEqual(sum(event[0] == 'play' for event in self.player.events), 1)
        self.assertTrue(all(alarm['enabled'] for alarm in self.service.list()))

    def test_recent_alarm_in_another_room_does_not_suppress_due_alarm(self):
        self.save(); self.save(room='patio')
        self.clock.advance(10); self.service.tick()
        self.assertEqual([run['status'] for run in self.service.store.runs()], ['playing', 'playing'])
        self.assertEqual(sum(event[0] == 'play' for event in self.player.events), 2)

    def test_alarm_that_started_over_a_minute_ago_does_not_suppress_due_alarm(self):
        self.save(duration=None); self.save(time='12:01:01', duration=None)
        self.clock.advance(10); self.service.tick()
        self.clock.advance(61); self.service.tick()
        self.assertEqual(sum(event[0] == 'play' for event in self.player.events), 2)

    def test_shutdown_waits_for_inflight_event_and_later_events_are_empty(self):
        entered, release, closing, closed = (threading.Event() for _ in range(4))
        errors, events = [], []

        def identity(room):
            entered.set()
            if not release.wait(2): raise AssertionError('Event was never released')
            return 'RINCON_KITCHEN', 1

        def publish():
            try: events.append(self.service.event_values('ZoneGroupTopology', 'kitchen'))
            except Exception as error: errors.append(error)

        self.service.topology_identity = identity
        original_stop = self.service.scheduler.close
        def stop_scheduler():
            original_stop()
            closing.set()
        self.service.scheduler.close = stop_scheduler
        publisher = threading.Thread(target=publish)
        publisher.start()
        self.assertTrue(entered.wait(2))

        def shutdown():
            try: self.service.close()
            except Exception as error: errors.append(error)
            finally: closed.set()

        closer = threading.Thread(target=shutdown)
        closer.start()
        try:
            self.assertTrue(closing.wait(2))
            self.assertFalse(closed.is_set())
        finally:
            release.set()
            publisher.join(2)
            closer.join(2)
        self.assertFalse(publisher.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events, [{'AlarmRunSequence': 'RINCON_KITCHEN:1:0'}])
        for service in ('AlarmClock', 'ZoneGroupTopology', 'AVTransport'):
            self.assertEqual(self.service.event_values(service, 'kitchen'), {})
        self.service.close()

    def test_crud_persists_versions_and_rejects_stale_edits(self):
        alarm = self.save(); version = self.service.store.version
        changed = self.service.save({'id': alarm['id'], 'volume': 35}, expected_revision=alarm['revision'])
        self.assertEqual(changed['revision'], 2)
        self.assertGreater(self.service.store.version, version)
        with self.assertRaises(AlarmError): self.service.save({'id': alarm['id'], 'volume': 80}, expected_revision=1)
        web = self.make(executor=False)
        self.assertEqual(web.list()[0]['volume'], 35)
        web.delete(alarm['id'], expected_revision=2)
        self.assertEqual(self.service.list(), [])

    def test_credential_fields_and_temporary_urls_are_rejected(self):
        for ref in ({'provider': 'radio', 'content_id': 'https://stream/?token=x'},
                    {'provider': 'spotify', 'content_id': 'playlist:x', 'access_token': 'secret'},
                    {'provider': 'radio', 'content_id': 'station:s1?token=secret'}):
            with self.assertRaises(AlarmError): self.save(media_ref=ref)

    def test_resolved_urls_and_credentials_never_enter_database(self):
        alarm, run = self.fire()
        self.assertEqual(run['status'], 'playing')
        serialized = json.dumps(self.service.store.records() + self.service.store.runs())
        self.assertNotIn('DO-NOT-PERSIST', serialized)
        self.assertNotIn('secret playback metadata', serialized)
        self.assertNotIn('https://stream.example', serialized)

    def test_due_occurrence_resolves_fresh_once_and_preserves_live_queue(self):
        alarm, run = self.fire(include_linked=True, volume=43)
        self.service.tick(); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)
        event = self.player.events[0]
        self.assertEqual(event[:3], ('play', 'live', True))
        self.assertTrue(event[3]['include_linked']); self.assertEqual(event[3]['volume'], 43)
        self.assertEqual(len(self.service.store.runs()), 1)

    def test_duration_stops_only_matching_playback(self):
        alarm, run = self.fire()
        self.clock.advance(60); self.service.tick()
        self.assertEqual(self.service.store.run(run['id'])['status'], 'expired')
        self.assertEqual(self.player.events[-1], ('stop', run['handle']))

    def test_old_duration_never_stops_new_manual_playback(self):
        alarm, run = self.fire()
        self.player.current['kitchen'] = 'manual-selection'
        self.clock.advance(60); self.service.tick()
        self.assertEqual(self.service.store.run(run['id'])['status'], 'superseded')
        self.assertEqual(self.player.current['kitchen'], 'manual-selection')
        self.assertFalse(any(x[0] == 'stop' for x in self.player.events))

    def test_nine_minute_snooze_persists_and_resolves_again(self):
        alarm, run = self.fire()
        result = self.service.snooze('kitchen')
        self.assertEqual(result['resume_at'], self.clock() + 540)
        self.close_service(self.service); self.service = self.make()
        self.clock.advance(539); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)
        self.clock.advance(1); self.service.tick()
        self.assertEqual(len(self.provider.calls), 2)
        child = self.service.store.runs()[-1]
        self.assertEqual(child['parent_id'], run['id']); self.assertEqual(child['status'], 'playing')

    def test_stop_cancels_snooze(self):
        alarm, run = self.fire(); self.service.snooze('kitchen'); self.service.stop('kitchen')
        self.clock.advance(541); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)

    def test_disabling_future_alarm_cancels_pending_retry_and_snooze(self):
        self.provider.failure = AlarmError('directory unavailable')
        alarm, run = self.fire()
        self.assertEqual(run['status'], 'retry')
        self.service.save({'id': alarm['id'], 'enabled': False})
        self.clock.advance(20); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)

    def test_confirmed_provider_failure_retries_then_uses_shared_chime(self):
        self.provider.failure = PermissionError('expired future OAuth SECRET')
        alarm, run = self.fire()
        self.assertEqual(run['status'], 'retry')
        self.clock.advance(5); self.service.tick()
        run = self.service.store.run(run['id'])
        self.assertEqual(run['status'], 'playing'); self.assertTrue(run['fallback'])
        self.assertNotIn('SECRET', json.dumps(run))

    def test_confirmed_playback_failure_uses_fallback(self):
        self.player.failure = AlarmError('unsupported stream')
        alarm, run = self.fire()
        self.assertTrue(run['fallback']); self.assertEqual(run['status'], 'playing')

    def test_unconfirmed_playback_failure_is_not_blindly_replayed(self):
        self.player.failure = TimeoutError('speaker may have accepted it')
        alarm, run = self.fire()
        self.assertEqual(run['status'], 'uncertain')
        self.service.tick(); self.clock.advance(10); self.service.tick()
        self.assertEqual([x[0] for x in self.player.events], ['play'])

    def test_missing_executor_handle_is_not_nominal_success(self):
        self.player.execute = lambda request, context: None
        self.close_service(self.service); self.service = self.make()
        alarm, run = self.fire()
        self.assertEqual(run['status'], 'uncertain')

    def test_restart_reconciles_playing_ownership_without_replay(self):
        alarm, run = self.fire()
        self.close_service(self.service); self.service = self.make()
        self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.service.store.run(run['id'])['status'], 'playing')

    def test_web_reader_does_not_recover_or_change_active_run(self):
        alarm, run = self.fire(); web = self.make(executor=False)
        self.assertEqual(web.snapshot()['runs'][-1]['status'], 'playing')
        self.assertEqual(self.service.store.run(run['id'])['status'], 'playing')
        with self.assertRaises(AlarmError): web.tick()

    def test_cross_process_commands_are_durable_and_idempotent(self):
        alarm, run = self.fire(); web = self.make(executor=False)
        cmd = web.command('snooze', room='kitchen', request_id='browser-request-1')
        self.assertEqual(cmd['state'], 'pending')
        self.service.tick()
        self.assertEqual(web.store.command(cmd['id'])['state'], 'complete')
        repeated = web.command('snooze', room='kitchen', request_id='browser-request-1')
        self.assertEqual(repeated['state'], 'complete')
        self.assertEqual(self.service.store.run(run['id'])['status'], 'snoozed')
        self.assertEqual(len([x for x in self.player.events if x[0] == 'stop']), 1)
        with self.assertRaises(AlarmError): web.command('stop', room='kitchen', request_id='browser-request-1')

    def test_global_second_provider_works_without_scheduler_changes(self):
        spotify = Provider('spotify', 'queue'); self.registry.register('spotify', spotify)
        alarm = self.save(media_ref={'provider': 'spotify', 'content_id': 'spotify:playlist:fixture', 'account_ref': 'household-account'})
        self.clock.advance(10); self.service.tick()
        self.assertEqual(self.player.events[0][:3], ('play', 'queue', False))
        self.assertEqual(spotify.calls[0][0].account_ref, 'household-account')
        self.assertEqual(len(self.service.sources()), 2)
        self.assertEqual(len(self.service.references('spotify', 'spotify:playlist:fixture')), 1)

    def test_edit_during_resolution_cancels_dispatch(self):
        alarm = self.save()
        self.provider.on_resolve = lambda: self.service.save({'id': alarm['id'], 'enabled': False})
        self.clock.advance(10); self.service.tick()
        self.assertEqual(self.player.events, [])
        self.assertEqual(self.service.store.runs()[-1]['status'], 'cancelled')

    def test_old_missed_alarm_is_journaled_without_late_audio(self):
        self.save(); self.clock.advance(3600); self.service.tick()
        self.assertEqual(self.player.events, [])
        self.assertEqual(self.service.store.runs()[0]['status'], 'missed')

    def test_restart_within_grace_fires_once(self):
        self.save(); self.close_service(self.service)
        self.clock.advance(100); self.service = self.make(); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)
        self.service.tick(); self.assertEqual(len(self.provider.calls), 1)

    def test_expired_retry_does_not_play_hours_late(self):
        self.provider.failure = AlarmError('temporary')
        alarm, run = self.fire(); self.clock.advance(3600); self.service.tick()
        self.assertEqual(self.service.store.run(run['id'])['status'], 'missed')
        self.assertEqual(self.player.events, [])

    def test_weekday_numbering_and_dst_gap_fold(self):
        alarm = self.save(time='02:30:00', timezone='America/New_York', recurrence='ON_1')
        nxt = next_occurrence(alarm, stamp('2026-11-03T00:00:00Z'))
        self.assertEqual(nxt, stamp('2026-11-09T07:30:00Z'))
        alarm.update(recurrence='DAILY', time='02:30:00')
        self.assertEqual(next_occurrence(alarm, stamp('2026-03-08T05:00:00Z')), stamp('2026-03-09T06:30:00Z'))
        alarm['time'] = '01:30:00'
        first = next_occurrence(alarm, stamp('2026-11-01T04:00:00Z'))
        self.assertEqual(first, stamp('2026-11-01T05:30:00Z'))
        self.assertEqual(next_occurrence(alarm, first), stamp('2026-11-02T06:30:00Z'))

    def test_native_timezone_picker_updates_real_clock_and_existing_alarms(self):
        from alarm_timezones import NATIVE_TIMEZONES, native_timezone
        self.assertEqual(len(NATIVE_TIMEZONES),75)
        for index in range(75): self.assertTrue(native_timezone(index))
        self.service.timezone_from_index = native_timezone
        alarm = self.save()
        self.service.action('AlarmClock','SetTimeZone',{'Index':'13','AutoAdjustDst':'1'})
        self.assertEqual(self.service.timezone,'America/New_York')
        self.assertEqual(self.service.store.get(alarm['id'])['timezone'],'America/New_York')
        self.assertEqual(self.service.action('AlarmClock','GetTimeZone',{})['Index'],'13')
        self.assertEqual(self.service.action('AlarmClock','GetTimeNow',{})['CurrentLocalTime'],'2026-11-03 06:59:50')
        self.service.action('AlarmClock','SetTimeZone',{'Index':'13','AutoAdjustDst':'0'})
        self.assertEqual(self.service.timezone,'UTC-05:00')
        self.assertEqual(self.service.action('AlarmClock','GetTimeZone',{})['AutoAdjustDst'],'0')
        self.service.action('AlarmClock','SetTimeZone',{'Index':'49','AutoAdjustDst':'0'})
        self.assertEqual(self.service.timezone,'UTC+05:45')

    def test_native_clock_returns_binary_rules_for_every_picker_zone(self):
        from alarm_timezones import NATIVE_TIMEZONES, native_timezone_rule, native_rule_zone
        # Read-only physical ZP80 responses, not a second copy of our encoder.
        fixtures = {'America/New_York':'012c0b000102000003000202ffc4',
                    'Europe/Berlin':'ffc40a000503000003000502ffc4',
                    'Australia/Adelaide':'fdc60400010300000a000102ffc4'}
        for zone, rule in fixtures.items(): self.assertEqual(native_timezone_rule(zone, 2026), rule)
        for zone in NATIVE_TIMEZONES:
            rule = native_timezone_rule(zone, 2026)
            self.assertRegex(rule, '^[0-9a-f]{28}$')
        self.service.set_timezone('America/New_York')
        rule = fixtures['America/New_York']
        self.assertEqual(self.service.action('AlarmClock','GetTimeNow',{})['CurrentTimeZone'],rule)
        self.assertEqual(self.service.action('AlarmClock','GetTimeZoneAndRule',{})['CurrentTimeZone'],rule)
        self.assertEqual(self.service.event_values('AlarmClock')['TimeZone'],rule)
        self.assertEqual(native_rule_zone(rule,datetime(2026,7,1)).utcoffset(None).total_seconds(),-4*3600)
        self.assertEqual(native_rule_zone(rule,datetime(2026,11,5)).utcoffset(None).total_seconds(),-5*3600)

    def test_manual_native_clock_persists_ticks_and_restores_internet_time(self):
        from alarms import NATIVE_TIME_SERVER
        self.service.set_timezone('America/New_York')
        rule = self.service.action('AlarmClock','GetTimeNow',{})['CurrentTimeZone']
        original = self.clock()
        generation = int(self.service.action('AlarmClock','GetTimeNow',{})['CurrentTimeGeneration'])
        self.service.action('AlarmClock','SetTimeServer',{'DesiredTimeServer':''})
        self.service.action('AlarmClock','SetTimeNow',{'DesiredTime':'2026-11-03 08:00:00',
                                                    'TimeZoneForDesiredTime':rule})
        self.assertEqual(self.clock(),original)  # Host clock remains untouched.
        self.assertEqual(self.service.action('AlarmClock','GetTimeNow',{})['CurrentLocalTime'],'2026-11-03 08:00:00')
        self.assertGreater(int(self.service.action('AlarmClock','GetTimeNow',{})['CurrentTimeGeneration']),generation)
        self.assertEqual(self.service.action('AlarmClock','GetTimeServer',{})['CurrentTimeServer'],'')
        self.clock.advance(12)
        self.close_service(self.service); self.service=self.make()
        self.assertEqual(self.service.action('AlarmClock','GetTimeNow',{})['CurrentLocalTime'],'2026-11-03 08:00:12')
        self.service.action('AlarmClock','SetTimeServer',{'DesiredTimeServer':NATIVE_TIME_SERVER})
        self.assertEqual(self.service.clock(),self.clock())
        self.assertEqual(self.service.action('AlarmClock','GetTimeServer',{})['CurrentTimeServer'],NATIVE_TIME_SERVER)

    def test_native_rule_offsets_match_iana_for_all_picker_zones_through_year(self):
        from alarm_timezones import NATIVE_TIMEZONES, native_timezone_rule, native_rule_zone
        from zoneinfo import ZoneInfo
        # Casablanca in tzdata2026e has three offset transitions, including a
        # change of standard offset. It must not invent a two-transition year.
        for name in NATIVE_TIMEZONES:
            for month in range(1,13):
                instant=datetime(2026,month,15,12,tzinfo=timezone.utc)
                local=instant.astimezone(ZoneInfo(name))
                rule=native_timezone_rule(name,2026,instant=instant.timestamp())
                actual=native_rule_zone(rule,local.replace(tzinfo=None)).utcoffset(None)
                self.assertEqual(actual,local.utcoffset(),(name,month,rule))

    def test_fixed_rule_fallback_uses_household_instant_and_manual_zone(self):
        from unittest.mock import patch
        from alarm_timezones import native_rule_zone
        self.service.set_timezone('America/New_York')
        # Force an unrepresentable year without depending on host tzdata age.
        with patch('alarm_timezones._annual_native_timezone_rule',return_value=None):
            winter=self.service.action('AlarmClock','GetTimeNow',{})
            self.assertEqual(native_rule_zone(winter['CurrentTimeZone'],datetime(2026,11,3)).utcoffset(None).total_seconds(),-5*3600)
            self.service.action('AlarmClock','SetTimeNow',{'DesiredTime':'2026-07-01 08:00:00',
                'TimeZoneForDesiredTime':winter['CurrentTimeZone']})
            summer=self.service.action('AlarmClock','GetTimeNow',{})
            self.assertEqual(summer['CurrentLocalTime'],'2026-07-01 08:00:00')
            self.assertEqual(summer['CurrentUTCTime'],'2026-07-01 12:00:00')
            self.assertEqual(native_rule_zone(summer['CurrentTimeZone'],datetime(2026,7,1)).utcoffset(None).total_seconds(),-4*3600)

    def test_manual_clock_drives_alarm_scheduler_and_preserves_sleep_duration(self):
        self.player.current['kitchen']='owned'
        self.service.set_sleep_timer('kitchen',60)
        self.service.action('AlarmClock','SetTimeNow',{'DesiredTime':'2026-11-03 10:00:00',
                                                    'TimeZoneForDesiredTime':'UTC'})
        self.assertEqual(self.service.sleep_timer('kitchen')['RemainingSleepTimerDuration'],'0:01:00')
        self.save(time='10:00:10')
        self.clock.advance(10); self.service.tick()
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.service.sleep_timer('kitchen')['RemainingSleepTimerDuration'],'0:00:50')

    def test_invalid_manual_time_does_not_change_clock(self):
        before=self.service.action('AlarmClock','GetTimeNow',{})
        for value, rule in [('no date','UTC'),('2026-02-30 08:00:00','UTC'),
                            ('2026-11-03 08:00:00','ffff0d000102000003000202ffc4')]:
            with self.assertRaises(AlarmError):
                self.service.action('AlarmClock','SetTimeNow',{'DesiredTime':value,'TimeZoneForDesiredTime':rule})
            self.assertEqual(self.service.action('AlarmClock','GetTimeNow',{}),before)

    def test_clock_adjustment_preserves_running_alarm_expiry(self):
        alarm=self.save(duration=30)
        self.service.run_now(alarm['id'])
        self.clock.advance(10)
        self.service.action('AlarmClock','SetTimeNow',{'DesiredTime':'2026-11-03 08:00:00',
                                                    'TimeZoneForDesiredTime':'UTC'})
        self.clock.advance(19); self.service.tick()
        self.assertEqual(self.service.store.runs()[-1]['status'],'playing')
        self.clock.advance(1); self.service.tick()
        self.assertEqual(self.service.store.runs()[-1]['status'],'expired')

    def test_once_alarm_is_not_repeated_next_day(self):
        self.fire(recurrence='ONCE')
        self.clock.advance(86400); self.service.tick()
        self.assertEqual(len(self.provider.calls), 1)

    def test_sleep_timer_and_cancellation_share_all_sources(self):
        self.player.current['kitchen'] = 'spotify-playback-generation'
        result = self.service.set_sleep_timer('kitchen', 30)
        self.assertEqual(result['RemainingSleepTimerDuration'], '0:00:30')
        self.service.set_sleep_timer('kitchen', None)
        self.clock.advance(31); self.service.tick()
        self.assertEqual(self.player.current['kitchen'], 'spotify-playback-generation')
        self.service.set_sleep_timer('kitchen', 30)
        self.clock.advance(30); self.service.tick()
        self.assertNotIn('kitchen', self.player.current)

    def test_sleep_timer_never_stops_manual_takeover(self):
        self.player.current['kitchen'] = 'radio-a'
        self.service.set_sleep_timer('kitchen', 30)
        self.player.current['kitchen'] = 'new-track'
        self.clock.advance(30); self.service.tick()
        self.assertEqual(self.player.current['kitchen'], 'new-track')

    def test_native_crud_and_snooze_use_same_records_as_web(self):
        args = {'StartLocalTime':'12:00:00','Duration':'0:01:00','Recurrence':'DAILY',
                'Enabled':'1','RoomUUID':'kitchen','ProgramURI':'x-sonosapi-stream:s1?sid=254',
                'ProgramMetaData':'','PlayMode':'NORMAL','Volume':'30','IncludeLinkedZones':'1'}
        created = self.service.action('AlarmClock', 'CreateAlarm', args, 'kitchen')
        alarm_id = int(created['AssignedID'])
        listing = self.service.action('AlarmClock', 'ListAlarms', {}, 'kitchen')
        entry = ET.fromstring(listing['CurrentAlarmList'])[0]
        self.assertEqual(entry.get('ID'), str(alarm_id)); self.assertEqual(entry.get('Volume'), '30')
        self.service.action('AlarmClock', 'UpdateAlarm', {**args,'ID':str(alarm_id),'Volume':'41'}, 'kitchen')
        self.assertEqual(self.service.list()[0]['volume'], 41)
        self.clock.advance(10); self.service.tick()
        running = self.service.action('AVTransport','GetRunningAlarmProperties',{},'kitchen')
        self.assertEqual(running['AlarmID'], str(alarm_id))
        self.service.action('AVTransport','SnoozeAlarm',{'Duration':'0:09:00'},'kitchen')
        self.assertEqual(self.service.active('kitchen')[0]['status'], 'snoozed')
        self.service.action('AlarmClock','DestroyAlarm',{'ID':str(alarm_id)},'kitchen')
        self.assertEqual(self.service.list(), [])

    def test_native_run_event_fields_change_for_start_snooze_stop(self):
        alarm,run = self.fire()
        fields = self.service.event_values('AVTransport','kitchen')
        self.assertEqual((fields['AlarmRunning'],fields['SnoozeRunning'],fields['AlarmIDRunning']),('1','0',str(alarm['id'])))
        self.assertEqual(self.service.event_values('ZoneGroupTopology','kitchen')['AlarmRunSequence'],'kitchen:1:1')
        self.service.snooze('kitchen')
        self.assertEqual(self.service.event_values('AVTransport','kitchen')['SnoozeRunning'],'1')
        self.service.stop('kitchen')
        self.assertEqual(self.service.event_values('AVTransport','kitchen')['AlarmRunning'],'0')

    def test_web_delete_queues_stop_for_active_occurrence(self):
        alarm,run = self.fire(); web = self.make(executor=False)
        web.delete(alarm['id']); self.service.tick()
        self.assertEqual(self.service.list(),[])
        self.assertIn(('stop',run['handle']),self.player.events)

    def test_native_ownership_is_verified_and_bridge_does_not_also_fire(self):
        native = Native()
        self.close_service(self.service); self.service = self.make(native_delegate=native)
        alarm = self.save(execution_owner='native')
        self.assertEqual(alarm['execution_owner'], 'native')
        self.assertEqual(native.events[0][0:2], ('install', False))
        self.clock.advance(10); self.service.tick()
        self.assertEqual(self.player.events, [])
        self.assertEqual(self.service.store.runs()[-1]['status'], 'delegated')
        self.service.save({'id': alarm['id'], 'execution_owner':'bridge'})
        self.clock.advance(1); self.service.tick()
        self.assertEqual(self.player.events, [])

    def test_unconfirmed_old_native_disablement_blocks_new_owner(self):
        native = Native()
        self.close_service(self.service); self.service = self.make(native_delegate=native)
        alarm = self.save(execution_owner='native'); native.refuse_disable = True
        with self.assertRaises(AlarmError): self.service.save({'id':alarm['id'],'execution_owner':'bridge'})
        record = self.service.store.get(alarm['id'])
        self.assertEqual(record['execution_owner'], 'blocked'); self.assertFalse(record['enabled'])
        self.clock.advance(10); self.service.tick(); self.assertEqual(self.player.events, [])
        native.refuse_disable = False; self.service.tick()
        self.assertEqual(self.service.store.get(alarm['id'])['execution_owner'], 'bridge')

    def test_two_services_cannot_claim_one_occurrence_twice(self):
        alarm = self.save(); other = self.make()
        self.clock.advance(10)
        barrier = threading.Barrier(2)
        failures = []
        def dispatch(service):
            try: barrier.wait(); service.tick()
            except Exception as error: failures.append(error)
        threads = [threading.Thread(target=dispatch,args=(s,)) for s in (self.service,other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures, []); self.assertEqual(len(self.provider.calls), 1)


if __name__ == '__main__': unittest.main()
