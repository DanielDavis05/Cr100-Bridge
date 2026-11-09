"""Fresh, read-only CR100 backups before a firmware update.

The controller-side helpers are staged by the caller. This module starts the
existing Pi receivers, asks the root shell to stream data, then verifies the
receipts and compares two independent NAND reads. It never writes controller
flash or boot-state registers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid
from typing import Callable


NAND_PORT = 23177
FILE_PORT = 23179
NAND_TIMEOUT = 180
FILE_TIMEOUT = 25
FILE_MAX_BYTES = 4 << 20

FILE_SOURCES = (
    "/jffs/lockup.dmesg", "/jffs/hosts", "/jffs/upgrade.log",
    "/jffs/lockup.anacapa.trace", "/jffs/upgrade_prev.log",
    "/jffs/random-seed", "/jffs/localsettings.txt", "/jffs/wifi.txt",
    "/ramdisk/optlog/hh.anacapa.trace", "/ramdisk/optlog/hh.anacapa.pid",
    "/ramdisk/tmp/udhcpc.pid", "/ramdisk/tmp/wifi_card_mac_addr",
    "/ramdisk/var/run/inetd.pid", "/ramdisk/var/run/resolv.conf",
    "/ramdisk/var/run/normalIP", "/etc/upgradekey.pub",
    "/etc/upgrade/upgrade_conf.txt", "/proc/cmdline", "/proc/mtd",
    "/proc/mounts", "/proc/sys/kernel/bootsection",
    "/proc/sys/kernel/bootgeneration",
)

# These are needed to reconstruct local settings and identify the exact
# firmware/key/partition environment if a later update fails.
CRITICAL_FILES = frozenset((
    "/jffs/localsettings.txt", "/jffs/wifi.txt", "/etc/upgradekey.pub",
    "/etc/upgrade/upgrade_conf.txt", "/proc/cmdline", "/proc/mtd",
    "/proc/sys/kernel/bootsection", "/proc/sys/kernel/bootgeneration",
))


class BackupError(RuntimeError):
    """A fresh backup could not be validated for this controller."""


def _private_json(path: Path, payload: dict) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _ip(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as error:
        raise BackupError(f"Expected an IPv4 address: {value!r}") from error


def _shell_output(shell: Callable, command: str, *, timeout: int) -> str:
    result = shell(command, timeout=timeout)
    if result.returncode != 0:
        raise BackupError(f"Controller command failed (exit {result.returncode}): {command}")
    return result.output


def _live_state(shell: Callable) -> dict:
    marker = "CR100_STATE_" + uuid.uuid4().hex
    command = (
        f"echo {marker}_BEGIN; "
        "cat /proc/sys/kernel/bootsection; "
        "cat /proc/sys/kernel/bootgeneration; "
        f"echo {marker}_END"
    )
    output = _shell_output(shell, command, timeout=15)
    normalized = output.replace("\r", "")
    match = re.search(
        rf"(?m)^{marker}_BEGIN\n(?P<bank>[01])\n(?P<generation>[0-9]+)\n{marker}_END$",
        normalized,
    )
    if match is None:
        raise BackupError("Could not parse live boot section and generation")
    generation = int(match.group("generation"))
    if generation <= 0:
        raise BackupError("Live boot generation must be positive")
    return {"bank": int(match.group("bank")), "generation": generation}


def _mcu_version(shell: Callable, helper: str) -> dict:
    try:
        output = _shell_output(shell, shlex.quote(helper), timeout=20)
        version = re.search(r"MCU version:\s*([^\s]+)", output, re.IGNORECASE)
        if not version:
            raise BackupError("MCU helper did not report a version")
        return {"status": "captured", "version": version.group(1), "output": output.strip()[:500]}
    except (BackupError, OSError, TimeoutError) as error:
        return {"status": "unavailable", "reason": str(error)[:500]}


def _receive(
    receiver_script: Path,
    destination: Path,
    *,
    bind_ip: str,
    controller_ip: str,
    port: int,
    timeout: int,
    max_bytes: int | None,
    shell: Callable,
    sender_command: str,
) -> None:
    args = [
        sys.executable, str(receiver_script), "--bind", bind_ip,
        "--port", str(port), "--allow", controller_ip,
        "--timeout", str(timeout), "--output", str(destination),
    ]
    if max_bytes is not None:
        args += ["--max-bytes", str(max_bytes)]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sender_error: Exception | None = None
    receiver_output = receiver_error = ""
    try:
        time.sleep(0.3)
        if process.poll() is not None:
            raise BackupError(f"Receiver exited before the stream began: {receiver_script.name}")
        try:
            _shell_output(shell, sender_command, timeout=timeout + 10)
        except Exception as error:
            sender_error = error
            process.terminate()
        try:
            receiver_output, receiver_error = process.communicate(timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            process.kill()
            receiver_output, receiver_error = process.communicate()
            raise BackupError(f"Receiver timed out: {receiver_script.name}")
        if sender_error is not None:
            raise BackupError(f"Sender failed: {sender_error}") from sender_error
        if process.returncode != 0:
            raise BackupError(
                f"Receiver failed (exit {process.returncode}): {receiver_error.strip()[-500:]}"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        log = destination.with_suffix(destination.suffix + ".transfer.json")
        _private_json(log, {
            "receiver_exit": process.returncode,
            "receiver_stdout_tail": receiver_output[-1000:],
            "receiver_stderr_tail": receiver_error[-1000:],
            "sender_error": str(sender_error)[:500] if sender_error else None,
        })


def _receipt(destination: Path) -> dict:
    receipt_path = destination.with_suffix(destination.suffix + ".json")
    if not destination.is_file() or not receipt_path.is_file():
        raise BackupError(f"Capture or receipt missing: {destination}")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (ValueError, OSError) as error:
        raise BackupError(f"Could not read capture receipt: {receipt_path}") from error
    if receipt.get("path") != str(destination.resolve()):
        raise BackupError(f"Receipt points to another file: {receipt_path}")
    if receipt.get("bytes") != destination.stat().st_size or receipt.get("sha256") != _sha256(destination):
        raise BackupError(f"Capture hash or length differs from receipt: {destination}")
    return receipt


def _nand_receipt(destination: Path, *, controller_ip: str) -> dict:
    receipt = _receipt(destination)
    size = receipt.get("mtd_data_bytes")
    pages = receipt.get("pages")
    if (
        receipt.get("source") != f"tcp:{controller_ip}"
        or size not in (16 << 20, 32 << 20)
        or pages != size // 512
        or receipt.get("page_data_bytes") != 512
        or receipt.get("page_oob_bytes") != 16
        or receipt.get("erase_bytes") != 16384
        or receipt.get("bytes") != pages * 528
        or receipt.get("stream_version") != 2
        or receipt.get("host_received_complete_frame") is not True
    ):
        raise BackupError(f"NAND stream geometry or completion is invalid: {destination}")
    for kind in ("main", "oob"):
        sidecar = destination.with_suffix(destination.suffix + f".{kind}-status.bin")
        if not sidecar.is_file() or sidecar.stat().st_size != pages:
            raise BackupError(f"Missing or truncated NAND {kind} status: {sidecar}")
        if receipt.get(f"{kind}_status_sha256") != _sha256(sidecar):
            raise BackupError(f"NAND {kind} status hash mismatch: {sidecar}")
        count = sum(byte != 0 for byte in sidecar.read_bytes())
        if receipt.get(f"{kind}_error_pages") != count:
            raise BackupError(f"NAND {kind} error count mismatch: {sidecar}")
    return {
        "path": str(destination.resolve()), "sha256": receipt["sha256"],
        "bytes": receipt["bytes"], "mtd_data_bytes": size,
        "pages": pages, "main_error_pages": receipt["main_error_pages"],
        "oob_error_pages": receipt["oob_error_pages"],
        "main_status_sha256": receipt["main_status_sha256"],
        "oob_status_sha256": receipt["oob_status_sha256"],
        "receipt": str(destination.with_suffix(destination.suffix + ".json").resolve()),
    }


def _file_receipt(destination: Path, *, controller_ip: str) -> dict:
    receipt = _receipt(destination)
    if (
        receipt.get("source") != controller_ip
        or receipt.get("framed_complete") is not True
        or receipt.get("file_content_only") is not True
        or receipt["bytes"] > FILE_MAX_BYTES
    ):
        raise BackupError(f"File stream was incomplete or came from another host: {destination}")
    return {
        "path": str(destination.resolve()), "sha256": receipt["sha256"],
        "bytes": receipt["bytes"],
        "receipt": str(destination.with_suffix(destination.suffix + ".json").resolve()),
    }


def _analyze_nand(selector_path: Path, nand_path: Path) -> dict:
    spec = importlib.util.spec_from_file_location("cr100_backup_selector", selector_path)
    if spec is None or spec.loader is None:
        raise BackupError(f"Could not load NAND selector: {selector_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = nand_path.stat()
    report, _ = module.analyze(nand_path.read_bytes())
    after = nand_path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise BackupError("NAND capture changed while being analyzed")
    if report.get("error"):
        raise BackupError(f"NAND boot analysis failed: {report['error']}")
    return report


def _validate_selection(analysis: dict, nand: dict) -> tuple[int, int]:
    selected = analysis.get("selected_bank")
    generation = analysis.get("selected_generation")
    banks = analysis.get("banks", [])
    if (
        selected not in (0, 1)
        or not isinstance(generation, int)
        or generation <= 0
        or analysis.get("geometry", {}).get("data_bytes") != 16 << 20
        or nand["mtd_data_bytes"] != 16 << 20
        or len(banks) != 2
        or any(
            bank.get("bank") != index
            or bank.get("native_valid") is not True
            or bank.get("strict_integrity_valid") is not True
            for index, bank in enumerate(banks)
        )
        or not re.fullmatch(r"[0-9a-f]{64}", analysis.get("partition_table", {}).get("sha256", ""))
    ):
        raise BackupError("NAND geometry, boot banks, or partition table failed strict validation")
    return selected, generation


def _compare_two_passes(first: dict, second: dict, selection: dict, second_selection: dict) -> dict:
    """Allow changes only in the physical JFFS partition between live reads."""
    for field in ("bytes", "mtd_data_bytes", "pages"):
        if first[field] != second[field]:
            raise BackupError(f"Two NAND captures disagree on {field}")
    for field in ("geometry", "mdp", "partition_table", "partitions", "banks",
                  "selected_bank", "selected_generation"):
        if selection.get(field) != second_selection.get(field):
            raise BackupError(f"Two NAND analyses disagree on {field}")

    pages = first["pages"]
    blocks = pages // 32
    regions = ["unallocated"] * blocks
    jffs = [entry for entry in selection["partitions"] if entry["type"] == 64]
    if len(jffs) != 1 or jffs[0].get("name") != "jffs":
        raise BackupError("Expected exactly one JFFS partition in the NAND layout")
    for entry in selection["partitions"]:
        start, end = entry["physical_start_block"], entry["physical_end_block"]
        if not 0 <= start <= end < blocks:
            raise BackupError("NAND partition range exceeds device geometry")
        label = entry["name"]
        if entry.get("bank") is not None:
            label += f"-bank{entry['bank']}"
        for block in range(start, end + 1):
            if regions[block] != "unallocated":
                raise BackupError("NAND partitions overlap")
            regions[block] = label

    status = {}
    for name, capture in (("first", first), ("second", second)):
        image_path = Path(capture["path"])
        status[name] = (
            image_path.with_suffix(image_path.suffix + ".main-status.bin").read_bytes(),
            image_path.with_suffix(image_path.suffix + ".oob-status.bin").read_bytes(),
        )
    left_main, left_oob = status["first"]
    right_main, right_oob = status["second"]
    if any(len(item) != pages for item in (left_main, left_oob, right_main, right_oob)):
        raise BackupError("NAND page status sidecar length changed during comparison")

    result = {
        "first_sha256": first["sha256"],
        "second_sha256": second["sha256"],
        "differing_pages": 0,
        "raw_differing_pages": 0,
        "main_status_differing_pages": 0,
        "oob_status_differing_pages": 0,
        "non_jffs_differing_pages": 0,
        "regions": {},
        "first_differing_pages": [],
        "jffs_blocks": [jffs[0]["physical_start_block"], jffs[0]["physical_end_block"]],
    }
    with Path(first["path"]).open("rb") as left, Path(second["path"]).open("rb") as right:
        for page in range(pages):
            raw_diff = left.read(528) != right.read(528)
            main_diff = left_main[page] != right_main[page]
            oob_diff = left_oob[page] != right_oob[page]
            if not (raw_diff or main_diff or oob_diff):
                continue
            region = regions[page // 32]
            result["differing_pages"] += 1
            result["raw_differing_pages"] += int(raw_diff)
            result["main_status_differing_pages"] += int(main_diff)
            result["oob_status_differing_pages"] += int(oob_diff)
            result["regions"][region] = result["regions"].get(region, 0) + 1
            if region != "jffs":
                result["non_jffs_differing_pages"] += 1
            if len(result["first_differing_pages"]) < 32:
                result["first_differing_pages"].append({"physical_page": page, "region": region})
    result["two_pass_match"] = result["differing_pages"] == 0
    result["system_pages_match"] = result["non_jffs_differing_pages"] == 0
    return result


def capture_backup(
    run_dir: Path,
    shell: Callable,
    *,
    controller_ip: str,
    bind_ip: str,
    receiver_script: Path,
    file_receiver_script: Path,
    selector_path: Path,
    include_files: bool = True,
    mcu_helper: str = "/tmp/mcu_version",
) -> dict:
    """Capture and validate fresh evidence; raise :class:`BackupError` on a gate failure.

    ``shell`` must take ``(command, timeout=...)`` and return an object with
    ``returncode`` and ``output``. The two sender binaries are expected at
    ``/tmp/mtd_stream`` and ``/tmp/file_stream`` on the controller.
    """
    controller_ip, bind_ip = _ip(controller_ip), _ip(bind_ip)
    for script in (receiver_script, file_receiver_script, selector_path):
        if not Path(script).is_file():
            raise BackupError(f"Required backup tool missing: {script}")
    run_dir = Path(run_dir)
    backup_dir = run_dir / "backup"
    backup_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    report: dict = {
        "controller_ip": controller_ip,
        "bind_ip": bind_ip,
        "backup_dir": str(backup_dir.resolve()),
        "started_at_epoch": time.time(),
        "status": "in_progress",
        "recovery_grade_raw_clone": False,
        "nand": [],
        "files": [],
    }
    try:
        report["live_before"] = _live_state(shell)
        report["mcu"] = _mcu_version(shell, mcu_helper)
        for name in ("first", "second"):
            destination = backup_dir / f"{name}.nand"
            _receive(
                Path(receiver_script), destination, bind_ip=bind_ip,
                controller_ip=controller_ip, port=NAND_PORT,
                timeout=NAND_TIMEOUT, max_bytes=None, shell=shell,
                sender_command=f"/tmp/mtd_stream {bind_ip} {NAND_PORT}",
            )
            report["nand"].append(_nand_receipt(destination, controller_ip=controller_ip))
        first, second = report["nand"]
        report["nand_hashes"] = [first["sha256"], second["sha256"]]
        report["nand_sha256"] = first["sha256"]
        report["selection"] = _analyze_nand(Path(selector_path), Path(first["path"]))
        report["selection_second"] = _analyze_nand(Path(selector_path), Path(second["path"]))
        analysis = report["selection"]
        selected, generation = _validate_selection(analysis, first)
        _validate_selection(report["selection_second"], second)
        report["comparison"] = _compare_two_passes(first, second, analysis, report["selection_second"])
        report["two_pass_match"] = report["comparison"]["two_pass_match"]
        if not report["comparison"]["system_pages_match"]:
            raise BackupError(
                "Two NAND captures differ outside JFFS on "
                f"{report['comparison']['non_jffs_differing_pages']} page(s)"
            )
        if report["live_before"] != {"bank": selected, "generation": generation}:
            raise BackupError("Live boot state differs from captured NAND selection")
        report["boot_state"] = report["live_before"]

        if include_files:
            files_dir = backup_dir / "files"
            files_dir.mkdir(mode=0o700)
            for source in FILE_SOURCES:
                destination = files_dir / source.lstrip("/").replace("/", "__")
                try:
                    _receive(
                        Path(file_receiver_script), destination, bind_ip=bind_ip,
                        controller_ip=controller_ip, port=FILE_PORT,
                        timeout=FILE_TIMEOUT, max_bytes=FILE_MAX_BYTES, shell=shell,
                        sender_command=f"/tmp/file_stream {bind_ip} {FILE_PORT} {shlex.quote(source)}",
                    )
                    result = _file_receipt(destination, controller_ip=controller_ip)
                    report["files"].append({"source_path": source, "status": "captured", **result})
                except Exception as error:
                    report["files"].append({"source_path": source, "status": "failed", "reason": str(error)[:500]})
            missing = [item["source_path"] for item in report["files"]
                       if item["status"] != "captured" and item["source_path"] in CRITICAL_FILES]
            if missing:
                raise BackupError("Critical files could not be backed up: " + ", ".join(missing))
        else:
            report["files_skipped"] = True

        report["live_after"] = _live_state(shell)
        if report["live_after"] != report["live_before"]:
            raise BackupError("Live boot section or generation changed during backup")
        report["summary"] = {
            "nand_sha256": report["nand_sha256"],
            "nand_second_sha256": second["sha256"],
            "nand_differing_pages": report["comparison"]["differing_pages"],
            "nand_differing_regions": report["comparison"]["regions"],
            "selected_bank": selected,
            "selected_generation": generation,
            "partition_table_sha256": analysis["partition_table"]["sha256"],
            "files_captured": sum(item["status"] == "captured" for item in report["files"]),
            "files_failed": sum(item["status"] == "failed" for item in report["files"]),
            "mcu_version": report["mcu"].get("version"),
        }
        report["status"] = "validated"
        report["finished_at_epoch"] = time.time()
        report["report_path"] = str((backup_dir / "backup-report.json").resolve())
        _private_json(backup_dir / "backup-report.json", report)
        return report
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = str(error)
        report["finished_at_epoch"] = time.time()
        _private_json(backup_dir / "failure.json", report)
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"Backup failed: {error}") from error
