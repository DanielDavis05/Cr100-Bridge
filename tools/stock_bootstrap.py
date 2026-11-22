"""User-confirmed stock 4.3.2 artwork bootstrap for one CR100.

The web UI owns this manager. It stages a package without an archived release
allowlist, advertises the bounded artwork fixture, waits for the owner at Now
Playing, then uses the stock updater's own check-only mode before offering one
flash launch. An interrupted launch is observed, never retried automatically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from bridge_config import write_json
from controller_state import normalize_mac
from cr100_flash.access import AccessError, AccessManager, ShellTransportError, UiRecoveryError
from cr100_flash.backup import BackupError, _live_state, capture_backup
from cr100_flash.flash import FlashError, Journal, _log_text, _uptime_minutes
from cr100_flash.launcher import build_launcher
from cr100_flash.server import PayloadServer
from stock_room_mode import RoomIsolation, RoomModeError, _read_record


HERE = Path(__file__).resolve().parent
ASSETS = HERE / "stock_bootstrap_assets"
PEER_DIR = HERE / "zp80"
STOCK_RELEASE = "25.2-50130"
PI_IP = "192.168.69.1"
FIXTURE_IP = "192.168.69.100"
# Kept for recovery records from runs made before per-run artwork identities.
EXPLOIT_UDN = "RINCON_02000000AB0B01400"
MAX_PACKAGE = 16 << 20
MAX_KEY = 4096
WAIT_SCREEN = 240
WAIT_FLASH = 1800
POSTBOOT_QUIET_SECONDS = 30
TERMINAL = frozenset(("completed", "failed", "cancelled", "uncertain"))
ACTIVE = frozenset(("preparing", "await_now_playing", "triggering", "root_ready",
                    "preflight", "await_flash_confirmation", "installing", "verifying"))


class BootstrapError(ValueError):
    """A public bootstrap operation error with an HTTP-compatible status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Cancelled(RuntimeError):
    pass


def _fixture_identity(run_id: str) -> tuple[str, str]:
    """Derive one locally administered peer identity from the durable run ID."""
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id):
        raise ValueError("invalid stock bootstrap run ID")
    value = sha256(run_id.encode("ascii")).hexdigest()[:10]
    mac = "02:" + ":".join(value[index:index + 2] for index in range(0, 10, 2))
    udn = "RINCON_02" + value.upper() + "01400"
    return mac, udn


def _public_failure(error: BaseException) -> str:
    if isinstance(error, (BootstrapError, RoomModeError)):
        message = str(error)
    elif isinstance(error, UiRecoveryError):
        message = ("Controller UI recovery needs attention; inspect the private bootstrap run log."
                   if error.recovery_required else
                   "Temporary controller access was stopped and controller HTTP status returned; inspect the private bootstrap run log.")
    elif isinstance(error, AccessError):
        message = "Controller access failed; inspect the private bootstrap run log."
    elif isinstance(error, BackupError):
        message = "Fresh controller backup failed; inspect the private bootstrap run log."
    else:
        message = "Bootstrap stopped unexpectedly; inspect the private bootstrap run log."
    return message[:497] + "..." if len(message) > 500 else message


class _Session:
    def __init__(self, mac: str, ip: str, package: Path, key: Path,
                 package_sha: str, run_dir: Path,
                 fixture_mac: str, fixture_udn: str):
        self.mac, self.ip = mac, ip
        self.package, self.key = package, key
        self.package_sha, self.run_dir = package_sha, run_dir
        self.fixture_mac, self.fixture_udn = fixture_mac, fixture_udn
        self.screen = threading.Event()
        self.flash = threading.Event()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.access: AccessManager | None = None
        self.isolation: RoomIsolation | None = None
        self.launch_sent = False


def _private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _status_bytes(ip: str, path: str, *, timeout: float = 4) -> bytes:
    with urlopen("http://%s:3400%s" % (ip, path), timeout=timeout) as response:
        return response.read(2 << 20)


def _required(shell, command: str, label: str, *, timeout: float = 35) -> str:
    result = shell.run(command, timeout=timeout)
    if result.returncode:
        raise BootstrapError("%s failed (exit %s)." % (label, result.returncode), 409)
    return result.output


def _updater_failure(output: str, returncode: int) -> str:
    """Expose the stock updater's own failure line without echoing shell input."""
    normalized = output.replace("\r", "\n")
    lines = [line.strip() for line in normalized.splitlines()]
    for line in reversed(lines):
        if line.startswith("Upgrade failed:") and len(line) <= 350:
            return line
    return "stock updater exited %s; inspect the private verify-only log" % returncode


def _stage_file(shell, name: str, size: int, port: int, *, executable: bool = True) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unsafe staging filename")
    mode = "700" if executable else "600"
    command = ('wget -O /tmp/%s http://%s:%d/%s && '
               'test "$(wc -c </tmp/%s)" -eq %d && chmod %s /tmp/%s'
               % (name, PI_IP, port, name, name, size, mode, name))
    _required(shell, command, "Staging " + name, timeout=90)


def _lease_ip(path: Path, mac: str) -> str:
    now = time.time()
    found = []
    try:
        rows = path.read_text().splitlines()
    except OSError as error:
        raise BootstrapError("Controller leases are unavailable.", 503) from error
    for row in rows:
        fields = row.split()
        if len(fields) < 3 or fields[1].lower() != mac:
            continue
        try:
            expiry = int(fields[0])
            address = ipaddress.IPv4Address(fields[2])
        except ValueError:
            continue
        if expiry > now and address in ipaddress.IPv4Network("192.168.69.0/24"):
            found.append((expiry, str(address)))
    if not found:
        raise BootstrapError("Wake this controller and wait for its SonosNet lease.", 409)
    return max(found)[1]


class BootstrapManager:
    """One active bootstrap across the bridge, with durable public progress."""

    def __init__(self, config_path, state_path, run_dir, players_pid_path, storage_dir):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.run_dir = Path(run_dir)
        self.players_pid_path = Path(players_pid_path)
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.storage_dir.chmod(0o700)
        self.record_path = self.storage_dir / "state.json"
        self.lock = threading.RLock()
        self.records: dict[str, dict] = {}
        self.session: _Session | None = None
        self.startup_error: str | None = None
        self.room_record_path = self.config_path.with_name("stock-room-mode.json")
        self._recovery_retry_at = 0.0
        if self.record_path.exists():
            try:
                records = json.loads(self.record_path.read_text())
                if isinstance(records, dict):
                    self.records = {key: value for key, value in records.items()
                                    if isinstance(key, str) and isinstance(value, dict)}
            except (OSError, ValueError):
                self.records = {}
        self.interrupted = {mac: dict(record) for mac, record in self.records.items()
                            if record.get("status") in ACTIVE}
        recovered = self._attempt_recovery(timeout=3)
        changed = False
        for record in self.records.values():
            if record.get("status") in ACTIVE:
                launch = bool(record.get("flash_launch_sent"))
                record["status"] = "uncertain" if launch else "failed"
                record["message"] = ("Bridge restarted after the flash launch; inspect the controller before retrying."
                                     if launch else "Bridge restarted during bootstrap; start a new session.")
                record["can_cancel"] = False
                changed = True
        if recovered or changed:
            self._save()

    def _attempt_recovery(self, *, timeout: float = 8) -> bool:
        """Withdraw a prior fixture, then verify the normal room view returns.

        The record stays in place if the controller UI is still unavailable.
        This also handles a timeout from the current web UI process after its
        worker has exited; a service restart is not needed for another try.
        """
        if self.session is not None and self.session.thread and self.session.thread.is_alive():
            return False
        errors = []
        try:
            room = _read_record(self.room_record_path)
        except RoomModeError as error:
            room = None
            errors.append("Controller room selection needs recovery: " + str(error))
        candidates = dict(self.interrupted)
        if room is not None:
            pending = self.records.get(room["mac"])
            if pending is None:
                errors.append("Controller room recovery has no matching bootstrap run.")
            else:
                candidates.setdefault(room["mac"], dict(pending))
                if pending.get("listener_preserved"):
                    errors.append("Controller UI recovery needs attention; the verified temporary root listener was retained.")
        # Service termination can strand the fixture child and its Pi alias.
        # Withdraw that exact run before restoring the controller's normal view.
        for mac, record in candidates.items():
            try:
                self._cleanup_interrupted_fixture(mac, record)
            except (OSError, ValueError, AccessError) as error:
                errors.append("Interrupted artwork cleanup needs attention: " + str(error))
            else:
                self.interrupted.pop(mac, None)
        recovered = False
        if not errors and room is not None:
            try:
                # A failed room restore already saved the prior selection.
                # Do not SIGHUP every player on each poll while the controller
                # UI is still down; wait for its own status service first.
                identity = ET.fromstring(_status_bytes(room["ip"], "/status/hh", timeout=2))
                if normalize_mac(identity.findtext(".//MACAddress") or "") != room["mac"]:
                    raise RoomModeError("controller identity changed during room recovery")
            except (OSError, ValueError, ET.ParseError, RoomModeError) as error:
                errors.append("Controller UI is not ready for room recovery: " + str(error))
        if not errors and room is not None:
            try:
                isolation = RoomIsolation(self.config_path, self.players_pid_path,
                                          room["mac"], room["ip"], room["exploit_udn"],
                                          recovery_path=self.room_record_path)
                isolation.restore(timeout=timeout)
                recovered = True
            except (OSError, ValueError, RoomModeError) as error:
                errors.append("Controller room selection needs recovery: " + str(error))
        self.startup_error = "; ".join(errors) if errors else None
        self._recovery_retry_at = time.monotonic() + (10 if errors else 0)
        if recovered:
            record = self.records.get(room["mac"])
            if record is not None and record.get("status") in TERMINAL:
                record["room_recovered"] = True
                if not record.get("listener_preserved"):
                    record["access_recovery_required"] = False
                if (not record.get("access_recovery_required") and
                        "recovery" in str(record.get("message", "")).lower()):
                    record["message"] = "Normal rooms restored after the controller UI recovered; bootstrap stopped before upgrade."
                self._save()
        return recovered

    def _cleanup_interrupted_fixture(self, mac: str, record: dict) -> None:
        run_id = record.get("run_id")
        ip = record.get("controller_ip")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id):
            raise AccessError("interrupted bootstrap run has no verified staging directory")
        try:
            ipaddress.IPv4Address(ip)
        except (TypeError, ValueError):
            raise AccessError("interrupted bootstrap run has no verified controller address") from None
        run_path = self.storage_dir / "runs" / run_id
        artwork = run_path / "artwork"
        if not artwork.exists():
            return
        access = AccessManager(
            target_ip=ip, expected_mac=mac,
            expected_version=STOCK_RELEASE, pi_bind_ip=PI_IP,
            fixture_ip=FIXTURE_IP, interface="sonos0",
            asset_rop_dir=ASSETS / "rop", peer_dir=PEER_DIR, run_dir=run_path)
        verified_process = False
        runner_pid_path = artwork / "runner.pid"
        if runner_pid_path.is_file():
            try:
                pid = int(runner_pid_path.read_text().strip())
                if pid <= 1:
                    raise ValueError("invalid PID")
                cmdline = Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ")
            except (OSError, ValueError):
                cmdline = b""
            if cmdline:
                if (b"run_rop_shell.sh" not in cmdline or
                        os.fsencode(str(artwork)) not in cmdline):
                    raise AccessError("interrupted artwork runner PID belongs to another process")
                verified_process = True
                os.kill(pid, 15)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and Path("/proc/%d" % pid).exists():
                    time.sleep(0.1)
        # _fixture_pid validates both the process command and the exact run
        # directory. A stale PID is harmless; an unrelated live PID is not.
        if (artwork / "fixture.pid").is_file():
            try:
                access._fixture_pid()
                verified_process = True
            except AccessError:
                try:
                    pid = int((artwork / "fixture.pid").read_text().strip())
                    if pid <= 1:
                        return
                    os.kill(pid, 0)
                except (OSError, ValueError):
                    pass
                else:
                    raise AccessError("interrupted fixture PID belongs to another process")
        if not verified_process:
            try:
                addresses = subprocess.run(
                    ["ip", "-o", "-4", "addr", "show", "dev", "sonos0"],
                    check=True, capture_output=True, text=True).stdout
            except (OSError, subprocess.CalledProcessError) as error:
                raise AccessError("could not inspect interrupted artwork address") from error
            if re.search(r"\binet %s/24\b" % re.escape(FIXTURE_IP), addresses):
                raise AccessError("artwork address remains without a verified fixture process")
            return
        access._fixture_launch_attempted = True
        access.cleanup_fixture()

    def _save(self) -> None:
        write_json(self.record_path, self.records, mode=0o600)
        # The launch intent must survive an abrupt restart before the one-shot
        # shell command is issued.
        with self.record_path.open("rb") as record:
            os.fsync(record.fileno())
        directory = os.open(self.record_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _set(self, mac: str, status: str, message: str, **fields) -> dict:
        with self.lock:
            record = self.records.setdefault(mac, {})
            update = {"status": status, "message": message,
                      "updated_at": datetime.now(timezone.utc).isoformat(),
                      "can_cancel": status in ACTIVE and not record.get("flash_launch_sent", False)}
            update.update(fields)
            record.update(update)
            self._save()
            return dict(record)

    def _recover_if_needed(self, *, force: bool = False) -> None:
        if self.session is not None and self.session.thread and self.session.thread.is_alive():
            return
        if not (self.startup_error or self.interrupted or self.room_record_path.exists()):
            return
        if force or time.monotonic() >= self._recovery_retry_at:
            self._attempt_recovery()

    def status(self, mac) -> dict:
        mac = normalize_mac(mac)
        with self.lock:
            self._recover_if_needed()
            snapshot = dict(self.records.get(mac) or
                        {"status": "none", "message": "No stock bootstrap staged.",
                         "can_cancel": False})
            if self.room_record_path.exists():
                snapshot["room_recovered"] = False
            if self.startup_error:
                snapshot["recovery_required"] = True
                snapshot["message"] = self.startup_error
            elif self.room_record_path.exists() and not (
                    self.session is not None and self.session.thread and self.session.thread.is_alive()):
                snapshot["recovery_required"] = True
                snapshot["message"] = "Controller room selection still needs recovery."
            elif snapshot.get("access_recovery_required"):
                snapshot["recovery_required"] = True
            return snapshot

    def stage(self, mac, package_path, filename, key_path=None) -> dict:
        """Copy an operator-selected UPD and public key; do not enforce a release allowlist."""
        mac = normalize_mac(mac)
        source = Path(package_path)
        key_source = Path(key_path) if key_path is not None else ASSETS / "development-upgradekey.pub"
        if source.is_symlink() or not source.is_file() or key_source.is_symlink() or not key_source.is_file():
            raise BootstrapError("Choose a readable firmware file and public key.")
        package_size = source.stat().st_size
        key_size = key_source.stat().st_size
        if not 0 < package_size <= MAX_PACKAGE:
            raise BootstrapError("Firmware must be nonempty and at most 16 MiB.")
        if not 0 < key_size <= MAX_KEY:
            raise BootstrapError("The public key file is empty or too large.")
        label = Path(str(filename)).name
        if not label or len(label) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise BootstrapError("Invalid firmware filename.")
        with self.lock:
            if self.session is not None and self.session.thread and self.session.thread.is_alive():
                raise BootstrapError("A stock bootstrap is already running.", 409)
            self._recover_if_needed(force=True)
            if self.startup_error or self.room_record_path.exists():
                raise BootstrapError(self.startup_error or "Controller room selection still needs recovery.", 409)
            if any(record.get("access_recovery_required") for record in self.records.values()):
                raise BootstrapError("Controller UI recovery needs attention before another bootstrap.", 409)
            prior = self.records.get(mac, {})
            if prior.get("status") == "uncertain" or prior.get("flash_launch_sent") and prior.get("status") != "completed":
                raise BootstrapError("A prior updater launch has an uncertain outcome; inspect the controller before staging another package.", 409)
            directory = self.storage_dir / mac.replace(":", "")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            staged = directory / "candidate.upd"
            staged_key = directory / "public.pub"
            temporary = directory / ("candidate.%s.tmp" % os.urandom(6).hex())
            temporary_key = directory / ("public.%s.tmp" % os.urandom(6).hex())
            try:
                digest = sha256()
                copied = 0
                with source.open("rb") as input_file, temporary.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    while block := input_file.read(1 << 20):
                        copied += len(block)
                        if copied > MAX_PACKAGE:
                            raise BootstrapError("Firmware must be nonempty and at most 16 MiB.")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if copied != package_size:
                    raise BootstrapError("Firmware changed during upload; upload it again.", 409)
                with key_source.open("rb") as input_key:
                    key_bytes = input_key.read(MAX_KEY + 1)
                if len(key_bytes) != key_size:
                    raise BootstrapError("Public key changed during upload; upload it again.", 409)
                _private_bytes(temporary_key, key_bytes)
                os.replace(temporary, staged)
                os.replace(temporary_key, staged_key)
            finally:
                temporary.unlink(missing_ok=True)
                temporary_key.unlink(missing_ok=True)
            self.records[mac] = {
                "status": "staged", "message": "Firmware staged for stock bootstrap.",
                "filename": label, "sha256": digest.hexdigest(),
                "bytes": package_size, "key_sha256": sha256(key_bytes).hexdigest(),
                "key_bytes": len(key_bytes), "can_cancel": True,
                "flash_launch_sent": False, "art_fetched": False, "room_converged": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save()
            return dict(self.records[mac])

    def start(self, mac) -> dict:
        mac = normalize_mac(mac)
        with self.lock:
            self._recover_if_needed(force=True)
            if self.startup_error or self.room_record_path.exists():
                raise BootstrapError(self.startup_error or "Controller room selection still needs recovery.", 409)
            if any(record.get("access_recovery_required") for record in self.records.values()):
                raise BootstrapError("Controller UI recovery needs attention before another bootstrap.", 409)
            if self.session is not None and self.session.thread and self.session.thread.is_alive():
                raise BootstrapError("A stock bootstrap is already running.", 409)
            record = self.records.get(mac)
            if not record or record.get("status") not in ("staged", "failed", "cancelled"):
                raise BootstrapError("Stage firmware before starting a stock bootstrap.", 409)
            directory = self.storage_dir / mac.replace(":", "")
            package, key = directory / "candidate.upd", directory / "public.pub"
            if not package.is_file() or not key.is_file():
                raise BootstrapError("Staged firmware or public key is missing.", 409)
            package_sha = sha256(package.read_bytes()).hexdigest()
            if not hmac.compare_digest(package_sha, record["sha256"]):
                raise BootstrapError("Staged firmware changed; upload it again.", 409)
            key_sha = sha256(key.read_bytes()).hexdigest()
            if not hmac.compare_digest(key_sha, record["key_sha256"]):
                raise BootstrapError("Staged public key changed; upload it again.", 409)
            ip = _lease_ip(self.run_dir / "leases", mac)
            run_root = self.storage_dir / "runs"
            run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
            run_path = run_root / run_id
            run_path.mkdir(mode=0o700)
            fixture_mac, fixture_udn = _fixture_identity(run_id)
            session = _Session(mac, ip, package, key, package_sha, run_path,
                               fixture_mac, fixture_udn)
            self.session = session
            self._set(mac, "preparing", "Checking the stock controller and starting the artwork room.",
                      controller_ip=ip, run_id=run_id, flash_launch_sent=False,
                      artwork_udn=fixture_udn, room_converged=False, art_fetched=False,
                      room_recovered=False, access_recovery_required=False,
                      listener_preserved=False)
            thread = threading.Thread(target=self._run, args=(session,),
                                      name="cr100-stock-bootstrap", daemon=True)
            session.thread = thread
            thread.start()
            return dict(self.records[mac])

    def confirm_screen(self, mac) -> dict:
        mac = normalize_mac(mac)
        with self.lock:
            session = self.session
            if session is None or session.mac != mac or self.records[mac]["status"] != "await_now_playing":
                raise BootstrapError("The artwork room is not ready for screen confirmation.", 409)
            session.screen.set()
            return dict(self.records[mac])

    def confirm_flash(self, mac, sha256_hex) -> dict:
        mac = normalize_mac(mac)
        with self.lock:
            session = self.session
            if session is None or session.mac != mac or self.records[mac]["status"] != "await_flash_confirmation":
                raise BootstrapError("The controller is not ready for the final flash confirmation.", 409)
            if not isinstance(sha256_hex, str) or not hmac.compare_digest(sha256_hex, session.package_sha):
                raise BootstrapError("Confirm the exact staged firmware SHA-256.", 409)
            session.flash.set()
            return dict(self.records[mac])

    def cancel(self, mac) -> dict:
        mac = normalize_mac(mac)
        with self.lock:
            if self.records.get(mac, {}).get("status") == "staged":
                directory = self.storage_dir / mac.replace(":", "")
                (directory / "candidate.upd").unlink(missing_ok=True)
                (directory / "public.pub").unlink(missing_ok=True)
                return self._set(mac, "cancelled", "Staged firmware discarded.", can_cancel=False)
            session = self.session
            if session is None or session.mac != mac or not session.thread or not session.thread.is_alive():
                raise BootstrapError("No stock bootstrap is running.", 409)
            if session.launch_sent:
                raise BootstrapError("The updater launch has already been sent; observe its result.", 409)
            session.cancel.set()
            self._set(mac, self.records[mac]["status"], "Cancellation requested; cleaning up.")
            return dict(self.records[mac])

    def close(self) -> None:
        """Request prelaunch cancellation when the web UI shuts down."""
        with self.lock:
            session = self.session
            if session is None or session.launch_sent:
                return
            session.cancel.set()
            thread = session.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=12)

    @staticmethod
    def _await(event: threading.Event, cancel: threading.Event, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not event.wait(0.25):
            if cancel.is_set():
                raise _Cancelled()
            if time.monotonic() >= deadline:
                raise BootstrapError("Confirmation timed out; the session was cleaned up.", 408)
        if cancel.is_set():
            raise _Cancelled()

    def _run(self, session: _Session) -> None:
        access = None
        isolation = None
        journal = Journal(session.run_dir)
        try:
            access = AccessManager(
                target_ip=session.ip, expected_mac=session.mac,
                expected_version=STOCK_RELEASE, pi_bind_ip=PI_IP,
                fixture_ip=FIXTURE_IP, interface="sonos0",
                asset_rop_dir=ASSETS / "rop", peer_dir=PEER_DIR,
                run_dir=session.run_dir, fixture_mac=session.fixture_mac)
            session.access = access
            identity = access.verify_identity()
            household = json.loads(self.state_path.read_text()).get("household")
            if identity.household != household:
                raise BootstrapError("Controller is paired to another household.", 409)
            journal.write("identity-verified", durable=True, mac=session.mac,
                          version=identity.version, candidate_sha256=session.package_sha)
            if session.cancel.is_set():
                raise _Cancelled()
            isolation = RoomIsolation(
                self.config_path, self.players_pid_path, session.mac,
                session.ip, session.fixture_udn,
                log=lambda item: journal.write(item.pop("event"), **item))
            session.isolation = isolation

            def screen_prompt(_message: str) -> None:
                isolation.enter(timeout=45)
                self._set(session.mac, "await_now_playing",
                          "Only Install firmware should appear. Open its Now Playing screen, then confirm here.",
                          room_converged=True)
                self._await(session.screen, session.cancel, WAIT_SCREEN)
                self._set(session.mac, "triggering",
                          "Sending one artwork response and waiting for temporary access.")

            if access.probe_existing_clean_shell():
                # An earlier session may have left a verified RAM listener.
                # Close that exact listener before this session's own trigger.
                access.close_clean_listener()
            access.acquire_root(screen_prompt)
            # acquire_root() withdraws the peer and address in its finally.
            access.cleanup_fixture()
            fixture_log = access.artwork_dir / "fixture.log"
            fetched = fixture_log.exists() and '"payload-response-complete"' in fixture_log.read_text(errors="replace")
            if not fetched:
                raise BootstrapError("The one-shot artwork response was not confirmed.", 409)
            self._set(session.mac, "triggering", "Artwork response completed; recovering the controller UI.",
                      art_fetched=True)
            access.establish_clean_listener()
            self._set(session.mac, "root_ready", "Temporary root access verified and controller UI recovered.")
            if session.cancel.is_set():
                raise _Cancelled()
            if isolation.recovery_path.exists():
                isolation.restore(timeout=45)
            self._set(session.mac, "preflight", "Taking a fresh backup and running the stock updater check.",
                      room_recovered=not isolation.recovery_path.exists())
            self._preflight_and_launch(session, access, journal)
        except _Cancelled:
            journal.write("cancelled", durable=True)
            self._set(session.mac, "cancelled", "Stock bootstrap cancelled and temporary resources withdrawn.",
                      can_cancel=False)
        except BaseException as error:
            journal.write("stopped", durable=True, error=repr(error),
                          flash_launch_sent=session.launch_sent)
            state = "uncertain" if session.launch_sent else "failed"
            recovery = isinstance(error, UiRecoveryError)
            self._set(session.mac, state, _public_failure(error), can_cancel=False,
                      access_recovery_required=bool(recovery and error.recovery_required),
                      listener_preserved=bool(recovery and error.listener_preserved))
        finally:
            fixture_clean = True
            try:
                if access is not None:
                    access.cleanup_fixture()
            except Exception as error:
                fixture_clean = False
                journal.write("fixture-cleanup-warning", error=repr(error))
            if fixture_clean and not getattr(access, "preserve_clean_listener", False):
                try:
                    if isolation is not None and isolation.recovery_path.exists():
                        isolation.restore(timeout=45)
                        current = self.status(session.mac)
                        message = ("Normal rooms restored after the controller UI recovered; bootstrap stopped before upgrade."
                                   if current.get("access_recovery_required") else current["message"])
                        self._set(session.mac, current["status"], message,
                                  room_recovered=True, access_recovery_required=False,
                                  listener_preserved=False, can_cancel=False)
                except Exception as error:
                    journal.write("room-restore-warning", error=repr(error), durable=True)
                    current = self.status(session.mac)["status"]
                    state = "uncertain" if session.launch_sent else "failed"
                    if current != "completed":
                        self._set(session.mac, state,
                                  "Controller room selection needs recovery: " + str(error),
                                  can_cancel=False)
            elif (fixture_clean and access is not None and
                  getattr(access, "preserve_clean_listener", False) and
                  isolation is not None and isolation.recovery_path.exists()):
                state = "uncertain" if session.launch_sent else "failed"
                self._set(session.mac, state,
                          "Controller UI recovery needs attention before normal rooms can be verified.",
                          access_recovery_required=True, listener_preserved=True,
                          can_cancel=False)
            elif isolation is not None and isolation.recovery_path.exists():
                state = "uncertain" if session.launch_sent else "failed"
                self._set(session.mac, state,
                          "Artwork cleanup needs recovery before normal rooms can be restored.",
                          can_cancel=False)
            if (not session.launch_sent and access is not None and access.shell is not None and
                    not getattr(access, "preserve_clean_listener", False)):
                try:
                    access.close_clean_listener()
                except Exception as error:
                    journal.write("listener-cleanup-warning", error=repr(error), durable=True)
                    state = "uncertain" if session.launch_sent else "failed"
                    self._set(session.mac, state,
                              "Temporary controller root listener cleanup needs attention; inspect the private run log.",
                              access_recovery_required=True, listener_preserved=True,
                              can_cancel=False)
            elif (not session.launch_sent and access is not None and
                  getattr(access, "preserve_clean_listener", False)):
                journal.write("listener-preserved-for-recovery", durable=True)
                state = "uncertain" if session.launch_sent else "failed"
                self._set(session.mac, state,
                          "Controller UI recovery needs attention; the verified temporary root listener was retained.",
                          access_recovery_required=True, listener_preserved=True,
                          can_cancel=False)
            journal.close()
            with self.lock:
                if self.session is session:
                    self.session = None

    def _preflight_and_launch(self, session: _Session, access: AccessManager,
                              journal: Journal) -> None:
        if access.shell is None:
            raise BootstrapError("Temporary root access is unavailable.", 409)
        package = session.package.read_bytes()
        if not hmac.compare_digest(sha256(package).hexdigest(), session.package_sha):
            raise BootstrapError("Staged firmware changed; upload it again.", 409)
        key = session.key.read_bytes()
        payloads = {
            "/firmware.upd": package, "/lab.pub": key,
            "/mcu_version": (ASSETS / "mcu_version").read_bytes(),
            "/mtd_stream": (ASSETS / "mtd_stream").read_bytes(),
            "/file_stream": (ASSETS / "file_stream").read_bytes(),
        }
        with PayloadServer(bind_ip=PI_IP, allow_ip=session.ip, port=0,
                           log_path=session.run_dir / "http.jsonl",
                           payloads=payloads, duration=3600) as server:
            for name in ("mcu_version", "mtd_stream", "file_stream"):
                _stage_file(access.shell, name, len(payloads["/" + name]), server.port)
            mcu = _required(access.shell, "/tmp/mcu_version", "MCU version check")
            if "MCU version: 1.0-29305" not in mcu:
                raise BootstrapError("Controller MCU version differs from the tested model.", 409)
            journal.write("mcu-verified", version="1.0-29305")
            backup = capture_backup(
                session.run_dir, access.shell.run, controller_ip=session.ip,
                bind_ip=PI_IP,
                receiver_script=ASSETS / "receive_dump.py",
                file_receiver_script=ASSETS / "receive_file.py",
                selector_path=ASSETS / "select_nand_boot.py",
                include_files=True)
            selection = backup["selection"]
            bank, generation = selection["selected_bank"], selection["selected_generation"]
            journal.write("backup-verified", durable=True, selected_bank=bank,
                          selected_generation=generation,
                          backup_report=backup["report_path"])
            if session.cancel.is_set():
                raise _Cancelled()
            _stage_file(access.shell, "lab.pub", len(key), server.port, executable=False)
            verify_result = access.shell.run(
                "/bin/upgrade -t -f -K /tmp/lab.pub http://%s:%d/firmware.upd"
                % (PI_IP, server.port), timeout=180)
            _private_bytes(session.run_dir / "verify-only.txt", verify_result.output.encode("utf-8", "replace"))
            if verify_result.returncode or "Upgrade file is good" not in verify_result.output:
                raise BootstrapError("The stock updater rejected this firmware in check-only mode: "
                                     + _updater_failure(verify_result.output, verify_result.returncode), 409)
            journal.write("stock-verify-passed", durable=True)
            pre_log = _status_bytes(session.ip, "/status/jffs/upgrade.log")
            pre_uptime = _status_bytes(session.ip, "/status/uptime")
            _private_bytes(session.run_dir / "preflash-upgrade-log.xml", pre_log)
            _private_bytes(session.run_dir / "preflash-uptime.xml", pre_uptime)
            pre_log_sha = sha256(pre_log).hexdigest()
            pre_minutes = _uptime_minutes(pre_uptime)
            live = _live_state(access.shell.run)
            if (live["bank"], live["generation"]) != (bank, generation):
                raise BootstrapError("Controller boot state changed after backup.", 409)
            _required(access.shell,
                      "test -c /dev/mtd/0 && grep ' /jffs ' /proc/mounts >/dev/null 2>&1 && "
                      "test ! -e /var/run/upgradeflag && test ! -e /var/run/stopanacapa && "
                      "test ! -e /tmp/lab-upgrade.phase && ! pidof upgrade >/dev/null 2>&1 && "
                      "pidof hh.anacapad >/dev/null 2>&1",
                      "Final controller state guard")
            launcher = build_launcher(bank, generation, PI_IP, server.port)
            server.add("/run.sh", launcher)
            _stage_file(access.shell, "run.sh", len(launcher), server.port)
            _required(access.shell, "sh -n /tmp/run.sh", "Launcher syntax check")
            destination, next_generation = 1 - bank, generation + 1
            self._set(session.mac, "await_flash_confirmation",
                      "Updater check passed. Review the candidate SHA-256 and destination before the one-time flash.",
                      destination_bank=destination,
                      destination_generation=next_generation)
            self._await(session.flash, session.cancel, WAIT_FLASH)
            if server.remaining_seconds() < 1200:
                raise BootstrapError("Firmware server lease is too short; restart the preflight.", 409)
            live = _live_state(access.shell.run)
            if (live["bank"], live["generation"]) != (bank, generation):
                raise BootstrapError("Controller boot state changed before launch.", 409)
            if session.cancel.is_set():
                raise _Cancelled()
            self._launch_once(session, access, journal, destination, next_generation)
            self._set(session.mac, "verifying",
                      "Waiting for reboot and the new upgrade log.",
                      flash_launch_sent=True, can_cancel=False)
            result = self._wait_postboot(session, destination, next_generation,
                                         pre_log_sha, pre_minutes)
            journal.write("postboot-verified", durable=True, **result)
            self._set(session.mac, "completed",
                      "Upgrade log confirms the destination bank and completed install.",
                      flash_launch_sent=True, can_cancel=False)

    def _launch_once(self, session: _Session, access: AccessManager,
                     journal: Journal, destination: int, generation: int) -> None:
        """Persist intent before the only shell send; an uncertain send is observed."""
        journal.write("flash-launch-sent", durable=True,
                      expected_destination=destination,
                      expected_generation=generation)
        session.launch_sent = True
        self._set(session.mac, "installing",
                  "One updater launch sent; waiting for the controller to reboot.",
                  flash_launch_sent=True, can_cancel=False)
        try:
            launched = access.shell.run(
                "(/bin/sh /tmp/run.sh </dev/null >/tmp/lab-upgrade-launch.log 2>&1) & echo LAUNCH_PID:$!",
                timeout=15)
            if launched.returncode or not re.search(r"(?m)^LAUNCH_PID:[0-9]+\r?$", launched.output):
                raise ShellTransportError("Updater launch PID was not observed.")
            journal.write("launch-command-returned")
        except (AccessError, ShellTransportError, BootstrapError) as error:
            journal.write("launch-result-uncertain", durable=True, detail=str(error))
            self._set(session.mac, "uncertain",
                      "Updater launch outcome is uncertain; observing without retrying.",
                      flash_launch_sent=True, can_cancel=False)

    @staticmethod
    def _wait_postboot(session: _Session, destination: int, generation: int,
                       pre_log_sha: str, pre_minutes: int | None,
                       timeout: float = 900) -> dict:
        """Allow each controller app startup a quiet interval before HTTP.

        Repeated status requests coincided with a physical CR100 UI restart
        loop; the UI recovered shortly after polling stopped. Give anacapa
        startup time between probes while keeping the original deadline and
        the updater's one-time launch behavior.
        """
        expected = "Destination section %d generation %d" % (destination, generation)
        deadline = time.monotonic() + timeout
        went_offline = False
        seen = set()
        time.sleep(min(POSTBOOT_QUIET_SECONDS, max(0, deadline - time.monotonic())))
        while time.monotonic() < deadline:
            try:
                log_xml = _status_bytes(session.ip, "/status/jffs/upgrade.log")
                log_text = _log_text(log_xml)
                if (sha256(log_xml).hexdigest() != pre_log_sha and expected in log_text and
                    "Upgrade file is good" in log_text and
                    "Writing kernel to partition" in log_text and
                    "Writing / to partition" in log_text and
                    "Doing post-install steps" in log_text):
                    uptime = _status_bytes(session.ip, "/status/uptime")
                    post_minutes = _uptime_minutes(uptime)
                    if not went_offline and not (pre_minutes is not None and
                                                 post_minutes is not None and
                                                 post_minutes < pre_minutes):
                        seen.add("awaiting-reboot-evidence")
                    else:
                        identity = _status_bytes(session.ip, "/status/hh")
                        root = ET.fromstring(identity)
                        mac = normalize_mac(root.findtext(".//MACAddress") or "")
                        if mac != session.mac:
                            raise BootstrapError("Controller identity changed after reboot.", 409)
                        for name, data in (("upgrade-log.xml", log_xml),
                                           ("uptime.xml", uptime), ("hh.xml", identity)):
                            _private_bytes(session.run_dir / name, data)
                        return {"destination": destination, "generation": generation,
                                "upgrade_log_sha256": sha256(log_xml).hexdigest(),
                                "status_went_offline": went_offline}
                else:
                    seen.add("old-or-incomplete-log")
            except (OSError, TimeoutError):
                seen.add("status-unreachable")
                went_offline = True
            except (ET.ParseError, FlashError):
                seen.add("status-invalid")
            time.sleep(min(POSTBOOT_QUIET_SECONDS, max(0, deadline - time.monotonic())))
        raise BootstrapError("Postboot confirmation timed out; observed %s." %
                             ", ".join(sorted(seen)), 408)
