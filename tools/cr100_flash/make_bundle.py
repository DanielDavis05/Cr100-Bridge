"""Assemble a private, self-contained Pi bundle for one reviewed update.

Run from the workstation after building and signing a candidate. The signing
private key is deliberately absent from the explicit copy list.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .package import validate_candidate, validate_combined_release
import workspace


BRIDGE = workspace.BRIDGE
HERE = Path(__file__).resolve().parent
ZP80_FIXTURES = BRIDGE / "tools/zp80/fixtures"


def sources(candidate: Path, profile: str = "ui-only") -> dict[str, Path]:
    """Locate reviewed inputs, failing explicitly when a workspace asset is absent."""
    if profile == "ui-only":
        public_key = workspace.RUNS / "controller-deployment-2026-11-02/lab-upgradekey.pub"
    elif profile == "combined-release":
        public_key = workspace.FIRMWARE / "build/release/development-upgradekey.pub"
    else:
        raise ValueError(f"unsupported profile: {profile}")
    if not ZP80_FIXTURES.is_dir(): raise FileNotFoundError(ZP80_FIXTURES)
    mapping = {
        "assets/candidate.upd": candidate,
        "assets/stock.upd": workspace.STOCK_UPD,
        "assets/lab.pub": public_key,
        "assets/mcu_version": workspace.RUNS / "physical-upgrade-preflight-2026-11-04/mcu_version",
        "assets/mtd_stream": workspace.RUNS / "physical-backup-tool-2026-11-04/mtd_stream",
        "assets/file_stream": workspace.RUNS / "physical-backup-tool-2026-11-04/file_stream",
        "assets/receive_dump.py": workspace.RUNS / "physical-backup-tool-2026-11-04/receive_dump.py",
        "assets/receive_file.py": workspace.RUNS / "physical-backup-tool-2026-11-04/receive_file.py",
        "assets/select_nand_boot.py": workspace.RUNS / "controller-native-install-2026-11-02/select_nand_boot.py",
        "assets/rop/run_rop_shell.sh": workspace.RUNS / "physical-preflight-2026-11-04/run_rop_shell.sh",
        "assets/rop/wait_root_shell.py": workspace.RUNS / "physical-preflight-2026-11-04/wait_root_shell.py",
        "assets/rop/warmup_rop_art_fixture.py": workspace.RUNS / "physical-rop-system-2026-11-04/warmup_rop_art_fixture.py",
        "assets/rop/fixed-rop-system-root-shell.png": workspace.RUNS / "physical-rop-system-2026-11-04/fixed-rop-system-root-shell.png",
        "assets/zp80/zp80_peer.py": BRIDGE / "tools/zp80/zp80_peer.py",
        "assets/zp80/netstart_peer.py": BRIDGE / "tools/zp80/netstart_peer.py",
        **{f"assets/zp80/fixtures/{path.relative_to(ZP80_FIXTURES)}": path
           for path in ZP80_FIXTURES.rglob("*") if path.is_file()},
        "README.md": HERE / "README.md",
        **{f"cr100_flash/{name}": HERE / name for name in (
            "__init__.py", "access.py", "backup.py", "flash.py", "launcher.py",
            "package.py", "server.py")},
    }
    for path in mapping.values():
        if not path.is_file(): raise FileNotFoundError(path)
    return mapping


def build_bundle(candidate: Path, output: Path, profile: str = "ui-only") -> dict:
    candidate, output = candidate.resolve(), output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"bundle already exists: {output}")
    mapping = sources(candidate, profile)
    for path in mapping.values():
        if not path.is_file():
            raise FileNotFoundError(path)
    validator = validate_candidate if profile == "ui-only" else validate_combined_release
    report = validator(candidate, mapping["assets/stock.upd"], mapping["assets/lab.pub"])
    expected_key = {
        "ui-only": "aef526dba14610f92557e85405de59cd6afdbc2e6e09bb330d147eae61c8c063",
        "combined-release": "de42331e3e9a22f57402c4d04114854b78fd2b2ec9a060f41028613cf8e8bef6",
    }[profile]
    if report["lab_public_key_sha256"] != expected_key:
        raise ValueError("not the reviewed public verification key")

    partial = output.with_name(output.name + ".building-" + uuid4().hex[:8])
    partial.mkdir(mode=0o700, parents=True)
    try:
        manifest = {"format": 1, "profile": profile,
                    "candidate_sha256": report["candidate_sha256"],
                    "package_validation": report, "files": {}}
        for relative, source in mapping.items():
            destination = partial / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            data = destination.read_bytes()
            manifest["files"][relative] = {"bytes": len(data),
                                            "sha256": sha256(data).hexdigest()}
        manifest_path = partial / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.chmod(0o600)
        (partial / "README.txt").write_text(
            f"CR100 4.3.2 model-2 {profile} flash bundle. Private: keep mode 0700.\n"
            "On the Pi: cd to this directory and run:\n"
            "  python3 -m cr100_flash.flash check\n"
            "  python3 -m cr100_flash.flash flash\n"
            "Each flash creates a separate private run directory under ~/cr100-flashes.\n"
            "Keep the controller on stable cradle power. Never copy the signing private key here.\n")
        (partial / "README.txt").chmod(0o600)
        os.replace(partial, output)
    except BaseException:
        shutil.rmtree(partial)
        raise
    return {"output": str(output), "candidate_sha256": report["candidate_sha256"],
            "files": len(mapping)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True,
                        help="signed .upd produced for this flash")
    parser.add_argument("--output", type=Path, required=True,
                        help="new private bundle directory; must not exist")
    parser.add_argument("--profile", choices=("ui-only", "combined-release"),
                        default="ui-only", help="reviewed package change policy")
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.candidate, args.output, args.profile), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
