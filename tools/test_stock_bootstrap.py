"""Offline state, confirmation, and interrupted-run tests for stock bootstrap."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cr100_flash.access import AccessError, ShellTransportError, UiRecoveryError
from cr100_flash.flash import Journal
from cr100_flash.access import ROP_PNG_SHA256
import distribution
from stock_bootstrap import BootstrapError, BootstrapManager, _Session, _updater_failure
import stock_bootstrap
from stock_room_mode import RoomModeError


MAC = "00:0e:58:02:3a:d9"


class StockBootstrap(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.state = self.root / "state.json"
        self.run = self.root / "run"
        self.run.mkdir()
        self.pid = self.run / "players.pid"
        self.storage = self.root / "private"
        self.config.write_text(json.dumps({"rooms": [], "controllers": {MAC: {}}}))
        self.state.write_text(json.dumps({"household": "Sonos_123", "controllers": []}))
        self.candidate = self.root / "candidate.upd"
        self.candidate.write_bytes(b"arbitrary CFW package, no release manifest")
        self.key = self.root / "arbitrary.pub"
        self.key.write_bytes(b"operator selected public key")
        (self.run / "leases").write_text(f"{int(time.time()) + 300} {MAC} 192.168.69.53 *\n")
        status_xml = ("<ZPSupportInfo><ControllerInfo><MACAddress>%s</MACAddress>"
                      "</ControllerInfo></ZPSupportInfo>" % MAC).encode()
        self.status_patch = patch.object(stock_bootstrap, "_status_bytes", return_value=status_xml)
        self.status_patch.start()
        self.addCleanup(self.status_patch.stop)
        self.manager = BootstrapManager(self.config, self.state, self.run, self.pid, self.storage)

    def test_artwork_room_identity_changes_between_runs(self):
        first = stock_bootstrap._fixture_identity("20261119T061719Z-47d088fc")
        second = stock_bootstrap._fixture_identity("20261119T062719Z-47d088fd")
        self.assertEqual(first, stock_bootstrap._fixture_identity("20261119T061719Z-47d088fc"))
        self.assertNotEqual(first, second)
        self.assertTrue(first[0].startswith("02:"))
        self.assertEqual(first[1], "RINCON_" + first[0].replace(":", "").upper() + "01400")
        self.assertNotEqual(first[1], stock_bootstrap.EXPLOIT_UDN)

    def write_room_recovery(self):
        path = self.config.with_name("stock-room-mode.json")
        path.write_text(json.dumps({
            "format": 1, "session": "a1c98e01cdbdc0659ac9b711", "mac": MAC,
            "ip": "192.168.69.53", "exploit_udn": stock_bootstrap.EXPLOIT_UDN,
            "selection_present": False, "selection": None, "prior_rooms": [],
        }))
        path.chmod(0o600)
        return path

    def test_arbitrary_package_stages_and_can_be_discarded(self):
        snapshot = self.manager.stage(MAC, self.candidate, "candidate.upd", self.key)
        self.assertEqual(snapshot["sha256"], sha256(self.candidate.read_bytes()).hexdigest())
        self.assertEqual(snapshot["key_sha256"], sha256(self.key.read_bytes()).hexdigest())
        self.assertTrue(snapshot["can_cancel"])
        self.assertNotIn(str(self.root), str(snapshot))
        self.assertEqual(self.manager.cancel(MAC)["status"], "cancelled")
        self.assertFalse((self.storage / MAC.replace(":", "") / "candidate.upd").exists())

    def test_distribution_contains_pinned_fixture_and_public_key_only(self):
        selected = {path.relative_to(distribution.ROOT).as_posix()
                    for path in distribution.files()}
        self.assertIn("tools/stock_bootstrap.py", selected)
        self.assertIn("tools/stock_room_mode.py", selected)
        self.assertIn("tools/stock_bootstrap_assets/development-upgradekey.pub", selected)
        self.assertNotIn("tools/stock_bootstrap_assets/rop/launch_clean_inetd", selected)
        payload = distribution.ROOT / "tools/stock_bootstrap_assets/rop/fixed-rop-system-root-shell.png"
        self.assertEqual(sha256(payload.read_bytes()).hexdigest(), ROP_PNG_SHA256)
        self.assertFalse([path for path in selected if path.startswith("tools/stock_bootstrap_assets/")
                          and ("private" in path.lower() or path.endswith((".pem", ".key")))])

    def test_stock_updater_diagnostic_excludes_shell_command(self):
        output = ("/bin/upgrade -t -f -K /tmp/lab.pub http://192.168.69.1:1/firmware.upd\r\n"
                  "Upgrade failed: (6) upgrade file signature verification failed\r\n"
                  "pull_upgrade failed\r\nCR100_RC_abc:1\r\n")
        self.assertEqual(_updater_failure(output, 1),
                         "Upgrade failed: (6) upgrade file signature verification failed")

    def test_uncertain_launch_cannot_be_erased_by_new_upload(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        self.manager._set(MAC, "uncertain", "Prior launch outcome unknown.",
                          flash_launch_sent=True, can_cancel=False)
        with self.assertRaises(BootstrapError):
            self.manager.stage(MAC, self.candidate, "replacement.upd")
        self.assertEqual(self.manager.status(MAC)["status"], "uncertain")
        with self.assertRaises(BootstrapError):
            self.manager.start(MAC)

    def test_preserved_root_listener_blocks_another_bootstrap(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        self.manager._set(MAC, "failed", "Controller UI recovery needs attention.",
                          access_recovery_required=True, listener_preserved=True,
                          run_id="20261119T000000Z-00000001",
                          controller_ip="192.168.69.53", can_cancel=False)
        room_path = self.write_room_recovery()
        with patch.object(self.manager, "_cleanup_interrupted_fixture"), \
             patch.object(stock_bootstrap, "RoomIsolation",
                          side_effect=AssertionError("room restore must wait for root cleanup")):
            self.assertTrue(self.manager.status(MAC)["recovery_required"])
            with self.assertRaisesRegex(BootstrapError, "UI recovery needs attention"):
                self.manager.stage(MAC, self.candidate, "replacement.upd")
            with self.assertRaisesRegex(BootstrapError, "UI recovery needs attention"):
                self.manager.start(MAC)
        self.assertTrue(room_path.exists())

    def test_launch_intent_is_durable_before_one_uncertain_send(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        run_path = self.storage / "runs" / "unit"
        run_path.mkdir(parents=True)
        session = _Session(MAC, "192.168.69.53", self.candidate, self.key,
                           sha256(self.candidate.read_bytes()).hexdigest(), run_path,
                           "02:fd:11:22:33:44", "RINCON_02FD1122334401400")
        calls = []

        def send(_command, *, timeout):
            self.assertEqual(timeout, 15)
            calls.append(json.loads(self.manager.record_path.read_text())[
                MAC]["flash_launch_sent"])
            raise ShellTransportError("response lost")

        access = SimpleNamespace(shell=SimpleNamespace(run=send))
        journal = Journal(run_path)
        try:
            self.manager._launch_once(session, access, journal, 1, 7)
        finally:
            journal.close()
        self.assertEqual(calls, [True])
        self.assertTrue(session.launch_sent)
        self.assertEqual(self.manager.status(MAC)["status"], "uncertain")
        self.assertEqual(
            (run_path / "events.jsonl").read_text().count('"event": "flash-launch-sent"'), 1)

    def test_postboot_probes_leave_thirty_second_startup_windows(self):
        run_path = self.storage / "runs" / "postboot"
        run_path.mkdir(parents=True)
        session = _Session(MAC, "192.168.69.53", self.candidate, self.key,
                           "candidate-sha", run_path, "02:fd:11:22:33:44",
                           "RINCON_02FD1122334401400")
        clock = SimpleNamespace(now=0.0, sleeps=[])
        probes = []
        completed_log = (b"<ZPSupportInfo><File>Upgrade file is good; "
                         b"Destination section 1 generation 14; "
                         b"Writing kernel to partition 4; Writing / to partition 5; "
                         b"Doing post-install steps</File></ZPSupportInfo>")

        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds

        def status(_ip, path):
            probes.append((clock.now, path))
            if path == "/status/jffs/upgrade.log":
                count = sum(name == path for _, name in probes)
                if count == 1:
                    raise OSError("controller restarting")
                if count == 2:
                    return b"<incomplete"
                if count == 3:
                    return b"<ZPSupportInfo><File>old log</File></ZPSupportInfo>"
                return completed_log
            if path == "/status/uptime":
                return b"<ZPSupportInfo><Command>up 1 min</Command></ZPSupportInfo>"
            if path == "/status/hh":
                return ("<ZPSupportInfo><MACAddress>%s</MACAddress></ZPSupportInfo>" % MAC).encode()
            raise AssertionError(path)

        fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
        with patch.object(stock_bootstrap, "time", fake_time), \
             patch.object(stock_bootstrap, "_status_bytes", side_effect=status):
            receipt = self.manager._wait_postboot(session, 1, 14,
                                                  sha256(b"old log").hexdigest(), 100,
                                                  timeout=180)
        self.assertEqual([time for time, path in probes
                          if path == "/status/jffs/upgrade.log"], [30, 60, 90, 120])
        self.assertEqual(clock.sleeps, [30, 30, 30, 30])
        self.assertEqual(receipt["generation"], 14)
        self.assertTrue(receipt["status_went_offline"])
        self.assertEqual((run_path / "upgrade-log.xml").read_bytes(), completed_log)

    def test_postboot_invalid_xml_does_not_count_as_reboot(self):
        run_path = self.storage / "runs" / "postboot-no-reboot"
        run_path.mkdir(parents=True)
        session = _Session(MAC, "192.168.69.53", self.candidate, self.key,
                           "candidate-sha", run_path, "02:fd:11:22:33:44",
                           "RINCON_02FD1122334401400")
        clock = SimpleNamespace(now=0.0, sleeps=[])
        probes = []
        completed_log = (b"<ZPSupportInfo><File>Upgrade file is good; "
                         b"Destination section 1 generation 14; "
                         b"Writing kernel to partition 4; Writing / to partition 5; "
                         b"Doing post-install steps</File></ZPSupportInfo>")

        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds

        def status(_ip, path):
            probes.append((clock.now, path))
            if path == "/status/jffs/upgrade.log":
                if sum(name == path for _, name in probes) == 1:
                    return b"<incomplete"
                return completed_log
            if path == "/status/uptime":
                return b"<ZPSupportInfo><Command>up 1 min</Command></ZPSupportInfo>"
            raise AssertionError("identity must not be read without reboot evidence")

        fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
        with patch.object(stock_bootstrap, "time", fake_time), \
             patch.object(stock_bootstrap, "_status_bytes", side_effect=status):
            with self.assertRaisesRegex(BootstrapError,
                                        "awaiting-reboot-evidence, status-invalid"):
                self.manager._wait_postboot(session, 1, 14,
                                            sha256(b"old log").hexdigest(), 1,
                                            timeout=95)
        self.assertEqual([time for time, path in probes
                          if path == "/status/jffs/upgrade.log"], [30, 60, 90])
        self.assertEqual(clock.sleeps, [30, 30, 30, 5])
        self.assertFalse((run_path / "upgrade-log.xml").exists())

    def test_recovery_retries_after_players_process_appears(self):
        records = {MAC: {"status": "await_now_playing", "message": "old",
                         "run_id": "20261119T000000Z-00000001", "controller_ip": "192.168.69.53",
                         "flash_launch_sent": False}}
        self.storage.mkdir(exist_ok=True)
        (self.storage / "state.json").write_text(json.dumps(records))
        room_path = self.write_room_recovery()
        attempts = []

        class FakeRooms:
            def __init__(self, *_args, **_kwargs): pass

            def restore(self, timeout):
                attempts.append(timeout)
                if len(attempts) < 3:
                    raise RoomModeError("players unavailable")
                room_path.unlink()

        with patch.object(stock_bootstrap, "RoomIsolation", FakeRooms):
            restarted = BootstrapManager(self.config, self.state, self.run, self.pid, self.storage)
            self.assertTrue(restarted.startup_error)
            with self.assertRaisesRegex(BootstrapError, "room selection needs recovery"):
                restarted.start(MAC)
            self.assertTrue(restarted.startup_error)
            restarted._recovery_retry_at = 0
            self.assertEqual(restarted.status(MAC)["status"], "failed")
            self.assertIsNone(restarted.startup_error)
            self.assertFalse(room_path.exists())
            self.assertEqual(attempts, [3, 8, 8])

    def test_failed_cleanup_is_retried_before_restoring_pending_rooms(self):
        self.storage.mkdir(exist_ok=True)
        (self.storage / "state.json").write_text(json.dumps({
            MAC: {"status": "failed", "run_id": "20261119T000000Z-00000001",
                  "controller_ip": "192.168.69.53", "flash_launch_sent": False}}))
        room_path = self.write_room_recovery()
        order = []

        class FakeRooms:
            def __init__(self, *_args, **_kwargs): pass

            def restore(self, timeout):
                order.append("rooms")
                room_path.unlink()

        with patch.object(BootstrapManager, "_cleanup_interrupted_fixture",
                          side_effect=lambda *_: order.append("fixture")), \
             patch.object(stock_bootstrap, "RoomIsolation", FakeRooms):
            BootstrapManager(self.config, self.state, self.run, self.pid, self.storage)
        self.assertEqual(order, ["fixture", "rooms"])

    def test_same_process_status_retries_room_recovery_after_ui_returns(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        self.manager._set(MAC, "failed", "Controller room selection needs recovery.",
                          run_id="20261119T000000Z-00000001",
                          controller_ip="192.168.69.53", can_cancel=False,
                          room_recovered=True)
        room_path = self.write_room_recovery()
        order = []
        fixture_ready = False

        def cleanup(*_args):
            order.append("fixture")
            if not fixture_ready:
                raise AccessError("fixture still running")

        class FakeRooms:
            def __init__(self, *_args, **_kwargs): pass

            def restore(self, timeout):
                order.append("rooms")
                room_path.unlink()

        with patch.object(self.manager, "_cleanup_interrupted_fixture", side_effect=cleanup), \
             patch.object(stock_bootstrap, "RoomIsolation", FakeRooms):
            status = self.manager.status(MAC)
            self.assertTrue(status["recovery_required"])
            self.assertFalse(status["room_recovered"])
            self.assertEqual(order, ["fixture"])
            with self.assertRaisesRegex(BootstrapError, "artwork cleanup"):
                self.manager.stage(MAC, self.candidate, "replacement.upd")
            self.assertTrue(room_path.exists())
            fixture_ready = True
            self.manager._recovery_retry_at = 0
            status = self.manager.status(MAC)
        self.assertEqual(order, ["fixture", "fixture", "fixture", "rooms"])
        self.assertFalse(status.get("recovery_required", False))
        self.assertTrue(status["room_recovered"])
        self.assertEqual(status["status"], "failed")
        self.assertFalse(room_path.exists())

    def test_status_does_not_reload_rooms_while_controller_ui_is_down(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        self.manager._set(MAC, "failed", "Controller room selection needs recovery.",
                          run_id="20261119T000000Z-00000001",
                          controller_ip="192.168.69.53", can_cancel=False)
        room_path = self.write_room_recovery()
        with patch.object(self.manager, "_cleanup_interrupted_fixture"), \
             patch.object(stock_bootstrap, "_status_bytes", side_effect=OSError("connection refused")), \
             patch.object(stock_bootstrap, "RoomIsolation",
                          side_effect=AssertionError("must not reload while HTTP is down")):
            status = self.manager.status(MAC)
        self.assertTrue(status["recovery_required"])
        self.assertFalse(status["room_recovered"])
        self.assertTrue(room_path.exists())
        self.assertIn("not ready", status["message"])

    def test_existing_listener_still_requires_artwork_and_screen_confirmation(self):
        staged = self.manager.stage(MAC, self.candidate, "candidate.upd")
        self.manager._set(MAC, "failed", "Earlier run recovered.", room_recovered=True)
        events = []
        fixture_identity = {}
        manager = self.manager
        root = self.root

        class FakeAccess:
            def __init__(self, **kwargs):
                fixture_identity["mac"] = kwargs["fixture_mac"]
                self.artwork_dir = kwargs["run_dir"] / "artwork"
                self.artwork_dir.mkdir()
                self.shell = None
                self.fixture_ready = False

            def verify_identity(self):
                return SimpleNamespace(household="Sonos_123", version="25.2-50130")

            def probe_existing_clean_shell(self):
                return True

            def close_clean_listener(self):
                events.append("old-listener-closed")

            def acquire_root(self, prompt):
                self.fixture_ready = True
                events.append("fixture-ready")
                try:
                    prompt("screen")
                    (self.artwork_dir / "fixture.log").write_text('{"event":"payload-response-complete"}')
                    events.append("artwork-sent")
                finally:
                    self.cleanup_fixture()

            def cleanup_fixture(self):
                if self.fixture_ready:
                    self.fixture_ready = False
                    events.append("fixture-stopped")

            def establish_clean_listener(self):
                self.shell = SimpleNamespace()
                events.append("clean-listener")

        class FakeRooms:
            def __init__(self, *_args, **_kwargs):
                fixture_identity["udn"] = _args[4]
                self.recovery_path = root / "room-recovery.json"

            def enter(self, timeout):
                self.recovery_path.write_text("pending")
                events.append("room-isolated")

            def restore(self, timeout):
                self.recovery_path.unlink()
                events.append("rooms-restored")

        def complete(session, _access, _journal):
            self.assertTrue(manager.status(session.mac)["room_recovered"])
            self.assertTrue(json.loads(manager.record_path.read_text())[MAC]["room_recovered"])
            manager._set(session.mac, "completed", "Preflight fixture completed.",
                         can_cancel=False)

        self_outer = self
        with patch.object(stock_bootstrap, "AccessManager", FakeAccess), \
             patch.object(stock_bootstrap, "RoomIsolation", FakeRooms), \
             patch.object(manager, "_preflight_and_launch", side_effect=complete):
            self.manager.start(MAC)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.manager.status(MAC)["status"] != "await_now_playing":
                time.sleep(0.01)
            self.assertEqual(self.manager.status(MAC)["status"], "await_now_playing")
            self.assertFalse(self.manager.status(MAC)["room_recovered"])
            self.assertEqual(events, ["old-listener-closed", "fixture-ready", "room-isolated"])
            self.assertEqual(staged["sha256"], self.manager.status(MAC)["sha256"])
            self.assertEqual(self.manager.status(MAC)["artwork_udn"], fixture_identity["udn"])
            self.assertEqual(fixture_identity["udn"],
                             "RINCON_" + fixture_identity["mac"].replace(":", "").upper() + "01400")
            self.manager.confirm_screen(MAC)
            self.manager.session.thread.join(timeout=3)
        self.assertEqual(self.manager.status(MAC)["status"], "completed")
        self.assertTrue(self.manager.status(MAC)["room_recovered"])
        self.assertLess(events.index("fixture-stopped"), events.index("rooms-restored"))
        self.assertIn("artwork-sent", events)

    def test_failed_ui_handoff_preserves_verified_recovery_listener(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        events = []
        root = self.root

        class FakeAccess:
            def __init__(self, **kwargs):
                self.artwork_dir = kwargs["run_dir"] / "artwork"
                self.artwork_dir.mkdir()
                self.shell = None
                self.preserve_clean_listener = False

            def verify_identity(self):
                return SimpleNamespace(household="Sonos_123", version="25.2-50130")

            def probe_existing_clean_shell(self): return False

            def acquire_root(self, prompt):
                prompt("screen")
                (self.artwork_dir / "fixture.log").write_text('{"event":"payload-response-complete"}')

            def cleanup_fixture(self): events.append("fixture-stopped")

            def establish_clean_listener(self):
                self.shell = SimpleNamespace()
                self.preserve_clean_listener = True
                raise UiRecoveryError("verified listener retained", recovery_required=True,
                                      listener_preserved=True)

            def close_clean_listener(self): events.append("listener-closed")

        class FakeRooms:
            def __init__(self, *_args, **_kwargs):
                self.recovery_path = root / "fake-room-record"

            def enter(self, timeout): self.recovery_path.write_text("pending")

            def restore(self, timeout):
                self.recovery_path.unlink()
                events.append("rooms-restored")

        with patch.object(stock_bootstrap, "AccessManager", FakeAccess), \
             patch.object(stock_bootstrap, "RoomIsolation", FakeRooms):
            self.manager.start(MAC)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.manager.status(MAC)["status"] != "await_now_playing":
                time.sleep(0.01)
            self.manager.confirm_screen(MAC)
            thread = self.manager.session.thread
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("rooms-restored", events)
        self.assertNotIn("listener-closed", events)
        status = self.manager.status(MAC)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["recovery_required"])
        self.assertTrue(status["listener_preserved"])

    def test_safe_ui_handoff_abort_reports_http_return_without_root_listener(self):
        self.manager.stage(MAC, self.candidate, "candidate.upd")
        root = self.root

        class FakeAccess:
            def __init__(self, **kwargs):
                self.artwork_dir = kwargs["run_dir"] / "artwork"
                self.artwork_dir.mkdir()
                self.shell = None
                self.preserve_clean_listener = False

            def verify_identity(self):
                return SimpleNamespace(household="Sonos_123", version="25.2-50130")

            def probe_existing_clean_shell(self): return False

            def acquire_root(self, prompt):
                prompt("screen")
                (self.artwork_dir / "fixture.log").write_text('{"event":"payload-response-complete"}')

            def cleanup_fixture(self): pass

            def establish_clean_listener(self):
                raise UiRecoveryError("both inetds closed; HTTP returned", recovery_required=False)

        class FakeRooms:
            def __init__(self, *_args, **_kwargs):
                self.recovery_path = root / "fake-room-record"

            def enter(self, timeout): self.recovery_path.write_text("pending")

            def restore(self, timeout): self.recovery_path.unlink()

        with patch.object(stock_bootstrap, "AccessManager", FakeAccess), \
             patch.object(stock_bootstrap, "RoomIsolation", FakeRooms):
            self.manager.start(MAC)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.manager.status(MAC)["status"] != "await_now_playing":
                time.sleep(0.01)
            self.manager.confirm_screen(MAC)
            thread = self.manager.session.thread
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        status = self.manager.status(MAC)
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status.get("recovery_required", False))
        self.assertFalse(status["listener_preserved"])
        self.assertIn("HTTP status returned", status["message"])


if __name__ == "__main__":
    unittest.main()
