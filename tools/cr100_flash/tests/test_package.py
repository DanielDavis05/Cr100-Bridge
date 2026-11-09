"""Package policy tests against the audited stock and lab-signed fixtures."""

from pathlib import Path
import struct
import sys
import zlib

import pytest


WORKSPACE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(WORKSPACE / "cr100-bridge" / "tools"))

from cr100_flash.package import (  # noqa: E402
    ROOTFS_CAPACITY,
    _CramFS,
    _records,
    validate_candidate,
    validate_combined_release,
)


STOCK = WORKSPACE / "update-analysis/7.1-access/cr100/downloads/25.2-50130-1-2.upd"
CANDIDATE = WORKSPACE / (
    "cr100-bridge/runs/controller-deployment-2026-11-02/"
    "25.2-50130-1-2-settings-ui-lab-signed.upd"
)
LAB_KEY = WORKSPACE / "cr100-bridge/runs/controller-deployment-2026-11-02/lab-upgradekey.pub"
COMBINED = WORKSPACE / "cr100-firmware/build/release/cr100-custom-4.3.2.upd"
DEV_KEY = WORKSPACE / "cr100-firmware/build/release/development-upgradekey.pub"


@pytest.fixture(autouse=True)
def require_audited_artifacts():
    missing = [p for p in (STOCK, CANDIDATE, LAB_KEY) if not p.is_file()]
    if missing:
        pytest.skip(f"Audited update fixture unavailable: {missing[0]}")


def _mutated(tmp_path: Path, change, name: str = "mutated.upd") -> Path:
    data = bytearray(CANDIDATE.read_bytes())
    change(data)
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_audited_lab_package_passes():
    report = validate_candidate(CANDIDATE, STOCK, LAB_KEY)
    assert report["candidate_sha256"] == (
        "99d70a0b20d5ac0e192128debe202410d4fadb2c926fcc4bd8fd038d01a211a2"
    )
    assert report["signature_valid"] is True
    assert report["release"] == "25.2-50130"
    assert report["protected_records_identical"] == 9
    assert report["rootfs_bytes"] == 4_169_728
    assert report["changed_paths"] == ["/opt/swf/MainUI.swf"]
    assert report["filesystem_paths_verified"] == 288
    assert report["partition_table_sha256"] == (
        "94b7528474328b0e138145ed5121390e81e817fff0af081a1fea6ba1d2fd59d7"
    )
    assert report["kernel_sha256"] == (
        "f39c4c6c7b8ca14e8d8d467d5ccc6293bccb87183fbe3d361da8a7ab9069e4b5"
    )
    assert report["mcu_sha256"] == (
        "0f96ddc311a81c5e13f8dd4af92dccba86e89770218d5b93e518d215b3d47747"
    )


def test_rejects_other_release_before_signature(tmp_path):
    def change(data):
        struct.pack_into("<I", data, 16 + 8, 50131)

    path = _mutated(tmp_path, change)
    with pytest.raises(ValueError, match="Candidate release"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_modified_install_script(tmp_path):
    def change(data):
        install = _records(data)[3]
        data[install.offset + 16] ^= 1

    path = _mutated(tmp_path, change)
    with pytest.raises(ValueError, match="Protected UPD record type 3"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_unknown_record_order(tmp_path):
    def change(data):
        marker = _records(data)[6]
        struct.pack_into("<I", data, marker.offset + 4, 8)

    path = _mutated(tmp_path, change)
    with pytest.raises(ValueError, match="record order"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_rootfs_over_bank_capacity(tmp_path):
    data = bytearray(CANDIDATE.read_bytes())
    root = _records(data)[5]
    extra = ROOTFS_CAPACITY - (root.length - 16) + 1
    data[root.offset + root.length:root.offset + root.length] = b"\0" * extra
    struct.pack_into("<I", data, root.offset + 8, root.length + extra)
    path = tmp_path / "oversize.upd"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Root filesystem exceeds"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_invalid_signature(tmp_path):
    path = _mutated(tmp_path, lambda data: data.__setitem__(-1, data[-1] ^ 1))
    with pytest.raises(ValueError, match="signature is invalid"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_modified_non_ui_file_with_valid_cramfs(tmp_path):
    data = bytearray(CANDIDATE.read_bytes())
    root = _records(data)[5]
    root_start = root.offset + 16
    root_end = root.offset + root.length
    filesystem = _CramFS(bytes(data[root_start:root_end]))
    node = filesystem.nodes["/etc/host.conf"]
    original = filesystem.contents(node)
    old_blocks = filesystem.compressed_blocks(node)
    assert len(old_blocks) == 1
    changed = b"A" + original[1:]
    compressed = zlib.compress(changed, 9)
    assert len(compressed) == len(old_blocks[0])
    block_start = root_start + node["offset"] + 4
    data[block_start:block_start + len(compressed)] = compressed
    struct.pack_into("<I", data, root_start + 32, 0)
    crc = zlib.crc32(data[root_start:root_end]) & 0xFFFFFFFF
    struct.pack_into("<I", data, root_start + 32, crc)
    path = tmp_path / "non-ui-change.upd"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Non-UI file content changed: /etc/host.conf"):
        validate_candidate(path, STOCK, LAB_KEY)


def test_rejects_wrong_stock_reference(tmp_path):
    stock = bytearray(STOCK.read_bytes())
    stock[-1] ^= 1
    wrong = tmp_path / "wrong-stock.upd"
    wrong.write_bytes(stock)
    with pytest.raises(ValueError, match="Stock reference"):
        validate_candidate(CANDIDATE, wrong, LAB_KEY)


def test_exact_combined_release_passes():
    if not COMBINED.is_file() or not DEV_KEY.is_file():
        pytest.skip("Combined release artifacts are unavailable")
    report = validate_combined_release(COMBINED, STOCK, DEV_KEY)
    assert report["policy"] == "combined-release-exact"
    assert report["candidate_sha256"] == (
        "26d0bf0f0a1817e629336563c24204954f71f1d011e2545c00da76d4ddd33608"
    )
    assert report["development_public_key_sha256"] == (
        "de42331e3e9a22f57402c4d04114854b78fd2b2ec9a060f41028613cf8e8bef6"
    )
    assert report["changed_paths"] == ["/bin/upgrade", "/opt/swf/MainUI.swf"]
    assert report["updater_changed_offsets"] == [0x70CF]
    assert report["updater_sha256"] == (
        "5eb6f17a71b5998090a71f03d05af5a74a9326391aea21db0551fe0ef7c5168b"
    )
    assert report["main_ui_sha256"] == (
        "57365e1f16c4c5c59be1b600bf352279b962e8e9913492f3d344c7785b590bf1"
    )
    assert report["rootfs_bytes"] == 4_182_016 < ROOTFS_CAPACITY
    assert report["protected_records_identical"] == 9
    assert report["signature_valid"] is True


def test_combined_release_does_not_relax_ui_only_default():
    if not COMBINED.is_file() or not DEV_KEY.is_file():
        pytest.skip("Combined release artifacts are unavailable")
    with pytest.raises(ValueError, match="Non-UI file content changed: /bin/upgrade"):
        validate_candidate(COMBINED, STOCK, DEV_KEY)


def test_combined_release_rejects_other_public_key():
    if not COMBINED.is_file():
        pytest.skip("Combined release artifact is unavailable")
    with pytest.raises(ValueError, match="Development public key"):
        validate_combined_release(COMBINED, STOCK, LAB_KEY)


def test_combined_release_rejects_one_byte_mutation(tmp_path):
    if not COMBINED.is_file() or not DEV_KEY.is_file():
        pytest.skip("Combined release artifacts are unavailable")
    data = bytearray(COMBINED.read_bytes())
    data[-1] ^= 1
    changed = tmp_path / "changed-combined.upd"
    changed.write_bytes(data)
    with pytest.raises(ValueError, match="not the exact reviewed combined release"):
        validate_combined_release(changed, STOCK, DEV_KEY)
