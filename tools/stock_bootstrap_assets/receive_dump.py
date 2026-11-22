#!/usr/bin/env python3
"""Receive one framed CR100 NAND read into an immutable, hashed host file."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time

MAGIC = b'CRNAND1\0'
HEADER = struct.Struct('<8s6I')
PAGE_BYTES = 512 + 16


def exact(stream, amount):
    chunks = []
    while amount:
        data = stream.recv(min(amount, 65536))
        if not data:
            raise EOFError(f'stream ended with {amount} bytes still expected')
        chunks.append(data)
        amount -= len(data)
    return b''.join(chunks)


def capture(stream, destination, prefix=b'', source=None):
    destination = Path(destination)
    partial = destination.with_suffix(destination.suffix + '.partial')
    main_status = destination.with_suffix(destination.suffix + '.main-status.bin')
    oob_status = destination.with_suffix(destination.suffix + '.oob-status.bin')
    main_partial = main_status.with_suffix(main_status.suffix + '.partial')
    oob_partial = oob_status.with_suffix(oob_status.suffix + '.partial')
    receipt_path = destination.with_suffix(destination.suffix + '.json')
    for path in (destination, partial, main_status, oob_status,
                 main_partial, oob_partial, receipt_path):
        if path.exists(): raise FileExistsError(path)
    header = prefix + exact(stream, HEADER.size - len(prefix))
    magic, size, writesize, oobsize, erasesize, count, version = HEADER.unpack(header)
    if (magic != MAGIC or size not in (16 << 20, 32 << 20) or
            writesize != 512 or oobsize != 16 or erasesize != 16384 or
            count != size // 512 or version not in (1, 2)):
        raise ValueError(f'unexpected device geometry or stream header: {header.hex()}')
    digest = hashlib.sha256()
    main_digest = hashlib.sha256()
    oob_digest = hashlib.sha256()
    main_errors, oob_errors = [], []
    started = time.time()
    try:
        with ExitStack() as stack:
            def private(path):
                return stack.enter_context(os.fdopen(os.open(path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'))
            out = private(partial)
            if version == 2:
                main_out, oob_out = private(main_partial), private(oob_partial)
                for page in range(count):
                    frame = exact(stream, PAGE_BYTES + 2)
                    main_code, oob_code = frame[0], frame[1]
                    payload = frame[2:]
                    if main_code and any(payload[:512]):
                        raise ValueError(f'page {page}: nonzero main placeholder')
                    if oob_code and any(payload[512:]):
                        raise ValueError(f'page {page}: nonzero OOB placeholder')
                    if main_code: main_errors.append(page)
                    if oob_code: oob_errors.append(page)
                    status = bytes((main_code,))
                    main_out.write(status); main_digest.update(status)
                    status = bytes((oob_code,))
                    oob_out.write(status); oob_digest.update(status)
                    out.write(payload); digest.update(payload)
            else:
                remaining = count * PAGE_BYTES
                while remaining:
                    data = exact(stream, min(remaining, 65536))
                    out.write(data); digest.update(data)
                    remaining -= len(data)
            for file in (out, main_out, oob_out) if version == 2 else (out,):
                file.flush(); os.fsync(file.fileno())
        partial.rename(destination)
        if version == 2:
            main_partial.rename(main_status)
            oob_partial.rename(oob_status)
    except BaseException:
        # A partial capture is retained for diagnosis; never rename it to a
        # completed recovery image.
        raise
    receipt = {
        'source': source, 'path': str(destination.resolve()),
        'bytes': count * PAGE_BYTES, 'sha256': digest.hexdigest(),
        'mtd_data_bytes': size, 'page_data_bytes': writesize,
        'page_oob_bytes': oobsize, 'erase_bytes': erasesize,
        'pages': count, 'stream_version': version,
        'elapsed_seconds': round(time.time() - started, 3),
        'host_received_complete_frame': True,
        'main_error_pages': len(main_errors), 'oob_error_pages': len(oob_errors),
        'main_error_examples': main_errors[:32], 'oob_error_examples': oob_errors[:32],
        'main_status_sha256': main_digest.hexdigest() if version == 2 else None,
        'oob_status_sha256': oob_digest.hexdigest() if version == 2 else None,
        'physical_raw_equivalence': 'unverified: MTD main reads may correct ECC; errors use zero placeholders',
        'recovery_grade_raw_clone': False,
    }
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(json.dumps(receipt, indent=2) + '\n')
    return receipt


def tcp(args):
    if args.bind not in ('127.0.0.1', '::1') and not args.allow:
        raise ValueError('--allow is required when binding beyond loopback')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.bind, args.port))
        listener.listen(1)
        listener.settimeout(args.timeout)
        with listener.accept()[0] as client:
            peer = client.getpeername()[0]
            if args.allow and peer != args.allow:
                raise PermissionError(f'unexpected sender {peer}')
            client.settimeout(args.timeout)
            return capture(client, args.output, source=f'tcp:{peer}')


def serial(args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(args.timeout)
        connection.connect(str(args.serial))
        if args.command:
            connection.sendall(args.command.encode() + b'\r')
        probe = bytearray()
        while MAGIC not in probe:
            probe.extend(connection.recv(4096))
            if len(probe) > 65536:
                del probe[:-65536]
        start = probe.index(MAGIC)
        # The socket may already contain bytes past the header. Keep them.
        buffered = bytes(probe[start:])

        class Buffered:
            def __init__(self): self.head = buffered
            def recv(self, amount):
                if self.head:
                    part, self.head = self.head[:amount], self.head[amount:]
                    return part
                return connection.recv(amount)

        return capture(Buffered(), args.output, source='qemu-serial')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=23177)
    parser.add_argument('--allow', help='only accept this sender IP')
    parser.add_argument('--serial', type=Path, help='QEMU console Unix socket for offline test')
    parser.add_argument('--command', help='command to send to QEMU console before capture')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    receipt = serial(args) if args.serial else tcp(args)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
