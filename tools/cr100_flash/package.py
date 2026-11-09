"""Offline validation of a model-2 CR100 4.3.2 laboratory update.

This module only reads local files. A successful result establishes the package
layout, preserved stock records, rootfs size and lab-key signature; it does not
establish that a particular controller can boot the image.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import stat
import struct
import zlib


STOCK_SHA256 = "34cf093de96313ea6b7ae8f786f4800fc6d8daa84c4a4ebe00f88f4ad68b9380"
RELEASE = (25, 2, 50130)
RECORD_TYPES = (1, 2, 2, 3, 6, 4, 5, 9, 10, 12, 0xFFFFFFFF)
ROOTFS_CAPACITY = 4_194_304
TARGET = "/opt/swf/MainUI.swf"
UPDATER_TARGET = "/bin/upgrade"
COMBINED_RELEASE_SHA256 = "26d0bf0f0a1817e629336563c24204954f71f1d011e2545c00da76d4ddd33608"
COMBINED_KEY_SHA256 = "de42331e3e9a22f57402c4d04114854b78fd2b2ec9a060f41028613cf8e8bef6"
COMBINED_ROOTFS_SHA256 = "1ed3f2975bb2df08b7e4596219eb10a3bd16429fe3f8acec28865adc22c5933c"
COMBINED_UI_SHA256 = "57365e1f16c4c5c59be1b600bf352279b962e8e9913492f3d344c7785b590bf1"
STOCK_UPDATER_SHA256 = "4f432f494821e5544550b75b0bcb74f58254f1518013714ce8656358816be17c"
COMBINED_UPDATER_SHA256 = "5eb6f17a71b5998090a71f03d05af5a74a9326391aea21db0551fe0ef7c5168b"
UPDATER_PATCH_OFFSET = 0x70CF
UPD_MAGIC = 0x35167F49
CRAMFS_MAGIC = 0x28CD3D45
MD5_DIGEST_INFO = bytes.fromhex("3020300c06082a864886f70d020505000410")


@dataclass(frozen=True)
class _Record:
    offset: int
    kind: int
    length: int
    flags: int

    def raw(self, data: bytes) -> bytes:
        return data[self.offset:self.offset + self.length]

    def payload(self, data: bytes) -> bytes:
        return data[self.offset + 16:self.offset + self.length]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _records(data: bytes) -> list[_Record]:
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError(f"Truncated UPD record at {offset:#x}")
        magic, kind, length, flags = struct.unpack_from("<4I", data, offset)
        if magic != UPD_MAGIC or length < 16 or length > len(data) - offset:
            raise ValueError(f"Invalid UPD record at {offset:#x}")
        result.append(_Record(offset, kind, length, flags))
        offset += length
    if tuple(r.kind for r in result) != RECORD_TYPES:
        raise ValueError("UPD record order differs from audited 4.3.2 package")
    if result[-1].length != 144:
        raise ValueError("Expected a final 128-byte UPD signature")
    return result


def _release(data: bytes, record: _Record) -> tuple[int, int, int]:
    if record.length != 28:
        raise ValueError("Invalid UPD version record length")
    return struct.unpack_from("<3I", data, record.offset + 16)


def _check_cramfs(image: bytes) -> None:
    if len(image) < 76:
        raise ValueError("Root filesystem is too short for CramFS")
    magic, size, flags, future = struct.unpack_from("<4I", image)
    if (magic, size, flags, future) != (CRAMFS_MAGIC, len(image), 3, 0):
        raise ValueError("Invalid CramFS header or declared size")
    if image[16:32] != b"Compressed ROMFS":
        raise ValueError("Invalid CramFS signature")
    stored_crc = struct.unpack_from("<I", image, 32)[0]
    crc_input = bytearray(image)
    struct.pack_into("<I", crc_input, 32, 0)
    if zlib.crc32(crc_input) & 0xFFFFFFFF != stored_crc:
        raise ValueError("Invalid CramFS checksum")


class _CramFS:
    """Read and verify the exact CramFS layout used by the audited builder."""

    def __init__(self, data: bytes):
        _check_cramfs(data)
        self.data = data
        self.nodes: dict[str, dict] = {}
        self._directory_ranges: set[tuple[int, int]] = set()
        self._walk(self._inode(64), "/")
        if len(self.nodes) != struct.unpack_from("<I", data, 44)[0]:
            raise ValueError("CramFS file count differs from inode walk")

        self.groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for node in self.nodes.values():
            if self.has_data(node) and node["size"]:
                self.groups[(node["offset"], node["size"])].append(node)
        if not self.groups:
            raise ValueError("CramFS has no data blocks")
        blocks = sum((size + 4095) // 4096 for _, size in self.groups)
        if blocks != struct.unpack_from("<I", data, 40)[0]:
            raise ValueError("CramFS block accounting differs from inode walk")
        metadata_end = max(
            node["offset"] + node["size"]
            for node in self.nodes.values()
            if stat.S_ISDIR(node["mode"])
        )
        self.first_data = min(offset for offset, _ in self.groups)
        if metadata_end != self.first_data:
            raise ValueError("CramFS directory tables are not a single prefix")

    @staticmethod
    def has_data(node: dict) -> bool:
        return stat.S_ISREG(node["mode"]) or stat.S_ISLNK(node["mode"])

    def _inode(self, pos: int) -> dict:
        if pos < 0 or pos + 12 > len(self.data):
            raise ValueError("CramFS inode is outside the image")
        a, b, c = struct.unpack_from("<3I", self.data, pos)
        return {
            "pos": pos, "a": a, "b": b, "c": c,
            "mode": a & 0xFFFF, "uid": a >> 16, "gid": b >> 24,
            "size": b & 0xFFFFFF, "name_len": (c & 63) * 4,
            "offset": (c >> 6) * 4,
        }

    def _walk(self, node: dict, path: str) -> None:
        if path in self.nodes:
            raise ValueError("Duplicate CramFS path")
        if len(path) > 4096:
            raise ValueError("CramFS path is too deep")
        self.nodes[path] = node
        if not stat.S_ISDIR(node["mode"]):
            return
        pos = node["offset"]
        end = pos + node["size"]
        if pos < 0 or end > len(self.data):
            raise ValueError("CramFS directory is outside the image")
        if node["size"]:
            directory_range = (pos, end)
            if directory_range in self._directory_ranges:
                raise ValueError("CramFS directory is reused or cyclic")
            self._directory_ranges.add(directory_range)
        while pos < end:
            child = self._inode(pos)
            if pos + 12 + child["name_len"] > end:
                raise ValueError("CramFS directory entry exceeds its table")
            raw_name = self.data[pos + 12:pos + 12 + child["name_len"]]
            name = raw_name.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if not name or "/" in name or name in (".", ".."):
                raise ValueError("Invalid CramFS directory entry")
            self._walk(child, path.rstrip("/") + "/" + name)
            pos += 12 + child["name_len"]
        if pos != end:
            raise ValueError("CramFS directory table length mismatch")

    def compressed_blocks(self, node: dict) -> list[bytes]:
        count = (node["size"] + 4095) // 4096
        if not count:
            return []
        offset = node["offset"]
        pos = offset + 4 * count
        if offset < 0 or pos > len(self.data):
            raise ValueError("CramFS block pointer table is outside the image")
        ends = struct.unpack_from("<" + "I" * count, self.data, offset)
        result = []
        for end in ends:
            if not pos <= end <= len(self.data):
                raise ValueError("Invalid CramFS compressed-block pointer")
            result.append(self.data[pos:end])
            pos = end
        return result

    def contents(self, node: dict) -> bytes:
        result = []
        for index, block in enumerate(self.compressed_blocks(node)):
            expected = min(4096, node["size"] - index * 4096)
            if not block:
                chunk = bytes(expected)
            else:
                decoder = zlib.decompressobj()
                try:
                    chunk = decoder.decompress(block, expected + 1)
                except zlib.error as exc:
                    raise ValueError("Invalid CramFS zlib block") from exc
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError("Invalid CramFS zlib block")
            if len(chunk) != expected:
                raise ValueError("CramFS decompressed-block size mismatch")
            result.append(chunk)
        return b"".join(result)


def _compare_ui_only(stock: _CramFS, candidate: _CramFS) -> dict:
    if stock.nodes.keys() != candidate.nodes.keys():
        raise ValueError("CramFS filesystem paths changed")
    if TARGET not in stock.nodes:
        raise ValueError("MainUI.swf is missing from audited stock filesystem")
    if not stat.S_ISREG(stock.nodes[TARGET]["mode"]):
        raise ValueError("Stock MainUI.swf is not a regular file")

    # The builder changes only these three CramFS accounting fields.
    stock_header = bytearray(stock.data[:64])
    candidate_header = bytearray(candidate.data[:64])
    for offset in (4, 32, 40):
        stock_header[offset:offset + 4] = b"\0" * 4
        candidate_header[offset:offset + 4] = b"\0" * 4
    if stock_header != candidate_header:
        raise ValueError("CramFS superblock metadata changed")

    changed_paths = []
    ui_before = b""
    ui_after = b""
    for path, old in stock.nodes.items():
        new = candidate.nodes[path]
        for field in ("pos", "a", "mode", "uid", "gid", "name_len"):
            if old[field] != new[field]:
                raise ValueError(f"CramFS metadata changed for {path}: {field}")
        old_name = stock.data[old["pos"] + 12:old["pos"] + 12 + old["name_len"]]
        new_name = candidate.data[new["pos"] + 12:new["pos"] + 12 + new["name_len"]]
        if old_name != new_name:
            raise ValueError(f"CramFS name or padding changed for {path}")
        if stock.has_data(old):
            if path != TARGET and old["size"] != new["size"]:
                raise ValueError(f"Non-UI file size changed: {path}")
            old_bytes = stock.contents(old)
            new_bytes = candidate.contents(new)
            if path != TARGET:
                if old_bytes != new_bytes:
                    raise ValueError(f"Non-UI file content changed: {path}")
                if stock.compressed_blocks(old) != candidate.compressed_blocks(new):
                    raise ValueError(f"Non-UI compressed blocks changed: {path}")
            else:
                if not stat.S_ISREG(new["mode"]):
                    raise ValueError("Candidate MainUI.swf is not a regular file")
                ui_before, ui_after = old_bytes, new_bytes
                if old_bytes != new_bytes:
                    changed_paths.append(path)
        elif (old["b"], old["c"]) != (new["b"], new["c"]):
            raise ValueError(f"CramFS directory or device metadata changed: {path}")
    if changed_paths != [TARGET]:
        raise ValueError("MainUI.swf must be the sole changed filesystem file")
    if ui_after[:3] not in (b"FWS", b"CWS"):
        raise ValueError("Candidate MainUI.swf is not a supported SWF")
    return {
        "changed_paths": changed_paths,
        "filesystem_paths_verified": len(stock.nodes),
        "main_ui_original_sha256": _sha256(ui_before),
        "main_ui_sha256": _sha256(ui_after),
        "main_ui_bytes": len(ui_after),
    }


def _compare_combined_release(stock: _CramFS, candidate: _CramFS) -> dict:
    """Prove the exact two-file change, including the updater's single byte."""
    if stock.nodes.keys() != candidate.nodes.keys():
        raise ValueError("CramFS filesystem paths changed")
    changed = []
    expected = {TARGET, UPDATER_TARGET}
    stock_header = bytearray(stock.data[:64])
    candidate_header = bytearray(candidate.data[:64])
    for offset in (4, 32, 40):
        stock_header[offset:offset + 4] = b"\0" * 4
        candidate_header[offset:offset + 4] = b"\0" * 4
    if stock_header != candidate_header:
        raise ValueError("CramFS superblock metadata changed")

    payloads = {}
    for path, old in stock.nodes.items():
        new = candidate.nodes[path]
        for field in ("pos", "a", "mode", "uid", "gid", "name_len"):
            if old[field] != new[field]:
                raise ValueError(f"CramFS metadata changed for {path}: {field}")
        old_name = stock.data[old["pos"] + 12:old["pos"] + 12 + old["name_len"]]
        new_name = candidate.data[new["pos"] + 12:new["pos"] + 12 + new["name_len"]]
        if old_name != new_name:
            raise ValueError(f"CramFS name or padding changed for {path}")
        if stock.has_data(old):
            if path not in expected and old["size"] != new["size"]:
                raise ValueError(f"Unapproved file size changed: {path}")
            old_bytes = stock.contents(old)
            new_bytes = candidate.contents(new)
            if path in expected:
                if not stat.S_ISREG(old["mode"]) or not stat.S_ISREG(new["mode"]):
                    raise ValueError(f"Approved target is not a regular file: {path}")
                payloads[path] = (old_bytes, new_bytes)
                if old_bytes != new_bytes:
                    changed.append(path)
            else:
                if old_bytes != new_bytes:
                    raise ValueError(f"Unapproved file content changed: {path}")
                if stock.compressed_blocks(old) != candidate.compressed_blocks(new):
                    raise ValueError(f"Unapproved compressed blocks changed: {path}")
        elif (old["b"], old["c"]) != (new["b"], new["c"]):
            raise ValueError(f"CramFS directory or device metadata changed: {path}")
    if set(changed) != expected or len(changed) != 2:
        raise ValueError("Combined release must change exactly MainUI.swf and /bin/upgrade")
    ui_before, ui_after = payloads[TARGET]
    upgrade_before, upgrade_after = payloads[UPDATER_TARGET]
    if ui_after[:3] not in (b"FWS", b"CWS") or _sha256(ui_after) != COMBINED_UI_SHA256:
        raise ValueError("Combined release MainUI.swf differs from pinned build")
    if (_sha256(upgrade_before) != STOCK_UPDATER_SHA256 or
            _sha256(upgrade_after) != COMBINED_UPDATER_SHA256 or
            len(upgrade_before) != 214_572 or len(upgrade_after) != 214_572):
        raise ValueError("Combined release updater differs from pinned binaries")
    differences = [index for index, (before, after) in
                   enumerate(zip(upgrade_before, upgrade_after)) if before != after]
    if (differences != [UPDATER_PATCH_OFFSET] or
            upgrade_before[UPDATER_PATCH_OFFSET] != 0x8D or
            upgrade_after[UPDATER_PATCH_OFFSET] != 0xA0):
        raise ValueError("Combined release updater is not the reviewed one-byte patch")
    return {
        "changed_paths": sorted(changed),
        "filesystem_paths_verified": len(stock.nodes),
        "main_ui_original_sha256": _sha256(ui_before),
        "main_ui_sha256": _sha256(ui_after),
        "main_ui_bytes": len(ui_after),
        "updater_original_sha256": _sha256(upgrade_before),
        "updater_sha256": _sha256(upgrade_after),
        "updater_changed_offsets": differences,
    }


def _verify_signature(data: bytes, signature_record: _Record, key: bytes) -> None:
    if len(key) != 260:
        raise ValueError("Expected a 260-byte legacy RSA public key")
    bits = struct.unpack_from("<I", key)[0]
    modulus = int.from_bytes(key[4:132], "big")
    exponent = int.from_bytes(key[132:260], "big")
    if bits != 1024 or modulus.bit_length() != bits or exponent <= 1:
        raise ValueError("Unsupported legacy RSA public key")
    signature = int.from_bytes(signature_record.payload(data), "big")
    if not 0 < signature < modulus:
        raise ValueError("UPD signature is invalid under the supplied lab key")
    digest_info = MD5_DIGEST_INFO + hashlib.md5(data[:signature_record.offset]).digest()
    expected = b"\0\1" + b"\xff" * (128 - len(digest_info) - 3) + b"\0" + digest_info
    recovered = pow(signature, exponent, modulus).to_bytes(128, "big")
    if not hmac.compare_digest(recovered, expected):
        raise ValueError("UPD signature is invalid under the supplied lab key")


def validate_candidate(candidate: Path, stock: Path, lab_pub: Path) -> dict:
    """Validate *candidate* against the exact audited stock UPD and *lab_pub*.

    Raises ``ValueError`` for any rejected package. The supplied public key is
    reported by hash; the caller must pin that hash to its intended lab key.
    """
    candidate_data = Path(candidate).read_bytes()
    stock_data = Path(stock).read_bytes()
    key_data = Path(lab_pub).read_bytes()

    stock_sha = _sha256(stock_data)
    if stock_sha != STOCK_SHA256:
        raise ValueError("Stock reference is not the audited model-2 4.3.2 UPD")

    stock_records = _records(stock_data)
    candidate_records = _records(candidate_data)
    if _release(stock_data, stock_records[0]) != RELEASE:
        raise ValueError("Audited stock reference has an unexpected release")
    if _release(candidate_data, candidate_records[0]) != RELEASE:
        raise ValueError("Candidate release must be 25.2-50130")

    protected_count = 0
    for source, proposed in zip(stock_records, candidate_records):
        if source.kind == 4:
            if proposed.flags != source.flags:
                raise ValueError("Root filesystem record flags changed")
            continue
        if source.kind == 0xFFFFFFFF:
            if proposed.flags != source.flags:
                raise ValueError("Signature record flags changed")
            continue
        if source.raw(stock_data) != proposed.raw(candidate_data):
            raise ValueError(f"Protected UPD record type {source.kind} changed")
        protected_count += 1

    rootfs = candidate_records[5].payload(candidate_data)
    if len(rootfs) > ROOTFS_CAPACITY:
        raise ValueError(f"Root filesystem exceeds {ROOTFS_CAPACITY} bytes")
    filesystem_report = _compare_ui_only(
        _CramFS(stock_records[5].payload(stock_data)), _CramFS(rootfs)
    )

    table_9 = candidate_records[7].payload(candidate_data)
    table_10 = candidate_records[8].payload(candidate_data)
    if table_9 != table_10:
        raise ValueError("Redundant partition tables differ")
    _verify_signature(candidate_data, candidate_records[-1], key_data)

    return {
        "candidate": str(Path(candidate).resolve()),
        "candidate_sha256": _sha256(candidate_data),
        "candidate_bytes": len(candidate_data),
        "stock_sha256": stock_sha,
        "lab_public_key_sha256": _sha256(key_data),
        "release": "25.2-50130",
        "record_types": list(RECORD_TYPES),
        "protected_records_identical": protected_count,
        "rootfs_bytes": len(rootfs),
        "rootfs_capacity_bytes": ROOTFS_CAPACITY,
        "rootfs_sha256": _sha256(rootfs),
        "partition_table_sha256": _sha256(table_9),
        "kernel_sha256": _sha256(candidate_records[4].payload(candidate_data)),
        "mcu_sha256": _sha256(candidate_records[9].payload(candidate_data)),
        "signature_valid": True,
        "signature_algorithm": "RSA-1024 / PKCS#1 v1.5 / MD5",
        **filesystem_report,
    }


def validate_combined_release(candidate: Path, stock: Path, dev_pub: Path) -> dict:
    """Validate the pinned two-file 4.3.2 release under its development key.

    This separate policy does not relax ``validate_candidate``: the latter
    continues to accept only a MainUI.swf change and the original lab key.
    The candidate, public key, rootfs, UI and updater are all pinned by exact
    digest, while record and logical filesystem checks independently prove the
    reviewed change scope.
    """
    candidate_data = Path(candidate).read_bytes()
    stock_data = Path(stock).read_bytes()
    key_data = Path(dev_pub).read_bytes()
    stock_sha, candidate_sha, key_sha = map(_sha256,
        (stock_data, candidate_data, key_data))
    if stock_sha != STOCK_SHA256:
        raise ValueError("Stock reference is not the audited model-2 4.3.2 UPD")
    if candidate_sha != COMBINED_RELEASE_SHA256:
        raise ValueError("Candidate is not the exact reviewed combined release")
    if key_sha != COMBINED_KEY_SHA256:
        raise ValueError("Development public key is not the reviewed release key")

    stock_records, candidate_records = _records(stock_data), _records(candidate_data)
    if (_release(stock_data, stock_records[0]) != RELEASE or
            _release(candidate_data, candidate_records[0]) != RELEASE):
        raise ValueError("Combined release must be 25.2-50130")
    protected_count = 0
    for source, proposed in zip(stock_records, candidate_records):
        if source.kind in (4, 0xFFFFFFFF):
            if proposed.flags != source.flags:
                raise ValueError("Rootfs or signature record flags changed")
            continue
        if source.raw(stock_data) != proposed.raw(candidate_data):
            raise ValueError(f"Protected UPD record type {source.kind} changed")
        protected_count += 1

    rootfs = candidate_records[5].payload(candidate_data)
    if len(rootfs) > ROOTFS_CAPACITY:
        raise ValueError(f"Root filesystem exceeds {ROOTFS_CAPACITY} bytes")
    if _sha256(rootfs) != COMBINED_ROOTFS_SHA256:
        raise ValueError("Combined root filesystem differs from reviewed build")
    filesystem_report = _compare_combined_release(
        _CramFS(stock_records[5].payload(stock_data)), _CramFS(rootfs)
    )
    table_9 = candidate_records[7].payload(candidate_data)
    table_10 = candidate_records[8].payload(candidate_data)
    if table_9 != table_10:
        raise ValueError("Redundant partition tables differ")
    _verify_signature(candidate_data, candidate_records[-1], key_data)

    return {
        "policy": "combined-release-exact",
        "candidate": str(Path(candidate).resolve()),
        "candidate_sha256": candidate_sha,
        "candidate_bytes": len(candidate_data),
        "stock_sha256": stock_sha,
        "lab_public_key_sha256": key_sha,
        "development_public_key_sha256": key_sha,
        "release": "25.2-50130",
        "record_types": list(RECORD_TYPES),
        "protected_records_identical": protected_count,
        "rootfs_bytes": len(rootfs),
        "rootfs_capacity_bytes": ROOTFS_CAPACITY,
        "rootfs_sha256": _sha256(rootfs),
        "partition_table_sha256": _sha256(table_9),
        "kernel_sha256": _sha256(candidate_records[4].payload(candidate_data)),
        "mcu_sha256": _sha256(candidate_records[9].payload(candidate_data)),
        "signature_valid": True,
        "signature_algorithm": "RSA-1024 / PKCS#1 v1.5 / MD5",
        **filesystem_report,
    }
