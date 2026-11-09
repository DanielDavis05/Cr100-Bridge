"""Meaningful state-gate tests; no physical controller or flash operations."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cr100_flash import flash
from cr100_flash.access import BootState, ControllerIdentity, ShellResult, ShellTransportError
from cr100_flash.backup import BackupError


class FakeShell:
    def __init__(self, *, fail_verify=False, drop_launch=False):
        self.commands = []
        self.fail_verify = fail_verify
        self.drop_launch = drop_launch

    def run(self, command, *, timeout=35):
        self.commands.append(command)
        if command == "/tmp/mcu_version":
            return ShellResult(0, "/dev/hhkbd MCU version: 1.0-29305\n", 0)
        if command.startswith("/bin/upgrade -t"):
            return ShellResult(1 if self.fail_verify else 0,
                               "Upgrade file is good\n", 0)
        if command.startswith("(/bin/sh /tmp/run.sh"):
            if self.drop_launch:
                raise ShellTransportError("listener closed after send")
            return ShellResult(0, "LAUNCH_PID:123\n", 0)
        return ShellResult(0, "", 0)


class FakeAccess:
    latest = None

    def __init__(self, **_kwargs):
        self.shell = FakeShell()
        self.closed = False
        FakeAccess.latest = self

    def verify_identity(self):
        return ControllerIdentity(flash.TARGET_MAC, flash.RELEASE, "private-household")

    def probe_existing_clean_shell(self):
        return True

    def read_boot_state(self):
        return BootState(0, 11, "mtd0: 01000000")

    def cleanup_fixture(self):
        pass

    def close_clean_listener(self):
        self.closed = True


class FakeServer:
    remaining = 2400
    def __init__(self, **_kwargs):
        self.added = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def add(self, path, content):
        self.added.append((path, content))

    def remaining_seconds(self):
        return self.remaining

    def stop(self):
        pass


def fake_backup(_run_dir, _shell, **_kwargs):
    return {
        "selection": {
            "partition_table": {"sha256": "pt-sha"},
            "selected_bank": 0, "selected_generation": 11,
            "partitions": [{"type": 17, "bank": 0, "payload": {"sha256": "b" * 64}}],
        },
        "nand_sha256": "nand-sha", "report_path": "/private/report.json",
        "files": [{"status": "captured"}],
        "summary": {"nand_differing_pages": 0},
    }


class FlashStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        assets = self.bundle / "assets"
        assets.mkdir(parents=True)
        for name in ("candidate.upd", "lab.pub", "mcu_version", "mtd_stream", "file_stream"):
            (assets / name).write_bytes(name.encode())
        self.report = {"candidate_sha256": sha256(b"candidate.upd").hexdigest(),
                       "partition_table_sha256": "pt-sha", "rootfs_sha256": "c" * 64,
                       "lab_public_key_sha256": sha256(b"lab.pub").hexdigest(),
                       "profile": "ui-only"}

    def _run(self, *, preflight_only=False, backup=fake_backup,
             fail_verify=False, drop_launch=False, confirm_flash=True,
             server_remaining=2400, live_state=None):
        FakeServer.remaining = server_remaining
        def access_factory(**kwargs):
            result = FakeAccess(**kwargs)
            result.shell = FakeShell(fail_verify=fail_verify, drop_launch=drop_launch)
            return result

        def prompt(message, expected):
            if expected.startswith("FLASH") and not confirm_flash:
                raise flash.FlashError("no typed flash confirmation")

        patches = (
            patch.object(flash, "require_pi_link"),
            patch.object(flash, "LAB_KEY_SHA256", sha256(b"lab.pub").hexdigest()),
            patch.object(flash, "STATIC_HASHES", {
                f"assets/{name}": sha256(name.encode()).hexdigest()
                for name in ("mcu_version", "mtd_stream", "file_stream")
            }),
            patch.object(flash, "AccessManager", side_effect=access_factory),
            patch.object(flash, "PayloadServer", FakeServer),
            patch.object(flash, "capture_backup", side_effect=backup),
            patch.object(flash, "_live_state", return_value=live_state or
                         {"bank": 0, "generation": 11}),
            patch.object(flash, "stage_file"),
            patch.object(flash, "_read_status", side_effect=lambda path:
                         b"<ZPSupportInfo><Command>up 90 min</Command></ZPSupportInfo>"
                         if path == "/status/uptime" else b"<ZPSupportInfo><File>old</File></ZPSupportInfo>"),
            patch.object(flash, "prompt_exact", side_effect=prompt),
            patch.object(flash, "wait_postboot", return_value={"destination": 1, "generation": 12}),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return flash.preflight_and_flash(self.bundle, self.report, self.root / "runs",
                                         preflight_only=preflight_only)

    def test_preflight_only_never_launches_and_closes_root(self):
        self._run(preflight_only=True)
        commands = FakeAccess.latest.shell.commands
        self.assertEqual(sum("/bin/sh /tmp/run.sh" in c for c in commands), 0)
        self.assertTrue(FakeAccess.latest.closed)

    def test_failed_backup_prevents_launch(self):
        with self.assertRaises(BackupError):
            self._run(backup=lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("bad image")))
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))
        self.assertTrue(FakeAccess.latest.closed)

    def test_failed_stock_verify_prevents_launch(self):
        with self.assertRaises(flash.FlashError):
            self._run(fail_verify=True)
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))
        self.assertTrue(FakeAccess.latest.closed)

    def test_already_active_rootfs_prevents_launch(self):
        def active_backup(*args, **kwargs):
            result = fake_backup(*args, **kwargs)
            result["selection"]["partitions"][0]["payload"]["sha256"] = "c" * 64
            return result
        with self.assertRaises(flash.FlashError):
            self._run(backup=active_backup)
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))

    def test_missing_typed_confirmation_prevents_launch(self):
        with self.assertRaises(flash.FlashError):
            self._run(confirm_flash=False)
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))
        self.assertTrue(FakeAccess.latest.closed)

    def test_changed_live_boot_state_prevents_launch(self):
        with self.assertRaises(flash.FlashError):
            self._run(live_state={"bank": 1, "generation": 12})
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))
        self.assertTrue(FakeAccess.latest.closed)

    def test_expired_server_prevents_launch(self):
        with self.assertRaises(flash.FlashError):
            self._run(server_remaining=100)
        self.assertFalse(any("/bin/sh /tmp/run.sh" in c for c in FakeAccess.latest.shell.commands))
        self.assertTrue(FakeAccess.latest.closed)

    def test_approved_flash_launches_once_for_current_bank(self):
        self._run()
        commands = FakeAccess.latest.shell.commands
        self.assertEqual(sum("/bin/sh /tmp/run.sh" in c for c in commands), 1)
        self.assertFalse(FakeAccess.latest.closed)

    def test_dropped_shell_does_not_retry_flash(self):
        self._run(drop_launch=True)
        commands = FakeAccess.latest.shell.commands
        self.assertEqual(sum("/bin/sh /tmp/run.sh" in c for c in commands), 1)
        self.assertFalse(FakeAccess.latest.closed)

    def test_postboot_requires_changed_log_and_reboot_evidence(self):
        old = b"<ZPSupportInfo><File>old</File></ZPSupportInfo>"
        new = (b"<ZPSupportInfo><File>Upgrade file is good\n"
               b"Destination section 1 generation 12\n"
               b"Writing kernel to partition 4\nWriting / to partition 5\n"
               b"Doing post-install steps</File></ZPSupportInfo>")
        uptime = b"<ZPSupportInfo><Command> 5:58pm up 2 min, load</Command></ZPSupportInfo>"
        hh = (b"<ZPSupportInfo><ControllerInfo><MACAddress>00:0e:58:02:6f:a9</MACAddress>"
              b"<SoftwareVersion>25.2-50130</SoftwareVersion></ControllerInfo></ZPSupportInfo>")
        with patch.object(flash, "_read_status", side_effect=lambda path: {
            "/status/jffs/upgrade.log": new,
            "/status/uptime": uptime,
            "/status/hh": hh,
        }[path]):
            report = flash.wait_postboot(self.root, destination=1, generation=12,
                                         previous_log_sha256=sha256(old).hexdigest(),
                                         preflash_uptime_minutes=90, timeout=1)
        self.assertTrue(report["uptime_reset"])

        with patch.object(flash, "_read_status", return_value=new), \
             patch.object(flash.time, "sleep"):
            with self.assertRaises(flash.FlashError):
                flash.wait_postboot(self.root, destination=1, generation=12,
                                    previous_log_sha256=sha256(new).hexdigest(),
                                    preflash_uptime_minutes=90, timeout=0.01)


if __name__ == "__main__":
    unittest.main()
