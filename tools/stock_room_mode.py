"""Temporarily show a stock controller only the bootstrap artwork room.

The ordinary bridge rooms remain configured and continue serving other
controllers. A private recovery record preserves this controller's previous
selection across a web UI crash. The stock controller's own cached topology is
the convergence check; the operator must also check its physical Rooms screen.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import time
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from bridge_config import update_config_file
from controller_visibility import configured_components


MAX_TOPOLOGY_BYTES = 64 * 1024
POLL_INTERVAL = 0.35
RELOAD_RETRY_SECONDS = 5.0
UDN = re.compile(r"RINCON_[0-9A-F]{12}01400\Z")
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")


class RoomModeError(RuntimeError):
    """The temporary controller view could not be applied or restored."""


def _fsync_directory(path: Path) -> None:
    """Make the recovery record's creation/removal durable when supported."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _record_path(config_path: Path) -> Path:
    return config_path.with_name("stock-room-mode.json")


def _read_record(path: Path) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not path.is_file() or path.is_symlink() or info.st_mode & 0o077
            or info.st_uid != os.geteuid()):
        raise RoomModeError("unsafe stock room recovery record")
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RoomModeError("stock room recovery record is unreadable") from error
    if not isinstance(record, dict) or record.get("format") != 1:
        raise RoomModeError("unsupported stock room recovery record")
    if not isinstance(record.get("mac"), str) or not MAC.fullmatch(record["mac"]):
        raise RoomModeError("stock room recovery record has an invalid controller")
    if not isinstance(record.get("selection_present"), bool):
        raise RoomModeError("stock room recovery record has no prior selection")
    if not isinstance(record.get("session"), str) or not re.fullmatch(r"[0-9a-f]{24}", record["session"]):
        raise RoomModeError("stock room recovery record has no session")
    try:
        ipaddress.IPv4Address(record["ip"])
    except (KeyError, TypeError, ValueError) as error:
        raise RoomModeError("stock room recovery record has an invalid address") from error
    if not isinstance(record.get("exploit_udn"), str) or not UDN.fullmatch(record["exploit_udn"]):
        raise RoomModeError("stock room recovery record has an invalid room")
    prior = record.get("prior_rooms")
    if not isinstance(prior, list) or any(not isinstance(value, str) or not UDN.fullmatch(value)
                                          for value in prior):
        raise RoomModeError("stock room recovery record has invalid prior rooms")
    return record


def _write_record(path: Path, record: dict) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RoomModeError("another stock room session needs recovery") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(record, destination, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _topology_members(payload: bytes) -> set[str]:
    if len(payload) > MAX_TOPOLOGY_BYTES:
        raise RoomModeError("controller topology response is too large")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise RoomModeError("controller topology response is not XML") from error
    players = root.find(".//ZonePlayers")
    if players is None:
        raise RoomModeError("controller topology response has no ZonePlayers")
    result = set()
    for player in players.findall("ZonePlayer"):
        uuid = player.get("uuid", "")
        if not UDN.fullmatch(uuid):
            raise RoomModeError("controller topology contains an invalid room UUID")
        result.add(uuid)
    return result


def _expected_room_udns(config: dict, selection) -> set[str]:
    visible = None if selection is None else set(selection)
    result = set()
    for index, (room, component) in enumerate(zip(config.get("rooms", []), configured_components(config))):
        if visible is not None and component["key"] not in visible:
            continue
        slot = int(room.get("slot", index))
        if not 0 <= slot < 38:
            raise RoomModeError("configured room has an invalid player slot")
        result.add("RINCON_0269000000%02X01400" % (slot + 1))
    return result


class RoomIsolation:
    """Apply and verify one controller's temporary empty bridge selection.

    ``enter`` is called after a separate artwork peer is advertising. It
    returns only when the stock controller reports exactly that peer. The
    caller then asks the operator to confirm the physical Rooms screen.
    ``restore`` is called after the peer has sent byebye and stopped, even if
    ``enter`` fails. Until then, the recovery record and temporary selection
    remain in place so restoration can verify the normal rooms return.
    """

    def __init__(self, config_path, players_pid_path, target_mac, target_ip,
                 exploit_udn, log=None, *, recovery_path=None,
                 fetch_topology=None, reload_players=None):
        self.config_path = Path(config_path)
        self.players_pid_path = Path(players_pid_path)
        self.target_mac = target_mac.lower()
        self.target_ip = str(ipaddress.IPv4Address(target_ip))
        self.exploit_udn = exploit_udn.upper()
        if not MAC.fullmatch(self.target_mac) or not UDN.fullmatch(self.exploit_udn):
            raise ValueError("invalid controller or artwork room identity")
        self.recovery_path = Path(recovery_path) if recovery_path else _record_path(self.config_path)
        self.log = log or (lambda _record: None)
        self.fetch_topology = fetch_topology or self._fetch_topology
        self.reload_players = reload_players or self._reload_players
        self.session = None

    def _fetch_topology(self) -> set[str]:
        with urlopen("http://%s:3400/status/topology" % self.target_ip, timeout=2.5) as response:
            payload = response.read(MAX_TOPOLOGY_BYTES + 1)
        return _topology_members(payload)

    def _reload_players(self) -> None:
        try:
            pid = int(self.players_pid_path.read_text().strip())
            if pid <= 1:
                raise ValueError("invalid player PID")
            os.kill(pid, signal.SIGHUP)
        except (OSError, ValueError) as error:
            raise RoomModeError("could not reload bridge rooms") from error

    def _wait(self, predicate, *, timeout: float, action: str) -> set[str]:
        deadline = time.monotonic() + timeout
        retry_at = time.monotonic() + RELOAD_RETRY_SECONDS
        retried = False
        last = "no response"
        while True:
            try:
                members = self.fetch_topology()
                last = ", ".join(sorted(members)) or "no rooms"
                if predicate(members):
                    return members
            except (OSError, TimeoutError, RoomModeError, ET.ParseError) as error:
                last = str(error) or type(error).__name__
            now = time.monotonic()
            if now >= deadline:
                raise RoomModeError("controller topology did not %s; last observed %s" % (action, last))
            if not retried and now >= retry_at:
                self.reload_players()  # SIGHUP can be ignored during an exec boundary.
                retried = True
            time.sleep(min(POLL_INTERVAL, max(0.0, deadline - now)))

    def enter(self, timeout: float = 45) -> set[str]:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.session is not None:
            raise RoomModeError("stock room session is already active")
        try:
            baseline = self.fetch_topology()
        except (OSError, TimeoutError, RoomModeError) as error:
            raise RoomModeError("could not read the controller's rooms before isolation") from error
        session = os.urandom(12).hex()
        record = {"format": 1, "session": session, "mac": self.target_mac,
                  "ip": self.target_ip, "exploit_udn": self.exploit_udn}

        def change(config):
            profiles = config.get("controllers")
            if profiles is None:
                profiles = config["controllers"] = {}
            if not isinstance(profiles, dict):
                raise RoomModeError("controller settings are invalid")
            profile = profiles.setdefault(self.target_mac, {})
            if not isinstance(profile, dict):
                raise RoomModeError("controller profile is invalid")
            record["selection_present"] = "visible_components" in profile
            record["selection"] = copy.deepcopy(profile.get("visible_components"))
            record["prior_rooms"] = sorted(baseline & _expected_room_udns(config, record["selection"]))
            _write_record(self.recovery_path, record)
            profile["visible_components"] = []

        update_config_file(self.config_path, change)
        self.session = session
        self.reload_players()
        members = self._wait(lambda values: values == {self.exploit_udn},
                             timeout=timeout, action="show only the artwork room")
        self.log({"event": "stock-room-isolated", "mac": self.target_mac})
        return members

    def restore(self, timeout: float = 45) -> set[str] | None:
        """Restore the prior selection, preserving other configuration edits.

        ``timeout=0`` restores the local selection without checking the
        controller. Startup recovery uses it after withdrawing the peer when
        the controller may be offline.
        """
        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        record = _read_record(self.recovery_path)
        if record is None:
            return None
        if record["mac"] != self.target_mac or record["ip"] != self.target_ip or record["exploit_udn"] != self.exploit_udn:
            raise RoomModeError("stock room recovery record belongs to another controller")
        if self.session is not None and record["session"] != self.session:
            raise RoomModeError("stock room recovery record belongs to another session")
        conflict = False

        def change(config):
            nonlocal conflict
            profiles = config.get("controllers") or {}
            profile = profiles.get(self.target_mac)
            if not isinstance(profile, dict):
                conflict = True
                return
            current_present = "visible_components" in profile
            current = profile.get("visible_components")
            original_present = record["selection_present"]
            original = record.get("selection")
            if current_present and current == []:
                if original_present:
                    profile["visible_components"] = copy.deepcopy(original)
                else:
                    profile.pop("visible_components", None)
            elif current_present != original_present or current != original:
                conflict = True  # A user changed this controller during the session.

        config = update_config_file(self.config_path, change)
        if conflict:
            self.recovery_path.unlink()
            _fsync_directory(self.recovery_path.parent)
            self.session = None
            raise RoomModeError("controller room selection changed during bootstrap; preserved the new selection")
        # Keep the recovery record until the reload and requested convergence
        # succeed. If either fails, the next service start can try again.
        self.reload_players()
        members = None
        if timeout:
            profile = ((config.get("controllers") or {}).get(self.target_mac) or {})
            selection = profile.get("visible_components")
            expected = set(record["prior_rooms"]) & _expected_room_udns(config, selection)
            members = self._wait(lambda values: self.exploit_udn not in values and expected <= values,
                                 timeout=timeout, action="restore the normal rooms")
        self.recovery_path.unlink()
        _fsync_directory(self.recovery_path.parent)
        self.session = None
        self.log({"event": "stock-room-restored", "mac": self.target_mac})
        return members


def recover_pending(config_path, players_pid_path, log=None, *, recovery_path=None,
                    reload_players=None) -> bool:
    """Restore a saved controller view after an interrupted web UI session."""
    config_path = Path(config_path)
    path = Path(recovery_path) if recovery_path else _record_path(config_path)
    record = _read_record(path)
    if record is None:
        return False
    mode = RoomIsolation(config_path, players_pid_path, record["mac"], record["ip"],
                         record["exploit_udn"], log, recovery_path=path,
                         reload_players=reload_players)
    mode.restore(timeout=0)
    return True
