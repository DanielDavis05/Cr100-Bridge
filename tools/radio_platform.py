#!/usr/bin/env python3
"""Supported boards and the radio each one uses for the CR100 link.

This is the one place that says which hardware the bridge supports. The installer
reads it to decide what to install; nothing here is consulted while the bridge runs.

A board is identified by the first entry of /proc/device-tree/compatible, the same
name the Wi-Fi driver uses to pick its firmware file. Each board names its onboard
radio. A radio lists what the installer must set up for it:

  packages        apt packages providing its driver and firmware
  dkms_module     DKMS module that must be built for the running kernel, or ''
  firmware_patch  script (relative to the release) run with --apply, or ''
  interface       network interface the bridge reserves for the CR100

Supporting another board with an existing radio is one BOARDS line (plus the same
name in install.sh's early list, which test_install.py keeps in step). A new kind
of radio, such as a USB adapter, is a RADIOS entry; pass its id to radio_for() to
choose it over the onboard one. The runtime in bridge_service.py and
sonosnet_link.py still assumes Nexmon on wlan0 and would need the same choice.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
from pathlib import Path
import sys

COMPATIBLE = Path('/proc/device-tree/compatible')


@dataclass(frozen=True)
class Radio:
    description: str
    interface: str
    packages: tuple[str, ...] = ()
    dkms_module: str = ''
    firmware_patch: str = ''


RADIOS = {
    # Kali's Nexmon build of the BCM43455c0 (CYW43455) fitted to the Pi 3B+ and Pi 4.
    # One firmware image serves every board, so the reviewed byte patch does too.
    'bcm43455c0-nexmon': Radio(
        description='Onboard BCM43455c0 with Nexmon 7.45.206',
        interface='wlan0',
        packages=('brcmfmac-nexmon-dkms', 'firmware-nexmon'),
        dkms_module='brcmfmac-nexmon',
        firmware_patch='firmware/nexmon-ownership/apply.py'),
}

# compatible name -> (display name, onboard radio id)
BOARDS = {
    'raspberrypi,4-model-b': ('Raspberry Pi 4 Model B', 'bcm43455c0-nexmon'),
    'raspberrypi,3-model-b-plus': ('Raspberry Pi 3 Model B+', 'bcm43455c0-nexmon'),
}


class UnsupportedPlatform(ValueError): pass


def current_board(path=COMPATIBLE):
    """The running board's compatible name, or '' when there is no device tree."""
    try: return Path(path).read_bytes().split(b'\0')[0].decode('ascii', 'replace')
    except OSError: return ''


def radio_for(board, radio=None):
    """Return (radio id, Radio) for a supported board; its onboard radio unless one is named."""
    if board not in BOARDS:
        raise UnsupportedPlatform('Unsupported board: %s. Supported: %s.' % (
            board or 'unknown', ', '.join(name for name, _ in BOARDS.values())))
    radio = radio or BOARDS[board][1]
    if radio not in RADIOS: raise UnsupportedPlatform('Unknown radio: ' + radio)
    return radio, RADIOS[radio]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--board', help='compatible name (default: this machine)')
    parser.add_argument('--radio', help='radio id (default: the board\'s onboard radio)')
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--boards', action='store_true', help='list supported compatible names')
    output.add_argument('--field', choices=['radio'] + [field.name for field in fields(Radio)],
                        help='print one value for the selected radio; lists are space-separated')
    args = parser.parse_args()
    if args.boards:
        print(' '.join(BOARDS))
        return 0
    try: name, radio = radio_for(args.board or current_board(), args.radio)
    except UnsupportedPlatform as error:
        print('Error: %s' % error, file=sys.stderr)
        return 1
    value = name if args.field == 'radio' else getattr(radio, args.field)
    print(' '.join(value) if isinstance(value, tuple) else value)
    return 0


if __name__ == '__main__': raise SystemExit(main())
