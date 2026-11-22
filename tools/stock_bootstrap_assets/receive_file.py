#!/usr/bin/env python3
"""Receive one CRFILE2 stream to a restricted host file with a size cap."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time


def exact(stream, n):
    out = bytearray()
    while len(out) < n:
        part = stream.recv(n - len(out))
        if not part: raise EOFError('file stream ended early')
        out.extend(part)
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=23179)
    parser.add_argument('--allow', help='expected sender IP; required beyond loopback')
    parser.add_argument('--max-bytes', type=int, default=64 << 20)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    if args.bind not in ('127.0.0.1', '::1') and not args.allow:
        parser.error('--allow is required beyond loopback')
    output = args.output
    partial = output.with_suffix(output.suffix + '.partial')
    receipt_path = output.with_suffix(output.suffix + '.json')
    for path in (output, partial, receipt_path):
        if path.exists(): raise FileExistsError(path)
    start = time.time(); digest = hashlib.sha256(); size = 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.bind, args.port));server.listen(1)
        server.settimeout(args.timeout)
        with server.accept()[0] as peer:
            source = peer.getpeername()[0]
            if args.allow and source != args.allow:
                raise PermissionError(f'unexpected sender {source}')
            peer.settimeout(args.timeout)
            if exact(peer, 8) != b'CRFILE2\0':
                raise ValueError('wrong file stream magic')
            with os.fdopen(os.open(partial,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
                while True:
                    length, = struct.unpack('<I', exact(peer, 4))
                    if length == 0: break
                    if length > 4096 or size + length > args.max_bytes:
                        raise ValueError('file stream exceeded frame or total cap')
                    data = exact(peer, length)
                    out.write(data);digest.update(data);size += length
                out.flush();os.fsync(out.fileno())
    partial.rename(output)
    receipt = {'path': str(output.resolve()), 'bytes': size,
               'sha256': digest.hexdigest(), 'source': source,
               'elapsed_seconds': round(time.time() - start, 3),
               'framed_complete': True, 'file_content_only': True}
    with os.fdopen(os.open(receipt_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        out.write(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__': main()
