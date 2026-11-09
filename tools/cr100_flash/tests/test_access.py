"""Offline tests for the temporary CR100 access transport and cleanup gates."""

from io import BytesIO
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest


WORKSPACE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(WORKSPACE / "cr100-bridge" / "tools"))

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


def test_shell_result_uses_return_code_not_echoed_input(monkeypatch):
    monkeypatch.setattr(access.secrets, "token_hex", lambda _: "abc123")
    port, worker = _one_connection_server([
        b"# false\r\n# echo CR100_RC_abc123:$?\r\n",
        b"CR100_RC_abc123:",
        b"7\r\n# ",
    ])
    result = access.RootShellClient("127.0.0.1", port).run("false", timeout=3)
    worker.join(timeout=2)
    assert result.returncode == 7
    assert "CR100_RC_abc123:$?" in result.output
    assert "CR100_RC_abc123:7" in result.output
    assert result.elapsed >= 0


def test_shell_transport_rejects_just_echoed_marker(monkeypatch):
    monkeypatch.setattr(access.secrets, "token_hex", lambda _: "abc123")
    port, worker = _one_connection_server([b"# echo CR100_RC_abc123:$?\r\n"])
    with pytest.raises(access.ShellTransportError) as caught:
        access.RootShellClient("127.0.0.1", port).run("true", timeout=3)
    worker.join(timeout=2)
    assert "CR100_RC_abc123:$?" in caught.value.partial_output


def test_read_proc_requires_boundaries_and_simple_proc_path(monkeypatch):
    monkeypatch.setattr(access.secrets, "token_hex", lambda _: "abc123")
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

    monkeypatch.setattr(shell, "run", fake_run)
    assert shell.read_proc("/proc/mtd") == "active partition"
    assert "cat /proc/mtd" in commands[0]
    with pytest.raises(ValueError):
        shell.read_proc("/proc/../etc/passwd")


def test_read_proc_retries_missing_boundaries_and_fails_closed(monkeypatch):
    nonces = iter(("first", "second", "third", "fourth", "fifth"))
    monkeypatch.setattr(access.secrets, "token_hex", lambda _: next(nonces))
    monkeypatch.setattr(access.time, "sleep", lambda _: None)
    shell = access.RootShellClient("127.0.0.1")
    calls = []

    def transient(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return access.ShellResult(0, "# echo CR100_RC_x:$?\nCR100_RC_x:0\n", 0.1)
        return access.ShellResult(
            0, "CR100_BEGIN_second\n11\nCR100_END_second\n", 0.1,
        )

    monkeypatch.setattr(shell, "run", transient)
    assert shell.read_proc("/proc/sys/kernel/bootgeneration") == "11"
    assert len(calls) == 2

    def malformed(command, **_kwargs):
        calls.append(command)
        return access.ShellResult(0, "CR100_RC_x:0\n", 0.1)

    monkeypatch.setattr(shell, "run", malformed)
    with pytest.raises(access.AccessError, match="after 3 attempts"):
        shell.read_proc("/proc/sys/kernel/bootgeneration")
    assert len(calls) == 5


def test_verify_identity_requires_exact_mac_and_version(monkeypatch, tmp_path):
    manager = _manager(tmp_path)

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    xml = (b"<Status><MACAddress>00:0e:58:02:6f:a9</MACAddress>"
           b"<SoftwareVersion>25.2-50130</SoftwareVersion>"
           b"<HHID>private-household</HHID></Status>")
    monkeypatch.setattr(access, "urlopen", lambda *_args, **_kwargs: Response(xml))
    identity = manager.verify_identity()
    assert identity.mac == "00:0e:58:02:6f:a9"
    assert identity.version == "25.2-50130"
    assert identity.household == "private-household"
    assert "private-household" not in repr(identity)

    bad = xml.replace(b"25.2-50130", b"24.1-75101")
    monkeypatch.setattr(access, "urlopen", lambda *_args, **_kwargs: Response(bad))
    with pytest.raises(access.AccessError, match="version mismatch"):
        manager.verify_identity()


def test_fixture_address_gate_requires_pi_address_and_no_alias(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[0] == "ip":
            return subprocess.CompletedProcess(
                command, 0, "4: sonos0 inet 192.168.69.1/24 scope global sonos0\n", ""
            )
        return subprocess.CompletedProcess(command, 1,
            "2 packets transmitted, 0 packets received, 100% unanswered\n", "")

    monkeypatch.setattr(access.subprocess, "run", run)
    manager.check_fixture_address()
    assert calls[1][:4] == ["sudo", "-n", "arping", "-i"]

    def occupied(command, **_kwargs):
        if command[0] == "ip":
            return subprocess.CompletedProcess(command, 0,
                "4: sonos0 inet 192.168.69.1/24 scope global sonos0\n", "")
        return subprocess.CompletedProcess(command, 0,
            "2 packets transmitted, 1 packets received, 50% unanswered\n", "")
    monkeypatch.setattr(access.subprocess, "run", occupied)
    with pytest.raises(access.AccessError, match="already be in use"):
        manager.check_fixture_address()

    monkeypatch.setattr(access.subprocess, "run", lambda command, **_kwargs: (
        subprocess.CompletedProcess(command, 0, "4: sonos0 inet 192.168.69.100/24\n", "")
    ))
    with pytest.raises(access.AccessError, match="Pi address"):
        manager.check_fixture_address()

    monkeypatch.setattr(access.subprocess, "run", lambda command, **_kwargs: (
        subprocess.CompletedProcess(
            command, 0,
            "4: sonos0 inet 192.168.69.1/24\n4: sonos0 inet 192.168.69.100/24\n", ""
        )
    ))
    with pytest.raises(access.AccessError, match="already assigned"):
        manager.check_fixture_address()


def test_artwork_stage_copies_audited_payload_into_private_run(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
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
    monkeypatch.setattr(access, "ROP_PNG_SHA256", hashlib.sha256(payload).hexdigest())
    manager._prepare_artwork()
    assert (manager.artwork_dir / "fixed-rop-system-root-shell.png").read_bytes() == payload
    assert manager.artwork_dir.stat().st_mode & 0o777 == 0o700
    with pytest.raises(access.AccessError, match="already exists"):
        manager._prepare_artwork()


def test_artwork_stage_requires_peer_description(tmp_path):
    manager = _manager(tmp_path)
    manager.asset_rop_dir.mkdir()
    manager.peer_dir.mkdir()
    (manager.peer_dir / "zp80_peer.py").write_text("# peer\n")
    with pytest.raises(access.AccessError, match="device_description.xml"):
        manager._prepare_artwork()


def test_fixture_event_parser_accepts_adjacent_json_records(tmp_path):
    manager = _manager(tmp_path)
    manager.artwork_dir.mkdir()
    (manager.artwork_dir / "fixture.log").write_text(
        '{"event":"ready","expires_in":300}{"event":"unicast-alive"}\n'
        '{"event":"control-response-complete"}\n'
    )
    assert manager._wait_fixture_event("ready", access.time.monotonic() + 1)["expires_in"] == 300
    assert manager._wait_fixture_event("control-response-complete",
                                       access.time.monotonic() + 1)["event"] == "control-response-complete"


def test_cleanup_without_our_fixture_never_removes_another_alias(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    monkeypatch.setattr(access.subprocess, "run", lambda *_args, **_kwargs: (
        pytest.fail("cleanup inspected or changed the network before launching a fixture")
    ))
    manager.cleanup_fixture()


def test_inetd_inventory_keeps_exact_arguments(tmp_path):
    output = (
        "# for p in $(pidof inetd); do ...\r\n"
        "CR100_INETD_PID:11\r\n/usr/sbin/inetd\r\n/etc/inetd.conf\r\nCR100_INETD_END:11\r\n"
        "CR100_INETD_PID:12\r\n/usr/sbin/inetd\r\n/tmp/cve-inetd.conf\r\nCR100_INETD_END:12\r\n"
        "CR100_INETD_PID:13\r\n/usr/sbin/inetd\r\n/tmp/cve-inetd-clean.conf\r\n"
        "CR100_INETD_END:13\r\n"
    )
    assert _manager(tmp_path)._parse_inetd_processes(output) == {
        11: ("/usr/sbin/inetd", "/etc/inetd.conf"),
        12: ("/usr/sbin/inetd", "/tmp/cve-inetd.conf"),
        13: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
    }


def test_close_listener_targets_only_exact_temporary_inetd(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    commands = []

    class FakeShell:
        def run(self, command, **_kwargs):
            commands.append(command)
            return access.ShellResult(0, "", 0.1)

    manager.shell = FakeShell()
    monkeypatch.setattr(manager, "_inetd_processes", lambda _shell: {
        11: ("/usr/sbin/inetd", "/etc/inetd.conf"),
        12: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf", "extra"),
        13: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
    })
    monkeypatch.setattr(access.socket, "create_connection", lambda *_args, **_kwargs: (
        _raise_connection_refused()
    ))
    manager.close_clean_listener()
    assert commands == ["(sleep 1; kill -TERM 13) </dev/null >/dev/null 2>&1 &"]
    assert manager.shell is None


def _raise_connection_refused():
    raise ConnectionRefusedError("closed")


def test_close_listener_refuses_ambiguous_processes(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    manager.shell = object()
    monkeypatch.setattr(manager, "_inetd_processes", lambda _shell: {
        13: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
        14: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
    })
    with pytest.raises(access.AccessError, match="uniquely"):
        manager.close_clean_listener()


def test_clean_listener_retries_transient_refusal_without_duplicate_start(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
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

    monkeypatch.setattr(access, "RootShellClient", FakeClient)
    monkeypatch.setattr(access.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(manager, "_inetd_processes", lambda _shell: {
        10: ("/usr/sbin/inetd", "/tmp/cve-inetd.conf"),
        11: ("/usr/sbin/inetd", "/tmp/cve-inetd-clean.conf"),
    })
    monkeypatch.setattr(manager, "verify_identity", lambda **_kwargs: object())
    manager.establish_clean_listener()
    assert clean_attempts == 2
    assert sum("/usr/sbin/inetd /tmp/cve-inetd-clean.conf" in command
               for _, command in calls) == 1
