"""Interactive Pi workflow for one CR100 model-2 4.3.2 UI-only flash.

`check` is entirely offline. `flash` verifies target identity, obtains a
temporary RAM-only root shell through the bounded artwork fixture, makes fresh
software backups, verifies the package with the stock updater's no-write mode,
and requires a typed confirmation before launching one install. It never
retries a launch whose result is uncertain.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from .access import AccessError, AccessManager, ShellTransportError
from .backup import BackupError, capture_backup, _live_state
from .launcher import build_launcher
from .package import validate_candidate, validate_combined_release
from .server import PayloadServer


TARGET_IP = "192.168.69.52"
TARGET_MAC = "00:0e:58:02:6f:a9"
RELEASE = "25.2-50130"
PI_IP = "192.168.69.1"
FIXTURE_IP = "192.168.69.100"
INTERFACE = "sonos0"
PORT = 18091
LAB_KEY_SHA256 = "aef526dba14610f92557e85405de59cd6afdbc2e6e09bb330d147eae61c8c063"
COMBINED_KEY_SHA256 = "de42331e3e9a22f57402c4d04114854b78fd2b2ec9a060f41028613cf8e8bef6"
PROFILE_KEY_SHA256 = {"ui-only": LAB_KEY_SHA256, "combined-release": COMBINED_KEY_SHA256}
STATIC_HASHES = {
    "assets/stock.upd": "34cf093de96313ea6b7ae8f786f4800fc6d8daa84c4a4ebe00f88f4ad68b9380",
    "assets/mcu_version": "9cc2d4cc1320a0bb8bd236deca34d49565aa85433c2ddbfc7283ec3a129dbfda",
    "assets/mtd_stream": "8336b330c3e16a78ec78053b2a9788fbff9d3d6825d8d03bf483cfa58fefe332",
    "assets/file_stream": "3f2218bf6146960f1703469467c1443b0e1bcd0183a0375a5b1e11345f5a4856",
    "assets/rop/fixed-rop-system-root-shell.png": "5173a58755f4f092e3c34addcb9c2b6d9b4b746e209802511e3469177583c053",
}


class FlashError(RuntimeError):
    """A required preflight, transfer, or postboot check did not pass."""


class Journal:
    """Append-only private record, fsynced before the irreversible launch."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "events.jsonl"
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def write(self, event: str, *, durable: bool = False, **fields):
        row = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        os.write(self.fd, (json.dumps(row, sort_keys=True) + "\n").encode())
        if durable:
            os.fsync(self.fd)

    def close(self):
        os.close(self.fd)


def verify_bundle(bundle: Path) -> dict:
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise FlashError("unsupported bundle manifest")
    profile = manifest.get("profile", "ui-only")
    if profile not in PROFILE_KEY_SHA256:
        raise FlashError("unsupported bundle profile")
    for relative, expected in manifest["files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise FlashError("bundle manifest contains an unsafe path")
        candidate = bundle / path
        if not candidate.is_file() or candidate.is_symlink():
            raise FlashError(f"bundle file is missing or linked: {relative}")
        data = candidate.read_bytes()
        digest = sha256(data).hexdigest()
        if len(data) != expected.get("bytes") or digest != expected.get("sha256"):
            raise FlashError(f"bundle file changed: {relative}")
    for relative, digest in STATIC_HASHES.items():
        if manifest["files"].get(relative, {}).get("sha256") != digest:
            raise FlashError(f"critical asset is not the audited copy: {relative}")
    if manifest["files"].get("assets/lab.pub", {}).get("sha256") != PROFILE_KEY_SHA256[profile]:
        raise FlashError("public key is not the reviewed one for this profile")
    return manifest


def package_check(bundle: Path) -> dict:
    manifest = verify_bundle(bundle)
    profile = manifest.get("profile", "ui-only")
    validator = validate_candidate if profile == "ui-only" else validate_combined_release
    report = validator(bundle / "assets/candidate.upd", bundle / "assets/stock.upd",
                       bundle / "assets/lab.pub")
    if report["candidate_sha256"] != manifest.get("candidate_sha256"):
        raise FlashError("candidate hash differs from bundle manifest")
    if report["lab_public_key_sha256"] != PROFILE_KEY_SHA256[profile]:
        raise FlashError("candidate uses an unexpected public key")
    expected_changes = (["/opt/swf/MainUI.swf"] if profile == "ui-only" else
                        ["/bin/upgrade", "/opt/swf/MainUI.swf"])
    if report.get("changed_paths") != expected_changes:
        raise FlashError("candidate changes differ from the reviewed profile")
    report["profile"] = profile
    return report


def require_pi_link():
    if platform.system() != "Linux":
        raise FlashError("physical flash command must run on the Linux Pi")
    result = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", INTERFACE],
                            capture_output=True, text=True, check=True)
    if not re.search(rf"\binet {re.escape(PI_IP)}/24\b", result.stdout):
        raise FlashError(f"{INTERFACE} lacks {PI_IP}/24")
    subprocess.run(["sudo", "-n", "true"], capture_output=True, check=True)


def create_run_dir(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.urandom(3).hex()}"
    path = root / name
    path.mkdir(mode=0o700)
    return path


def prompt_exact(message: str, expected: str):
    answer = input(f"{message}\nType {expected} to continue: ").strip()
    if answer != expected:
        raise FlashError("confirmation was not entered")


def _require_shell(shell, command: str, action: str, *, timeout: float = 35) -> str:
    result = shell.run(command, timeout=timeout)
    if result.returncode:
        raise FlashError(f"{action} failed with RC {result.returncode}; see private run logs")
    return result.output


def stage_file(shell, name: str, size: int):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unsafe staging filename")
    url = f"http://{PI_IP}:{PORT}/{name}"
    mode = "600" if name == "lab.pub" else "700"
    command = (f"wget -O /tmp/{name} {url} && "
               f'test "$(wc -c </tmp/{name})" -eq {size} && chmod {mode} /tmp/{name}')
    _require_shell(shell, command, f"staging {name}", timeout=60)


def _read_status(path: str, *, timeout: float = 3) -> bytes:
    with urlopen(f"http://{TARGET_IP}:3400{path}", timeout=timeout) as response:
        return response.read(2 << 20)


def _log_text(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise FlashError("updater status is invalid XML") from error
    return root.findtext(".//File") or ""


def _uptime_minutes(xml_bytes: bytes) -> int | None:
    try:
        command = ET.fromstring(xml_bytes).findtext(".//Command") or ""
    except ET.ParseError:
        return None
    match = re.search(r"\bup\s+(?:(\d+)\s+days?,\s*)?(?:(\d+):(\d+)|(\d+)\s+min)",
                      command)
    if match is None:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or match.group(4) or 0)
    return days * 1440 + hours * 60 + minutes


def wait_postboot(run_dir: Path, *, destination: int, generation: int,
                  previous_log_sha256: str, preflash_uptime_minutes: int | None,
                  timeout: float = 900) -> dict:
    """Read status until the controller's new updater log confirms this run."""
    deadline = time.monotonic() + timeout
    expected = f"Destination section {destination} generation {generation}"
    seen = set()
    status_went_offline = False
    while time.monotonic() < deadline:
        try:
            log_xml = _read_status("/status/jffs/upgrade.log")
            log_text = _log_text(log_xml)
            if (sha256(log_xml).hexdigest() != previous_log_sha256 and
                    expected in log_text and "Upgrade file is good" in log_text and
                    "Writing kernel to partition" in log_text and
                    "Writing / to partition" in log_text and
                    "Doing post-install steps" in log_text):
                uptime = _read_status("/status/uptime")
                post_uptime = _uptime_minutes(uptime)
                uptime_reset = (preflash_uptime_minutes is not None and
                                post_uptime is not None and
                                post_uptime < preflash_uptime_minutes)
                if not (status_went_offline or uptime_reset):
                    seen.add("awaiting-reboot-evidence")
                    time.sleep(2)
                    continue
                identity = _read_status("/status/hh")
                for name, data in (("upgrade-log.xml", log_xml),
                                   ("uptime.xml", uptime), ("hh.xml", identity)):
                    path = run_dir / name
                    with path.open("xb") as output:
                        output.write(data)
                    path.chmod(0o600)
                root = ET.fromstring(identity)
                mac = root.findtext(".//MACAddress") or ""
                version = root.findtext(".//SoftwareVersion") or ""
                if mac.lower() != TARGET_MAC or version != RELEASE:
                    raise FlashError("postboot controller identity changed")
                return {"destination": destination, "generation": generation,
                        "upgrade_log_sha256": sha256(log_xml).hexdigest(),
                        "uptime_sha256": sha256(uptime).hexdigest(),
                        "status_went_offline": status_went_offline,
                        "uptime_reset": uptime_reset}
            seen.add("old-or-incomplete-log")
        except (OSError, TimeoutError, ET.ParseError):
            seen.add("status-unreachable")
            status_went_offline = True
        time.sleep(4)
    raise FlashError(f"postboot confirmation timed out; observed {sorted(seen)}")


def preflight_and_flash(bundle: Path, report: dict, run_root: Path,
                        *, preflight_only: bool = False) -> Path:
    require_pi_link()
    run_dir = create_run_dir(run_root)
    journal = Journal(run_dir)
    assets = bundle / "assets"
    access = AccessManager(
        target_ip=TARGET_IP, expected_mac=TARGET_MAC, expected_version=RELEASE,
        pi_bind_ip=PI_IP, fixture_ip=FIXTURE_IP, interface=INTERFACE,
        asset_rop_dir=assets / "rop", peer_dir=assets / "zp80", run_dir=run_dir,
    )
    launch_sent = False
    try:
        journal.write("run-start", candidate_sha256=report["candidate_sha256"],
                      profile=report["profile"], preflight_only=preflight_only)
        identity = access.verify_identity()
        journal.write("identity-verified", mac=identity.mac, release=identity.version)
        print(f"Controller: {identity.mac}, release {identity.version} at {TARGET_IP}")
        prompt_exact("Keep this controller awake and on stable cradle power. Confirm the other 4.3 unit is off.",
                     "READY")
        if access.probe_existing_clean_shell():
            prompt_exact("An existing temporary root listener is open on this controller.", "REUSE")
        else:
            access.acquire_root(lambda message: input(message + " "))
            access.establish_clean_listener()
        if access.shell is None:
            raise FlashError("clean root shell is unavailable")
        journal.write("root-ready")

        payloads = {
            "/firmware.upd": (assets / "candidate.upd").read_bytes(),
            "/lab.pub": (assets / "lab.pub").read_bytes(),
            "/mcu_version": (assets / "mcu_version").read_bytes(),
            "/mtd_stream": (assets / "mtd_stream").read_bytes(),
            "/file_stream": (assets / "file_stream").read_bytes(),
        }
        expected_payload_hashes = {
            "/firmware.upd": report["candidate_sha256"],
            "/lab.pub": report["lab_public_key_sha256"],
            "/mcu_version": STATIC_HASHES["assets/mcu_version"],
            "/mtd_stream": STATIC_HASHES["assets/mtd_stream"],
            "/file_stream": STATIC_HASHES["assets/file_stream"],
        }
        for path, content in payloads.items():
            if sha256(content).hexdigest() != expected_payload_hashes[path]:
                raise FlashError(f"staged bytes changed since package validation: {path}")
        with PayloadServer(bind_ip=PI_IP, allow_ip=TARGET_IP, port=PORT,
                           log_path=run_dir / "http.jsonl", payloads=payloads) as server:
            for name in ("mcu_version", "mtd_stream", "file_stream"):
                stage_file(access.shell, name, len(payloads["/" + name]))
            mcu = _require_shell(access.shell, "/tmp/mcu_version", "MCU version check")
            if "/dev/hhkbd MCU version: 1.0-29305" not in mcu:
                raise FlashError("M16 version is not the audited 1.0-29305")
            journal.write("mcu-verified", version="1.0-29305")

            backup = capture_backup(
                run_dir, access.shell.run, controller_ip=TARGET_IP, bind_ip=PI_IP,
                receiver_script=assets / "receive_dump.py",
                file_receiver_script=assets / "receive_file.py",
                selector_path=assets / "select_nand_boot.py", include_files=True,
            )
            selection = backup["selection"]
            if selection["partition_table"]["sha256"] != report["partition_table_sha256"]:
                raise FlashError("live NAND partition table differs from package")
            section, generation = selection["selected_bank"], selection["selected_generation"]
            active_roots = [entry.get("payload", {}).get("sha256")
                            for entry in selection["partitions"]
                            if entry.get("type") == 17 and entry.get("bank") == section]
            if len(active_roots) != 1 or not re.fullmatch(r"[0-9a-f]{64}", active_roots[0] or ""):
                raise FlashError("could not identify the active root filesystem")
            if active_roots[0] == report["rootfs_sha256"]:
                raise FlashError("candidate root filesystem is already active")
            journal.write("backup-verified", durable=True, selected_bank=section,
                          selected_generation=generation, nand_sha256=backup["nand_sha256"],
                          backup_report=backup["report_path"],
                          summary=backup["summary"])
            print(f"Fresh software backup saved in {run_dir / 'backup'}")
            if backup["summary"]["nand_differing_pages"]:
                print(f"NAND passes differ on {backup['summary']['nand_differing_pages']} JFFS pages; "
                      "boot and system pages match.")
            print("The NAND capture includes read-error placeholders; it is diagnostic, not a raw recovery image.")

            stage_file(access.shell, "lab.pub", len(payloads["/lab.pub"]))
            verify = _require_shell(
                access.shell,
                f"/bin/upgrade -t -f -K /tmp/lab.pub http://{PI_IP}:{PORT}/firmware.upd",
                "stock verify-only updater", timeout=180,
            )
            (run_dir / "verify-only.txt").write_text(verify)
            (run_dir / "verify-only.txt").chmod(0o600)
            if "Upgrade file is good" not in verify:
                raise FlashError("stock updater did not accept candidate in verify-only mode")
            journal.write("stock-verify-passed", durable=True)

            preflash_log = _read_status("/status/jffs/upgrade.log")
            preflash_uptime = _read_status("/status/uptime")
            preflash_log_sha = sha256(preflash_log).hexdigest()
            preflash_uptime_value = _uptime_minutes(preflash_uptime)
            for name, content in (("preflash-upgrade-log.xml", preflash_log),
                                  ("preflash-uptime.xml", preflash_uptime)):
                path = run_dir / name
                with path.open("xb") as output:
                    output.write(content)
                path.chmod(0o600)
            journal.write("preflash-status-saved", upgrade_log_sha256=preflash_log_sha,
                          uptime_minutes=preflash_uptime_value)

            # Use the same single-command boot-state read that passed before
            # and after the NAND capture. Separate /proc reads can lose an
            # output boundary on this controller's old interactive shell.
            try:
                live = _live_state(access.shell.run)
            except BackupError:
                time.sleep(0.2)
                live = _live_state(access.shell.run)
            if (live["bank"], live["generation"]) != (section, generation):
                raise FlashError("boot section or generation changed after backup")
            _require_shell(
                access.shell,
                "test -c /dev/mtd/0 && grep ' /jffs ' /proc/mounts >/dev/null 2>&1 && "
                "test ! -e /var/run/upgradeflag && test ! -e /var/run/stopanacapa && "
                "test ! -e /tmp/lab-upgrade.phase && ! pidof upgrade >/dev/null 2>&1 && "
                "pidof hh.anacapad >/dev/null 2>&1",
                "final controller state guard",
            )
            launcher = build_launcher(section, generation, PI_IP, PORT)
            server.add("/run.sh", launcher)
            stage_file(access.shell, "run.sh", len(launcher))
            _require_shell(access.shell, "sh -n /tmp/run.sh", "launcher syntax check")

            destination, next_generation = 1 - section, generation + 1
            print(f"Candidate SHA-256: {report['candidate_sha256']}")
            print(f"Current bank/generation: {section}/{generation}")
            print(f"Expected destination: {destination}/{next_generation}")
            print(f"Run receipt: {run_dir}")
            if preflight_only:
                journal.write("preflight-complete", durable=True)
                print("Preflight completed; no flash launched.")
                return run_dir

            token = "FLASH " + report["candidate_sha256"][-8:]
            prompt_exact("The controller is ready for one flash. Keep cradle and Pi powered until it restarts.", token)
            if server.remaining_seconds() < 1200:
                raise FlashError("package server has less than 20 minutes remaining; preflight must be restarted")
            # Persist this intent *before* sending the command. A lost shell
            # connection cannot distinguish a failed send from an active write.
            journal.write("flash-launch-sent", durable=True, expected_destination=destination,
                          expected_generation=next_generation)
            launch_sent = True
            try:
                launch_output = _require_shell(
                    access.shell,
                    "(/bin/sh /tmp/run.sh </dev/null >/tmp/lab-upgrade-launch.log 2>&1) & "
                    "echo LAUNCH_PID:$!",
                    "single flash launch", timeout=15,
                )
                (run_dir / "launch.txt").write_text(launch_output)
                (run_dir / "launch.txt").chmod(0o600)
                if not re.search(r"(?m)^LAUNCH_PID:[0-9]+\r?$", launch_output):
                    raise FlashError("launch PID was not observed")
                journal.write("launch-command-returned")
            except (AccessError, FlashError, ShellTransportError) as error:
                journal.write("launch-result-uncertain", detail=str(error), durable=True)
                print("Launch result is uncertain. The script will only observe; it will not retry.")

            print("Waiting for the controller to reboot and publish its upgrade log...")
            result = wait_postboot(run_dir, destination=destination,
                                   generation=next_generation,
                                   previous_log_sha256=preflash_log_sha,
                                   preflash_uptime_minutes=preflash_uptime_value)
            journal.write("postboot-verified", durable=True, **result)
            server.stop()
            print("Upgrade log confirms the expected destination bank and completed install.")
            menu = ("Music > System Settings > Room Settings and Bridge Settings"
                    if report["profile"] == "combined-release" else
                    "Music > System Settings > Speaker Settings")
            prompt_exact(f"On the CR100, open {menu} and verify the menu responds.", "UI OK")
            journal.write("user-ui-confirmed", durable=True)
            return run_dir
    except BaseException as error:
        journal.write("run-stopped", durable=True, error=str(error),
                      flash_launch_sent=launch_sent)
        raise
    finally:
        try:
            access.cleanup_fixture()
        except Exception as error:
            journal.write("fixture-cleanup-warning", error=str(error))
        if not launch_sent and access.shell is not None:
            try:
                access.close_clean_listener()
            except Exception as error:
                journal.write("root-listener-cleanup-warning", error=str(error))
        journal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "flash", "preflight"))
    parser.add_argument("--bundle", type=Path, default=Path.cwd())
    parser.add_argument("--runs", type=Path, default=Path.home() / "cr100-flashes")
    args = parser.parse_args(argv)
    bundle = args.bundle.expanduser().resolve()
    try:
        report = package_check(bundle)
        print(json.dumps({key: report[key] for key in (
            "candidate_sha256", "release", "changed_paths", "rootfs_bytes",
            "partition_table_sha256", "signature_valid")}, indent=2))
        if args.command == "check":
            return 0
        run_dir = preflight_and_flash(bundle, report, args.runs,
                                      preflight_only=args.command == "preflight")
        print(f"Private run and backup directory: {run_dir}")
        return 0
    except (FlashError, AccessError, BackupError, OSError, ValueError, subprocess.CalledProcessError,
            KeyboardInterrupt) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
