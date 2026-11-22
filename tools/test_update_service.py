"""Production update adapter through real Household HTTP, GENA, UDP and TCP."""
import html
import http.client
import http.server
import io
import json
from pathlib import Path
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import bridge_updater
import build_info
import distribution
import online_updates
import update_service
import zoneplayers
from test_backends import MemoryBackend
from test_controller_components import SourceAddresses, request, soap, topology_body
from test_ha_backend import field, wait_for
from test_online_updates import (build_test_package, build_test_cramfs, build_test_movie,
                                 build_test_descriptor)
from test_zoneplayers import HOUSEHOLD, M_SEARCH, zp

A, B = '00:0e:58:01:02:03', '00:0e:58:04:05:06'
IP_A, IP_B, UNKNOWN = '127.0.0.2', '127.0.0.3', '127.0.0.4'
ADDRESSES = {IP_A: A, IP_B: B}
CHECK = '<UpdateType>Software</UpdateType><CachedOnly>0</CachedOnly><Version></Version>'
NS = {'d': 'urn:schemas-upnp-org:device-1-0'}


class UpdateServiceIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'bridge-config.json'
        self.config.write_text('{}')
        self.package, modulus = build_test_package()
        # Only the invented test package's public modulus changes in this
        # process. Production verification retains its pinned vendor public key.
        key = patch.object(online_updates, 'VENDOR_MODULUS', modulus)
        key.start()
        self.addCleanup(key.stop)
        self.backend = MemoryBackend('memory')
        self.logs = []
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], self.logs.append,
            port=0, backends=[self.backend], refs=[('memory', 'den')], bridge_ip='192.0.2.200',
            controller_resolver=ADDRESSES.get, controller_addresses=lambda: dict(ADDRESSES))
        self.addCleanup(self.house.close)
        self.manager = online_updates.UpdateManager(self.config)
        self.service = update_service.UpdateService(self.house, self.manager, '127.0.0.1',
            tls_port=None, http_port=0, tcp_port=0, phase_delay=0, recover=False)
        self.house.updates = self.service
        self.house.start('127.0.0.1', ssdp=False)
        # Drive tick/GENA explicitly rather than racing the periodic identity job.
        self.house.stop.set()
        self.room = self.house.players[0]
        self.bridge = self.house.about_players[zoneplayers.BRIDGE_KEY]
        self.assertEqual(len(self.house.devices()), 2)
        servers = [player.http for player in self.house.devices()] + self.service.transport.servers
        self.sources = SourceAddresses(servers, [IP_A, IP_B, UNKNOWN])
        self.addCleanup(self.sources.close)
        for player in self.house.devices():
            player.http._test_source_addresses = self.sources
            player.config.callback_network = '127.0.0.0/8'

    def stage(self, force=False, version=(25, 2, 50130), current='24.1-75101'):
        package, _ = build_test_package(version=version)
        return self.manager.stage_controller(A, package, filename='untrusted-name-99.9.upd',
            force_prompt=force, current_version=current)

    def check(self, source=IP_A, player=None, current='24.1-75101', demonstration=False):
        if demonstration:
            self.service.observe(source, 'Sonos/%s (CR100)' % current)
            return ET.fromstring(self.service.check(source,
                dict(UpdateType='Software', CachedOnly='0', Version=''), demonstration=True)['UpdateItem'])
        status, body, _ = soap(player or self.room, source, 'ZoneGroupTopology', 'CheckForUpdate', CHECK,
            extra_headers={'User-Agent': 'Linux UPnP/1.0 Sonos/%s (CR100)' % current})
        self.assertEqual(status, 200, body)
        item = field(body, 'UpdateItem')
        return ET.fromstring(item) if item else None

    def test_same_version_selection_is_offered_with_install_allowed(self):
        self.stage(current='25.2-50130')
        item = self.check(current='25.2-50130')
        self.assertEqual(item.get('Version'), self.service.version)
        status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(status, 200, body)
        manifest = ET.fromstring(body)
        self.assertEqual(manifest.get('system_version'), self.service.version)
        images = {image.get('model'): image for image in manifest.find('update_list')}
        self.assertEqual(images['1'].get('version'), self.service.version)
        self.assertEqual(images['2'].get('version'), '25.2-50130')
        self.assertTrue(images['2'].text.endswith('/25.2-50130/^25.2-50130'))
        message = self.service.message(A)
        self.assertEqual(message['AllowUpdate'], 'true')
        selected = self.manager.selection(A, '25.2-50130')
        self.assertIsNotNone(selected)
        self.assertEqual(self.manager.authorize_download(selected['token'], A).read_bytes(), self.package)

    def test_native_update_message_names_target_and_custom_release(self):
        self.stage(current='25.2-50130')
        self.check(current='25.2-50130')
        official = self.service.message(A)
        self.assertEqual(official['MessageHeader'], 'Official controller firmware')
        self.assertIn('Official Sonos firmware 25.2-50130', official['MessageBody'])
        self.assertIn('Official Sonos firmware 25.2-50130', official['IntroMessageBody'])
        self.assertEqual(official['ContinuePrompt'], 'Install official firmware')

        marker = next(build for build, release in online_updates.KNOWN_CFW_BUILDS.items()
                      if release == 'R15')
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        package = package[:-1] + bytes((package[-1] ^ 1,))
        staged = self.manager.stage_controller(A, package, arm=False,
            current_version='25.2-50130')
        self.manager.arm_controller(A, current_version='25.2-50130',
            expected_sha256=staged['sha256'], allow_unverified=True)
        self.check(current='25.2-50130')
        custom = self.service.message(A)
        self.assertEqual(custom['MessageHeader'], 'Custom controller firmware')
        self.assertIn('Custom firmware R15 (25.2-50130)', custom['MessageBody'])
        self.assertIn("bridge's one-time Install custom firmware setup", custom['MessageBody'])
        self.assertIn('instead of installing here', custom['MessageBody'])
        self.assertIn('R15', custom['IntroMessageBody'])
        self.assertEqual(custom['ContinuePrompt'], 'Install custom firmware')

        self.manager.cancel_controller(A)
        self.check(current='25.2-50130', demonstration=True)
        bridge = self.service.message(A)
        self.assertEqual(bridge['MessageHeader'], 'Bridge software update')
        self.assertIn('controller firmware will stay unchanged', bridge['MessageBody'])
        self.assertIn('controller firmware stays unchanged', bridge['IntroMessageBody'])
        for message in (official, custom, bridge):
            for field in ('MessageHeader', 'IntroMessageHeader', 'ContinuePrompt', 'CancelPrompt'):
                self.assertLessEqual(len(message[field].encode('utf-8')), 39)
            for field in ('MessageBody', 'IntroMessageBody'):
                self.assertLessEqual(len(message[field].encode('utf-8')), 510)

    def test_paired_native_build_report_completes_only_its_exact_downloaded_image(self):
        marker = 'cfw3:' + 'ab' * 32
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        self.manager.stage_controller(A, package, current_version='25.2-50130')
        self.check(current='25.2-50130')

        def report(source, value, version='25.2-50130'):
            return soap(self.room, source, 'SystemProperties', 'SetString',
                '<VariableName>CR100_CFWBuild</VariableName><StringValue>%s</StringValue>' % html.escape(value),
                path='/SystemProperties/Control', extra_headers={
                    'User-Agent': 'Linux UPnP/1.0 Sonos/%s (CR100)' % version})[0]

        self.assertEqual(report(IP_A, marker), 200)
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        self.assertEqual(self.manager.firmware_identity(A)['classification'], 'custom')
        self.assertEqual(self.manager.firmware_identity(B)['classification'], 'stock')
        self.manager.controller_started(self.manager.selection(A)['token'], A)
        self.assertEqual(report(IP_B, marker), 500)
        self.assertEqual(self.manager.firmware_identity(B)['classification'], 'stock')
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(report(UNKNOWN, marker), 500)
        for value, version in (('v3', '25.2-50130'), ('cfw3:' + 'cd' * 32, '25.2-50130'),
                               (marker, '24.1-75101')):
            self.assertEqual(report(IP_A, value, version), 500)
            self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(report(IP_A, marker), 200)
        self.assertEqual(self.manager.status(A)['status'], 'completed')
        self.assertEqual(self.manager.status(A)['installed_cfw_build_id'], marker)
        inert = self.check(current='25.2-50130')
        self.assertIsNotNone(inert)
        self.assertEqual(self.service.offers[A]['kind'], 'none')
        self.assertEqual(self.backend.calls, [])

    def test_known_build_report_marks_only_the_reporting_controller_live(self):
        # The combined R16 controller reports this ID with either UI cadence
        # and may have been installed before the bridge ever saw an UPD.
        marker = 'cfw3:7e6879933c4a4ecee735e5f3f6d83201a2403c82b86dbffaa4c7a18a4ece016a'
        now = [1000.0]
        self.manager.clock = lambda: now[0]
        body = ('<VariableName>CR100_CFWBuild</VariableName>'
                '<StringValue>%s</StringValue>' % marker)

        def report(source):
            return soap(self.room, source, 'SystemProperties', 'SetString', body,
                path='/SystemProperties/Control', extra_headers={
                    'User-Agent': 'Linux UPnP/1.0 Sonos/25.2-50130 (CR100)'})[0]

        self.assertEqual(self.manager.firmware_identity(A)['classification'], 'stock')
        self.assertEqual(report(IP_A), 200)
        identity = self.manager.firmware_identity(A)
        self.assertEqual(identity['classification'], 'custom')
        self.assertEqual(identity['release'], 'R16')
        self.assertEqual(self.manager.status(A)['status'], 'none')
        self.assertEqual(self.manager.firmware_identity(B)['classification'], 'stock')
        self.assertEqual(report(UNKNOWN), 500)
        now[0] += online_updates.LIVE_CFW_TTL
        self.assertEqual(self.manager.firmware_identity(A)['classification'], 'stock')

    def test_staged_r16_package_gets_release_label_when_bridge_is_updated(self):
        marker = 'cfw3:7e6879933c4a4ecee735e5f3f6d83201a2403c82b86dbffaa4c7a18a4ece016a'
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        package = package[:-1] + bytes((package[-1] ^ 1,))
        original_catalog = {build: label for build, label in online_updates.KNOWN_CFW_BUILDS.items()
                            if label != 'R16'}
        with patch.object(online_updates, 'KNOWN_CFW_BUILDS', original_catalog):
            staged = self.manager.stage_controller(A, package, arm=False,
                current_version='25.2-50130')
            self.assertIsNone(staged['cfw_release'])

        self.assertEqual(self.manager.status(A)['cfw_release'], 'R16')
        self.manager.arm_controller(A, current_version='25.2-50130',
            expected_sha256=staged['sha256'], allow_unverified=True)
        self.check(current='25.2-50130')
        message = self.service.message(A)
        self.assertIn('Custom firmware R16 (25.2-50130)', message['MessageBody'])

    def test_future_signed_release_live_and_exact_install_completion(self):
        marker = 'cfw3:' + 'ab' * 32
        release = 'R123.4-rc2'
        signed, modulus = build_test_descriptor(marker, release)
        stale, _ = build_test_descriptor('cfw3:' + 'cd' * 32, 'R122')
        self.assertNotIn(marker, online_updates.KNOWN_CFW_BUILDS)
        now = [1000.0]
        self.manager.clock = lambda: now[0]

        def report(source, value, property_name=online_updates.CFW_RELEASE_PROPERTY,
                   version='25.2-50130'):
            body = ('<VariableName>%s</VariableName><StringValue>%s</StringValue>'
                    % (property_name, html.escape(value)))
            return soap(self.room, source, 'SystemProperties', 'SetString', body,
                path='/SystemProperties/Control', extra_headers={
                    'User-Agent': 'Linux UPnP/1.0 Sonos/%s (CR100)' % version})[0]

        with patch.object(online_updates, '_CFW_MODULUS', modulus):
            self.assertEqual(report(UNKNOWN, signed), 500)
            self.assertEqual(report(IP_A, signed.replace(release, 'R900')), 500)
            self.assertEqual(self.manager.firmware_identity(A)['classification'], 'stock')
            self.assertEqual(report(IP_A, signed), 200)
            self.assertEqual(self.manager.status(A)['status'], 'none')
            self.assertEqual(self.manager.firmware_identity(B)['classification'], 'stock')
            reopened = online_updates.UpdateManager(self.config, clock=lambda: now[0])
            self.assertEqual(reopened.firmware_identity(A)['release'], release)
            now[0] += online_updates.LIVE_CFW_TTL - 1
            self.assertEqual(report(IP_A, marker, 'CR100_CFWBuild'), 200)
            self.assertEqual(reopened.firmware_identity(A)['release'], release)
            now[0] += 1
            self.assertEqual(reopened.firmware_identity(A)['classification'], 'stock')

            package, _ = build_test_package(rootfs=build_test_cramfs(
                build_test_movie(marker + '\0' + signed)))
            package = package[:-1] + bytes((package[-1] ^ 1,))
            staged = self.manager.stage_controller(A, package, arm=False, current_version='25.2-50130')
            self.assertEqual(staged['cfw_release'], release)
            self.assertEqual(self.manager.firmware_identity(A)['classification'], 'stock')
            self.manager.arm_controller(A, current_version='25.2-50130',
                expected_sha256=staged['sha256'], allow_unverified=True)
            self.manager.controller_started(self.manager.selection(A)['token'], A)
            self.assertEqual(report(IP_B, signed), 200)
            self.assertEqual(report(IP_A, marker, 'CR100_CFWBuild'), 200)
            self.assertEqual(report(IP_A, stale), 500)
            self.assertEqual(report(IP_A, signed, version='24.1-75101'), 500)
            self.assertEqual(self.manager.status(A)['status'], 'downloading')
            self.assertEqual(report(IP_A, signed), 200)
            self.assertEqual(self.manager.status(A)['completion_evidence'], 'installed-movie-build')
            self.assertEqual(self.manager.firmware_identity(A)['release'], release)

    def test_bridge_manifest_omits_controller_without_an_armed_package(self):
        now = [time.time()]
        self.manager.clock = lambda: now[0]
        # Omitting an unchanged handheld works for both stock versions and is
        # required by clients that can deliberately reinstall the same version.
        for current in ('24.1-75101', '25.2-50130'):
            for state in ('none', 'staged', 'cancelled', 'expired'):
                with self.subTest(current=current, state=state):
                    if state != 'none':
                        self.manager.stage_controller(A, self.package, arm=False, current_version=current)
                        if state in ('cancelled', 'expired'):
                            self.manager.arm_controller(A, current_version=current)
                            if state == 'cancelled':
                                self.manager.cancel_controller(A)
                            else:
                                now[0] += self.manager.ttl + 1
                    inert = self.check(current=current)
                    inert_path = urlsplit(inert.get('ManifestURL')).path
                    status, body, _ = self.update_http(IP_A, inert_path)
                    self.assertEqual(status, 200)
                    manifest = ET.fromstring(body)
                    self.assertEqual(manifest.get('system_version'), current)
                    images = {image.get('model'): image for image in manifest.find('update_list')}
                    self.assertEqual(set(images), {'1'})
                    self.assertEqual(images['1'].get('version'), self.service.version)
                    self.assertEqual(self.service.offers[A]['kind'], 'none')
                    previous = self.service.coordinator.status(A)
                    self.assertTrue(previous is None or previous['kind'] == 'bridge')
                    item = self.check(current=current, demonstration=True)
                    self.assertEqual(item.get('Version'), current)
                    status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
                    self.assertEqual(status, 200, body)
                    manifest = ET.fromstring(body)
                    self.assertEqual(manifest.get('system_version'), current)
                    images = {image.get('model'): image for image in manifest.find('update_list')}
                    self.assertEqual(set(images), {'1'})
                    self.assertEqual(images['1'].get('version'), self.service.coordinator.status(A)['target_version'])
                    self.assertEqual(self.service.message(A)['MessageHeader'], 'Bridge software update')
                    self.assertEqual(self.backend.calls, [])

    def test_legacy_none_manifest_cannot_offer_a_controller_placeholder(self):
        for current in ('24.1-75101', '25.2-50130'):
            offer = {'kind': 'none', 'token': 'legacy-no-update', 'version': current,
                     'controller_version': current}
            self.assertEqual(ET.fromstring(self.service.update_item(offer)).get('Version'), current)
            manifest = ET.fromstring(self.service.manifest(A, offer))
            self.assertEqual(manifest.get('system_version'), current)
            images = manifest.find('update_list')
            self.assertEqual([image.get('model') for image in images], ['1'])

    def test_unarmed_inert_manifest_is_source_bound_fresh_and_never_arms_players(self):
        first = self.check(current='25.2-50130')
        self.assertEqual(first.get('Version'), '25.2-50130')
        path = urlsplit(first.get('ManifestURL')).path
        self.assertEqual(self.update_http(IP_A, path)[0], 200)
        self.assertNotEqual(self.update_http(IP_B, path)[0], 200)
        self.assertEqual(self.update_http(UNKNOWN, path)[0], 403)
        token = self.service.offers[A]['token']
        url = '%s/updates/bridge/%s/begin' % (self.service.origin, token)
        self.assertEqual(self.begin(self.room, url)[0], 500)
        self.assertIsNone(self.service.coordinator.status(A))
        second = self.check(current='25.2-50130')
        self.assertNotEqual(first.get('ManifestURL'), second.get('ManifestURL'))
        self.assertNotEqual(self.update_http(IP_A, path)[0], 200)
        path = urlsplit(second.get('ManifestURL')).path
        now = self.manager.clock()
        with patch.object(self.manager, 'clock', return_value=now + 901):
            self.assertEqual(self.update_http(IP_A, path)[0], 410)
        self.stage(current='25.2-50130')
        self.assertEqual(self.update_http(IP_A, path)[0], 410)
        self.assertEqual(self.backend.calls, [])

    def test_bridge_manifest_prefers_latest_controller_report_and_keeps_player_target(self):
        offer = self.service.coordinator.offer(A, [self.room.config.udn], self.service.version)
        offer['controller_version'] = '24.1-75101'
        self.service.observe(IP_A, 'Linux UPnP/1.0 Sonos/25.2-50130 (CR100)')
        manifest = ET.fromstring(self.service.manifest(A, offer))
        self.assertEqual(manifest.get('system_version'), '25.2-50130')
        images = {image.get('model'): image for image in manifest.find('update_list')}
        self.assertEqual(set(images), {'1'})
        self.assertEqual(images['1'].get('version'), offer['target_version'])

    def test_bridge_manifest_does_not_invent_an_unknown_controller_version(self):
        offer = self.service.coordinator.offer(A, [self.room.config.udn], self.service.version)
        self.assertEqual(ET.fromstring(self.service.update_item(offer)).get('Version'), offer['target_version'])
        manifest = ET.fromstring(self.service.manifest(A, offer))
        self.assertEqual(manifest.get('system_version'), offer['target_version'])
        self.assertEqual([image.get('model') for image in manifest.find('update_list')], ['1'])

    def description(self, source):
        status, body, _ = request(self.room, source, 'GET', '/xml/device_description.xml')
        self.assertEqual(status, 200)
        return ET.fromstring(body).find('d:device', NS)

    def topology(self, source):
        status, body, _ = soap(self.room, source, 'ZoneGroupTopology', 'GetZoneGroupState')
        self.assertEqual(status, 200, body)
        return ET.fromstring(topology_body(body))

    def update_http(self, source, path, method='GET', headers=None):
        server = self.service.transport.servers[0]
        connection = self.sources.connection(*server.server_address, source)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def callback(self):
        events = queue.Queue()

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                events.put((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for source, path in ((IP_A, 'a'), (IP_B, 'b')):
            status, _, _ = request(self.room, source, 'SUBSCRIBE', '/ZoneGroupTopology/Event', headers={
                'CALLBACK': '<http://127.0.0.1:%d/%s>' % (server.server_port, path),
                'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
            self.assertEqual(status, 200)
        initial = {path: body for path, body in (events.get(timeout=3), events.get(timeout=3))}
        return events, initial

    def events_for(self, events, predicate, count=2):
        found, deadline = {}, time.monotonic() + 3
        while len(found) < count and time.monotonic() < deadline:
            path, body = events.get(timeout=max(.01, deadline - time.monotonic()))
            topology = ET.fromstring(topology_body(body))
            if predicate(path, topology):
                found[path] = topology
        self.assertEqual(len(found), count)
        return found

    def test_force_floor_is_scoped_in_actual_description_topology_and_gena_then_check_releases_it(self):
        events, initial = self.callback()
        for body in initial.values():
            self.assertEqual({m.get('LegacyCompatibleVersion') for m in
                              ET.fromstring(topology_body(body)).iter('ZoneGroupMember')}, {'24.1-00000'})
        self.stage(force=True)
        self.service.tick()
        for source, wanted in ((IP_A, '24.1-75102'), (IP_B, '24.1-00000')):
            device = self.description(source)
            self.assertEqual(device.findtext('d:minCompatibleVersion', namespaces=NS), wanted)
            self.assertEqual(device.findtext('d:legacyCompatibleVersion', namespaces=NS), wanted)
            self.assertEqual({m.get('LegacyCompatibleVersion') for m in self.topology(source).iter('ZoneGroupMember')}, {wanted})
        changed = self.events_for(events, lambda path, tree:
            {m.get('LegacyCompatibleVersion') for m in tree.iter('ZoneGroupMember')} ==
            ({'24.1-75102'} if path == '/a' else {'24.1-00000'}))
        self.assertNotEqual(next(changed['/a'].iter('ZoneGroupMember')).get('BootSeq'),
                            next(changed['/b'].iter('ZoneGroupMember')).get('BootSeq'))
        selected = self.manager.selection(A)
        self.check()
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        self.assertEqual(self.manager.selection(A)['token'], selected['token'])
        for source in (IP_A, IP_B):
            self.assertEqual(self.description(source).findtext('d:minCompatibleVersion', namespaces=NS), '24.1-00000')
        self.events_for(events, lambda _, tree:
            {m.get('LegacyCompatibleVersion') for m in tree.iter('ZoneGroupMember')} == {'24.1-00000'})
        self.assertEqual(self.backend.calls, [])

    def test_manual_stage_never_changes_admission_floor(self):
        self.stage(force=False)
        self.service.tick()
        for source in (IP_A, IP_B):
            self.assertEqual(self.description(source).findtext('d:minCompatibleVersion', namespaces=NS), '24.1-00000')
            self.assertEqual({m.get('LegacyCompatibleVersion') for m in self.topology(source).iter('ZoneGroupMember')}, {'24.1-00000'})
        item = self.check()
        self.assertIn('/controller/', item.get('ManifestURL'))
        self.assertEqual(self.manager.floor(A), '24.1-00000')

    def test_unavailable_storage_releases_cached_force_floor_and_announces_recovery(self):
        events, _ = self.callback()
        self.stage(force=True)
        self.service.tick()
        self.events_for(events, lambda path, tree:
            {x.get('LegacyCompatibleVersion') for x in tree.iter('ZoneGroupMember')} ==
            ({'24.1-75102'} if path == '/a' else {'24.1-00000'}))
        before = self.service.generation(IP_A)
        with patch.object(self.manager, '_state', side_effect=OSError('Storage unavailable')):
            self.service.tick()
            self.assertEqual(self.service.generation(IP_A), before + 1)
            self.assertEqual(self.description(IP_A).findtext('d:minCompatibleVersion', namespaces=NS), '24.1-00000')
            self.assertEqual({x.get('LegacyCompatibleVersion') for x in
                self.topology(IP_A).iter('ZoneGroupMember')}, {'24.1-00000'})
            self.events_for(events, lambda _, tree:
                {x.get('LegacyCompatibleVersion') for x in tree.iter('ZoneGroupMember')} == {'24.1-00000'})
        self.assertEqual(self.backend.calls, [])

    def test_staged_priority_manifest_preserves_signed_version_and_source_bound_delivery(self):
        # Native 4.3.2 supports the manual downgrade route; its incompatible
        # controller screen must never be used to force admission.
        self.stage(force=False, version=(24, 1, 75101), current='25.2-50130')
        item = self.check(current='25.2-50130')
        path = urlsplit(item.get('ManifestURL')).path
        status, body, _ = self.update_http(IP_A, path)
        self.assertEqual(status, 200, body)
        manifest = ET.fromstring(body)
        self.assertEqual(manifest.get('system_version'), self.service.version)
        images = {image.get('model'): image for image in manifest.find('update_list')}
        self.assertEqual(images['1'].get('version'), self.service.version)
        self.assertEqual(images['2'].get('version'), '24.1-75101')
        self.assertTrue(images['2'].text.endswith('/24.1-75101/^24.1-75101'))
        self.assertIsNone(self.service.coordinator.status(A))
        package_path = urlsplit(images['2'].text).path
        # Caller-controlled headers cannot substitute controller A's identity.
        status, denied, _ = self.update_http(IP_B, package_path, headers={'X-Controller-MAC': A, 'X-Forwarded-For': IP_A})
        self.assertNotEqual(status, 200)
        self.assertNotIn(b'invented test kernel', denied)
        self.assertEqual(self.update_http(UNKNOWN, package_path)[0], 403)
        self.assertEqual(self.update_http(IP_A, package_path, 'HEAD')[0], 200)
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        expected, _ = build_test_package(version=(24, 1, 75101))
        status, downloaded, _ = self.update_http(IP_A, package_path)
        self.assertEqual((status, downloaded), (200, expected))
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(self.manager.floor(A), '24.1-00000')
        self.manager.cancel_controller(A)
        self.assertNotEqual(self.update_http(IP_A, package_path)[0], 200)
        self.assertNotEqual(self.update_http(IP_A, path)[0], 200)

    def test_unsupported_force_stage_is_rejected_without_changing_discovery(self):
        for current in ('25.2-50130', None):
            with self.subTest(current=current):
                with self.assertRaises(online_updates.UpdateError) as error:
                    self.stage(force=True, version=(24, 1, 75101), current=current)
                self.assertEqual(error.exception.status, 400)
                self.assertEqual(self.manager.status(A)['status'], 'staged')
                self.service.tick()
                for source in (IP_A, IP_B):
                    self.assertEqual(self.description(source).findtext(
                        'd:minCompatibleVersion', namespaces=NS), '24.1-00000')
                    self.assertEqual({m.get('LegacyCompatibleVersion') for m in
                        self.topology(source).iter('ZoneGroupMember')}, {'24.1-00000'})

    def test_previous_unsupported_armed_receipt_recovers_to_normal_admission(self):
        for current in ('25.2-50129', None):
            with self.subTest(current=current):
                self.stage(force=True, current='24.1-75101')
                persisted = json.loads(self.manager.state_path.read_text())
                # Represent a receipt created by an older version that allowed
                # an unsafe force choice. Recovery must retain manual staging.
                persisted['controllers'][A].update(current_version=current,
                    force_prompt=True, prompt_acknowledged=False)
                self.manager.state_path.write_text(json.dumps(persisted))
                self.service.tick()
                self.assertEqual(self.manager.status(A)['status'], 'armed')
                self.assertFalse(self.manager.status(A)['force_active'])
                self.assertIsNotNone(self.manager.selection(A))
                for source in (IP_A, IP_B):
                    self.assertEqual(self.description(source).findtext(
                        'd:legacyCompatibleVersion', namespaces=NS), '24.1-00000')
                    self.assertEqual({m.get('LegacyCompatibleVersion') for m in
                        self.topology(source).iter('ZoneGroupMember')}, {'24.1-00000'})

    def test_adapter_tick_expires_abandoned_bridge_job_without_a_controller_request(self):
        now = [1000.0]
        self.service.coordinator.clock = lambda: now[0]
        self.check(current='25.2-50130', demonstration=True)
        token = self.service.coordinator.status(A)['token']
        old_manifest = '/updates/bridge/%s/manifest.upm' % token
        url = '%s/updates/bridge/%s/begin' % (self.service.origin, token)
        self.assertEqual(self.begin(self.room, url)[0], 200)
        now[0] += 901
        self.service.tick()
        self.assertEqual(self.service.coordinator.status(A)['status'], 'failed')
        self.assertIsNone(self.service.coordinator.active)
        self.assertEqual(self.manager.bridge_status()['status'], 'failed')
        self.assertEqual(self.update_http(IP_A, old_manifest)[0], 410)
        self.assertEqual(self.service.message(A)['AllowUpdate'], 'false')
        # A different controller can now start; no packet from the abandoned
        # transaction was needed to release household serialization.
        self.check(source=IP_B, current='25.2-50130', demonstration=True)
        other = self.service.coordinator.status(B)
        other_url = '%s/updates/bridge/%s/begin' % (self.service.origin, other['token'])
        status, _, _ = soap(self.room, IP_B, 'ZoneGroupTopology', 'BeginSoftwareUpdate',
            '<UpdateURL>%s</UpdateURL><Flags>2</Flags>' % html.escape(other_url))
        self.assertEqual(status, 200)
        self.assertEqual(self.service.version, self.service.base_version)
        self.assertEqual(self.backend.calls, [])

    def test_older_returning_controller_is_admitted_without_completing_a_different_target(self):
        self.stage(version=(24, 1, 75101), current='25.2-50130')
        item = self.check(current='25.2-50130')
        status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(status, 200)
        image = next(x for x in ET.fromstring(body).find('update_list') if x.get('model') == '2')
        self.assertEqual(self.update_http(IP_A, urlsplit(image.text).path)[0], 200)
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        events, _ = self.callback()
        before = self.service.generation(IP_A)
        # A real socket request reports a version below the ordinary admission
        # floor, but different from the signed target under installation.
        status, body, _ = request(self.room, IP_A, 'GET', '/xml/device_description.xml', headers={
            'User-Agent': 'Linux UPnP/1.0 Sonos/23.1-12345 (CR100)'})
        self.assertEqual(status, 200)
        device = ET.fromstring(body).find('d:device', NS)
        self.assertEqual(device.findtext('d:minCompatibleVersion', namespaces=NS), '23.1-12345')
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(self.manager.selection(A)['version'], '24.1-75101')
        self.service.tick()
        self.assertEqual(self.service.generation(IP_A), before + 1)
        self.events_for(events, lambda path, tree:
            {x.get('LegacyCompatibleVersion') for x in tree.iter('ZoneGroupMember')} ==
            ({'23.1-12345'} if path == '/a' else {'24.1-00000'}))
        self.assertEqual(self.description(IP_B).findtext('d:minCompatibleVersion', namespaces=NS), '24.1-00000')
        # An invalid observed version cannot replace the last valid admission
        # version or falsely complete the pending signed target.
        request(self.room, IP_A, 'GET', '/xml/device_description.xml', headers={
            'User-Agent': 'Sonos/999.999-999999999999 (CR100)'})
        self.assertEqual(self.description(IP_A).findtext('d:minCompatibleVersion', namespaces=NS), '23.1-12345')
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(self.manager.status(A)['current_version'], '23.1-12345')
        request(self.room, IP_A, 'GET', '/xml/device_description.xml', headers={
            'User-Agent': 'Sonos/24.1-75101 (CR100)'})
        self.service.tick()
        self.assertEqual(self.manager.status(A)['status'], 'completed')
        self.assertIsNone(self.manager.selection(A))
        self.assertEqual(self.description(IP_A).findtext('d:minCompatibleVersion', namespaces=NS), '24.1-00000')

    def test_expired_unaccepted_bridge_offer_cannot_serve_a_manifest_or_confirmation(self):
        now = [1000.0]
        self.service.coordinator.clock = lambda: now[0]
        item = self.check(demonstration=True)
        path = urlsplit(item.get('ManifestURL')).path
        self.assertEqual(self.update_http(IP_A, path)[0], 200)
        now[0] += 901
        self.assertEqual(self.update_http(IP_A, path)[0], 410)
        self.assertEqual(self.service.message(A)['AllowUpdate'], 'false')

    def test_rearming_invalidates_old_manifest_and_package_tokens(self):
        self.stage()
        old = self.check()
        old_token = self.manager.selection(A)['token']
        old_package = '/updates/controller/%s/25.2-50130/25.2-50130-1-2.upd' % old_token
        self.manager.cancel_controller(A)
        self.manager.arm_controller(A, current_version='24.1-75101')
        fresh = self.check()
        self.assertNotEqual(old.get('ManifestURL'), fresh.get('ManifestURL'))
        for path in (urlsplit(old.get('ManifestURL')).path, old_package):
            self.assertNotEqual(self.update_http(IP_A, path)[0], 200)
        self.assertEqual(self.update_http(IP_A, urlsplit(fresh.get('ManifestURL')).path)[0], 200)

    def test_interrupted_package_stream_releases_downloading_state_and_allows_retry(self):
        for failure in (BrokenPipeError('Disconnected'), ConnectionResetError('Reset')):
            with self.subTest(failure=type(failure).__name__):
                self.stage()
                self.check()
                original = self.manager.selection(A)['token']
                path = '/updates/controller/%s/25.2-50130/25.2-50130-1-2.upd' % original
                with patch('update_transport.shutil.copyfileobj', side_effect=failure):
                    with self.assertRaises(http.client.IncompleteRead):
                        self.update_http(IP_A, path)
                wait_for(lambda: self.manager.status(A)['status'] == 'failed', timeout=2)
                self.assertEqual(self.manager.floor(A), '24.1-00000')
                self.assertIn('interrupted', self.manager.status(A)['last_error'])
                self.assertIn('Upload the firmware again', self.manager.status(A)['last_error'])
                reviewed = self.manager.stage_controller(A, self.package, arm=False, current_version='24.1-75101')
                self.manager.arm_controller(A, current_version='24.1-75101', expected_sha256=reviewed['sha256'])
                retry = self.manager.selection(A)['token']
                self.assertNotEqual(retry, original)
                self.check()
                self.assertEqual(self.update_http(IP_A,
                    '/updates/controller/%s/25.2-50130/25.2-50130-1-2.upd' % retry)[0], 200)
                self.assertEqual(self.manager.status(A)['status'], 'downloading')
                self.manager.cancel_controller(A)

    def test_late_download_failure_cannot_fail_another_controller_or_replacement_token(self):
        self.stage()
        self.check()
        original = self.manager.selection(A)['token']
        path = '/updates/controller/%s/25.2-50130/25.2-50130-1-2.upd' % original
        self.assertEqual(self.update_http(IP_A, path)[0], 200)
        self.service.delivery_failed(IP_B, path, BrokenPipeError('Other controller'))
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.manager.cancel_controller(A)
        self.stage()
        replacement = self.manager.selection(A)['token']
        self.service.delivery_failed(IP_A, path, ConnectionResetError('Old socket'))
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        self.assertEqual(self.manager.selection(A)['token'], replacement)

    def begin(self, player, url, flags='2', extra=''):
        return soap(player, IP_A, 'ZoneGroupTopology', 'BeginSoftwareUpdate',
                    '<UpdateURL>%s</UpdateURL><Flags>%s</Flags>%s' % (html.escape(url), flags, extra))

    def test_only_selected_internal_stub_bypasses_real_backend_and_informational_guards(self):
        self.check(demonstration=True)
        token = self.service.coordinator.status(A)['token']
        selected = '%s/updates/bridge/%s/begin' % (self.service.origin, token)
        for player in (self.room, self.bridge):
            for url, flags, extra in (
                    ('http://example.invalid/vendor.upd', '2', ''),
                    (selected, '1', ''),
                    (selected, '2', '<ExtraOptions>--force</ExtraOptions>')):
                status, _, _ = self.begin(player, url, flags, extra)
                self.assertEqual(status, 500)
            self.assertEqual(self.begin(player, selected)[0], 200)
        status, _, _ = soap(self.bridge, IP_A, 'AVTransport', 'Play',
                            '<InstanceID>0</InstanceID><Speed>1</Speed>')
        self.assertEqual(status, 500)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(set(self.service.coordinator.status(A)['accepted']),
                         {self.room.config.udn, self.bridge.config.udn})

    def tcp_stream(self, address):
        connection = self.sources.connection(*address, IP_A)
        if connection.sock is None:
            connection.connect()
        self.addCleanup(connection.close)
        stream = connection.sock.makefile('rwb', buffering=0)
        self.addCleanup(stream.close)
        return stream

    def tcp_phases(self, stream):
        stream.write(b'HELLO?version=25.2-50130\n')
        self.assertEqual(stream.readline(), b'HELLO?version=1\n')
        for phase in (b'DOWNLOAD', b'FLASHWRITE'):
            stream.write(phase + b'\n')
            for percent in (0, 25, 75, 100):
                self.assertEqual(stream.readline(), b'STATUS?phase=' + phase +
                                 b';percent-complete=' + str(percent).encode() + b'\n')
            self.assertEqual(stream.readline(), b'COMPLETE?phase=' + phase + b'\n')

    def controller_player_offer(self):
        self.stage(current='25.2-50130')
        item = self.check(current='25.2-50130')
        status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(status, 200)
        return {x.get('model'): x for x in ET.fromstring(body).find('update_list')}

    def finish_player(self):
        server = self.service.transport.player_servers['127.0.0.1']
        stream = self.tcp_stream(server.server_address)
        self.tcp_phases(stream)
        stream.write(b'REBOOT?Delay=0\n')
        self.assertEqual(stream.readline(), b'')

    def test_controller_offer_selected_subset_restarts_current_persona_and_preserves_retry(self):
        images = self.controller_player_offer()
        url = images['1'].text
        token = self.manager.selection(A)['token']
        before = {p.config.udn: p.config.boot_sequence for p in self.house.devices()}
        original_version = self.service.version
        self.assertEqual(self.begin(self.room, url)[0], 200)
        self.finish_player()
        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
        receipt = self.service.coordinator.status(A)
        self.assertEqual(receipt['kind'], 'controller-compatibility')
        self.assertEqual(receipt['accepted'], [self.room.config.udn])
        self.assertEqual(self.room.config.boot_sequence, before[self.room.config.udn] + 1)
        self.assertEqual(self.bridge.config.boot_sequence, before[self.bridge.config.udn])
        self.assertEqual(self.service.version, original_version)
        self.assertIsNone(self.service.coordinator.installed_version)
        for source in (IP_A, IP_B):
            self.assertEqual(self.description(source).findtext('d:softwareVersion', namespaces=NS), original_version)
            self.assertEqual({x.get('SoftwareVersion') for x in self.topology(source).iter('ZoneGroupMember')},
                             {original_version})
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        # A late native BEGIN is another selected participant, not a new release.
        self.assertEqual(self.begin(self.bridge, url)[0], 200)
        self.finish_player()
        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
        self.assertEqual(self.bridge.config.boot_sequence, before[self.bridge.config.udn] + 1)
        self.assertEqual(self.room.config.boot_sequence, before[self.room.config.udn] + 1)
        for _ in range(2):
            self.assertEqual(self.update_http(IP_A, urlsplit(images['2'].text).path)[0], 200)
            self.service.observe(IP_A, 'Sonos/25.2-50130 (CR100)')
            self.assertEqual(self.manager.status(A)['status'], 'downloading')
            self.assertEqual(self.manager.selection(A)['token'], token)
            self.assertIsNotNone(self.check(current='25.2-50130'))
        self.assertEqual(self.begin(self.room, url)[0], 200)
        self.finish_player()
        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
        self.assertEqual(self.room.config.boot_sequence, before[self.room.config.udn] + 2)
        self.assertEqual(self.manager.status(A)['status'], 'downloading')
        self.assertEqual(self.backend.calls, [])

    def test_controller_offer_two_shared_ip_participants_finish_without_bridge_install(self):
        images = self.controller_player_offer()
        before = {p.config.udn: p.config.boot_sequence for p in self.house.devices()}
        for player in (self.room, self.bridge):
            self.assertEqual(self.begin(player, images['1'].text)[0], 200)
        self.finish_player()
        self.assertNotEqual(self.service.coordinator.status(A)['status'], 'completed')
        self.finish_player()
        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
        self.assertEqual(self.service.version, self.service.base_version)
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        for player in self.house.devices():
            self.assertEqual(player.config.boot_sequence, before[player.config.udn] + 1)
        self.assertEqual(self.backend.calls, [])

    def test_controller_first_completion_then_inert_check_preserves_old_two_player_recovery(self):
        for proof in ('build', 'version'):
            with self.subTest(proof=proof):
                marker = 'cfw3:' + 'ab' * 32
                package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker))
                    if proof == 'build' else None)
                current = '25.2-50130' if proof == 'build' else '24.1-75101'
                self.manager.stage_controller(A, package, current_version=current)
                item = self.check(current=current)
                old_manifest = urlsplit(item.get('ManifestURL')).path
                status, body, _ = self.update_http(IP_A, old_manifest)
                self.assertEqual(status, 200)
                images = {x.get('model'): x for x in ET.fromstring(body).find('update_list')}
                original_revision = ET.fromstring(body).get('revision')
                package_url = urlsplit(images['2'].text).path
                self.assertEqual(self.update_http(IP_A, package_url)[0], 200)
                if proof == 'build':
                    status, _, _ = soap(self.room, IP_A, 'SystemProperties', 'SetString',
                        '<VariableName>CR100_CFWBuild</VariableName><StringValue>%s</StringValue>' % marker,
                        path='/SystemProperties/Control', extra_headers={
                            'User-Agent': 'Sonos/25.2-50130 (CR100)'})
                    self.assertEqual(status, 200)
                else:
                    self.service.observe(IP_A, 'Sonos/25.2-50130 (CR100)')
                self.assertEqual(self.manager.status(A)['status'], 'completed')
                self.assertIsNone(self.manager.selection(A))
                fresh = self.check(current='25.2-50130')
                self.assertNotEqual(fresh.get('ManifestURL'), item.get('ManifestURL'))
                self.assertEqual(self.service.offers[A]['kind'], 'none')
                self.assertEqual(self.service.message(A)['AllowUpdate'], 'true')
                self.assertEqual(self.service.message(A)['MessageHeader'], 'Finishing system update')
                status, body, _ = self.update_http(IP_A, old_manifest)
                self.assertEqual(status, 200)
                recovery = ET.fromstring(body)
                self.assertNotEqual(recovery.get('revision'), original_revision)
                self.assertEqual({x.get('model') for x in recovery.find('update_list')}, {'1'})
                self.assertEqual(recovery.get('system_version'), '25.2-50130')
                self.assertNotEqual(self.update_http(IP_A, package_url)[0], 200)
                self.assertNotEqual(self.update_http(IP_B, old_manifest)[0], 200)
                before = {p.config.udn: p.config.boot_sequence for p in self.house.devices()}
                for player in (self.room, self.bridge):
                    self.assertEqual(self.begin(player, images['1'].text)[0], 200)
                self.finish_player()
                self.finish_player()
                wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
                self.assertEqual(self.manager.status(A)['status'], 'completed')
                self.assertEqual(self.service.version, self.service.base_version)
                for player in self.house.devices():
                    self.assertEqual(player.config.boot_sequence, before[player.config.udn] + 1)
                self.manager.cancel_controller(A)
                self.assertNotEqual(self.update_http(IP_A, old_manifest)[0], 200)
                self.assertEqual(self.begin(self.room, images['1'].text)[0], 500)
        self.assertEqual(self.backend.calls, [])

    def test_recovery_available_software_gena_is_scoped_live_and_present_at_subscribe(self):
        marker = 'cfw3:' + 'ab' * 32
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        self.manager.stage_controller(A, package, current_version='25.2-50130')
        self.service.observe(IP_A, 'Sonos/25.2-50130 (CR100)')
        self.service.tick()
        events, initial = self.callback()

        def hint(body):
            return next((node.text or '' for node in ET.fromstring(body).iter()
                         if node.tag.endswith('AvailableSoftwareUpdate')), '')

        for body in initial.values():
            self.assertEqual(hint(body), '')
        self.assertEqual(self.service.available_update(UNKNOWN), '')
        before = {p.config.udn: p.config.boot_sequence for p in self.house.devices()}
        generation = self.service.generation(IP_A)
        token = self.manager.selection(A)['token']
        path = '/updates/controller/%s/25.2-50130/^25.2-50130' % token
        self.assertEqual(self.update_http(IP_A, path)[0], 200)

        def wait_hints(expected_a):
            found, deadline = {}, time.monotonic() + 3
            while len(found) < 2 and time.monotonic() < deadline:
                address, body = events.get(timeout=max(.01, deadline - time.monotonic()))
                text = hint(body)
                if text == (expected_a if address == '/a' else ''):
                    found[address] = text
            self.assertEqual(set(found), {'/a', '/b'})

        active = self.service.available_update(IP_A)
        self.assertIn(token, active)
        self.assertEqual(self.service.available_update(IP_B), '')
        wait_hints(active)
        self.manager.report_build(A, marker, '25.2-50130')
        self.service.tick()
        recovery = self.service.available_update(IP_A)
        item = ET.fromstring(recovery)
        self.assertEqual(item.get('Version'), '25.2-50130')
        self.assertIn('/updates/controller/%s/' % token, item.get('ManifestURL'))
        self.assertNotIn('controller-recovery', item.get('ManifestURL'))
        wait_hints(recovery)
        # The newly booted controller receives the recovery hint in its first
        # notification; it need not issue another manual CheckForUpdate.
        _, boot_events = self.callback()
        self.assertEqual(hint(boot_events['/a']), recovery)
        self.assertEqual(hint(boot_events['/b']), '')
        self.manager.cancel_controller(A)
        self.service.tick()
        self.assertEqual(self.service.available_update(IP_A), '')
        wait_hints('')
        self.assertEqual(self.service.generation(IP_A), generation)
        self.assertEqual({p.config.udn: p.config.boot_sequence for p in self.house.devices()}, before)
        self.assertEqual(self.backend.calls, [])

    def test_controller_player_begin_rejects_wrong_source_url_flags_options_and_hidden_udn(self):
        images = self.controller_player_offer()
        url = images['1'].text
        for bad_url, flags, extra in (
                (url + '?x=1', '2', ''), (url.replace('/bridge/', '/controller/'), '2', ''),
                (url.replace('http:', 'https:'), '2', ''), (url, '1', ''),
                (url, '2', '<ExtraOptions>-F</ExtraOptions>')):
            self.assertEqual(self.begin(self.room, bad_url, flags, extra)[0], 500)
        for source in (IP_B, UNKNOWN):
            self.assertEqual(soap(self.room, source, 'ZoneGroupTopology', 'BeginSoftwareUpdate',
                '<UpdateURL>%s</UpdateURL><Flags>2</Flags>' % html.escape(url))[0], 500)
        with patch.object(self.house, 'devices', return_value=[self.bridge]):
            self.assertEqual(self.begin(self.room, url)[0], 500)
        self.assertIsNone(self.service.coordinator.status(A))
        self.assertEqual(self.begin(self.room, url, '0')[0], 200)
        with patch.object(self.house, 'devices', return_value=[self.bridge]):
            with self.assertRaises(update_service.UpdateTransportError):
                self.service.coordinator.claim(A, [self.room.config.udn], timeout=0)
        self.assertEqual(self.backend.calls, [])

    def test_controller_compatibility_cancel_rearm_expiry_and_interruption_invalidate_sessions(self):
        images = self.controller_player_offer()
        url = images['1'].text
        self.assertEqual(self.begin(self.room, url)[0], 200)
        old_session = self.service.coordinator.status(A)['token']
        self.service.coordinator.cancel(A, old_session)
        # Retrying the same armed controller selection gets a fresh transport
        # identity, so a delayed old TCP connection cannot mutate the retry.
        self.assertEqual(self.begin(self.room, url)[0], 200)
        fresh_session = self.service.coordinator.status(A)['token']
        self.assertNotEqual(old_session, fresh_session)
        with self.assertRaises(update_service.UpdateTransportError):
            self.service.coordinator.progress(A, old_session, self.room.config.udn, 'DOWNLOAD', 0)
        self.manager.cancel_controller(A)
        self.service.tick()
        self.assertEqual(self.service.coordinator.status(A)['status'], 'failed')
        self.assertEqual(self.begin(self.room, url)[0], 500)
        self.stage(current='25.2-50130')
        self.check(current='25.2-50130')
        self.assertEqual(self.begin(self.room, url)[0], 500)
        active = self.manager.selection(A)
        fresh_url = '%s/updates/bridge/%s/begin' % (self.service.origin, active['token'])
        self.assertEqual(self.begin(self.room, fresh_url)[0], 200)
        self.manager.clock = lambda: active['expires_at'] + 1
        self.service.tick()
        self.assertIsNone(self.service.coordinator.active)
        self.assertEqual(self.service.coordinator.status(A)['status'], 'failed')
        self.assertEqual(self.backend.calls, [])

    def test_controller_tcp_rechecks_paired_source_and_phase_order(self):
        images = self.controller_player_offer()
        self.assertEqual(self.begin(self.room, images['1'].text)[0], 200)
        server = self.service.transport.player_servers['127.0.0.1']
        stream = self.tcp_stream(server.server_address)
        stream.write(b'HELLO\n')
        self.assertEqual(stream.readline(), b'HELLO?version=1\n')
        with patch.dict(ADDRESSES, {IP_A: B}):
            stream.write(b'DOWNLOAD\n')
            self.assertTrue(stream.readline().startswith(b'ERROR?'))
        self.assertEqual(self.manager.status(A)['status'], 'armed')
        self.assertEqual(self.begin(self.room, images['1'].text)[0], 200)
        stream = self.tcp_stream(server.server_address)
        stream.write(b'FLASHWRITE\n')
        self.assertTrue(stream.readline().startswith(b'ERROR?'))
        self.assertEqual(self.backend.calls, [])

    def test_full_household_two_udns_shared_ip_updates_all_discovery_and_gena_after_both_reboots(self):
        events, _ = self.callback()
        item = self.check(current='25.2-50130', demonstration=True)
        manifest_status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(manifest_status, 200)
        images = {x.get('model'): x for x in ET.fromstring(body).find('update_list')}
        self.assertNotIn('2', images)
        target, url = images['1'].get('version'), images['1'].text
        before = {p.config.udn: p.config.boot_sequence for p in self.house.devices()}
        for player in self.house.devices():
            self.assertEqual(self.begin(player, url)[0], 200)
        tcp_server = self.service.transport.player_servers['127.0.0.1']
        streams = [self.tcp_stream(tcp_server.server_address) for _ in range(2)]
        self.tcp_phases(streams[0])
        streams[0].write(b'REBOOT?Delay=0\n')
        self.assertEqual(streams[0].readline(), b'')
        self.assertEqual(self.service.version, self.service.base_version)
        self.tcp_phases(streams[1])
        streams[1].write(b'REBOOT?Delay=0\n')
        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=3)
        self.assertEqual(self.service.version, target)
        for player in self.house.devices():
            self.assertEqual(player.config.boot_sequence, before[player.config.udn] + 1)
        for source in (IP_A, IP_B):
            self.assertEqual(self.description(source).findtext('d:softwareVersion', namespaces=NS), target)
            self.assertEqual({x.get('SoftwareVersion') for x in self.topology(source).iter('ZoneGroupMember')}, {target})
        self.events_for(events, lambda _, tree:
            {x.get('SoftwareVersion') for x in tree.iter('ZoneGroupMember')} == {target})
        self.assertEqual(soap(self.bridge, IP_A, 'SystemProperties', 'DoPostUpdateTasks',
                              path='/SystemProperties/Control')[0], 200)
        repeated = self.check(current='25.2-50130', demonstration=True)
        self.assertEqual(repeated.get('Version'), '25.2-50130')
        self.assertEqual(self.service.coordinator.status(A)['status'], 'completed')
        self.assertEqual(self.service.coordinator.status(A)['target_version'], target)
        self.assertEqual(self.backend.calls, [])

    def test_force_prompt_generation_reaches_original_ssdp_loop_over_udp_for_one_controller(self):
        self.stage(force=True)
        self.service.tick()
        real_socket = socket.socket
        source_ports = {}

        class Datagram:
            # Keep genuine UDP packets while translating only test source IPs
            # at the socket boundary on macOS, which lacks 127/8 aliases.
            def __init__(self, *args, **kwargs):
                self.socket = real_socket(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.socket, name)

            def recvfrom(self, size):
                data, source = self.socket.recvfrom(size)
                return data, (source_ports.get(source[1], source[0]), source[1])

            def sendto(self, data, address):
                if address[0] in ADDRESSES:
                    address = ('127.0.0.1', address[1])
                return self.socket.sendto(data, address)

        class SocketModule:
            socket = Datagram

            def __getattr__(self, name):
                return getattr(socket, name)

        with real_socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            self.house.ssdp_port = reserve.getsockname()[1]
        stop = threading.Event()
        with patch.object(zoneplayers, 'socket', SocketModule()):
            worker = threading.Thread(target=self.house.ssdp_loop, args=('127.0.0.1', stop), daemon=True)
            worker.start()
            try:
                responses = {}
                for logical in (IP_A, IP_B):
                    with real_socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                        client.bind(('127.0.0.1', 0))
                        client.settimeout(.2)
                        source_ports[client.getsockname()[1]] = logical
                        deadline = time.monotonic() + 3
                        packets = []
                        while len(packets) < 2 and time.monotonic() < deadline:
                            client.sendto((M_SEARCH % HOUSEHOLD).encode(), ('127.0.0.1', self.house.ssdp_port))
                            try:
                                packets.append(client.recvfrom(8192)[0].decode())
                            except socket.timeout:
                                continue
                        self.assertEqual(len(packets), 2)
                        responses[logical] = [{k.upper(): v.strip() for k, v in
                            (line.split(':', 1) for line in text.split('\r\n') if ':' in line)} for text in packets]
                by_identity = {source: {p['USN']: p for p in packets} for source, packets in responses.items()}
                self.assertEqual(set(by_identity[IP_A]), set(by_identity[IP_B]))
                for identity, a in by_identity[IP_A].items():
                    b = by_identity[IP_B][identity]
                    self.assertEqual(int(a['X-RINCON-BOOTSEQ']), int(b['X-RINCON-BOOTSEQ']) + 1)
                    self.assertEqual(a['LOCATION'], b['LOCATION'])
                self.assertTrue(worker.is_alive())
            finally:
                stop.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


class GitHubSelfUpdateIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'bridge-config.json'
        self.config.write_text('{}')
        self.package, modulus = build_test_package()
        key = patch.object(online_updates, 'VENDOR_MODULUS', modulus)
        key.start()
        self.addCleanup(key.stop)
        self.install_root = self.root / 'opt/cr100-bridge'
        self.current_root = self.root / 'running'
        self.current_root.mkdir()
        self.commit_a = 'a' * 40
        self.commit_b = 'b' * 40
        (self.current_root / build_info.STAMP).write_text(json.dumps({
            'version': 'master',
            'commit': self.commit_a,
            'short_commit': self.commit_a[:8],
            'description': self.commit_a[:8],
            'dirty': False,
            'tag': '',
        }))
        archive_path = self.root / 'github-source.tar.gz'
        distribution.archive(archive_path)
        self.tarball = archive_path.read_bytes()
        self.restarted = threading.Event()
        self.api_error = None

        def opener(req, timeout=None):
            if req.full_url.startswith('https://api.github.com/'):
                if self.api_error is not None:
                    raise self.api_error
                body = json.dumps({
                    'sha': self.commit_b,
                    'commit': {'message': 'Support Pi 3B+ and self-updates'},
                }).encode()
            elif req.full_url.startswith('https://codeload.github.com/'):
                body = self.tarball
            else:
                raise AssertionError('Unexpected URL: ' + req.full_url)
            stream = io.BytesIO(body)
            stream.headers = {'Content-Length': str(len(body))}
            return stream

        def runner(cmd, **kwargs):
            if len(cmd) >= 3 and str(cmd[1]).endswith('tools/distribution.py'):
                return subprocess.run(cmd, **kwargs)
            if len(cmd) >= 3 and cmd[1:3] == ['-m', 'venv']:
                venv_python = Path(cmd[3]) / 'bin/python'
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.write_text('#!/bin/sh\nexit 0\n')
                venv_python.chmod(0o755)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        self.backend = MemoryBackend('memory')
        self.logs = []
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'], self.logs.append,
            port=0, backends=[self.backend], refs=[('memory', 'den')], bridge_ip='192.0.2.200',
            controller_resolver=ADDRESSES.get, controller_addresses=lambda: dict(ADDRESSES))
        self.addCleanup(self.house.close)
        self.manager = online_updates.UpdateManager(self.config)
        self.updater = bridge_updater.BridgeReleaseUpdater(
            self.config, install_root=self.install_root, current_root=self.current_root,
            bin_dir=None, systemd_dir=None, compatible_path=None,
            opener=opener, command_runner=runner, restart_callback=self.restarted.set)
        self.service = update_service.UpdateService(
            self.house, self.manager, '127.0.0.1', tls_port=None, http_port=0, tcp_port=0,
            phase_delay=0, recover=False, release_updater=self.updater)
        self.house.updates = self.service
        self.house.start('127.0.0.1', ssdp=False)
        self.house.stop.set()
        self.room = self.house.players[0]
        self.bridge = self.house.about_players[zoneplayers.BRIDGE_KEY]
        servers = [player.http for player in self.house.devices()] + self.service.transport.servers
        self.sources = SourceAddresses(servers, [IP_A, IP_B, UNKNOWN])
        self.addCleanup(self.sources.close)
        for player in self.house.devices():
            player.http._test_source_addresses = self.sources
            player.config.callback_network = '127.0.0.0/8'

    def check(self, source=IP_A, current='25.2-50130'):
        status, body, _ = soap(self.room, source, 'ZoneGroupTopology', 'CheckForUpdate', CHECK,
            extra_headers={'User-Agent': 'Linux UPnP/1.0 Sonos/%s (CR100)' % current})
        self.assertEqual(status, 200, body)
        item = field(body, 'UpdateItem')
        return ET.fromstring(item) if item else None

    def update_http(self, source, path):
        server = self.service.transport.servers[0]
        connection = self.sources.connection(*server.server_address, source)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_live_github_bridge_update_flows_through_native_protocol_and_restarts(self):
        events = queue.Queue()

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                events.put((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, _, _ = request(self.room, IP_A, 'SUBSCRIBE', '/ZoneGroupTopology/Event', headers={
            'CALLBACK': '<http://127.0.0.1:%d/a>' % server.server_port, 'NT': 'upnp:event', 'TIMEOUT': 'Second-300'})
        self.assertEqual(status, 200)

        item = self.check()
        self.assertEqual(item.get('Version'), '25.2-50130')
        manifest_status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(manifest_status, 200)
        manifest = ET.fromstring(body)
        self.assertEqual(manifest.get('system_version'), '25.2-50130')
        images = {x.get('model'): x for x in manifest.find('update_list')}
        self.assertEqual(set(images), {'1'})
        target, url = images['1'].get('version'), images['1'].text

        message = self.service.message(A)
        self.assertEqual(message['MessageHeader'], 'Bridge software update')
        self.assertEqual(message['AllowUpdate'], 'true')
        self.assertIn(self.commit_b[:8], message['MessageBody'])
        self.assertIn('Support Pi 3B+ and self-updates', message['MessageBody'])

        for player in self.house.devices():
            begin_status, _, _ = soap(player, IP_A, 'ZoneGroupTopology', 'BeginSoftwareUpdate',
                '<UpdateURL>%s</UpdateURL><Flags>2</Flags>' % html.escape(url))
            self.assertEqual(begin_status, 200)
        tcp_server = self.service.transport.player_servers['127.0.0.1']
        for _ in range(2):
            connection = self.sources.connection(*tcp_server.server_address, IP_A)
            if connection.sock is None:
                connection.connect()
            self.addCleanup(connection.close)
            stream = connection.sock.makefile('rwb', buffering=0)
            self.addCleanup(stream.close)
            stream.write(b'HELLO?version=25.2-50130\n')
            self.assertEqual(stream.readline(), b'HELLO?version=1\n')
            for phase in (b'DOWNLOAD', b'FLASHWRITE'):
                stream.write(phase + b'\n')
                for percent in (0, 25, 75, 100):
                    self.assertEqual(stream.readline(), b'STATUS?phase=' + phase +
                                     b';percent-complete=' + str(percent).encode() + b'\n')
                self.assertEqual(stream.readline(), b'COMPLETE?phase=' + phase + b'\n')
            stream.write(b'REBOOT?Delay=0\n')
            self.assertEqual(stream.readline(), b'')

        wait_for(lambda: self.service.coordinator.status(A)['status'] == 'completed', timeout=5)
        self.assertEqual(self.service.version, target)
        deadline = time.monotonic() + 3
        saw_target = False
        while time.monotonic() < deadline:
            _, notify_body = events.get(timeout=max(0.05, deadline - time.monotonic()))
            tree = ET.fromstring(ET.fromstring(notify_body).findtext('.//ZoneGroupState'))
            if {x.get('SoftwareVersion') for x in tree.iter('ZoneGroupMember')} == {target}:
                saw_target = True
                break
        self.assertTrue(saw_target)

        status = self.manager.bridge_status()
        self.assertEqual(status['mode'], 'github')
        self.assertFalse(status['stub'])
        self.assertTrue(status['automatic_installation'])
        self.assertEqual(status['target_commit'], self.commit_b)
        installed = (self.install_root / 'current').resolve()
        self.assertEqual(build_info.read(installed)['commit'], self.commit_b)

        with patch.object(self.updater, 'expedite_restart',
                          side_effect=lambda: self.updater._arm_restart_timer_locked(0.02)):
            self.assertEqual(soap(self.bridge, IP_A, 'SystemProperties', 'DoPostUpdateTasks',
                                  path='/SystemProperties/Control')[0], 200)
            self.assertTrue(self.restarted.wait(2))
        self.assertFalse(self.updater.check_for_update()['available'])

    def test_github_check_network_error_falls_back_to_inert_manifest(self):
        self.api_error = OSError('network unreachable')
        inert = self.check()
        self.assertIsNotNone(inert)
        self.assertEqual(self.service.offers[A]['kind'], 'none')
        self.assertEqual(self.service.message(A)['AllowUpdate'], 'false')

    def test_armed_controller_package_takes_precedence_over_available_github_update(self):
        self.manager.stage_controller(A, self.package, current_version='25.2-50130')
        item = self.check()
        status, body, _ = self.update_http(IP_A, urlsplit(item.get('ManifestURL')).path)
        self.assertEqual(status, 200)
        images = {x.get('model'): x for x in ET.fromstring(body).find('update_list')}
        self.assertEqual(set(images), {'1', '2'})
        self.assertEqual(images['2'].get('version'), '25.2-50130')


if __name__ == '__main__':
    unittest.main()
