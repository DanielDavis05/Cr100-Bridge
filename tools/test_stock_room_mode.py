"""Reversible controller-only isolation for the stock artwork bootstrap."""

import json
from pathlib import Path
import tempfile
import unittest

from bridge_config import update_config_file
from stock_room_mode import RoomIsolation, RoomModeError, _topology_members, recover_pending


MAC = "00:0e:58:02:3a:d9"
OTHER = "00:0e:58:02:6f:a9"
EXPLOIT = "RINCON_02000000AB0901400"
BEDROOM = "RINCON_02690000000201400"
BOSE = "RINCON_02690000000301400"


class RoomIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "bridge-config.json"
        self.pid = self.root / "players.pid"
        self.pid.write_text("1234\n")
        self.config.write_text(json.dumps({
            "backend": "ha",
            "rooms": [{"entity": "media_player.bedroom", "slot": 1},
                      {"entity": "media_player.bose", "slot": 2}],
            "controllers": {MAC: {"name": "Stock"},
                            OTHER: {"name": "CFW", "visible_components": ["ha:media_player.bose"]}},
            "unrelated": {"keep": True},
        }))
        self.reloads = []

    def saved(self):
        return json.loads(self.config.read_text())

    def mode(self, readings):
        if callable(readings):
            reader = readings
        else:
            source = iter(readings)
            reader = lambda: set(next(source))
        return RoomIsolation(self.config, self.pid, MAC, "192.168.69.53", EXPLOIT,
                             fetch_topology=reader,
                             reload_players=lambda: self.reloads.append("reload"))

    def test_isolates_then_restores_only_target_and_keeps_prior_absence(self):
        mode = self.mode([{BEDROOM, BOSE}, {BEDROOM, BOSE}, {EXPLOIT},
                          {EXPLOIT}, {BEDROOM, BOSE}])
        self.assertEqual(mode.enter(timeout=2), {EXPLOIT})
        isolated = self.saved()
        self.assertEqual(isolated["controllers"][MAC]["visible_components"], [])
        self.assertEqual(isolated["controllers"][OTHER]["visible_components"], ["ha:media_player.bose"])
        self.assertTrue(mode.recovery_path.is_file())
        self.assertEqual(mode.recovery_path.stat().st_mode & 0o777, 0o600)

        update_config_file(self.config, lambda config: config["unrelated"].update(new="user edit"))
        self.assertEqual(mode.restore(timeout=2), {BEDROOM, BOSE})
        restored = self.saved()
        self.assertNotIn("visible_components", restored["controllers"][MAC])
        self.assertEqual(restored["unrelated"], {"keep": True, "new": "user edit"})
        self.assertFalse(mode.recovery_path.exists())
        self.assertEqual(len(self.reloads), 2)

    def test_failed_convergence_keeps_recovery_until_peer_withdrawal(self):
        update_config_file(self.config, lambda config: config["controllers"][MAC].update(
            visible_components=["ha:media_player.bedroom"]))
        mode = self.mode(lambda: {BEDROOM, BOSE})
        with self.assertRaisesRegex(RoomModeError, "did not show only"):
            mode.enter(timeout=0.02)
        self.assertEqual(self.saved()["controllers"][MAC]["visible_components"], [])
        self.assertTrue(mode.recovery_path.exists())
        # The caller withdraws the artwork fixture before restoring rooms.
        readings = iter([{EXPLOIT}, set(), {BEDROOM}])
        mode.fetch_topology = lambda: set(next(readings))
        self.assertEqual(mode.restore(timeout=2), {BEDROOM})
        self.assertEqual(self.saved()["controllers"][MAC]["visible_components"],
                         ["ha:media_player.bedroom"])
        self.assertFalse(mode.recovery_path.exists())
        self.assertGreaterEqual(len(self.reloads), 2)

    def test_restore_keeps_a_new_user_selection(self):
        mode = self.mode([{BEDROOM, BOSE}, {EXPLOIT}])
        mode.enter(timeout=1)
        update_config_file(self.config, lambda config: config["controllers"][MAC].update(
            visible_components=["ha:media_player.bose"]))
        with self.assertRaisesRegex(RoomModeError, "preserved the new selection"):
            mode.restore(timeout=0)
        self.assertEqual(self.saved()["controllers"][MAC]["visible_components"],
                         ["ha:media_player.bose"])
        self.assertFalse(mode.recovery_path.exists())

    def test_recover_after_restart_restores_explicit_null(self):
        update_config_file(self.config, lambda config: config["controllers"][MAC].update(
            visible_components=None))
        mode = self.mode([{BEDROOM, BOSE}, {EXPLOIT}])
        mode.enter(timeout=1)
        self.assertTrue(recover_pending(self.config, self.pid,
                                        reload_players=lambda: self.reloads.append("recover")))
        self.assertIn("visible_components", self.saved()["controllers"][MAC])
        self.assertIsNone(self.saved()["controllers"][MAC]["visible_components"])
        self.assertEqual(self.reloads[-1], "recover")
        self.assertFalse(recover_pending(self.config, self.pid,
                                         reload_players=lambda: self.reloads.append("again")))

    def test_restore_waits_for_all_previous_rooms_not_only_exploit_withdrawal(self):
        mode = self.mode([{BEDROOM, BOSE}, {EXPLOIT}, set(), {BEDROOM}, {BEDROOM, BOSE}])
        mode.enter(timeout=1)
        self.assertEqual(mode.restore(timeout=2), {BEDROOM, BOSE})

    def test_restore_requires_only_rooms_seen_before_isolation(self):
        # Bose is configured, but unavailable in this controller's baseline.
        mode = self.mode([{BEDROOM}, {EXPLOIT}, {BEDROOM}])
        mode.enter(timeout=1)
        self.assertEqual(mode.restore(timeout=1), {BEDROOM})

    def test_reload_failure_keeps_recovery_record_for_retry(self):
        mode = self.mode([{BEDROOM}, {EXPLOIT}, {BEDROOM}])
        mode.enter(timeout=1)
        mode.reload_players = lambda: (_ for _ in ()).throw(RoomModeError("reload unavailable"))
        with self.assertRaisesRegex(RoomModeError, "reload unavailable"):
            mode.restore(timeout=1)
        self.assertTrue(mode.recovery_path.exists())
        self.assertNotIn("visible_components", self.saved()["controllers"][MAC])
        mode.reload_players = lambda: self.reloads.append("retry")
        self.assertEqual(mode.restore(timeout=1), {BEDROOM})
        self.assertFalse(mode.recovery_path.exists())

    def test_invalid_or_stale_topology_does_not_pass(self):
        mode = self.mode(lambda: {EXPLOIT, BEDROOM})
        with self.assertRaises(RoomModeError):
            mode.enter(timeout=0.02)
        self.assertTrue(mode.recovery_path.exists())
        self.assertEqual(self.saved()["controllers"][MAC]["visible_components"], [])


class TopologyParseTest(unittest.TestCase):
    def test_reads_stock_controller_cache(self):
        payload = (b'<?xml version="1.0"?><ZPSupportInfo><ZonePlayers>'
                   b'<ZonePlayer uuid="' + BEDROOM.encode() + b'">Bedroom</ZonePlayer>'
                   b'</ZonePlayers></ZPSupportInfo>')
        self.assertEqual(_topology_members(payload), {BEDROOM})

    def test_rejects_missing_or_untrusted_member(self):
        for payload in (b"<ZPSupportInfo/>",
                        b'<ZPSupportInfo><ZonePlayers><ZonePlayer uuid="bad"/></ZonePlayers></ZPSupportInfo>',
                        b"not xml"):
            with self.subTest(payload=payload), self.assertRaises(RoomModeError):
                _topology_members(payload)


if __name__ == "__main__":
    unittest.main()
