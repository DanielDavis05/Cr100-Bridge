"""Where optional development inputs outside this repository live.

The installed bridge never needs these. They are only used by lab tools
(`radio_emulator_lab.py`, `lab_peer_vm.py`, `cr100_flash.make_bundle`) and by the optional
private-capture regression in `test_link.py`, which skips when they are absent.

Defaults follow the author's workspace layout:

    <workspace>/
      projects/cr100-bridge/      this repository
      projects/cr100-firmware/    the companion custom-firmware project
      stock-firmware/             25.2-50130-1-2.upd (official CR100 4.3.2 package)
      research/cr100-emulator/    SH4 emulator of the original controller
      research/bridge-runs/       historical lab evidence
      research/bridge-captures/   private radio captures and their pairing state

Override with CR100_WORKSPACE, CR100_STOCK_UPD, CR100_RESEARCH, CR100_FIRMWARE,
CR100_BRIDGE_RUNS or CR100_CAPTURES.
"""
from __future__ import annotations

import os
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


WORKSPACE = _env_path('CR100_WORKSPACE') or BRIDGE.parents[1]
RESEARCH = _env_path('CR100_RESEARCH') or WORKSPACE / 'research'
FIRMWARE = _env_path('CR100_FIRMWARE') or BRIDGE.parent / 'cr100-firmware'
STOCK_UPD = _env_path('CR100_STOCK_UPD') or WORKSPACE / 'stock-firmware/25.2-50130-1-2.upd'
EMULATOR = RESEARCH / 'cr100-emulator'
RUNS = _env_path('CR100_BRIDGE_RUNS') or RESEARCH / 'bridge-runs'
CAPTURES = _env_path('CR100_CAPTURES') or RESEARCH / 'bridge-captures'
