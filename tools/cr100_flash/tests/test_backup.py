"""Offline backup gates; all controller calls and streams are synthetic."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest


WORKSPACE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(WORKSPACE / "cr100-bridge" / "tools"))

from cr100_flash import backup  # noqa: E402


ASSETS = WORKSPACE / "cr100-bridge" / "runs" / "physical-backup-tool-2026-11-04"
SELECTOR = WORKSPACE / "cr100-bridge" / "runs" / "controller-native-install-2026-11-02" / "select_nand_boot.py"
LAB_NAND = ASSETS / "lab-backup-test.nand"


@pytest.fixture(autouse=True)
def require_lab_assets():
    if not all(path.is_file() for path in (LAB_NAND, SELECTOR, ASSETS / "receive_dump.py", ASSETS / "receive_file.py")):
        pytest.skip("Synthetic NAND and receiver assets are unavailable")


class FakeShell:
    def __init__(self, states=((0, 1), (0, 1))):
        self.states = iter(states)
        self.commands = []

    def __call__(self, command: str, *, timeout: int):
        self.commands.append(command)
        match = re.search(r"(CR100_STATE_[0-9a-f]{32})_BEGIN", command)
        if match:
            bank, generation = next(self.states)
            marker = match.group(1)
            output = f"{marker}_BEGIN\n{bank}\n{generation}\n{marker}_END\n"
        elif command == "/tmp/mcu_version":
            output = "/dev/hhkbd MCU version: 1.0-29305\n"
        else:
            output = "sender complete\n"
        return SimpleNamespace(returncode=0, output=output)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def fake_receiver(*, corrupt_second=False, corrupt_status=False, jffs_change=False,
                  boot_status_change=False, missing_file=None):
    def receive(receiver_script, destination, **kwargs):
        destination = Path(destination)
        if destination.suffix == ".nand":
            shutil.copyfile(LAB_NAND, destination)
            if corrupt_second and destination.name == "second.nand":
                with destination.open("r+b") as stream:
                    stream.seek(5000)
                    current = stream.read(1)
                    stream.seek(5000)
                    stream.write(bytes((current[0] ^ 1,)))
            if jffs_change and destination.name == "second.nand":
                # The synthetic layout's sole JFFS partition begins at block 322.
                with destination.open("r+b") as stream:
                    stream.seek(10314 * 528 + 20)
                    current = stream.read(1)
                    stream.seek(10314 * 528 + 20)
                    stream.write(bytes((current[0] ^ 1,)))
            pages = (16 << 20) // 512
            main = destination.with_suffix(".nand.main-status.bin")
            oob = destination.with_suffix(".nand.oob-status.bin")
            main.write_bytes(bytes(pages))
            oob.write_bytes(bytes(pages))
            if jffs_change and destination.name == "second.nand":
                codes = bytearray(pages)
                codes[10315] = 5
                oob.write_bytes(codes)
            if boot_status_change and destination.name == "second.nand":
                codes = bytearray(pages)
                codes[520] = 5
                oob.write_bytes(codes)
            if corrupt_status and destination.name == "second.nand":
                main.write_bytes(b"X" + bytes(pages - 1))
            receipt = {
                "path": str(destination.resolve()), "source": "tcp:127.0.0.1",
                "bytes": destination.stat().st_size, "sha256": _hash(destination),
                "mtd_data_bytes": 16 << 20, "page_data_bytes": 512,
                "page_oob_bytes": 16, "erase_bytes": 16384,
                "pages": pages, "stream_version": 2,
                "host_received_complete_frame": True,
                "main_error_pages": sum(byte != 0 for byte in main.read_bytes()),
                "oob_error_pages": sum(byte != 0 for byte in oob.read_bytes()),
                "main_status_sha256": _hash(main if not corrupt_status or destination.name != "second.nand" else
                                              destination.parent / "first.nand.main-status.bin"),
                "oob_status_sha256": _hash(oob),
            }
        else:
            source = kwargs["sender_command"].split()[-1]
            if source == missing_file:
                raise backup.BackupError("synthetic missing file")
            destination.write_bytes(source.encode())
            receipt = {
                "path": str(destination.resolve()), "source": "127.0.0.1",
                "bytes": destination.stat().st_size, "sha256": _hash(destination),
                "framed_complete": True, "file_content_only": True,
            }
        destination.with_suffix(destination.suffix + ".json").write_text(json.dumps(receipt))

    return receive


def _capture(tmp_path, monkeypatch, *, shell=None, include_files=True, **fake_options):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(backup, "_receive", fake_receiver(**fake_options))
    return backup.capture_backup(
        run_dir, shell or FakeShell(), controller_ip="127.0.0.1",
        bind_ip="127.0.0.1", receiver_script=ASSETS / "receive_dump.py",
        file_receiver_script=ASSETS / "receive_file.py", selector_path=SELECTOR,
        include_files=include_files,
    )


def test_full_backup_validates_two_reads_and_all_files(tmp_path, monkeypatch):
    report = _capture(tmp_path, monkeypatch)
    assert report["status"] == "validated"
    assert report["two_pass_match"] is True
    assert report["nand_sha256"] == _hash(LAB_NAND)
    assert report["boot_state"] == {"bank": 0, "generation": 1}
    assert report["selection"]["selected_bank"] == 0
    assert report["selection"]["partition_table"]["sha256"]
    assert all(bank["strict_integrity_valid"] for bank in report["selection"]["banks"])
    assert report["summary"]["files_captured"] == 22
    assert report["summary"]["files_failed"] == 0
    assert len(report["files"]) == 22
    assert all(item["status"] == "captured" for item in report["files"])
    report_path = Path(report["report_path"])
    assert report_path.is_file()
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report_path.parent.stat().st_mode & 0o777 == 0o700


def test_rejects_different_nand_passes_and_keeps_evidence(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="differ outside JFFS"):
        _capture(tmp_path, monkeypatch, include_files=False, corrupt_second=True)
    failure = json.loads((tmp_path / "run" / "backup" / "failure.json").read_text())
    assert failure["status"] == "failed"
    assert len(failure["nand"]) == 2
    assert (tmp_path / "run" / "backup" / "first.nand").is_file()


def test_allows_jffs_only_changes_and_records_both_hashes(tmp_path, monkeypatch):
    report = _capture(tmp_path, monkeypatch, include_files=False, jffs_change=True)
    comparison = report["comparison"]
    assert report["status"] == "validated"
    assert report["two_pass_match"] is False
    assert report["nand_hashes"][0] != report["nand_hashes"][1]
    assert comparison["differing_pages"] == 2
    assert comparison["regions"] == {"jffs": 2}
    assert comparison["raw_differing_pages"] == 1
    assert comparison["oob_status_differing_pages"] == 1
    assert comparison["system_pages_match"] is True


def test_rejects_boot_area_status_difference(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="differ outside JFFS"):
        _capture(tmp_path, monkeypatch, include_files=False, boot_status_change=True)
    failure = json.loads((tmp_path / "run" / "backup" / "failure.json").read_text())
    assert failure["comparison"]["regions"] == {"kernel-bank0": 1}
    assert failure["comparison"]["oob_status_differing_pages"] == 1


def test_rejects_status_sidecar_hash_mismatch(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="status hash mismatch"):
        _capture(tmp_path, monkeypatch, include_files=False, corrupt_status=True)
    assert (tmp_path / "run" / "backup" / "failure.json").is_file()


def test_rejects_live_boot_state_different_from_nand(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="Live boot state differs"):
        _capture(tmp_path, monkeypatch, include_files=False, shell=FakeShell(states=((1, 1),)))


def test_rejects_boot_state_change_during_capture(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="changed during backup"):
        _capture(tmp_path, monkeypatch, include_files=False, shell=FakeShell(states=((0, 1), (1, 1))))


def test_missing_critical_file_blocks_after_collecting_others(tmp_path, monkeypatch):
    with pytest.raises(backup.BackupError, match="Critical files could not be backed up"):
        _capture(tmp_path, monkeypatch, missing_file="/jffs/wifi.txt")
    failure = json.loads((tmp_path / "run" / "backup" / "failure.json").read_text())
    assert len(failure["files"]) == 22
    assert len([item for item in failure["files"] if item["status"] == "failed"]) == 1


def test_missing_volatile_file_is_reported(tmp_path, monkeypatch):
    report = _capture(tmp_path, monkeypatch, missing_file="/ramdisk/optlog/hh.anacapa.pid")
    failures = [item for item in report["files"] if item["status"] == "failed"]
    assert [item["source_path"] for item in failures] == ["/ramdisk/optlog/hh.anacapa.pid"]
    assert report["status"] == "validated"


def test_real_file_receiver_with_fake_local_sender(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    payload = b"offline CR100 file stream test\n"

    def shell(command: str, *, timeout: int):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
            stream.sendall(b"CRFILE2\0" + struct.pack("<I", len(payload)) + payload + struct.pack("<I", 0))
        return SimpleNamespace(returncode=0, output="sent")

    destination = tmp_path / "received.txt"
    backup._receive(
        ASSETS / "receive_file.py", destination, bind_ip="127.0.0.1",
        controller_ip="127.0.0.1", port=port, timeout=5, max_bytes=1024,
        shell=shell, sender_command="synthetic sender",
    )
    assert destination.read_bytes() == payload
    assert backup._file_receipt(destination, controller_ip="127.0.0.1")["sha256"] == hashlib.sha256(payload).hexdigest()


def test_real_nand_receiver_with_synthetic_bad_blocks(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def shell(command: str, *, timeout: int):
        result = subprocess.run(
            [sys.executable, str(ASSETS / "simulate_framed_send.py"),
             "--nand", str(LAB_NAND), "--receipt", str(ASSETS / "lab-backup-test.json"),
             "--host", "127.0.0.1", "--port", str(port)],
            capture_output=True, text=True, timeout=timeout,
        )
        return SimpleNamespace(returncode=result.returncode, output=result.stdout + result.stderr)

    destination = tmp_path / "synthetic.nand"
    backup._receive(
        ASSETS / "receive_dump.py", destination, bind_ip="127.0.0.1",
        controller_ip="127.0.0.1", port=port, timeout=60, max_bytes=None,
        shell=shell, sender_command="synthetic NAND sender",
    )
    receipt = backup._nand_receipt(destination, controller_ip="127.0.0.1")
    assert receipt["mtd_data_bytes"] == 16 << 20
    assert receipt["main_error_pages"] == 128
    assert receipt["oob_error_pages"] == 0
