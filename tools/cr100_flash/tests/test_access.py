"""Offline tests for the temporary CR100 access transport and cleanup gates."""

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cr100_flash import access  # noqa: E402


def _manager(tmp_path: Path) -> access.AccessManager:
    return access.AccessManager(
        target_ip="192.168.69.52",
        expected_mac="00:0E:58:02:6F:A9",
        expected_version="25.2-50130",
        pi_bind_ip="192.168.69.1",
        fixture_ip="192.168.69.100",
        interface="sonos0",
        asset_rop_dir=tmp_path / "assets",
        peer_dir=tmp_path / "peer",
        run_dir=tmp_path,
    )


def _one_connection_server(reply: list[bytes]) -> tuple[int, threading.Thread]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                conn.recv(4096)
                for piece in reply:
                    conn.sendall(piece)
        finally:
            listener.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return port, worker


def _raise_connection_refused():
    raise ConnectionRefusedError("closed")


class AccessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def patch(self, target, name, value):
        replacement = patch.object(target, name, value)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_shell_result_uses_return_code_not_echoed_input(self):
        self.patch(access.secrets, "token_hex", lambda _: "abc123")
        port, worker = _one_connection_server([
            b"# false\r\n# echo CR100_RC_abc123:$?\r\n",
            b"CR100_RC_abc123:",
            b"7\r\n# ",
        ])
        result = access.RootShellClient("127.0.0.1", port).run("false", timeout=3)
        worker.join(timeout=2)
        self.assertEqual(result.returncode, 7)
        self.assertIn('CR100_RC_abc123:$?', result.output)
        self.assertIn('CR100_RC_abc123:7', result.output)
        self.assertGreaterEqual(result.elapsed, 0)

    def test_shell_transport_rejects_just_echoed_marker(self):
        self.patch(access.secrets, "token_hex", lambda _: "abc123")
        port, worker = _one_connection_server([b"# echo CR100_RC_abc123:$?\r\n"])
        with self.assertRaises(access.ShellTransportError) as caught:
            access.RootShellClient("127.0.0.1", port).run("true", timeout=3)
        worker.join(timeout=2)
        self.assertIn('CR100_RC_abc123:$?', caught.exception.partial_output)

    def test_read_proc_requires_boundaries_and_simple_proc_path(self):
        self.patch(access.secrets, "token_hex", lambda _: "abc123")
        shell = access.RootShellClient("127.0.0.1")
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return access.ShellResult(0, (
                "# " + command + "\r\n"
                "CR100_BEGIN_abc123\r\n"
                "active partition\r\n"
                "CR100_END_abc123\r\n"
            ), 0.1)

        self.patch(shell, "run", fake_run)
        self.assertEqual(shell.read_proc('/proc/mtd'), 'active partition')
        self.assertIn('cat /proc/mtd', commands[0])
        with self.assertRaises(ValueError):
            shell.read_proc("/proc/../etc/passwd")

    def test_read_proc_retries_missing_boundaries_and_fails_closed(self):
        nonces = iter(("first", "second", "third", "fourth", "fifth"))
        self.patch(access.secrets, "token_hex", lambda _: next(nonces))
        self.patch(access.time, "sleep", lambda _: None)
        shell = access.RootShellClient("127.0.0.1")
        calls = []

        def transient(command, **_kwargs):
            calls.append(command)
            if len(calls) == 1:
                return access.ShellResult(0, "# echo CR100_RC_x:$?\nCR100_RC_x:0\n", 0.1)
            return access.ShellResult(
                0, "CR100_BEGIN_second\n11\nCR100_END_second\n", 0.1,
            )

        self.patch(shell, "run", transient)
        self.assertEqual(shell.read_proc('/proc/sys/kernel/bootgeneration'), '11')
        self.assertEqual(len(calls), 2)

        def malformed(command, **_kwargs):
            calls.append(command)
            return access.ShellResult(0, "CR100_RC_x:0\n", 0.1)

        self.patch(shell, "run", malformed)
        with self.assertRaisesRegex(access.AccessError, "after 3 attempts"):
            shell.read_proc("/proc/sys/kernel/bootgeneration")
        self.assertEqual(len(calls), 5)

    def test_verify_identity_requires_exact_mac_and_version(self):
        manager = _manager(self.tmp_path)

        class Response(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        xml = (b"<Status><MACAddress>00:0e:58:02:6f:a9</MACAddress>"
               b"<SoftwareVersion>25.2-50130</SoftwareVersion>"
               b"<HHID>private-household</HHID></Status>")
        self.patch(access, "urlopen", lambda *_args, **_kwargs: Response(xml))
        identity = manager.verify_identity()
        self.assertEqual(identity.mac, '00:0e:58:02:6f:a9')
        self.assertEqual(identity.version, '25.2-50130')
        self.assertEqual(identity.household, 'private-household')
        self.assertNotIn('private-household', repr(identity))

        bad = xml.replace(b"25.2-50130", b"24.1-75101")
        self.patch(access, "urlopen", lambda *_args, **_kwargs: Response(bad))
        with self.assertRaisesRegex(access.AccessError, "version mismatch"):
            manager.verify_identity()

    def test_fixture_address_gate_requires_pi_address_and_no_alias(self):
        manager = _manager(self.tmp_path)
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if command[0] == "ip":
                return subprocess.CompletedProcess(
                    command, 0, "4: sonos0 inet 192.168.69.1/24 scope global sonos0\n", ""
                )
            return subprocess.CompletedProcess(command, 1,
                "2 packets transmitted, 0 packets received, 100% unanswered\n", "")

        self.patch(access.subprocess, "run", run)
        manager.check_fixture_address()
        expected = ['arping', '-i'] if os.geteuid() == 0 else ['sudo', '-n', 'arping', '-i']
        self.assertEqual(calls[1][:len(expected)], expected)

        def occupied(command, **_kwargs):
            if command[0] == "ip":
                return subprocess.CompletedProcess(command, 0,
                    "4: sonos0 inet 192.168.69.1/24 scope global sonos0\n", "")
            return subprocess.CompletedProcess(command, 0,
                "2 packets transmitted, 1 packets received, 50% unanswered\n", "")
        self.patch(access.subprocess, "run", occupied)
        with self.assertRaisesRegex(access.AccessError, "already be in use"):
            manager.check_fixture_address()

        self.patch(access.subprocess, "run", lambda command, **_kwargs: (
            subprocess.CompletedProcess(command, 0, "4: sonos0 inet 192.168.69.100/24\n", "")
        ))
        with self.assertRaisesRegex(access.AccessError, "Pi address"):
            manager.check_fixture_address()

        self.patch(access.subprocess, "run", lambda command, **_kwargs: (
            subprocess.CompletedProcess(
                command, 0,
                "4: sonos0 inet 192.168.69.1/24\n4: sonos0 inet 192.168.69.100/24\n", ""
            )
        ))
        with self.assertRaisesRegex(access.AccessError, "already assigned"):
            manager.check_fixture_address()

    def test_artwork_stage_copies_audited_payload_into_private_run(self):
        manager = _manager(self.tmp_path)
        manager.asset_rop_dir.mkdir()
        manager.peer_dir.mkdir()
        (manager.peer_dir / "zp80_peer.py").write_text("# peer\n")
        for relative in ("fixtures/xml/device_description.xml",
                         "fixtures/img/icon-ZP80.png",
                         "fixtures/xml/AVTransport1.xml",
                         "fixtures/xml/RenderingControl1.xml"):
            path = manager.peer_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        payload = b"test PNG fixture"
        for name in access.ROP_FILES:
            (manager.asset_rop_dir / name).write_bytes(
                payload if name.endswith(".png") else b"# fixture\n"
            )
        self.patch(access, "ROP_PNG_SHA256", hashlib.sha256(payload).hexdigest())
        manager._prepare_artwork()
        self.assertEqual((manager.artwork_dir / 'fixed-rop-system-root-shell.png').read_bytes(), payload)
        self.assertEqual(manager.artwork_dir.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(access.AccessError, "already exists"):
            manager._prepare_artwork()

    def test_artwork_stage_requires_peer_description(self):
        manager = _manager(self.tmp_path)
        manager.asset_rop_dir.mkdir()
        manager.peer_dir.mkdir()
        (manager.peer_dir / "zp80_peer.py").write_text("# peer\n")
        with self.assertRaisesRegex(access.AccessError, "device_description.xml"):
            manager._prepare_artwork()

    def test_fixture_event_parser_accepts_adjacent_json_records(self):
        manager = _manager(self.tmp_path)
        manager.artwork_dir.mkdir()
        (manager.artwork_dir / "fixture.log").write_text(
            '{"event":"ready","expires_in":300}{"event":"unicast-alive"}\n'
            '{"event":"control-response-complete"}\n'
        )
        self.assertEqual(manager._wait_fixture_event('ready', access.time.monotonic() + 1)['expires_in'], 300)
        self.assertEqual(manager._wait_fixture_event('control-response-complete', access.time.monotonic() + 1)['event'], 'control-response-complete')

    def test_cleanup_without_our_fixture_never_removes_another_alias(self):
        manager = _manager(self.tmp_path)
        self.patch(access.subprocess, "run", lambda *_args, **_kwargs: (
            self.fail("cleanup inspected or changed the network before launching a fixture")
        ))
        manager.cleanup_fixture()

    def test_inetd_inventory_keeps_exact_arguments(self):
        output = (
            "# for p in $(pidof inetd); do ...\r\n"
            "CR100_INETD_PID:11\r\n/usr/sbin/inetd\r\n/etc/inetd.conf\r\nCR100_INETD_END:11\r\n"
            "CR100_INETD_PID:12\r\n/usr/sbin/inetd\r\n/tmp/cve-inetd.conf\r\nCR100_INETD_END:12\r\n"
            "CR100_INETD_PID:13\r\n/usr/sbin/inetd\r\n/tmp/cve-inetd-clean.conf\r\n"
            "CR100_INETD_END:13\r\n"
        )
        self.assertEqual(_manager(self.tmp_path)._parse_inetd_processes(output), {11: ('/usr/sbin/inetd', '/etc/inetd.conf'), 12: ('/usr/sbin/inetd', '/tmp/cve-inetd.conf'), 13: ('/usr/sbin/inetd', '/tmp/cve-inetd-clean.conf')})

    def test_close_listener_targets_only_exact_temporary_inetd(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeShell:
            def run(self, command, **_kwargs):
                commands.append(command)
                return access.ShellResult(0, "", 0.1)

        manager.shell = FakeShell()
        self.patch(manager, "_inetd_processes", lambda _shell: {
            11: ("/usr/sbin/inetd", "/etc/inetd.conf"),
            12: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf", "extra"),
            13: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
        })
        self.patch(access.socket, "create_connection", lambda *_args, **_kwargs: (
            _raise_connection_refused()
        ))
        manager.close_clean_listener()
        self.assertEqual(commands, ['(sleep 1; kill -TERM 13) </dev/null >/dev/null 2>&1 &'])
        self.assertIs(manager.shell, None)

    def test_close_listener_refuses_ambiguous_processes(self):
        manager = _manager(self.tmp_path)
        manager.shell = object()
        self.patch(manager, "_inetd_processes", lambda _shell: {
            13: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
            14: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
        })
        with self.assertRaisesRegex(access.AccessError, "uniquely"):
            manager.close_clean_listener()

    def test_clean_listener_retries_transient_refusal_without_duplicate_start(self):
        manager = _manager(self.tmp_path)
        calls = []
        clean_attempts = 0

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, command, **_kwargs):
                nonlocal clean_attempts
                calls.append((self.port, command))
                if self.port == 22348 and command == "id; uname -m":
                    clean_attempts += 1
                    if clean_attempts == 1:
                        raise access.ShellTransportError("temporary connection refused")
                    return access.ShellResult(0, "uid=0(root)\nsh4\n", 0)
                return access.ShellResult(0, "", 0)

        self.patch(access, "RootShellClient", FakeClient)
        self.patch(access.time, "sleep", lambda *_args: None)
        self.patch(manager, "_start_clean_inetd", lambda _first: calls.append((22347, "start_clean_inetd")))
        self.patch(manager, "_inetd_processes", lambda _shell: {
            10: ("/usr/sbin/inetd", "/tmp/cve-inetd.conf"),
            11: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
        })
        self.patch(manager, "_prove_clean_listener_fds", lambda *_args: None)
        self.patch(manager, "_wait_old_listener_released", lambda *_args, **_kwargs: None)
        self.patch(manager, "verify_identity", lambda **_kwargs: object())
        manager.establish_clean_listener()
        self.assertEqual(clean_attempts, 2)
        self.assertEqual(calls.count((22347, "start_clean_inetd")), 1)

    def test_clean_listener_waits_for_daemonizing_inetd_parent(self):
        manager = _manager(self.tmp_path)
        inventories = []
        proved = []
        sleeps = []
        ui_waits = []

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, command, **_kwargs):
                if self.port == 22348 and command == "id; uname -m":
                    return access.ShellResult(0, "uid=0(root)\nsh4\n", 0)
                return access.ShellResult(0, "", 0)

        def inventory(_shell):
            inventories.append(True)
            processes = {
                10: ("/usr/sbin/inetd", access.OLD_INETD_CONFIG),
                12: ("/usr/sbin/inetd", access.CLEAN_INETD_CONFIG),
            }
            if len(inventories) == 1:
                processes[11] = ("/usr/sbin/inetd", access.CLEAN_INETD_CONFIG)
            return processes

        self.patch(access, "RootShellClient", FakeClient)
        self.patch(access.time, "sleep", lambda seconds: sleeps.append(seconds))
        self.patch(manager, "_start_clean_inetd", lambda _first: None)
        self.patch(manager, "_inetd_processes", inventory)
        self.patch(manager, "_prove_clean_listener_fds", lambda _shell, pid: proved.append(pid))
        self.patch(manager, "_wait_old_listener_released", lambda *_args, **_kwargs: None)
        self.patch(manager, "_wait_for_ui", lambda seconds, **kwargs: ui_waits.append((seconds, kwargs)))
        manager.establish_clean_listener()
        self.assertEqual(len(inventories), 2)
        self.assertEqual(sleeps, [0.2])
        self.assertEqual(proved, [12])
        self.assertEqual(ui_waits, [(60, {'quiet_seconds': 15, 'poll_seconds': 3})])
        self.assertFalse(manager.preserve_clean_listener)

    def test_clean_listener_preserves_shell_after_inetd_ambiguity_timeout(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        inventories = []
        commands = []

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, command, **_kwargs):
                commands.append(command)
                if self.port == 22348 and command == "id; uname -m":
                    return access.ShellResult(0, "uid=0(root)\nsh4\n", 0)
                return access.ShellResult(0, "", 0)

        def inventory(_shell):
            inventories.append(True)
            return {
                10: ("/usr/sbin/inetd", access.OLD_INETD_CONFIG),
                11: ("/usr/sbin/inetd", access.CLEAN_INETD_CONFIG),
                12: ("/usr/sbin/inetd", access.CLEAN_INETD_CONFIG),
            }

        self.patch(access, "RootShellClient", FakeClient)
        self.patch(access.time, "monotonic", lambda: clock[0])
        self.patch(access.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.patch(manager, "_start_clean_inetd", lambda _first: None)
        self.patch(manager, "_inetd_processes", inventory)
        with self.assertRaisesRegex(access.UiRecoveryError, "found 2 clean") as caught:
            manager.establish_clean_listener()
        self.assertTrue(caught.exception.listener_preserved)
        self.assertTrue(manager.preserve_clean_listener)
        self.assertGreaterEqual(clock[0], access.INETD_SETTLE_SECONDS)
        self.assertLessEqual(len(inventories), 12)
        self.assertFalse(any(command.startswith("kill ") for command in commands))

    def test_clean_listener_descriptor_proof_rejects_inherited_ui_socket(self):
        manager = _manager(self.tmp_path)
        tcp = (
            '  sl  local_address rem_address   st tx_queue:rx_queue tr:tm->when retrnsmt   uid  timeout inode\n'
            '   0: 00000000:0D48 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 34000\n'
            '   1: 00000000:574C 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 22348\n'
        )

        class FakeShell:
            def read_proc(self, path):
                assert path == '/proc/net/tcp'
                return tcp

            def run(self, command, **_kwargs):
                self.command = command
                return access.ShellResult(0, (
                    'lrwx------ 1 root root 64 /proc/11/fd/3 -> socket:[22348]\r\n'
                    'lrwx------ 1 root root 64 /proc/11/fd/4 -> socket:[34000]\r\n'
                ), 0)

        shell = FakeShell()
        with self.assertRaisesRegex(access.AccessError, 'inherited.*3400'):
            manager._prove_clean_listener_fds(shell, 11)
        self.assertIn('/bin/ls -l /proc/11/fd/*', shell.command)
        report = manager.run_dir / 'clean-listener-fds.json'
        self.assertEqual(json.loads(report.read_text())['clean_holds_ui_listener'], True)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manager._listening_inodes(tcp, 3400), {34000})
        self.assertEqual(manager._listening_inodes(tcp, 22348), {22348})
        report.unlink()
        self.patch(shell, 'run', lambda *_args, **_kwargs: access.ShellResult(
            0, 'lrwx------ 1 root root 64 /proc/11/fd/3 -> socket:[22348]\n', 0))
        manager._prove_clean_listener_fds(shell, 11)
        report.unlink()
        self.patch(shell, 'run', lambda *_args, **_kwargs: access.ShellResult(
            0, 'lrwx------ 1 root root 64 /proc/11/fd/4 -> socket:[34000]\n', 0))
        with self.assertRaisesRegex(access.AccessError, 'did not prove'):
            manager._prove_clean_listener_fds(shell, 11)

    def test_old_listener_release_retries_only_exact_pid_then_records_proof(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        commands = []

        class FakeShell:
            old_alive = True

            def read_proc(self, path):
                self_path = path
                self_test.assertEqual(self_path, '/proc/net/tcp')
                if not self.old_alive:
                    return ''
                return ('0: 00000000:0D48 00000000:0000 0A 00000000:00000000 '
                        '00:00000000 00000000 0 0 142\n')

            def run(self, command, **_kwargs):
                commands.append(command)
                if command.startswith('kill -TERM 10'):
                    self.old_alive = False
                return access.ShellResult(0, '', 0)

        self_test = self
        shell = FakeShell()
        self.patch(manager, '_inetd_processes', lambda _shell: {
            10: ('/usr/sbin/inetd', access.OLD_INETD_CONFIG)
        } if shell.old_alive else {})
        manager._wait_old_listener_released(
            shell, old_pid=10, clean_pid=11, ui_before={142})
        self.assertEqual(commands, ['kill -TERM 10 || test ! -e /proc/10'])
        self.assertGreaterEqual(clock[0], access.OLD_INETD_RETRY_SECONDS)
        report = self.tmp_path / 'old-listener-release.jsonl'
        row = json.loads(report.read_text().splitlines()[0])
        self.assertTrue(row['released'])
        self.assertTrue(row['term_retried'])
        self.assertEqual(row['ui_listener_inodes_remaining'], [])
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_old_listener_release_requires_original_ui_inode_to_close(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        commands = []

        class FakeShell:
            def read_proc(self, path):
                self_test.assertEqual(path, '/proc/net/tcp')
                return ('0: 00000000:0D48 00000000:0000 0A 00000000:00000000 '
                        '00:00000000 00000000 0 0 142\n')

            def run(self, command, **_kwargs):
                commands.append(command)
                return access.ShellResult(0, '', 0)

        self_test = self
        self.patch(manager, '_inetd_processes', lambda _shell: {})
        with self.assertRaisesRegex(access.AccessError, 'original UI listener remained'):
            manager._wait_old_listener_released(
                FakeShell(), old_pid=10, clean_pid=11, ui_before={142})
        self.assertGreaterEqual(clock[0], access.OLD_INETD_RELEASE_SECONDS)
        self.assertFalse(any(command.startswith('kill -TERM') for command in commands))
        row = json.loads((self.tmp_path / 'old-listener-release.jsonl').read_text().splitlines()[0])
        self.assertFalse(row['released'])
        self.assertEqual(row['ui_listener_inodes_remaining'], [142])

    def test_old_listener_release_rejects_pid_identity_change_without_signal(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeShell:
            def read_proc(self, path):
                assert path == '/proc/net/tcp'
                return ''

            def run(self, command, **_kwargs):
                commands.append(command)
                return access.ShellResult(0, '', 0)

        self.patch(manager, '_inetd_processes', lambda _shell: {
            10: ('/usr/sbin/inetd', '/etc/inetd.conf')
        })
        with self.assertRaisesRegex(access.AccessError, 'changed identity'):
            manager._wait_old_listener_released(
                FakeShell(), old_pid=10, clean_pid=11, ui_before={142})
        self.assertFalse(any(command.startswith('kill -TERM') for command in commands))

    def test_preterm_old_fd_snapshot_records_ui_and_m16_holder(self):
        manager = _manager(self.tmp_path)

        class FakeShell:
            def run(self, command, **_kwargs):
                self_command = command
                self_test.assertEqual(self_command, '/bin/ls -l /proc/10/fd/*')
                return access.ShellResult(0, (
                    'lrwx------ 1 root root 64 /proc/10/fd/3 -> socket:[142]\n'
                    'lrwx------ 1 root root 64 /proc/10/fd/4 -> /dev/hhkbd\n'
                ), 0)

        self_test = self
        held = manager._save_old_listener_before_term(FakeShell(), old_pid=10,
                                                     ui_before={142, 999})
        report = self.tmp_path / 'old-listener-before-term.json'
        row = json.loads(report.read_text())
        self.assertTrue(row['old_holds_ui_listener'])
        self.assertEqual(held, {142})
        self.assertEqual(row['old_ui_listener_inodes'], [142])
        self.assertTrue(row['old_holds_hhkbd'])
        self.assertEqual(row['old_pid'], 10)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_abort_closes_clean_when_old_gone_but_ui_inode_remains(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeShell:
            def read_proc(self, path):
                self_test.assertEqual(path, '/proc/net/tcp')
                return ('0: 00000000:0D48 00000000:0000 0A 00000000:00000000 '
                        '00:00000000 00000000 0 0 142\n')

            def run(self, command, **_kwargs):
                commands.append(command)
                if command.startswith('for p in /proc/[0-9]*'):
                    return access.ShellResult(0, (
                        'lrwx------ 1 root root 64 /proc/99/fd/8 -> socket:[142]\n'
                    ), 0)
                return access.ShellResult(0, '', 0)

        self_test = self
        shell = FakeShell()
        self.patch(manager, '_inetd_processes', lambda _shell: {
            11: ('/usr/sbin/inetd', access.CLEAN_INETD_CONFIG)
        })
        self.patch(manager, 'close_clean_listener', lambda: commands.append('close-clean'))
        manager._abort_temporary_listeners(shell, 10, 11, {142})
        self.assertIn('rm -f /var/run/stopanacapa && test ! -e /var/run/stopanacapa',
                      commands)
        self.assertEqual(commands[-1], 'close-clean')
        self.assertFalse(any(command.startswith('kill -TERM') for command in commands))
        row = json.loads((self.tmp_path / 'old-listener-release.jsonl').read_text().splitlines()[0])
        self.assertTrue(row['abort_cleanup'])
        self.assertFalse(row['released'])
        self.assertIn('/proc/99/fd/8', row['ui_inode_holders'][0])

    def test_dirty_clean_listener_aborts_both_before_ui_recovery(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, command, **_kwargs):
                commands.append((self.port, command))
                if self.port == 22348 and command == 'id; uname -m':
                    return access.ShellResult(0, 'uid=0(root)\nsh4\n', 0)
                return access.ShellResult(0, '', 0)

        self.patch(access, 'RootShellClient', FakeClient)
        self.patch(manager, '_start_clean_inetd', lambda _first: None)
        self.patch(manager, '_inetd_processes', lambda _shell: {
            10: ('/usr/sbin/inetd', '/tmp/cve-inetd.conf'),
            11: ('/usr/sbin/inetd', '/tmp/cve-inetd-clean.conf'),
        })
        def dirty_proof(*_args):
            commands.append(('proof', 'dirty'))
            raise access.AccessError('clean inetd inherited the controller UI port 3400')

        def close_clean():
            commands.append(('abort', 'close-clean'))
            manager.shell = None

        self.patch(manager, '_prove_clean_listener_fds', dirty_proof)
        self.patch(manager, '_wait_old_listener_released', lambda *_args, **_kwargs: commands.append(('old', 'released')))
        self.patch(manager, 'close_clean_listener', close_clean)
        self.patch(manager, '_wait_for_ui', lambda _seconds, **_kwargs: commands.append(('ui', 'recovered')))
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager.establish_clean_listener()
        self.assertFalse(caught.exception.recovery_required)
        self.assertFalse(caught.exception.listener_preserved)
        self.assertFalse(manager.preserve_clean_listener)
        self.assertLess(commands.index(('proof', 'dirty')),
                        commands.index((22348, 'kill -TERM 10 || test ! -e /proc/10')))
        self.assertIn(('abort', 'close-clean'), commands)
        self.assertIn(('ui', 'recovered'), commands)

    def test_failed_handoff_abort_preserves_verified_recovery_shell(self):
        manager = _manager(self.tmp_path)
        shell = object()
        manager.shell = shell
        def cannot_abort(*_args):
            raise access.AccessError('cannot inspect exact PID')

        self.patch(manager, '_abort_temporary_listeners', cannot_abort)
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager._abort_handoff(shell, 10, 11, 'dirty clean listener')
        self.assertTrue(caught.exception.recovery_required)
        self.assertTrue(caught.exception.listener_preserved)
        self.assertTrue(manager.preserve_clean_listener)
        self.assertIs(manager.shell, shell)

    def test_safe_abort_with_ui_still_down_reports_recovery_without_listener(self):
        manager = _manager(self.tmp_path)
        shell = object()
        manager.shell = shell
        self.patch(manager, '_abort_temporary_listeners', lambda *_args: setattr(manager, 'shell', None))

        def still_down(_seconds):
            self.assertEqual(_seconds, 180)
            raise access.AccessError('HTTP status still unreachable')

        self.patch(manager, '_wait_for_ui', still_down)
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager._abort_handoff(shell, 10, 11, 'dirty clean listener')
        self.assertTrue(caught.exception.recovery_required)
        self.assertFalse(caught.exception.listener_preserved)
        self.assertFalse(manager.preserve_clean_listener)

    def test_unreadable_inetd_inventory_preserves_verified_shell(self):
        manager = _manager(self.tmp_path)

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, _command, **_kwargs):
                return access.ShellResult(0, 'uid=0(root)\nsh4\n', 0)

        self.patch(access, 'RootShellClient', FakeClient)
        self.patch(manager, '_start_clean_inetd', lambda _first: None)

        def inventory(_shell):
            raise access.AccessError('inventory unreadable')

        self.patch(manager, '_inetd_processes', inventory)
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager.establish_clean_listener()
        self.assertTrue(caught.exception.listener_preserved)
        self.assertTrue(manager.preserve_clean_listener)
        self.assertIsNotNone(manager.shell)

    def test_abort_ui_recovery_wait_allows_more_than_old_90_seconds(self):
        manager = _manager(self.tmp_path)
        clock = [0]
        attempts = [0]
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

        def verify(**_kwargs):
            attempts[0] += 1
            if clock[0] < 95:
                raise access.AccessError('controller still restarting')
            return 'recovered'

        self.patch(manager, 'verify_identity', verify)
        self.assertEqual(manager._wait_for_ui(access.UI_ABORT_RECOVERY_SECONDS), 'recovered')
        self.assertGreater(attempts[0], 90)

    def test_normal_ui_wait_keeps_quiet_then_probes_slowly(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        checks = []
        snapshots = []
        manager.shell = object()
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

        def verify(**_kwargs):
            checks.append(clock[0])
            if clock[0] < 21:
                raise access.AccessError('controller still restarting')
            return 'recovered'

        self.patch(manager, 'verify_identity', verify)
        self.patch(manager, '_record_ui_recovery_snapshot', lambda name='ui-recovery-snapshot.json': snapshots.append((name, clock[0])))
        self.assertEqual(manager._wait_for_ui(60, quiet_seconds=15, poll_seconds=3), 'recovered')
        self.assertEqual(checks, [15, 18, 21])
        self.assertEqual(snapshots, [('ui-before-http-snapshot.json', 15)])
        event_path = self.tmp_path / 'handoff-events.jsonl'
        events = [json.loads(line)['event'] for line in event_path.read_text().splitlines()]
        self.assertEqual(event_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(events, ['ui-quiet-start', 'ui-quiet-end',
                                  'first-http-identity-probe', 'http-identity-returned'])

    def test_normal_ui_wait_bounds_quiet_and_probes_to_60_seconds(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        checks = []
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

        def unavailable(**_kwargs):
            checks.append(clock[0])
            raise access.AccessError('controller still restarting')

        self.patch(manager, 'verify_identity', unavailable)
        with self.assertRaisesRegex(access.AccessError, 'after 60s'):
            manager._wait_for_ui(60, quiet_seconds=15, poll_seconds=3)
        self.assertEqual(clock[0], 60)
        self.assertEqual(checks[0], 15)
        self.assertEqual(checks[-1], 57)
        events = [json.loads(line)['event'] for line in (self.tmp_path / 'handoff-events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1], 'http-identity-timeout')

    def test_ui_recovery_collects_one_snapshot_without_interrupting_recovery(self):
        manager = _manager(self.tmp_path)
        clock = [0.0]
        snapshots = []
        manager.shell = object()
        self.patch(access.time, 'monotonic', lambda: clock[0])
        self.patch(access.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

        def verify(**_kwargs):
            if clock[0] < 12:
                raise access.AccessError('controller still restarting')
            return 'recovered'

        self.patch(manager, 'verify_identity', verify)
        self.patch(manager, '_record_ui_recovery_snapshot', lambda: snapshots.append(clock[0]))
        self.assertEqual(manager._wait_for_ui(20), 'recovered')
        self.assertEqual(snapshots, [10.0])

    def test_ui_recovery_snapshot_is_private_and_read_only(self):
        manager = _manager(self.tmp_path)

        class FakeShell:
            def run(self, command, **_kwargs):
                self_test.assertIn('/bin/ps -ef', command)
                self_test.assertIn('cat /proc/net/tcp', command)
                self_test.assertIn('/opt/log/hh.anacapa.pid', command)
                return access.ShellResult(0, 'process and socket inventory', 0)

        self_test = self
        manager.shell = FakeShell()
        manager._record_ui_recovery_snapshot()
        path = self.tmp_path / 'ui-recovery-snapshot.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('process and socket inventory', json.loads(path.read_text())['output'])

    def test_early_handoff_failure_closes_exact_old_and_clean_listeners(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeShell:
            def run(self, command, **_kwargs):
                commands.append(command)
                if command == 'id; uname -m':
                    return access.ShellResult(0, 'uid=0(root)\nsh4\n', 0)
                return access.ShellResult(0, '', 0)

        self.patch(manager, '_inetd_processes', lambda _shell: {
            10: ('/usr/sbin/inetd', '/tmp/cve-inetd.conf'),
            11: ('/usr/sbin/inetd', '/tmp/cve-inetd-clean.conf'),
            12: ('/usr/sbin/inetd', '/etc/inetd.conf'),
        })
        self.patch(manager, '_temporary_ports_closed', lambda: True)
        self.patch(manager, '_wait_for_ui', lambda _seconds: 'recovered')
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager._abort_before_clean_verified(FakeShell(), 'helper launch failed')
        self.assertFalse(caught.exception.recovery_required)
        self.assertFalse(manager.preserve_clean_listener)
        self.assertIsNone(manager.shell)
        self.assertIn('(sleep 1; kill -TERM 10 11) </dev/null >/dev/null 2>&1 &', commands)
        self.assertFalse(any(' 12' in command and 'kill' in command for command in commands))

    def test_early_handoff_ambiguous_identity_preserves_old_root(self):
        manager = _manager(self.tmp_path)

        class FakeShell:
            def run(self, command, **_kwargs):
                if command == 'id; uname -m':
                    return access.ShellResult(0, 'uid=0(root)\nsh4\n', 0)
                self.failed = True
                return access.ShellResult(0, '', 0)

        shell = FakeShell()
        self.patch(manager, '_inetd_processes', lambda _shell: {
            10: ('/usr/sbin/inetd', '/tmp/cve-inetd.conf'),
            13: ('/usr/sbin/inetd', '/tmp/cve-inetd.conf'),
        })
        with self.assertRaises(access.UiRecoveryError) as caught:
            manager._abort_before_clean_verified(shell, 'helper launch failed')
        self.assertTrue(caught.exception.recovery_required)
        self.assertTrue(caught.exception.listener_preserved)
        self.assertTrue(manager.preserve_clean_listener)
        self.assertIs(manager.shell, shell)
        self.assertFalse(hasattr(shell, 'failed'))

    def test_clean_inetd_launch_checks_current_shell_fds_and_saves_transcript(self):
        manager = _manager(self.tmp_path)
        commands = []

        class FakeShell:
            def run(self, command, **kwargs):
                commands.append((command, kwargs))
                return access.ShellResult(0, '0 -> socket:[100]\n1 -> socket:[100]\n', 0)

        manager._start_clean_inetd(FakeShell())
        self.assertEqual(len(commands), 1)
        command, kwargs = commands[0]
        self.assertIn('>/tmp/cve-inetd-clean.conf && { fd_ok=1;', command)
        self.assertIn('for fd in /proc/$$/fd/*; do test -L "$fd" || continue;', command)
        self.assertIn('/bin/ls -l /proc/$$/fd && test "$fd_ok" -eq 1;', command)
        self.assertIn('} && /usr/sbin/inetd /tmp/cve-inetd-clean.conf', command)
        self.assertFalse(command.rstrip().endswith('&'))
        self.assertEqual(kwargs['timeout'], 45)
        report = manager.run_dir / 'clean-launch-shell.txt'
        self.assertIn('socket:[100]', report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_clean_inetd_launch_rejects_fd_gate_or_daemon_failure(self):
        manager = _manager(self.tmp_path)

        class FakeShell:
            def run(self, _command, **_kwargs):
                return access.ShellResult(1, '9 -> /dev/null\n', 0)

        with self.assertRaisesRegex(access.AccessError, 'RC 1'):
            manager._start_clean_inetd(FakeShell())
        self.assertIn('9 -> /dev/null', (manager.run_dir / 'clean-launch-shell.txt').read_text())

    def test_clean_inetd_lost_transport_saves_partial_output(self):
        manager = _manager(self.tmp_path)

        class FakeShell:
            def run(self, _command, **_kwargs):
                raise access.ShellTransportError('connection lost', 'fd inventory incomplete')

        with self.assertRaises(access.ShellTransportError):
            manager._start_clean_inetd(FakeShell())
        self.assertEqual((manager.run_dir / 'clean-launch-shell.txt').read_text(),
                         'fd inventory incomplete')

    def test_failed_clean_launch_aborts_old_listener_before_clean_probe(self):
        manager = _manager(self.tmp_path)
        called = []

        class FakeClient:
            def __init__(self, _host, port):
                self.port = port

            def run(self, _command, **_kwargs):
                return access.ShellResult(1, 'extra descriptor\n', 0)

        def abort(_first, reason):
            called.append(reason)
            raise access.UiRecoveryError('aborted', recovery_required=False)

        self.patch(access, 'RootShellClient', FakeClient)
        self.patch(manager, '_abort_before_clean_verified', abort)
        with self.assertRaises(access.UiRecoveryError):
            manager.establish_clean_listener()
        self.assertIn('RC 1', called[0])


if __name__ == "__main__":
    unittest.main()
