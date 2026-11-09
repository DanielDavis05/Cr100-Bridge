#!/usr/bin/env python3
"""Build a guarded overlay for the exact installed BCM43455c0 Nexmon image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
BASE_SHA256 = '37b46819372d624e9b8153d164857dc983b696919b0899bf8034a1bad65bf832'
PRISTINE_SHA256 = '7fe369f7fdfded9eeceb8e438f7346e58634741b326d0715ad1b15275e6fa3a4'
LEGACY_OWNED_SHA256 = '54484fd4e0c2afc4eeb87e554dc80b76dab26356b7792a3928be6cd93e959cf4'
PREVIOUS_RADIO_SHA256 = 'e88ca542742c49321f2d5d7624e646ef16b5cf830697cebdd7770f548ebd62a9'
SUPPORTED_UPGRADES = (LEGACY_OWNED_SHA256, PREVIOUS_RADIO_SHA256)
BASE_SIZE = 624943
RAM_START, PATCH_START, PATCH_END = 0x198000, 0x22c7f0, 0x2307f0
OVERLAY_START = 0x22d100
OLD_SEND_POINTER = bytes.fromhex('09c92200')
OLD_IOCTL_POINTER = bytes.fromhex('47c92200')
OLD_FILTER_BL = bytes.fromhex('fff731ff')


def sha256(data): return hashlib.sha256(data).hexdigest()


def thumb_bl(address, target):
    offset = (target & ~1) - (address + 4)
    if offset & 1 or not -(1 << 24) <= offset < (1 << 24):
        raise ValueError('Thumb BL target out of range or misaligned')
    value = offset & 0x1ffffff
    sign, i1, i2 = value >> 24, (value >> 23) & 1, (value >> 22) & 1
    first = 0xf000 | (sign << 10) | ((value >> 12) & 0x3ff)
    second = 0xd000 | ((~(i1 ^ sign) & 1) << 13) | ((~(i2 ^ sign) & 1) << 11) | ((value >> 1) & 0x7ff)
    return struct.pack('<HH', first, second)


def checked_base(base, pristine):
    if len(base) != BASE_SIZE or sha256(base) != BASE_SHA256:
        raise ValueError('Only the exact reviewed installed Nexmon firmware is supported')
    if sha256(pristine) != PRISTINE_SHA256:
        raise ValueError('Unexpected pristine firmware used to validate reserved patch storage')
    for address, expected in ((0x2037c0, OLD_SEND_POINTER), (0x20e264, OLD_IOCTL_POINTER),
                              (0x1a1f78, OLD_FILTER_BL), (0x19a3d8, struct.pack('<I', PATCH_START))):
        if base[address - RAM_START:address - RAM_START + len(expected)] != expected:
            raise ValueError('Unexpected firmware bytes at %#x' % address)
    if thumb_bl(0x1a1f78, 0x1a1dde) != OLD_FILTER_BL:
        raise ValueError('Branch encoder disagrees with the reviewed native filter call')
    start, end = OVERLAY_START - RAM_START, PATCH_END - RAM_START
    if len(pristine) != len(base) or pristine[start:end] != base[start:end]:
        raise ValueError('Reserved overlay tail contains existing patch changes')


def build(base_path, nexmon, output, upgrade_path=None):
    base_path, nexmon, output = base_path.resolve(), nexmon.resolve(), output.resolve()
    base = base_path.read_bytes()
    pristine_path = nexmon / 'firmwares/bcm43455c0/7_45_206/brcmfmac43455-sdio.bin'
    pristine = pristine_path.read_bytes()
    checked_base(base, pristine)
    upgrade_paths = ([] if upgrade_path is None else
                     [upgrade_path] if isinstance(upgrade_path, (str, Path)) else list(upgrade_path))
    upgrades = []
    for path in upgrade_paths:
        previous = Path(path).read_bytes()
        digest = sha256(previous)
        if len(previous) != BASE_SIZE or digest not in SUPPORTED_UPGRADES:
            raise ValueError('Only exact supported previous ownership/radio images may be upgraded')
        if any(existing == digest for existing, _ in upgrades):
            raise ValueError('Duplicate previous-image upgrade')
        upgrades.append((digest, previous))
    output.mkdir(parents=True, exist_ok=True)
    platform = 'osx' if sys.platform == 'darwin' else 'linux-x86'
    prefix = nexmon / ('buildtools/gcc-arm-none-eabi-5_4-2016q2-%s/bin/arm-none-eabi-' % platform)
    executable = lambda name: str(prefix) + name
    compiler = subprocess.check_output([executable('gcc'), '--version'], text=True).splitlines()[0]
    if '5.4.1 20160609' not in compiler:
        raise ValueError('The reviewed GCC 5.4.1 toolchain is required')
    linker = output / 'ownership.ld'
    linker.write_text('''ENTRY(ownership_filter)
MEMORY { patch (rwx) : ORIGIN = 0x22d100, LENGTH = 0x36f0 }
SECTIONS {
 .text : ALIGN(4) { *(.text*) *(.rodata*) } > patch
 .data : ALIGN(4) { *(.data*) } > patch
 .bss : ALIGN(4) { *(.bss*) *(COMMON) } > patch
 /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) *(.ARM.attributes*) }
}
ASSERT(SIZEOF(.bss) == 0, "No uninitialized runtime storage is permitted")
''')
    elf, blob_path = output / 'ownership.elf', output / 'ownership.bin'
    relative = lambda path: os.path.relpath(path, HERE)
    sources = ('ownership', 'tsf_query', 'shm_query', 'beacon_stamp')
    objects = [output / (name + '.o') for name in sources]
    commands = [[executable('gcc'), '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', '-ffreestanding',
         '-fno-builtin', '-fno-common', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
         '-mthumb', '-march=armv7-r', '-mno-unaligned-access', '-c', name + '.c', '-o', relative(obj)]
         for name, obj in zip(sources, objects)] + [
        [executable('ld'), '-T', relative(linker), '--build-id=none', '-Map', relative(output / 'ownership.map'),
         '-o', relative(elf), *[relative(obj) for obj in objects]],
        [executable('objcopy'), '-O', 'binary', relative(elf), relative(blob_path)],
    ]
    for command in commands: subprocess.run(command, cwd=HERE, check=True, timeout=60)
    undefined = subprocess.check_output([executable('nm'), '-u', str(elf)], text=True)
    if undefined.strip(): raise ValueError('Unexpected external symbols: ' + undefined)
    symbols = {}
    for line in subprocess.check_output([executable('nm'), '--defined-only', str(elf)], text=True).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] in ('ownership_filter', 'ownership_ioctl', 'ownership_stats', 'noack_stats', 'shm_query', 'beacon_stamp_build', 'beacon_stamp_stats'):
            symbols[parts[2]] = int(parts[0], 16)
    if len(symbols) != 7: raise ValueError('Missing overlay symbols')
    blob = blob_path.read_bytes()
    if not blob or OVERLAY_START + len(blob) > PATCH_END:
        raise ValueError('Overlay exceeds the reserved Nexmon patch region')
    image = bytearray(base)
    changes = [(OVERLAY_START, blob), (0x2037c0, struct.pack('<I', 0x1a1eed)),
               (0x1a1f78, thumb_bl(0x1a1f78, symbols['ownership_filter'])),
               (0x20e264, struct.pack('<I', symbols['ownership_ioctl'] | 1)),
               (0x19849c, struct.pack('<I', symbols['beacon_stamp_build'] | 1))]
    for address, data in changes: image[address - RAM_START:address - RAM_START + len(data)] = data
    allowed = {offset for address, data in changes for offset in range(address - RAM_START, address - RAM_START + len(data))}
    if any(a != b and offset not in allowed for offset, (a, b) in enumerate(zip(base, image))):
        raise ValueError('Firmware changed outside the explicit overlay ranges')
    destination = output / 'cyfmac43455-sdio-standard-owned.bin'
    destination.write_bytes(image)
    disassembly = subprocess.check_output([executable('objdump'), '-d', str(elf)], text=True)
    (output / 'ownership-disassembly.txt').write_text(disassembly)
    native = subprocess.check_output([executable('objdump'), '-D', '-b', 'binary', '-m', 'arm',
        '-M', 'force-thumb', '--adjust-vma=0x198000', '--start-address=0x1a1eec',
        '--stop-address=0x1a2052', str(destination)], text=True)
    (output / 'wl-send-disassembly.txt').write_text(native)
    receipt = {'candidate_only': True, 'base_sha256': BASE_SHA256, 'candidate_sha256': sha256(image),
               'size': len(image), 'compiler': compiler, 'overlay_start': hex(OVERLAY_START),
               'overlay_bytes': len(blob), 'overlay_end': hex(OVERLAY_START + len(blob)),
               'reserved_patch_end': hex(PATCH_END), 'pristine_sha256': sha256(pristine),
               'symbols': {name: hex(address) for name, address in symbols.items()},
               'changes': [{'address': hex(address), 'length': len(data), 'sha256': sha256(data),
                            'previous_sha256': sha256(base[address - RAM_START:address - RAM_START + len(data)])}
                           for address, data in changes],
               'source_sha256': {name: sha256((HERE / name).read_bytes()) for name in ('ownership.c', 'ownership.h', 'tsf_query.c', 'tsf_query.h', 'shm_query.c', 'shm_query.h', 'beacon_stamp.c', 'beacon_stamp.h')},
               'ioctl': {'command': 430, 'magic': 'CRNO', 'version': 1, 'size': 48},
               'noack': {'command': 432, 'magic': '0x414e5243', 'version': 1, 'size': 40,
                         'capabilities': 1, 'packet_tag_bit': '0x1000', 'descriptor_ack_bit': '0x80'},
               'beacon_stamp': {'command': 436, 'magic': '0x51545342', 'version': 1, 'size': 80, 'builder_slot': '0x19849c', 'original_builder': '0x1a63ed', 'high_mask': '0x80'},
               'shm_diagnostic': {'command': 435, 'magic': '0x514d4853', 'version': 1, 'size': 48, 'table_byte_start': '0xeee', 'table_words': 16, 'max_native_reads': 32},
               'clock': {'command': 433, 'magic': '0x51465354', 'version': 1, 'size': 24, 'status_valid': 1},
               'commands': [[part.replace(str(prefix), 'arm-none-eabi-').replace(relative(output), 'build')
                             for part in command] for command in commands]}
    (output / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    manifest = {'format': 1, 'base_sha256': BASE_SHA256, 'patched_sha256': sha256(image),
                'firmware_name': 'cyfmac43455-sdio-standard.bin', 'ram_base': hex(RAM_START),
                'stats': {'command': 430, 'magic': '0x4f4e5243', 'version': 1, 'size': 48},
                'patches': [{'offset': address - RAM_START,
                             'expected': base[address - RAM_START:address - RAM_START + len(data)].hex(),
                             'replacement': data.hex()} for address, data in changes]}
    for digest, previous in upgrades:
        if any(a != b and offset not in allowed for offset, (a, b) in enumerate(zip(previous, image))):
            raise ValueError('Previous image differs outside the reviewed patch ranges')
        manifest.setdefault('upgrades', []).append({'base_sha256': digest, 'patches': [
            {'offset': address - RAM_START,
             'expected': previous[address - RAM_START:address - RAM_START + len(data)].hex(),
             'replacement': data.hex()} for address, data in changes
            if previous[address - RAM_START:address - RAM_START + len(data)] != data]})
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({key: receipt[key] for key in ('candidate_only', 'candidate_sha256', 'size',
                                                 'overlay_start', 'overlay_bytes', 'symbols')}))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--nexmon', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=HERE / 'build')
    parser.add_argument('--upgrade-from', type=Path, action='append',
                        help='Optional exact previous ownership/radio image; repeat for each supported route')
    args = parser.parse_args()
    build(args.base, args.nexmon, args.output, args.upgrade_from)
