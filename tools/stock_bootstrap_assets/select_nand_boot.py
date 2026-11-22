#!/usr/bin/env python3
"""Select native CR100 boot banks and extract bytes from a frozen lab NAND.

Input is raw small-page NAND: 512 data + 16 OOB, 32 pages per eraseblock.
The native first/last-page acceptance policy and the stronger sequential
integrity audit are reported separately. No source file is ever modified.
"""
from __future__ import annotations

import argparse
from functools import reduce
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

PAGE, OOB, BLOCK_PAGES = 512, 16, 32
RAW_PAGE = PAGE + OOB
PT_MAGIC, MDP_MAGIC = 0x653503e4, 0xce10e47d
KINDS = {0: 'end', 1: 'diags', 2: 'reserved', 16: 'kernel',
         17: 'rootfs', 18: 'opt', 64: 'jffs'}
ECC_POS = (0, 1, 2, 3, 6, 7)
PARITY_COLUMNS = (0x55, 0x56, 0x59, 0x5a, 0x65, 0x66, 0x69, 0x6a)
PARITY = tuple(reduce(
    int.__xor__, (column for bit, column in enumerate(PARITY_COLUMNS)
                  if value & (1 << bit)), 0) for value in range(256))


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def nand_ecc(data):
    """Linux 2.4.22 Toshiba software ECC for one 256-byte subpage."""
    reg1 = reg2 = reg3 = 0
    for index, value in enumerate(data):
        parity = PARITY[value]
        reg1 ^= parity & 0x3f
        if parity & 0x40:
            reg3 ^= index
            reg2 ^= index ^ 255
    line = []
    for high in (7, 3):
        result = 0
        for offset in range(4):
            bit = high - offset
            result |= ((reg3 >> bit) & 1) << (7 - offset * 2)
            result |= ((reg2 >> bit) & 1) << (6 - offset * 2)
        line.append(result ^ 255)
    return bytes((*line, (((~reg1) << 2) | 3) & 255))


class Nand:
    def __init__(self, raw):
        if not raw or len(raw) % (RAW_PAGE * BLOCK_PAGES):
            raise ValueError('NAND size must be whole 32-page blocks of 512+16 bytes')
        self.raw = raw
        self.blocks = len(raw) // (RAW_PAGE * BLOCK_PAGES)
        if self.blocks not in (1024, 2048):
            raise ValueError('Only the CR100 driver supported 16/32-MiB geometries are accepted')
        # Small-page memory-based BBT scans OOB badblockpos5 on first TWO pages.
        self.bad = {block for block in range(self.blocks)
                    if any(self.page(block * BLOCK_PAGES + offset)[1][5] != 255
                           for offset in (0, 1))}
        self.good = [block for block in range(self.blocks) if block not in self.bad]
        self.ecc_cache = {}

    def page(self, number):
        start = number * RAW_PAGE
        if number < 0 or start + RAW_PAGE > len(self.raw):
            raise ValueError(f'Physical page {number} exceeds NAND')
        return self.raw[start:start + PAGE], self.raw[start + PAGE:start + RAW_PAGE]

    def ecc_ok(self, number):
        if number not in self.ecc_cache:
            data, spare = self.page(number)
            self.ecc_cache[number] = (bytes(spare[p] for p in ECC_POS) ==
                                      nand_ecc(data[:256]) + nand_ecc(data[256:]))
        return self.ecc_cache[number]

    def ltop(self, logical):
        if logical < 0 or logical >= len(self.good):
            raise ValueError(f'Logical block {logical} exceeds good-block map')
        return self.good[logical]

    def metadata(self, number):
        data, spare = self.page(number)
        generation, sequence = struct.unpack_from('<HI', spare, 10)
        return dict(physical_page=number, physical_block=number // BLOCK_PAGES,
                    raw_offset=number * RAW_PAGE, marker=spare[8:10].hex(),
                    generation=generation, sequence=sequence, ecc_valid=self.ecc_ok(number))


def native_check(nand, start, end):
    """Exact 4.3.2 check_partition: first good page, then physical last page.

    Exact kernel 0x8c09be24..0x8c09bf90 and GPL nand_rincon.c agree:
    sequence checks are absent; the page jump is end*32+31, not page++.
    """
    page = start * BLOCK_PAGES
    final_page = end * BLOCK_PAGES + BLOCK_PAGES - 1
    generation, checked = 0, []
    while page <= final_page:
        if page % BLOCK_PAGES == 0 and page // BLOCK_PAGES in nand.bad:
            page += BLOCK_PAGES
            continue
        meta = nand.metadata(page)
        checked.append(meta)
        if not meta['ecc_valid']:
            return dict(valid=False, error='ECC mismatch; no correction attempted', checked=checked)
        if meta['marker'] not in ('1974', '7419'):
            return dict(valid=False, error='bad magic', checked=checked)
        if not meta['generation']:
            return dict(valid=False, error='generation0', checked=checked)
        if not generation:
            generation = meta['generation']
        if generation != meta['generation']:
            return dict(valid=False, error='generation mismatch', checked=checked)
        if meta['marker'] == '7419':
            return dict(valid=True, generation=generation, checked=checked)
        if page == final_page:
            break
        page = final_page
    return dict(valid=False, error='no native completion marker', checked=checked)


def prefix_check(nand, start, end):
    """Audit actual good-block sequential data through the first end marker."""
    chunks, numbers, generation = [], [], None
    error = None
    for block in range(start, end + 1):
        if block in nand.bad:
            continue
        for offset in range(BLOCK_PAGES):
            number = block * BLOCK_PAGES + offset
            data, spare = nand.page(number)
            meta = nand.metadata(number)
            if not meta['ecc_valid']:
                error = f'ECC mismatch at physical page{number}; no correction attempted'
            elif meta['marker'] not in ('1974', '7419'):
                error = f'bad magic at physical page{number}'
            elif not meta['generation']:
                error = f'generation0 at physical page{number}'
            elif generation is not None and meta['generation'] != generation:
                error = f'generation mismatch at physical page{number}'
            elif meta['sequence'] != len(numbers):
                error = f'sequence mismatch at physical page{number}: expected{len(numbers)}, got{meta["sequence"]}'
            if error:
                return dict(valid=False, error=error, pages_checked=len(numbers) + 1), b'', []
            generation = meta['generation']
            chunks.append(data)
            numbers.append(number)
            if meta['marker'] == '7419':
                return dict(valid=True, generation=generation, pages_checked=len(numbers),
                            logical_data_bytes=len(chunks) * PAGE,
                            completion_page=number,
                            completion_is_physical_partition_last=(number == (end + 1) * BLOCK_PAGES - 1)), b''.join(chunks), numbers
    return dict(valid=False, error='no sequential completion marker', pages_checked=len(numbers)), b'', []


def payload_bytes(kind, content):
    if kind == 16:
        gzip_offset = 0x202c
        if content[gzip_offset:gzip_offset + 3] != b'\x1f\x8b\x08':
            raise ValueError('Kernel lacks original SH4 zImage gzip layout at0x202c')
        if struct.unpack_from('<I', content, 0x34)[0] != 0x8c011000:
            raise ValueError('Kernel zImage has unexpected final Image entry')
        stream = zlib.decompressobj(31)
        image = stream.decompress(content[gzip_offset:])
        if not stream.eof:
            raise ValueError('Kernel gzip stream is incomplete')
        size = len(content) - len(stream.unused_data)
        return content[:size], dict(format='SH4 zImage', gzip_offset=gzip_offset,
                                   decompressed_Image_bytes=len(image),
                                   decompressed_Image_sha256=sha(image))
    if kind == 17:
        if len(content) < 76 or struct.unpack_from('<I', content)[0] != 0x28cd3d45:
            raise ValueError('Rootfs does not have the little-endian CramFS magic')
        size = struct.unpack_from('<I', content, 4)[0]
        if not 76 <= size <= len(content):
            raise ValueError('CramFS superblock size exceeds completed data prefix')
        return content[:size], dict(format='CramFS', superblock_size=size)
    return content, dict(format='opaque opt partition; bounded by completion marker')


def analyze(raw):
    nand = Nand(raw)
    mdp, _ = nand.page(0)
    if struct.unpack_from('<5I', mdp) != (MDP_MAGIC, 1, 2, 3, 3):
        raise ValueError('Expected synthetic CR100 vendor1/model2/submodel3/revision3 MDP')
    if not nand.ecc_ok(0):
        raise ValueError('MDP software ECC mismatch')
    pt_page = nand.ltop(1) * BLOCK_PAGES
    pt, _ = nand.page(pt_page)
    magic, flags = struct.unpack_from('<II', pt)
    if magic != PT_MAGIC or flags & ~1:
        raise ValueError('Missing or unsupported partition table; no fallback layout guessed')
    if not nand.ecc_ok(pt_page):
        raise ValueError('Partition table software ECC mismatch')
    report = dict(geometry=dict(data_bytes=nand.blocks * PAGE * BLOCK_PAGES,
                                page_bytes=PAGE, oob_bytes=OOB,
                                pages_per_block=BLOCK_PAGES, bad_blocks=sorted(nand.bad)),
                  mdp=dict(sha256=sha(mdp), vendor=1, model=2, submodel=3, revision=3),
                  partition_table=dict(sha256=sha(pt), physical_page=pt_page,
                                       relative=bool(flags & 1), flags=flags),
                  native_policy='first good page then physical partition final page; OOB sequence ignored by native kernel',
                  native_policy_evidence=dict(
                      exact_Image_sha256='250a4713f17bf120c705001bfa3537899c046eec8d0539436009285ae56b8e5b',
                      check_partition_entry='0x8c09be24',
                      final_page_jump='0x8c09bf0e..0x8c09bf1a',
                      source='gpl-reference/linux-2.4.22/drivers/mtd/nand/nand_rincon.c'),
                  integrity_policy='all good-block prefix pages checked for ECC, magic, same nonzero generation and sequence through first final marker',
                  partitions=[], banks=[])
    bank, payloads, bank_entries = 0, {}, {0: [], 1: []}
    physical_ranges = []
    for index in range(10):
        kind, start, count = struct.unpack_from('<Iii', pt, 8 + index * 12)
        if kind == 0:
            break
        if kind not in KINDS or start < 0 or count <= 0:
            raise ValueError(f'Unsupported PT entry{index}')
        if flags & 1:
            first, last = nand.ltop(start), nand.ltop(start + count - 1)
        else:
            first, last = start, start + count - 1
        if first < 0 or last >= nand.blocks:
            raise ValueError(f'PT entry{index} exceeds NAND')
        if any(first <= b and a <= last for a, b in physical_ranges):
            raise ValueError(f'PT entry{index} overlaps earlier partition')
        physical_ranges.append((first, last))
        entry = dict(index=index, mtd_index=index + 1, type=kind, name=KINDS[kind],
                     table_start_block=start, table_blocks=count,
                     physical_start_block=first, physical_end_block=last,
                     skipped_bad_blocks=sorted(nand.bad.intersection(range(first, last + 1))))
        report['partitions'].append(entry)
        if kind == 64:
            bank += 1
            if bank > 1:
                raise ValueError('More than two boot sections are unsupported')
        if kind not in (16, 17, 18):
            continue
        entry['bank'] = bank
        entry['native'] = native_check(nand, first, last)
        prefix, content, pages = prefix_check(nand, first, last)
        entry['prefix'] = prefix
        bank_entries[bank].append(entry)
        if prefix['valid']:
            try:
                payload, properties = payload_bytes(kind, content)
                required_pages = (len(payload) + PAGE - 1) // PAGE
                entry['payload'] = dict(bytes=len(payload), sha256=sha(payload),
                                        data_pages=required_pages,
                                        first_physical_page=pages[0],
                                        last_payload_physical_page=pages[required_pages - 1],
                                        **properties)
                payloads[index] = payload
            except (ValueError, zlib.error, struct.error) as error:
                entry['payload_error'] = str(error)
    for number in (0, 1):
        entries = bank_entries[number]
        types = [entry['type'] for entry in entries]
        shape_ok = types.count(16) == 1 and types.count(17) == 1 and types.count(18) <= 1
        generations = [entry['native'].get('generation', 0) for entry in entries]
        native_valid = shape_ok and all(generations) and len(set(generations)) == 1
        integrity = shape_ok and all(entry['prefix']['valid'] and 'payload' in entry
                                    and entry['prefix']['generation'] == entry['native'].get('generation')
                                    for entry in entries)
        report['banks'].append(dict(bank=number, native_valid=bool(native_valid),
                                   generation=generations[0] if native_valid else 0,
                                   complete_kernel_and_rootfs=shape_ok,
                                   strict_integrity_valid=bool(integrity),
                                   partition_indices=[entry['index'] for entry in entries]))
    valid = [bank for bank in report['banks'] if bank['native_valid']]
    if not valid:
        report['error'] = 'No native-valid complete boot bank'
        return report, {}
    selected = max(valid, key=lambda bank: (bank['generation'], -bank['bank']))
    report['selected_bank'] = selected['bank']
    report['selected_generation'] = selected['generation']
    if not selected['strict_integrity_valid']:
        report['error'] = 'Native-selected bank fails stronger extraction integrity audit; refusing boot artifacts'
        return report, {}
    outputs = {'synthetic-mdp.bin': mdp, 'partition-table.bin': pt}
    for entry in bank_entries[selected['bank']]:
        outputs[{16: 'selected-zImage', 17: 'selected-rootfs.cramfs', 18: 'selected-opt.bin'}[entry['type']]] = payloads[entry['index']]
    return report, outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nand', type=Path, required=True, help='frozen/snapshotted raw NAND; never written')
    parser.add_argument('--output', type=Path, required=True, help='new directory; existing paths refused')
    parser.add_argument('--expected-nand-sha')
    parser.add_argument('--expected-kernel-sha')
    parser.add_argument('--expected-root-sha')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Refusing existing output directory')
    before = args.nand.stat()
    raw = args.nand.read_bytes()
    after = args.nand.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        parser.error('Input changed while reading; provide a frozen snapshot')
    source_sha = sha(raw)
    if args.expected_nand_sha and source_sha != args.expected_nand_sha.lower():
        parser.error('Source NAND SHA256 differs from --expected-nand-sha')
    try:
        report, outputs = analyze(raw)
    except (ValueError, struct.error) as error:
        parser.error(str(error))
    report['source'] = dict(path=str(args.nand.resolve()), bytes=len(raw), sha256=source_sha,
                            mtime_ns=after.st_mtime_ns, read_only=True)
    report['expected'] = dict(nand_sha256=args.expected_nand_sha,
                              kernel_sha256=args.expected_kernel_sha,
                              rootfs_sha256=args.expected_root_sha)
    for option, filename in ((args.expected_kernel_sha, 'selected-zImage'),
                             (args.expected_root_sha, 'selected-rootfs.cramfs')):
        if option and filename in outputs and sha(outputs[filename]) != option.lower():
            report['error'] = f'{filename} differs from expected SHA256'
            outputs = {}
    report['outputs'] = {name: dict(bytes=len(blob), sha256=sha(blob)) for name, blob in outputs.items()}
    report['limitations'] = ['No signature verification is inferred from NAND OOB; optional expected hashes pin intended payload bytes.',
                              'ECC mismatches are rejected without correcting or changing any byte.',
                              'Native kernel ignores OOB sequences and checks only first/last pages; stronger extraction audit is separate.']
    args.output.mkdir(parents=True, exist_ok=False)
    for name, blob in outputs.items():
        (args.output / name).write_bytes(blob)
    (args.output / 'selection.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(output=str(args.output.resolve()),
                         selected_bank=report.get('selected_bank'),
                         selected_generation=report.get('selected_generation'),
                         error=report.get('error'), outputs=report['outputs']), indent=2))
    return 2 if 'error' in report else 0


if __name__ == '__main__':
    sys.exit(main())
