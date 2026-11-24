"""Vendor package verification, isolated controller lifecycle and recovery."""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import base64
import hashlib
import json
import multiprocessing
from pathlib import Path
import struct
import stat
import tempfile
import unittest
import zlib
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import online_updates as updates

MAC = '00:0e:58:01:02:03'
OTHER = '00:0e:58:04:05:06'


@lru_cache(maxsize=1)
def _test_key():
    # Ephemeral invented test key; never a vendor or controller identity key.
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)


def build_test_cramfs(movie):
    """Small actual filesystem for package-bound installed-build evidence."""
    entries = [(b'opt', stat.S_IFDIR | 0o755), (b'swf', stat.S_IFDIR | 0o755),
               (b'MainUI.swf', stat.S_IFREG | 0o644)]
    out = bytearray(76)
    struct.pack_into('<I', out, 64, stat.S_IFDIR | 0o755)
    positions = []
    for name, mode in entries:
        positions.append(len(out))
        padded = (len(name) + 3) // 4 * 4
        out.extend(struct.pack('<3I', mode, 0, padded // 4) + name.ljust(padded, b'\0'))
    for pos, offset, size in [(64, positions[0], 16),
                              (positions[0], positions[1], 16),
                              (positions[1], positions[2], 24)]:
        struct.pack_into('<2I', out, pos + 4, size, (offset // 4 << 6) | (0 if pos == 64 else 1))
    file_pos = positions[2]
    offset = len(out)
    blocks = [zlib.compress(movie[n:n + 4096]) for n in range(0, len(movie), 4096)]
    out.extend(bytes(4 * len(blocks)))
    for index, block in enumerate(blocks):
        out.extend(block)
        struct.pack_into('<I', out, offset + index * 4, len(out))
    struct.pack_into('<2I', out, file_pos + 4, len(movie), (offset // 4 << 6) | 3)
    struct.pack_into('<4I', out, 0, 0x28cd3d45, len(out), 3, 0)
    out[16:32] = b'Compressed ROMFS'
    struct.pack_into('<2I', out, 40, len(blocks), 4)
    struct.pack_into('<I', out, 32, zlib.crc32(out) & 0xffffffff)
    return bytes(out)


def build_test_movie(marker):
    payload = b'\x00' + marker.encode() + b'\x00'
    movie = b'FWS\x06' + bytes(4) + payload
    return movie[:4] + struct.pack('<I', len(movie)) + movie[8:]


def build_test_descriptor(marker, release):
    def field(data):
        return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')
    key = _test_key()
    message = 'cfwrel1:%s:%s' % (release, field(bytes.fromhex(marker.removeprefix('cfw3:'))))
    signature = key.sign(message.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    return message + ':' + field(signature), key.public_key().public_numbers().n


def build_test_package(version=(25, 2, 50130), model=2, rootfs=None):
    """Return (minimal signed fixture bytes, invented public modulus).

    Tests patch VENDOR_MODULUS in-process only. Production callers always use the
    pinned vendor public key and cannot accept this generated fixture.
    """
    def record(kind, payload):
        return struct.pack('<4I', updates.UPD_MAGIC, kind, len(payload) + 16, 0) + payload
    body = b''.join((record(1, struct.pack('<3I', *version)),
        record(2, struct.pack('<7I', 1, model, 0, 3, 0, 0xfffffffe, 0xffffffff)),
        record(6, b'invented test kernel'), record(4, rootfs or b'invented test filesystem')))
    key = _test_key()
    signature = key.sign(body, padding.PKCS1v15(), hashes.MD5())
    return body + record(0xffffffff, signature), key.public_key().public_numbers().n


def _stage_in_process(config_path, package, modulus, index):
    updates.VENDOR_MODULUS = modulus
    manager = updates.UpdateManager(config_path)
    manager.stage_controller('00:0e:58:02:03:%02x' % index, package, arm=False)


class FirmwareValidation(unittest.TestCase):
    def test_candidate_kind_uses_vendor_signature_and_embedded_custom_build(self):
        marker = next(build for build, release in updates.KNOWN_CFW_BUILDS.items()
                      if release == 'R15')
        package, modulus = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        with patch.object(updates, 'VENDOR_MODULUS', modulus):
            _, official = updates.validate_firmware(package)
        self.assertEqual(official['firmware_kind'], 'official')
        self.assertIsNone(official['cfw_release'])
        with patch.object(updates, 'VENDOR_MODULUS', 3):
            _, custom = updates.validate_firmware(package, allow_unverified=True)
        self.assertEqual(custom['firmware_kind'], 'custom')
        self.assertEqual(custom['cfw_release'], 'R15')

        unknown_build = 'cfw3:' + 'ab' * 32
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(unknown_build)))
        with patch.object(updates, 'VENDOR_MODULUS', 3):
            _, future = updates.validate_firmware(package, allow_unverified=True)
        self.assertEqual(future['firmware_kind'], 'custom')
        self.assertIsNone(future['cfw_release'])

        package, _ = build_test_package()
        with patch.object(updates, 'VENDOR_MODULUS', 3):
            _, unverified = updates.validate_firmware(package, allow_unverified=True)
        self.assertEqual(unverified['firmware_kind'], 'unverified')
        self.assertIsNone(unverified['cfw_release'])

    def test_build_evidence_is_extracted_only_from_single_canonical_mainui_literal(self):
        marker = 'cfw3:' + 'ab' * 32
        for movie, expected in ((build_test_movie(marker), marker),
                (build_test_movie('cfw3:arbitrary'), None),
                (build_test_movie(marker.upper()), None),
                (build_test_movie(marker + '\0' + marker), None),
                (build_test_movie('cfw2:' + 'ab' * 32), None)):
            data, modulus = build_test_package(rootfs=build_test_cramfs(movie))
            with patch.object(updates, 'VENDOR_MODULUS', modulus):
                _, metadata = updates.validate_firmware(data)
            self.assertEqual(metadata['cfw_build_id'], expected)

    def test_embedded_signed_version_and_model_not_filename(self):
        data, modulus = build_test_package(version=(24, 1, 75101))
        with patch.object(updates, 'VENDOR_MODULUS', modulus):
            raw, info = updates.validate_firmware(data)
        self.assertEqual(raw, data)
        self.assertEqual(info['version'], '24.1-75101')
        self.assertEqual(info['model'], 2)
        self.assertEqual(info['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(info['signature_valid'])

    def test_future_signed_release_label_requires_exact_canonical_package_marker(self):
        marker = 'cfw3:' + 'ab' * 32
        release = 'R123.4-rc2'
        descriptor, modulus = build_test_descriptor(marker, release)
        self.assertNotIn(marker, updates.KNOWN_CFW_BUILDS)
        self.assertIsNone(updates.signed_cfw_identity(descriptor))
        with patch.object(updates, '_CFW_MODULUS', modulus):
            self.assertEqual(updates.signed_cfw_identity(descriptor), (marker, release))
            a, b, c, d = descriptor.split(':')
            alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
            noncanonical = d[:-1] + alphabet[alphabet.index(d[-1]) + 1]
            for evidence, expected in (
                (descriptor, release),
                (descriptor.replace(release, 'R123.5'), None),
                (':'.join((a, b, c, noncanonical)), None),
                (descriptor + '\x00' + descriptor, None),
                (build_test_descriptor('cfw3:' + 'cd' * 32, release)[0], None),
            ):
                with self.subTest(evidence=evidence):
                    data, _ = build_test_package(rootfs=build_test_cramfs(
                        build_test_movie(marker + '\x00' + evidence)))
                    _, meta = updates.validate_firmware(data, allow_unverified=True)
                    self.assertFalse(meta['signature_valid'])
                    self.assertEqual(meta['cfw_build_id'], marker)
                    self.assertEqual(meta['cfw_release'], expected)

    def test_corrupt_wrong_model_and_unsigned_packages_are_rejected(self):
        data, modulus = build_test_package()
        corrupt = bytearray(data)
        corrupt[20] ^= 1
        wrong_model, _ = build_test_package(model=1)
        with patch.object(updates, 'VENDOR_MODULUS', modulus):
            for invalid in (bytes(corrupt), wrong_model, data[:-1], data + b'extra', b'not firmware'):
                with self.subTest(length=len(invalid)), self.assertRaises(updates.UpdateError):
                    updates.validate_firmware(invalid)
        # The generated test signature is never accepted by the real trust root.
        with self.assertRaises(updates.UpdateError):
            updates.validate_firmware(data)

    def test_unverified_option_preserves_model_and_structure_validation(self):
        data, _ = build_test_package()
        for candidate in (data, data[:-144]):
            _, metadata = updates.validate_firmware(candidate, allow_unverified=True)
            self.assertFalse(metadata['signature_valid'])
            self.assertTrue(metadata['signature_warning'])
        wrong_model, _ = build_test_package(model=1)
        for candidate in (wrong_model, data[:-1], data + b'extra', b'not firmware'):
            with self.subTest(size=len(candidate)), self.assertRaises(updates.UpdateError):
                updates.validate_firmware(candidate, allow_unverified=True)

    def test_upload_is_bounded_before_parsing(self):
        class Stream:
            def read(self, count):
                self.count = count
                return b'x' * count
        stream = Stream()
        with self.assertRaises(updates.UpdateError):
            updates.validate_firmware(stream)
        self.assertEqual(stream.count, updates.MAX_FIRMWARE_BYTES + 1)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1000.0
        self.manager = updates.UpdateManager(self.directory / 'bridge-config.json', clock=lambda: self.now)
        self.package, modulus = build_test_package()
        patched = patch.object(updates, 'VENDOR_MODULUS', modulus)
        patched.start()
        self.addCleanup(patched.stop)

    def stage(self, **kwargs):
        return self.manager.stage_controller(MAC, self.package, filename='misleading-99.0.upd', **kwargs)

    def test_legacy_receipt_gets_candidate_kind_on_status_and_selection(self):
        marker = next(build for build, release in updates.KNOWN_CFW_BUILDS.items()
                      if release == 'R15')
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        with patch.object(updates, 'VENDOR_MODULUS', 3):
            staged = self.manager.stage_controller(MAC, package, arm=False)
            with self.manager._state() as state:
                record = state['controllers'][MAC]
                record.pop('firmware_kind', None)
                record.pop('cfw_release', None)
            status = self.manager.status(MAC)
            self.assertEqual((status['firmware_kind'], status['cfw_release']), ('custom', 'R15'))
            self.manager.arm_controller(MAC, expected_sha256=staged['sha256'], allow_unverified=True)
            with self.manager._state() as state:
                record = state['controllers'][MAC]
                record.pop('firmware_kind', None)
                record.pop('cfw_release', None)
            selection = self.manager.selection(MAC)
            self.assertEqual((selection['firmware_kind'], selection['cfw_release']), ('custom', 'R15'))

    def test_upload_is_staged_without_arming_or_floor_change(self):
        status = self.stage(arm=False, current_version='24.1-75101')
        self.assertEqual(status['status'], 'staged')
        self.assertEqual(status['version'], '25.2-50130')
        self.assertNotIn('token', status)
        self.assertIsNone(self.manager.selection(MAC))
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.status(OTHER)['status'], 'none')

    def test_force_prompt_is_controller_scoped_one_shot_and_cancel_safe(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        selection = self.manager.selection(MAC)
        self.assertEqual(self.manager.floor(MAC), '24.1-75102')
        self.assertEqual(self.manager.floor(OTHER), '1.0-00000')
        self.assertTrue(self.manager.status(MAC)['force_active'])
        self.manager.acknowledge_prompt(MAC, selection['token'])
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertFalse(self.manager.status(MAC)['force_active'])
        self.assertEqual(self.manager.selection(MAC)['status'], 'armed')
        self.manager.cancel_controller(MAC)
        self.assertIsNone(self.manager.selection(MAC))
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_short_prompt_lease_preserves_manual_stage_after_expiry(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        self.now += updates.PROMPT_TTL + 1
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.selection(MAC)['status'], 'armed')
        self.assertFalse(self.manager.status(MAC)['force_active'])

    def test_restart_releases_prompts_but_ui_constructors_do_not(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        other = updates.UpdateManager(self.directory / 'bridge-config.json', clock=lambda: self.now)
        self.assertEqual(other.floor(MAC), '24.1-75102')
        other.release_prompts()
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.selection(MAC)['status'], 'armed')

    def test_delivery_is_bound_to_controller_and_token_and_not_install_success(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        selected = self.manager.selection(MAC)
        for token, mac in (('wrong', MAC), (selected['token'], OTHER)):
            with self.assertRaises(updates.UpdateError):
                self.manager.authorize_download(token, mac)
        path = self.manager.authorize_download(selected['token'], MAC)
        self.assertEqual(path.read_bytes(), self.package)
        self.manager.controller_started(selected['token'], MAC)
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
        self.manager.report_controller(MAC, '24.1-75101')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
        self.manager.report_controller(MAC, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'completed')
        self.assertIsNone(self.manager.selection(MAC))
        with self.assertRaises(updates.UpdateError):
            self.manager.authorize_download(selected['token'], MAC)

    def test_failed_attempt_retry_has_a_new_token_and_fresh_download_deadline(self):
        self.stage(current_version='24.1-75101')
        first = self.manager.selection(MAC)
        self.manager.controller_started(first['token'], MAC)
        self.manager.fail_controller(first['token'], MAC, 'Download interrupted')
        self.now += updates.DOWNLOAD_TTL + 20
        self.manager.arm_controller(MAC, current_version='24.1-75101')
        second = self.manager.selection(MAC)
        self.assertNotEqual(first['token'], second['token'])
        with self.assertRaises(updates.UpdateError):
            self.manager.authorize_download(first['token'], MAC)
        self.manager.controller_started(second['token'], MAC)
        self.assertEqual(self.manager.status(MAC)['download_started_at'], self.now)
        self.assertEqual(self.manager.status(MAC)['expires_at'], self.now + updates.DOWNLOAD_TTL)

    def test_same_version_can_arm_without_additional_confirmation(self):
        self.stage(arm=False, current_version='25.2-50130')
        result = self.manager.arm_controller(MAC, current_version='25.2-50130')
        self.assertTrue(result['armed'])
        self.assertIsNotNone(self.manager.selection(MAC, '25.2-50130'))
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_same_version_reports_preserve_retry_without_install_evidence(self):
        self.stage(current_version='25.2-50130')
        token = self.manager.selection(MAC)['token']
        for attempt in range(2):
            self.assertEqual(self.manager.authorize_download(token, MAC).read_bytes(), self.package)
            self.manager.controller_started(token, MAC)
            # Ordinary reports, including a temporarily different version,
            # cannot change the pre-attempt baseline into installation proof.
            for version in ('25.2-50130', '24.1-75101', '25.2-50130'):
                self.manager.report_controller(MAC, version)
                self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
                self.assertEqual(self.manager.selection(MAC)['token'], token)
            self.assertNotIn('completed_at', self.manager.status(MAC))

    def test_unknown_or_legacy_baseline_does_not_invent_install_proof(self):
        for legacy in (False, True):
            self.stage(current_version=None)
            token = self.manager.selection(MAC)['token']
            self.manager.controller_started(token, MAC)
            if legacy:
                with self.manager._state() as state:
                    state['controllers'][MAC].pop('install_from_version', None)
            self.manager.report_controller(MAC, '25.2-50130')
            self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
            self.manager.cancel_controller(MAC)

    def test_target_report_before_download_cannot_become_a_version_transition(self):
        self.stage(current_version='24.1-75101')
        token = self.manager.selection(MAC)['token']
        self.manager.report_controller(MAC, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'armed')
        self.manager.controller_started(token, MAC)
        self.manager.report_controller(MAC, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')

    def test_same_version_completes_only_on_matching_package_build_and_target(self):
        marker = 'cfw3:' + 'ab' * 32
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        staged = self.manager.stage_controller(MAC, package, current_version='25.2-50130')
        self.assertEqual(staged['cfw_build_id'], marker)
        self.manager.report_build(MAC, marker, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'armed')
        token = self.manager.selection(MAC)['token']
        self.manager.controller_started(token, MAC)
        for proof, version in (('v3', '25.2-50130'), ('cfw3:' + 'cd' * 32, '25.2-50130'),
                               (marker, '24.1-75101')):
            with self.assertRaises(updates.UpdateError):
                self.manager.report_build(MAC, proof, version)
            self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
        self.manager.report_controller(MAC, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')

        with self.assertRaises(updates.UpdateError):
            self.manager.report_build(OTHER, marker, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')
        result = self.manager.report_build(MAC, marker, '25.2-50130')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completion_evidence'], 'installed-movie-build')
        self.assertIsNone(self.manager.selection(MAC))
        # A late repeat is harmless, and an old image without a marker remains
        # honestly unverified even when a caller supplies a valid-looking token.
        self.manager.report_build(MAC, marker, '25.2-50130')
        self.stage(current_version='25.2-50130')
        self.manager.controller_started(self.manager.selection(MAC)['token'], MAC)
        with self.assertRaises(updates.UpdateError):
            self.manager.report_build(MAC, marker, '25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'downloading')

    def test_live_cfw_identity_requires_fresh_recognized_report(self):
        self.assertEqual(self.manager.firmware_identity(MAC)['classification'], 'stock')
        for marker, release in updates.KNOWN_CFW_BUILDS.items():
            with self.subTest(release=release):
                self.manager.report_build(MAC, marker, '25.2-50130')
                current = self.manager.firmware_identity(MAC)
                self.assertEqual(current['classification'], 'custom')
                self.assertEqual(current['release'], release)
                self.assertEqual(current['build_id'], marker)
                self.assertEqual(current['observed_at'], self.now)
                self.assertEqual(current['expires_at'], self.now + updates.LIVE_CFW_TTL)
        self.assertEqual(self.manager.firmware_identity(OTHER)['classification'], 'stock')
        # The report survives a bridge restart, while freshness still expires.
        fresh = updates.UpdateManager(self.directory / 'bridge-config.json', clock=lambda: self.now)
        self.assertEqual(fresh.firmware_identity(MAC)['classification'], 'custom')
        self.now += updates.LIVE_CFW_TTL - 1
        self.manager.report_build(MAC, marker, '25.2-50130')
        self.now += updates.LIVE_CFW_TTL - 1
        self.assertEqual(fresh.firmware_identity(MAC)['classification'], 'custom')
        self.now += 1
        self.assertEqual(fresh.firmware_identity(MAC)['classification'], 'stock')

    def test_live_cfw_identity_rejects_unrecognized_and_malformed_builds(self):
        for marker in ('cfw3:' + 'cd' * 32, 'cfw3:' + 'CD' * 32, 'not-cfw', None):
            with self.subTest(marker=marker), self.assertRaises(updates.UpdateError):
                self.manager.report_build(MAC, marker, '25.2-50130')
            self.assertEqual(self.manager.firmware_identity(MAC)['classification'], 'stock')

    def test_staged_build_is_recognized_only_for_its_controller_and_live_report(self):
        marker = 'cfw3:' + 'ab' * 32
        package, _ = build_test_package(rootfs=build_test_cramfs(build_test_movie(marker)))
        self.manager.stage_controller(MAC, package, current_version='25.2-50130')
        self.assertEqual(self.manager.firmware_identity(MAC)['classification'], 'stock')
        with self.assertRaises(updates.UpdateError):
            self.manager.report_build(OTHER, marker, '25.2-50130')
        self.manager.report_build(MAC, marker, '25.2-50130')
        self.assertEqual(self.manager.firmware_identity(MAC)['classification'], 'custom')
        # Starting a known stock image clears evidence before its old receipt
        # could be mistaken for a live install.
        self.stage(current_version='25.2-50130')
        self.manager.controller_started(self.manager.selection(MAC)['token'], MAC)
        self.assertEqual(self.manager.firmware_identity(MAC)['classification'], 'stock')

    def test_completed_version_transition_retains_only_a_bounded_player_lease(self):
        self.stage(current_version='24.1-75101')
        original = self.manager.selection(MAC)
        self.now = original['expires_at'] - 5
        self.manager.controller_started(original['token'], MAC)
        self.manager.report_controller(MAC, '25.2-50130')
        self.assertIsNone(self.manager.selection(MAC))
        self.assertEqual(self.manager.compatibility_selection(MAC, original['token'])['kind'],
                         'controller-recovery')
        self.assertIsNone(self.manager.compatibility_selection(OTHER, original['token']))
        self.assertIsNone(self.manager.compatibility_selection(MAC, 'wrong-token'))
        with self.assertRaises(updates.UpdateError):
            self.manager.authorize_download(original['token'], MAC)
        self.now = original['expires_at'] + 1
        self.assertIsNone(self.manager.compatibility_selection(MAC, original['token']))

    def test_completed_player_lease_rejects_false_legacy_completion_and_record_replacement(self):
        for action in ('cancel', 'rearm', 'stage', 'false-completion'):
            self.stage(current_version='24.1-75101')
            token = self.manager.selection(MAC)['token']
            self.manager.controller_started(token, MAC)
            self.manager.report_controller(MAC, '25.2-50130')
            self.assertIsNotNone(self.manager.compatibility_selection(MAC, token))
            if action == 'cancel':
                self.manager.cancel_controller(MAC)
            elif action == 'rearm':
                self.manager.arm_controller(MAC, current_version='25.2-50130')
            elif action == 'stage':
                self.stage(current_version='25.2-50130')
            else:
                with self.manager._state() as state:
                    state['controllers'][MAC].update(install_from_version='25.2-50130',
                        download_from_version='25.2-50130', completion_evidence=None)
            self.assertIsNone(self.manager.compatibility_selection(MAC, token), action)

    def test_identical_target_discovered_after_arming_cannot_leave_a_force_floor(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        with self.assertRaises(updates.UpdateError):
            self.manager.arm_controller(MAC, force_prompt=True, current_version='25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'staged')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.stage(force_prompt=True, current_version='24.1-75101')
        self.assertIsNotNone(self.manager.selection(MAC, current_version='25.2-50130'))
        self.assertEqual(self.manager.status(MAC)['status'], 'armed')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_nonfinite_persisted_timestamps_recover_without_a_force_floor(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        state = json.loads(self.manager.state_path.read_text())
        state['controllers'][MAC]['expires_at'] = float('nan')
        self.manager.state_path.write_text(json.dumps(state))
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.status(MAC)['status'], 'none')

    def test_validly_signed_replacement_clears_floor_when_arm_metadata_differs(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        selected = self.manager.selection(MAC)
        different, _ = build_test_package(version=(24, 1, 75101))
        Path(selected['package_path']).write_bytes(different)
        with self.assertRaises(updates.UpdateError):
            self.manager.arm_controller(MAC, force_prompt=True, current_version='24.1-75101')
        self.assertEqual(self.manager.status(MAC)['status'], 'failed')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_reviewed_hash_prevents_installing_another_admins_replacement(self):
        reviewed = self.stage(arm=False, current_version='24.1-75101')
        replacement, _ = build_test_package(version=(24, 1, 75101))
        replaced = self.manager.stage_controller(MAC, replacement, arm=False, current_version='25.2-50130')
        with self.assertRaises(updates.UpdateError) as error:
            self.manager.arm_controller(MAC, expected_sha256=reviewed['sha256'], force_prompt=True)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.manager.status(MAC)['sha256'], replaced['sha256'])
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_file_mutation_fails_receipt_and_clears_floor_even_when_delivery_raises(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        selected = self.manager.selection(MAC)
        Path(selected['package_path']).write_bytes(b'changed')
        with self.assertRaises(updates.UpdateError):
            self.manager.authorize_download(selected['token'], MAC)
        self.assertEqual(self.manager.status(MAC)['status'], 'failed')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_arm_expiry_and_corrupt_state_cannot_leave_a_prompt(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        self.now += updates.DEFAULT_TTL + 1
        self.assertEqual(self.manager.status(MAC)['status'], 'expired')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.manager.arm_controller(MAC, force_prompt=True, current_version='24.1-75101')
        self.manager.state_path.write_text('{broken')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.status(MAC)['status'], 'none')
        self.assertEqual(len(list(self.manager.storage.glob('state.json.corrupt-*'))), 1)

    def test_cross_instance_writes_preserve_other_controller_records(self):
        def stage(index):
            mac = '00:0e:58:01:03:%02x' % index
            manager = updates.UpdateManager(self.directory / 'bridge-config.json', clock=lambda: self.now)
            manager.stage_controller(mac, self.package, arm=False)
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(stage, range(12)))
        self.assertEqual(len(self.manager.status()), 12)
        self.assertEqual(len(list(self.manager.storage.glob('*.upd'))), 1)

    def test_separate_processes_serialize_persisted_stages(self):
        context = multiprocessing.get_context('spawn')
        workers = [context.Process(target=_stage_in_process,
            args=(str(self.directory / 'bridge-config.json'), self.package,
                  updates.VENDOR_MODULUS, index)) for index in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(len(self.manager.status()), 3)
        self.assertEqual(len(list(self.manager.storage.glob('*.upd'))), 1)

    def test_force_is_rejected_on_432_and_unknown_while_manual_downgrade_works(self):
        older, _ = build_test_package(version=(24, 1, 75101))
        self.manager.stage_controller(MAC, older, arm=False, current_version='25.2-50130')
        for current in ('25.2-50130', None):
            with self.assertRaisesRegex(updates.UpdateError, 'only on a controller confirmed to run 4.3'):
                self.manager.arm_controller(MAC, force_prompt=True, current_version=current)
            self.assertEqual(self.manager.floor(MAC), '1.0-00000')
            self.assertEqual(self.manager.status(MAC)['status'], 'staged')
        self.manager.arm_controller(MAC, current_version='25.2-50130')
        self.assertEqual(self.manager.selection(MAC)['version'], '24.1-75101')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_unsupported_force_stage_retains_valid_upload_but_never_arms(self):
        with self.assertRaises(updates.UpdateError):
            self.stage(force_prompt=True, current_version='25.2-50130')
        self.assertEqual(self.manager.status(MAC)['status'], 'staged')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_newly_observed_432_releases_an_old_armed_force_override(self):
        self.stage(force_prompt=True, current_version='24.1-75101')
        self.manager.report_controller(MAC, '25.2-50129')
        self.assertEqual(self.manager.floor(MAC), '1.0-00000')
        self.assertEqual(self.manager.status(MAC)['status'], 'armed')
        self.assertFalse(self.manager.status(MAC)['force_active'])

    def test_storage_failure_always_advertises_default_floor(self):
        with patch.object(self.manager, '_state', side_effect=OSError('disk unavailable')):
            self.assertEqual(self.manager.floor(MAC), '1.0-00000')

    def test_bridge_job_status_exposes_progress_without_tokens_or_internal_paths(self):
        self.manager.storage.mkdir(parents=True)
        path = self.manager.storage / 'bridge.json'
        path.write_text(json.dumps(dict(active='bearer-secret', updated_at=1001,
            installed_version='24.1-75102', controllers={MAC: dict(mac=MAC,
                token='bearer-secret', status='downloading', phase='DOWNLOAD', percent=75,
                target_version='24.1-75103', states={'PLAYER': 'downloading'},
                players=['PLAYER'], package_path='/private/example.upd', created_at=1000)})))
        result = self.manager.bridge_status()
        self.assertEqual(result['status'], 'downloading')
        self.assertEqual(result['percent'], 75)
        self.assertEqual(result['controllers'][MAC]['phase'], 'DOWNLOAD')
        self.assertEqual(result['installed_version'], '24.1-75102')
        encoded = json.dumps(result)
        for internal in ('bearer-secret', 'package_path', 'states', '/private', 'PLAYER'):
            self.assertNotIn(internal, encoded)

    def test_missing_or_corrupt_bridge_job_is_a_stub_unavailable_status(self):
        self.assertEqual(self.manager.bridge_status()['status'], 'unavailable')
        self.manager.storage.mkdir(parents=True)
        (self.manager.storage / 'bridge.json').write_text('{broken')
        status = self.manager.bridge_status()
        self.assertEqual(status['status'], 'unavailable')
        self.assertTrue(status['stub'])


if __name__ == '__main__':
    unittest.main()
