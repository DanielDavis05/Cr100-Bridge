"""Loopback SOAP regressions for capability routing and unsupported mutations.

These tests use real virtual-player HTTP handlers and a recording in-memory
backend. No Home Assistant instance or physical Sonos device is contacted.
"""
import copy
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backends import BackendError
from room_capabilities import RoomBinding
import zoneplayers
from test_backends import MemoryBackend, MemoryRoom, STATION
from test_ha_backend import AVT, field, wait_for
from test_zoneplayers import HOUSEHOLD, groups, soap, zp

RC = '/MediaRenderer/RenderingControl/Control'
CD = '/MediaServer/ContentDirectory/Control'
DP = '/DeviceProperties/Control'
AC = '/AlarmClock/Control'
SP = '/SystemProperties/Control'
INSTANCE = '<InstanceID>0</InstanceID>'
BUILD = {'software': 'cap-test', 'display': 'dev', 'hardware': 'Virtual component', 'extra': 'offline test'}


class CapabilityRoom(MemoryRoom):
    def __init__(self, backend, key):
        super().__init__(backend, key)
        self.operations = {'play', 'pause', 'volume', 'mute', 'play_item', 'browse', 'group'}
        self.mode = (False, 'off')
        self.fail_play = False

    def operation_capabilities(self):
        return frozenset(self.operations)

    def shuffle_repeat(self):
        return self.mode

    def set_play_mode(self, shuffle, repeat):
        self.mode = (shuffle, repeat)
        self.record('play_mode', shuffle, repeat)

    def send_mute(self, value):
        self.reported_mute = value
        self.record('mute', value)

    def play_item(self, node):
        if self.fail_play:
            raise BackendError('device rejected playback')
        super().play_item(node)


class CapabilityBackend(MemoryBackend):
    def __init__(self):
        super().__init__('capability')
        self.domains = {'den': ('provider', 'one'), 'hall': ('provider', 'one'),
                        'shed': ('provider', 'two')}

    def room(self, key):
        return self.rooms.setdefault(key, CapabilityRoom(self, key))

    def binding(self, key):
        return RoomBinding(platform='test', device_id=key, group_key=self.domains[key])


class CapabilityRouting(unittest.TestCase):
    def setUp(self):
        self.backend = CapabilityBackend()
        self.house = zoneplayers.Household(
            zp, HOUSEHOLD, ['Den', 'Hall', 'Shed'], ['127.0.0.1'] * 3,
            lambda record: None, port=0, backends=[self.backend],
            refs=[('capability', key) for key in ('den', 'hall', 'shed')],
            build=BUILD, bridge_ip='192.0.2.200')
        self.addCleanup(self.house.close)
        self.house.start('127.0.0.1', ssdp=False)
        # Tests change bindings/capabilities explicitly; About's periodic worker
        # must not race assertions about a request's local effects.
        self.house.stop.set()
        self.den, self.hall, self.shed = self.house.players
        self.room = self.den.state.backend

    def rejected(self, player, path, service, action, args='', code='501'):
        status, body = soap(player, path, service, action, args)
        self.assertEqual(status, 500, body)
        fault = ET.fromstring(body).findtext('.//{urn:schemas-upnp-org:control-1-0}errorCode')
        self.assertEqual(fault, code, body)

    def snapshot(self, player=None):
        state = (player or self.den).state
        return copy.deepcopy({key: getattr(state, key) for key in
                              ('uri', 'metadata', 'play_mode', 'volume', 'mute',
                               'bass', 'treble', 'loudness', 'led', 'properties')}), state.config.room

    def test_unsupported_setters_never_mutate_the_standalone_peer(self):
        self.room.operations.discard('play_item')
        self.den.state.uri, self.den.state.metadata = 'original:source', 'original metadata'
        before = self.snapshot()
        cases = (
            (AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
             '<CurrentURI>http://example.invalid/new.mp3</CurrentURI><CurrentURIMetaData>new</CurrentURIMetaData>'),
            (AVT, 'AVTransport', 'SaveQueue', INSTANCE + '<Title>New list</Title><ObjectID></ObjectID>'),
            (AVT, 'AVTransport', 'ConfigureSleepTimer', INSTANCE + '<NewSleepTimerDuration>0:15:00</NewSleepTimerDuration>'),
            (RC, 'RenderingControl', 'SetBass', INSTANCE + '<DesiredBass>7</DesiredBass>'),
            (RC, 'RenderingControl', 'SetTreble', INSTANCE + '<DesiredTreble>-5</DesiredTreble>'),
            (RC, 'RenderingControl', 'SetLoudness', INSTANCE + '<Channel>Master</Channel><DesiredLoudness>0</DesiredLoudness>'),
            (RC, 'RenderingControl', 'SetOutputFixed', INSTANCE + '<DesiredFixed>1</DesiredFixed>'),
            (DP, 'DeviceProperties', 'SetLEDState', '<DesiredLEDState>Off</DesiredLEDState>'),
            (DP, 'DeviceProperties', 'SetZoneAttributes',
             '<DesiredZoneName>Renamed</DesiredZoneName><DesiredIcon>kitchen</DesiredIcon><DesiredConfiguration>1</DesiredConfiguration>'),
            (AC, 'AlarmClock', 'SetFormat', '<DesiredTimeFormat>12H</DesiredTimeFormat><DesiredDateFormat>MDY</DesiredDateFormat>'),
            (AC, 'AlarmClock', 'SetTimeNow', '<DesiredTime>2026-11-03 08:00:00</DesiredTime><TimeZoneForDesiredTime>UTC</TimeZoneForDesiredTime>'),
            (SP, 'SystemProperties', 'SetString', '<VariableName>R_InputEncoding</VariableName><StringValue>compressed</StringValue>'),
            (CD, 'ContentDirectory', 'DestroyObject', '<ObjectID>SQ:1</ObjectID>'),
            (CD, 'ContentDirectory', 'UpdateObject',
             '<ObjectID>SQ:1</ObjectID><CurrentTagValue>old</CurrentTagValue><NewTagValue>new</NewTagValue>'),
        )
        for path, service, action, args in cases:
            with self.subTest(action=action):
                self.rejected(self.den, path, service, action, args)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.backend.calls, [])

    def test_alarm_creation_is_not_a_success_shaped_stub(self):
        before = self.snapshot()
        self.rejected(self.den, AC, 'AlarmClock', 'CreateAlarm',
            '<StartLocalTime>07:00:00</StartLocalTime><Duration>01:00:00</Duration>'
            '<Recurrence>DAILY</Recurrence><Enabled>1</Enabled><RoomUUID>' + self.den.config.udn +
            '</RoomUUID><ProgramURI>x-rincon-buzzer:0</ProgramURI><ProgramMetaData></ProgramMetaData>'
            '<PlayMode>NORMAL</PlayMode><Volume>25</Volume><IncludeLinkedZones>0</IncludeLinkedZones>')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_contract_faults_precede_capability_checks(self):
        self.room.operations.clear()
        before = self.snapshot()
        self.rejected(self.den, AVT, 'AVTransport', 'SetUnsupportedAction', INSTANCE, code='401')
        self.rejected(self.den, AVT, 'AVTransport', 'Play', INSTANCE, code='402')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_no_queue_browse_does_not_call_a_catalog_or_sonos_service(self):
        self.rejected(self.den, CD, 'ContentDirectory', 'Browse',
            '<ObjectID>Q:0</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter>'
            '<StartingIndex>0</StartingIndex><RequestedCount>10</RequestedCount><SortCriteria></SortCriteria>')
        self.assertEqual(self.backend.content.browsed, [])
        self.assertEqual(self.backend.calls, [])

    def test_incompatible_groups_fail_before_uri_changes_or_backend_calls(self):
        before = self.snapshot(self.shed)
        self.rejected(self.shed, AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
            '<CurrentURI>x-rincon:' + self.den.config.udn + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(self.snapshot(self.shed), before)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(groups(self.house.topology()), [['Den'], ['Hall'], ['Shed']])

    def test_same_domain_is_insufficient_when_one_room_cannot_group(self):
        self.hall.state.backend.operations.discard('group')
        self.rejected(self.hall, AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
            '<CurrentURI>x-rincon:' + self.den.config.udn + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(self.backend.calls, [])

    def test_compatible_group_uses_one_backend_call(self):
        status, body = soap(self.hall, AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
            '<CurrentURI>x-rincon:' + self.den.config.udn + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(status, 200, body)
        self.assertEqual(self.backend.calls, [('den', 'join', 'hall')])
        self.assertTrue(wait_for(lambda: groups(self.house.topology()) == [['Den', 'Hall'], ['Shed']]))

    def test_about_only_endpoint_keeps_its_control_guard(self):
        bridge = self.house.about_players[zoneplayers.BRIDGE_KEY]
        self.rejected(bridge, AVT, 'AVTransport', 'Play', INSTANCE + '<Speed>1</Speed>')
        self.rejected(bridge, RC, 'RenderingControl', 'SetVolume',
                      INSTANCE + '<Channel>Master</Channel><DesiredVolume>80</DesiredVolume>')
        self.rejected(self.den, AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
            '<CurrentURI>x-rincon:' + bridge.config.udn + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(self.backend.calls, [])

    def test_fixed_output_is_not_advertised_by_the_compatibility_persona(self):
        status, body = soap(self.den, RC, 'RenderingControl', 'GetSupportsOutputFixed', INSTANCE)
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'CurrentSupportsFixed'), '0')

    def test_master_volume_works_and_balance_never_changes_master(self):
        for channel in ('LF', 'RF'):
            self.rejected(self.den, RC, 'RenderingControl', 'SetVolume', INSTANCE +
                '<Channel>' + channel + '</Channel><DesiredVolume>70</DesiredVolume>')
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.room.reported_volume, 20)
        status, body = soap(self.den, RC, 'RenderingControl', 'SetVolume',
                            INSTANCE + '<Channel>Master</Channel><DesiredVolume>55</DesiredVolume>')
        self.assertEqual(status, 200, body)
        self.assertTrue(wait_for(lambda: ('den', 'volume', 55) in self.backend.calls))
        self.assertEqual(self.backend.calls, [('den', 'volume', 55)])
        self.assertEqual(self.room.volume, 55)

    def test_backend_failure_does_not_stage_a_new_peer_uri(self):
        self.den.state.uri, self.den.state.metadata = 'original:source', 'original metadata'
        self.room.fail_play = True
        before = self.snapshot()
        self.rejected(self.den, AVT, 'AVTransport', 'SetAVTransportURI', INSTANCE +
            '<CurrentURI>' + STATION.uri + '</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_actions_and_evented_play_modes_follow_the_real_routes(self):
        status, body = soap(self.den, AVT, 'AVTransport', 'GetCurrentTransportActions', INSTANCE)
        self.assertEqual(status, 200, body)
        self.assertEqual(field(body, 'Actions'), 'Play,Pause')
        for operations, expected in (
            (set(), {'NORMAL'}),
            ({'shuffle'}, {'NORMAL', 'SHUFFLE_NOREPEAT'}),
            ({'repeat'}, {'NORMAL', 'REPEAT_ALL', 'REPEAT_ONE'}),
        ):
            self.room.operations.difference_update({'shuffle', 'repeat'})
            self.room.operations.update(operations)
            event = ET.fromstring(self.den.state.last_change())
            values = {element.tag.rpartition('}')[2]: element.get('val') for instance in event for element in instance}
            self.assertEqual(set(values['CurrentValidPlayModes'].split(',')), expected)
            self.assertEqual(values['CurrentTransportActions'], 'Play,Pause')
        # Repeat-only support cannot falsely accept a request that changes shuffle.
        before = self.snapshot()
        self.rejected(self.den, AVT, 'AVTransport', 'SetPlayMode',
                      INSTANCE + '<NewPlayMode>SHUFFLE_NOREPEAT</NewPlayMode>')
        self.assertEqual(self.room.mode, (False, 'off'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
