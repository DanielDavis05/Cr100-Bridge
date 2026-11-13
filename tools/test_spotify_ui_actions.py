"""Native occurrence actions use real SMAPI/player HTTP and loopback Sonos SOAP."""
import copy
from types import SimpleNamespace
import threading
import unittest
from urllib.parse import unquote
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from backends import BackendError
from fake_sonos import FakeHousehold, UPnPError
from sonos_backend import SonosBackend, SonosRoom
from sonos_upnp import UPnPFault
from spotify_autoplay import NativeTransport, Run, SpotifyAutoplay
from spotify_menu import default_items, scoped_id
from spotify_service import SpotifyService
from spotify_ui_actions import MAX_COLLECTION, OCCURRENCE_TTL, SpotifyUIActions
from test_ha_backend import wait_for
from test_smapi_stub import smapi, NS
from test_sonos_backend import KITCHEN, PATIO
from test_zoneplayers import HOUSEHOLD, soap, zp
from ui_events import decode_params, encode_request
import zoneplayers

ALBUM_ID, PLAYLIST_ID = 'a' * 22, 'p' * 22
ALBUM, PLAYLIST = 'spotify:album:' + ALBUM_ID, 'spotify:playlist:' + PLAYLIST_ID
MAC_A, MAC_B = '02:00:00:00:00:01', '02:00:00:00:00:02'
EXISTING = 'http://library.invalid/existing.mp3'


def track(index):
    ident = '%022d' % index
    return {'type': 'track', 'id': ident, 'name': 'Song ' + str(index), 'is_playable': True,
            'duration_ms': 100000, 'artists': [{'name': 'Artist'}],
            'album': {'id': ALBUM_ID, 'name': 'Album', 'images': []}}


class Catalogue:
    def __init__(self):
        self.rows = [track(i) for i in range(120)]
        self.rows[51] = copy.deepcopy(self.rows[50])  # Same recording, distinct occurrence.
        self.calls, self.revision, self.hook = [], 'snapshot-one', None

    def get(self, path, params=None):
        self.calls.append((path, copy.deepcopy(params)))
        if self.hook: self.hook(path, params or {})
        if path == '/playlists/' + PLAYLIST_ID and params and params.get('fields') == 'snapshot_id':
            return {'snapshot_id': self.revision}
        if path in ('/albums/' + ALBUM_ID, '/playlists/' + PLAYLIST_ID):
            return {'type': 'album' if '/albums/' in path else 'playlist',
                    'id': ALBUM_ID if '/albums/' in path else PLAYLIST_ID, 'name': 'Collection', 'images': []}
        if path.startswith('/tracks/'):
            return copy.deepcopy(next(row for row in self.rows if row['id'] == path[len('/tracks/'):]))
        if path in ('/albums/' + ALBUM_ID + '/tracks', '/playlists/' + PLAYLIST_ID + '/items'):
            rows = copy.deepcopy(self.rows[(params or {}).get('offset', 0):
                                          (params or {}).get('offset', 0) + (params or {}).get('limit', 50)])
            if path.endswith('/items'): rows = [{'item': row} for row in rows]
            return {'items': rows, 'total': len(self.rows)}
        raise AssertionError('Unexpected catalogue request: ' + path)


class SpotifyUIHTTP(unittest.TestCase):
    def setUp(self):
        self.logs, self.identities = [], {'127.0.0.1': MAC_A}
        self.fake = FakeHousehold()
        self.addCleanup(self.fake.close)
        self.speaker = self.fake.add(KITCHEN, 'Kitchen')
        self.member = self.fake.add(PATIO, 'Patio')
        self.fake.coordinator[PATIO] = KITCHEN
        self.speaker.queue = [(EXISTING, '')]
        original_position = self.speaker.do_GetPositionInfo
        def position(args):
            values = original_position(args)
            index = self.speaker.track_number - 1
            values['TrackURI'] = self.speaker.queue[index][0] if self.speaker.uri.startswith('x-rincon-queue:') \
                and 0 <= index < len(self.speaker.queue) else self.speaker.uri
            return values
        self.speaker.do_GetPositionInfo = position
        self.backend = SonosBackend([self.speaker.base], self.logs.append)
        self.backend.start()
        self.addCleanup(self.backend.close)
        self.assertTrue(self.backend.ready.wait(5), self.logs)
        self.assertTrue(wait_for(lambda: all(s.available and s.read_at for s in self.backend.speakers.values())))
        self.client = Catalogue()
        entries = default_items() + [dict(id='entry_album', kind='spotify', target=ALBUM, label='Album', locked=False),
                                     dict(id='entry_playlist', kind='spotify', target=PLAYLIST, label='Playlist', locked=False)]
        self.settings = {'controller_menus': {MAC_A: {'items': copy.deepcopy(entries)},
                                             MAC_B: {'items': copy.deepcopy(entries)}}}
        self.service = SpotifyService('127.0.0.1', 0, self.client, menu_settings=self.settings,
            controller_resolver=self.identities.get, log=self.logs.append)
        self.addCleanup(self.service.close)
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Kitchen', 'Patio', 'Simulated'], ['127.0.0.1'] * 3,
            self.logs.append, port=0, backends=[self.backend], refs=[('sonos', KITCHEN), ('sonos', PATIO), None],
            music_service=self.service, controller_resolver=self.identities.get)
        self.addCleanup(self.house.close)
        self.service.start()
        self.house.start('127.0.0.1', ssdp=False)
        self.player, self.member_player, self.simulated = self.house.players

    def browse(self, parent=ALBUM, index=0, count=50):
        alias = scoped_id('entry_album' if parent == ALBUM else 'entry_playlist', parent)
        status, doc = smapi(self.service, 'getMetadata', {'id': alias, 'index': index, 'count': count})
        self.assertEqual(status, 200, ET.tostring(doc))
        ids = [node.text for node in doc.findall('.//s:mediaMetadata/s:id', NS)]
        return alias, ids

    def send(self, request_id, parent, index, selected, *, action='play_from', player=None, params=None):
        args = params if params is not None else {'parent': '0004004c' + parent, 'index': str(index),
                                                  'track': '00032020' + selected}
        target = player or self.player
        wire = encode_request(request_id, 'spotify.240.' + action, target.config.udn, args)
        status, body = soap(target, '/SystemProperties/Control', 'SystemProperties', 'SetString',
            '<VariableName>CR100_UIEvent</VariableName><StringValue>' + escape(wire) + '</StringValue>')
        self.assertEqual(status, 200, body)
        status, body = soap(target, '/SystemProperties/Control', 'SystemProperties', 'GetString',
            '<VariableName>CR100_UIResult:' + request_id + '</VariableName>')
        self.assertEqual(status, 200, body)
        fields = list(map(unquote, ET.fromstring(body).find('.//StringValue').text.split('|')))
        return dict(status=fields[2], code=fields[3], message=fields[4], data=decode_params(fields[5]))

    def writes(self, speaker=None):
        return [(name, args) for name, args in (speaker or self.speaker).calls
                if name in ('RemoveAllTracksFromQueue', 'AddURIToQueue', 'SetAVTransportURI', 'Seek', 'Play')]

    def test_exact_duplicate_occurrence_beyond_first_page_and_native_scoped_ids(self):
        parent, _ = self.browse()
        _, ids = self.browse(index=50)
        self.assertEqual(self.writes(), [])
        result = self.send('duplicate-50', parent, 50, ids[0])
        self.assertEqual(result['status'], 'ok', result)
        actual = [unquote(uri) for uri, _ in self.speaker.queue]
        expected = ['spotify:track:' + row['id'] for row in self.client.rows[50:]]
        self.assertEqual(actual, expected)
        self.assertEqual(actual[:2], [expected[0], expected[0]])
        self.assertEqual(result['data']['tracks'], '70')
        self.assertEqual(self.speaker.transport, 'PLAYING')
        self.assertEqual(self.speaker.track_number, 1)
        self.assertTrue(any(params.get('offset') == 100 for path, params in self.client.calls
                            if path.endswith('/tracks') and params))

    def test_queue_from_preserves_existing_source_queue_and_duplicate_order(self):
        parent, ids = self.browse(index=100)
        self.speaker.uri, self.speaker.transport = 'x-sonos-vli:current', 'PLAYING'
        before = self.speaker.uri, self.speaker.transport
        result = self.send('append', parent, 118, ids[18], action='queue_from')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.speaker.queue[0][0], EXISTING)
        self.assertEqual([unquote(uri) for uri, _ in self.speaker.queue[1:]],
                         ['spotify:track:' + row['id'] for row in self.client.rows[118:]])
        self.assertEqual((self.speaker.uri, self.speaker.transport), before)
        self.assertFalse(any(name in ('RemoveAllTracksFromQueue', 'Play', 'Seek', 'SetAVTransportURI') for name, _ in self.writes()))

    def test_native_context_metadata_prefetch_preserves_exact_duplicate_suffix_for_play_and_queue(self):
        self.client.rows = [track(i) for i in range(70)]
        self.client.rows[4] = copy.deepcopy(self.client.rows[2])
        ticks = [100.0]
        self.service.ui_actions.clock = lambda: ticks[0]
        for action in ('play_from', 'queue_from'):
            with self.subTest(action=action):
                parent, ids = self.browse(count=500)
                self.browse(index=50, count=500)
                key = (MAC_A, parent)
                before = copy.deepcopy(self.service.ui_actions.contexts[key])
                ticks[0] += 10
                self.browse(index=0, count=1)
                for ident in (ids[0], ids[4]):
                    status, doc = smapi(self.service, 'getMediaMetadata', {'id': ident})
                    self.assertEqual(status, 200, ET.tostring(doc))
                self.assertEqual(self.service.ui_actions.contexts[key], before)
                self.speaker.queue = [(EXISTING, '')]
                self.speaker.uri, self.speaker.transport = 'x-sonos-vli:current', 'PLAYING'
                result = self.send('native-prefetch-' + action, parent, 4, ids[4], action=action)
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['data']['tracks'], '66')
                expected = ['spotify:track:' + row['id'] for row in self.client.rows[4:]]
                actual = [unquote(uri) for uri, _ in self.speaker.queue]
                self.assertEqual(actual, expected if action == 'play_from' else [EXISTING] + expected)
                if action == 'queue_from':
                    self.assertEqual(self.speaker.uri, 'x-sonos-vli:current')

    def test_native_metadata_prefetch_does_not_renew_expiry_or_authorize_another_controller(self):
        ticks = [100.0]
        self.service.ui_actions.clock = lambda: ticks[0]
        parent, ids = self.browse(count=500)
        self.browse(index=50, count=500)
        ticks[0] += OCCURRENCE_TTL - 1
        self.browse(count=1)
        ticks[0] += 1
        self.assertEqual(self.send('prefetch-expired', parent, 4, ids[4])['code'], 'expired_context')
        self.identities['127.0.0.1'] = MAC_B
        self.browse(count=1)
        self.assertEqual(self.send('prefetch-other-controller', parent, 4, ids[4])['code'], 'unavailable_context')
        self.assertEqual(self.writes(), [])

    def test_native_metadata_prefetch_does_not_preserve_changed_snapshot_or_catalogue(self):
        parent, ids = self.browse(PLAYLIST, count=500)
        self.browse(PLAYLIST, index=50, count=500)
        self.client.revision = 'snapshot-two'
        self.browse(PLAYLIST, count=1)
        self.assertEqual(self.send('prefetch-revision', parent, 4, ids[4])['code'], 'unissued_occurrence')
        parent, ids = self.browse(count=500)
        self.service.invalidate_catalog()
        self.browse(count=1)
        self.assertEqual(self.send('prefetch-catalogue', parent, 4, ids[4])['code'], 'unissued_occurrence')
        self.settings['controller_menus'][MAC_A]['items'][-2]['locked'] = True
        status, _ = smapi(self.service, 'getMetadata', {'id': parent, 'index': 0, 'count': 1})
        self.assertEqual(status, 500)
        self.assertEqual(self.send('prefetch-locked', parent, 4, ids[4])['code'], 'feature_locked')
        self.assertEqual(self.writes(), [])

    def test_unshown_occurrence_wrong_controller_and_expiry_do_not_write(self):
        parent, ids = self.browse()
        result = self.send('unshown', parent, 60, ids[0])
        self.assertEqual(result['code'], 'unissued_occurrence')
        self.identities['127.0.0.1'] = MAC_B
        self.assertEqual(self.send('other-controller', parent, 0, ids[0])['code'], 'unavailable_context')
        self.identities['127.0.0.1'] = MAC_A
        self.service.ui_actions.clock = lambda: 10**12
        self.assertEqual(self.send('expired', parent, 0, ids[0])['code'], 'expired_context')
        self.assertEqual(self.writes(), [])

    def test_route_locks_rebinding_and_catalogue_invalidation_withdraw_actions(self):
        parent, ids = self.browse()
        entry = self.settings['controller_menus'][MAC_A]['items'][-2]
        entry['locked'] = True
        self.assertEqual(self.send('locked', parent, 0, ids[0])['code'], 'feature_locked')
        entry['locked'], entry['target'] = False, PLAYLIST
        self.assertEqual(self.send('rebound', parent, 0, ids[0])['status'], 'error')
        entry['target'] = ALBUM
        self.service.invalidate_catalog()
        self.assertEqual(self.send('catalogue', parent, 0, ids[0])['code'], 'expired_context')
        self.assertEqual(self.writes(), [])

    def test_reordered_displayed_album_rows_are_rejected(self):
        parent, ids = self.browse()
        self.client.rows[0], self.client.rows[1] = self.client.rows[1], self.client.rows[0]
        self.assertEqual(self.send('reordered', parent, 0, ids[0])['code'], 'collection_changed')
        self.assertEqual(self.speaker.queue, [(EXISTING, '')])
        self.assertEqual(self.writes(), [])

    def test_playlist_snapshot_changed_since_browse_or_during_fetch_is_rejected(self):
        parent, ids = self.browse(PLAYLIST)
        self.client.revision = 'snapshot-two'
        self.assertEqual(self.send('before-changed', parent, 0, ids[0])['code'], 'collection_changed')
        parent, ids = self.browse(PLAYLIST)
        def change_midway(path, params):
            if path.endswith('/items') and params.get('offset') == 50: self.client.revision = 'snapshot-three'
        self.client.hook = change_midway
        self.assertEqual(self.send('during-changed', parent, 0, ids[0])['code'], 'collection_changed')
        self.assertEqual(self.writes(), [])

    def test_playlist_snapshot_provenance_refetches_new_revision_instead_of_cached_rows(self):
        self.browse(PLAYLIST)
        self.client.rows[0], self.client.rows[1] = self.client.rows[1], self.client.rows[0]
        self.client.revision = 'snapshot-two'
        parent, ids = self.browse(PLAYLIST)
        result = self.send('fresh-playlist', parent, 49, ids[49])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(unquote(self.speaker.queue[0][0]), 'spotify:track:' + self.client.rows[49]['id'])

    def test_unavailable_tail_and_encoding_failure_validate_before_clear(self):
        parent, ids = self.browse()
        self.client.rows[119]['is_playable'] = False
        self.assertEqual(self.send('unavailable', parent, 49, ids[49])['code'], 'unavailable_track')
        self.client.rows[119]['is_playable'] = True
        original = self.backend.playback_adapters.prepare
        def reject_last(uri, kind):
            if uri.endswith(self.client.rows[119]['id']): raise BackendError('Encoding refused')
            return original(uri, kind)
        self.backend.playback_adapters.prepare = reject_last
        self.assertEqual(self.send('encoding', parent, 49, ids[49])['status'], 'error')
        self.assertEqual(self.speaker.queue, [(EXISTING, '')])
        self.assertEqual(self.writes(), [])

    def test_collection_limit_unsupported_room_and_timeout_fail_before_queue_mutation(self):
        self.client.rows = [track(index) for index in range(1001)]
        parent, ids = self.browse()
        self.assertEqual(self.send('too-large', parent, 0, ids[0])['code'], 'unsupported_collection')
        before = len(self.client.calls)
        self.assertEqual(self.send('simulated', parent, 0, ids[0], player=self.simulated)['code'], 'unsupported_room')
        self.assertEqual(len(self.client.calls), before)
        self.client.rows = [track(index) for index in range(120)]
        self.service.invalidate_catalog()
        parent, ids = self.browse()
        ticks = [100.0]
        self.service.ui_actions.clock = lambda: ticks[0]
        def slow(path, params):
            if path.endswith('/tracks'): ticks[0] += 21
        self.client.hook = slow
        self.assertEqual(self.send('slow', parent, 0, ids[0])['code'], 'loading_timeout')
        self.assertEqual(self.writes(), [])

    def test_exact_prepared_plans_are_not_rebuilt_after_clearing(self):
        parent, ids = self.browse(index=100)
        original = self.backend.playback_adapters.prepare
        seen = set()
        def once(uri, kind):
            if uri in seen: raise AssertionError('Prepared again after validation')
            seen.add(uri)
            return original(uri, kind)
        self.backend.playback_adapters.prepare = once
        result = self.send('prepare-once', parent, 118, ids[18])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(seen), 2)

    def test_partial_write_reports_uncertainty_and_identical_retry_does_not_repeat_it(self):
        parent, ids = self.browse(index=100)
        original = self.speaker.do_AddURIToQueue
        def fail_third(args):
            if len(self.speaker.queue) >= 2: raise UPnPError(800)
            return original(args)
        self.speaker.do_AddURIToQueue = fail_third
        result = self.send('partial', parent, 117, ids[17])
        self.assertEqual(result['code'], 'partial_queue', result)
        self.assertIn('Confirmed 2 of 3', result['message'])
        self.assertIn('may also have changed', result['message'])
        writes, reads = len(self.writes()), len(self.client.calls)
        self.assertEqual(self.send('partial', parent, 117, ids[17]), result)
        self.assertEqual((len(self.writes()), len(self.client.calls)), (writes, reads))
        self.assertEqual(len(self.speaker.queue), 2)

    def test_member_targets_real_coordinator_and_autoplay_cancels_then_adopts_explicit_queue(self):
        parent, ids = self.browse(index=100)
        policy = SpotifyAutoplay(self.client, None, lambda: True, self.logs.append)
        policy.start = Mock()
        self.addCleanup(policy.close)
        self.service.playback_policy = policy
        self.house.playback_policies = [policy]
        room = self.backend.room(KITCHEN)
        cancelled = threading.Event()
        key = policy._key(room)
        policy.runs[key] = SimpleNamespace(cancelled=cancelled)
        result = self.send('adopt', parent, 118, ids[18], player=self.member_player)
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(cancelled.is_set())
        self.assertIn(key, policy.runs)
        self.assertEqual(policy.runs[key].mode, 'queue')
        self.assertEqual(policy.runs[key].queue, [uri for uri, _ in self.speaker.queue])
        self.assertEqual(self.writes(self.member), [])

    def test_provider_names_are_unique_for_multiple_service_instances(self):
        other = SpotifyService('127.0.0.1', 0, self.client, service_id=241, log=self.logs.append)
        self.addCleanup(other.close)
        self.house.ui_events.register_provider(other)
        self.assertTrue({'spotify.240.play_from', 'spotify.240.queue_from',
                         'spotify.241.play_from', 'spotify.241.queue_from'} <= self.house.ui_events.handlers.keys())

    def test_queue_from_journals_pending_explicit_tracks_without_interrupting_recommendation(self):
        parent, ids = self.browse(index=100)
        policy = SpotifyAutoplay(self.client, None, lambda: True, self.logs.append)
        policy.start = Mock()
        self.addCleanup(policy.close)
        self.service.playback_policy = policy
        self.house.playback_policies = [policy]
        room = self.backend.room(KITCHEN)
        run = Run(room, NativeTransport(room), 'native', 'spotify:track:' + self.client.rows[0]['id'],
                  queue=[EXISTING])
        key = policy._key(room)
        policy.runs[key] = run
        self.speaker.uri, self.speaker.transport = 'x-sonos-vli:current-recommendation', 'PLAYING'
        result = self.send('journal', parent, 118, ids[18], action='queue_from')
        self.assertEqual(result['status'], 'ok', result)
        self.assertIs(policy.runs[key], run)
        self.assertFalse(run.cancelled.is_set())
        self.assertEqual(run.queue, [uri for uri, _ in self.speaker.queue])
        self.assertEqual(self.speaker.uri, 'x-sonos-vli:current-recommendation')
        self.assertEqual(self.speaker.transport, 'PLAYING')

    def test_menu_lock_during_writes_stops_remaining_additions_with_honest_receipt(self):
        parent, ids = self.browse(index=100)
        original = self.speaker.do_AddURIToQueue
        def apply_then_lock(args):
            result = original(args)
            self.settings['controller_menus'][MAC_A]['items'][-2]['locked'] = True
            return result
        self.speaker.do_AddURIToQueue = apply_then_lock
        result = self.send('lock-during-write', parent, 118, ids[18])
        self.assertEqual(result['code'], 'partial_queue', result)
        self.assertIn('Confirmed 1 of 2', result['message'])
        self.assertEqual(len(self.speaker.queue), 1)

    def test_deadline_after_encoding_and_group_change_during_load_fail_before_clear(self):
        parent, ids = self.browse(index=100)
        ticks = [100.0]
        self.service.ui_actions.clock = lambda: ticks[0]
        original = self.backend.playback_adapters.prepare
        def slow_prepare(uri, kind):
            result = original(uri, kind)
            ticks[0] += 21
            return result
        self.backend.playback_adapters.prepare = slow_prepare
        self.assertEqual(self.send('encoding-timeout', parent, 118, ids[18])['code'], 'loading_timeout')
        self.backend.playback_adapters.prepare = original
        def regroup(path, params):
            if path.endswith('/tracks'):
                with self.backend.lock:
                    self.backend.groups = [(KITCHEN, [KITCHEN]), (PATIO, [PATIO])]
        self.client.hook = regroup
        result = self.send('regroup', parent, 118, ids[18], player=self.member_player)
        self.assertEqual(result['code'], 'room_changed', result)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.writes(self.member), [])

    def test_playlist_without_snapshot_still_browses_but_cannot_mutate_queue(self):
        self.client.revision = None
        parent, ids = self.browse(PLAYLIST)
        self.assertTrue(ids)
        result = self.send('no-snapshot', parent, 0, ids[0])
        self.assertEqual(result['code'], 'unverifiable_collection', result)
        self.assertEqual(self.writes(), [])

    def test_lost_acknowledgement_does_not_try_another_encoding_and_duplicate_the_track(self):
        parent, ids = self.browse(index=100)
        original = SonosRoom.add_to_queue
        attempts = []
        def add_then_lose_ack(room, uri, metadata, as_next=False):
            attempts.append(uri)
            original(room, uri, metadata, as_next)  # Real local Sonos SOAP succeeds.
            raise BackendError('Unknown delivery outcome') from UPnPFault('AddURIToQueue', None, 'lost acknowledgement')
        with patch.object(SonosRoom, 'add_to_queue', add_then_lose_ack):
            result = self.send('lost-ack', parent, 118, ids[18])
        self.assertEqual(result['code'], 'partial_queue', result)
        self.assertIn('Confirmed 0 of 2', result['message'])
        self.assertIn('may also have changed', result['message'])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.speaker.queue), 1)


class MetadataPrefetchGuard(unittest.TestCase):
    def test_only_matching_explicit_singleton_preserves_existing_finite_browse(self):
        cases = ('normal_page_zero', 'unspecified_count', 'expired', 'first_row_changed',
                 'total_changed', 'snapshot_changed', 'version_changed', 'policy_changed',
                 'no_observed_zero', 'only_observed_zero', 'oversized')
        for case in cases:
            with self.subTest(case=case):
                ticks = [100.0]
                entry = dict(id='entry_album', kind='spotify', target=ALBUM)
                service = SimpleNamespace(request=SimpleNamespace(entry='entry_album', address='controller'),
                                          controller_resolver=lambda address: MAC_A, catalog_version=1,
                                          _entry=lambda ident: entry)
                actions = SpotifyUIActions(service, clock=lambda: ticks[0])
                nodes = [{'id': 'spotify:track:' + track(i)['id']} for i in range(3)]
                actions.record(ALBUM, 0, 70, nodes, snapshot='revision')
                key = (MAC_A, scoped_id('entry_album', ALBUM))
                epoch = actions.contexts[key]['epoch']
                kwargs = dict(snapshot='revision', requested_count=1)
                total, index = 70, 0
                if case == 'normal_page_zero': kwargs['requested_count'] = 50
                if case == 'unspecified_count': kwargs.pop('requested_count')
                if case == 'expired': ticks[0] += OCCURRENCE_TTL
                if case == 'first_row_changed': nodes[0] = nodes[1]
                if case == 'total_changed': total = 71
                if case == 'snapshot_changed': kwargs['snapshot'] = 'other-revision'
                if case == 'version_changed': service.catalog_version += 1
                if case == 'policy_changed': entry['target'] = PLAYLIST
                if case == 'no_observed_zero': actions.contexts[key]['observed'].pop(0)
                if case == 'only_observed_zero': actions.contexts[key]['observed'] = {0: nodes[0]['id']}
                if case == 'oversized': total = MAX_COLLECTION + 1
                actions.record(ALBUM, index, total, nodes[:1], **kwargs)
                current = actions.contexts[key]
                self.assertNotEqual(current['epoch'], epoch)
                self.assertEqual(current['observed'], {} if case == 'oversized' else {0: nodes[0]['id']})


if __name__ == '__main__': unittest.main()
