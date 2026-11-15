#!/usr/bin/env python3
"""Compile the controller preview's ActionScript host layer into host-actions.json.

Developer tool, run after editing bootstrap.as, music.as or hooks.as. It needs a JDK
and JPEXS (ffdec.jar); the bridge itself never compiles ActionScript. The compiled
DoAction bytecode is our own code and does not depend on the user's firmware, so
controller_preview.py injects it into their MainUI.swf at run time.

usage: build_host.py --ffdec /path/to/ffdec.jar
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / 'host-actions.json'
# (output field, source files compiled together as one DoAction)
UNITS = (('bootstrap', ('bootstrap.as', 'music.as')), ('hooks', ('hooks.as',)))


def tag(code, payload):
    return struct.pack('<HI', (code << 6) | 63, len(payload)) + payload


def tags(data):
    position = 0
    while position < len(data):
        header = struct.unpack_from('<H', data, position)[0]
        position += 2
        code, size = header >> 6, header & 63
        if size == 63:
            size = struct.unpack_from('<I', data, position)[0]
            position += 4
        yield code, data[position:position + size]
        position += size


def seed_movie():
    """An empty SWF 6 movie (320x240, 12 fps) with one DoAction to replace.

    MainUI.swf is SWF 6; the version decides which actions JPEXS may emit.
    """
    bits, values = 15, (0, 320 * 20, 0, 240 * 20)
    rect = bits
    for value in values:
        rect = (rect << bits) | value
    total = 5 + 4 * bits
    rect <<= (-total) % 8
    rect = rect.to_bytes((total + 7) // 8, 'big')
    body = rect + struct.pack('<HH', 12 << 8, 1) + tag(12, b'\0') + tag(1, b'') + tag(0, b'')
    return b'FWS\x06' + struct.pack('<I', 8 + len(body)) + body


def header_end(movie):
    return 8 + (5 + (movie[8] >> 3) * 4 + 7) // 8 + 4


def compile_unit(source, ffdec, work):
    scripts = work / 'scripts/frame_1'
    scripts.mkdir(parents=True)
    (scripts / 'DoAction.as').write_text(source)
    (work / 'seed.swf').write_bytes(seed_movie())
    subprocess.run(['java', '-jar', str(ffdec), '-onerror', 'abort', '-importScript', str(work / 'seed.swf'),
                    str(work / 'compiled.swf'), str(work / 'scripts')],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    compiled = (work / 'compiled.swf').read_bytes()
    if compiled[:4] != b'FWS\x06':
        raise ValueError('Expected an uncompressed SWF 6 from JPEXS')
    return next(payload for code, payload in tags(compiled[header_end(compiled):]) if code == 12)


def sources_digest():
    digest = hashlib.sha256()
    for _, names in UNITS:
        for name in names:
            digest.update(name.encode() + b'\0' + (HERE / name).read_bytes() + b'\0')
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ffdec', type=Path, required=True)
    args = parser.parse_args()
    result = {'format': 1, 'swf_version': 6, 'sources_sha256': sources_digest()}
    with tempfile.TemporaryDirectory() as temporary:
        for field, names in UNITS:
            source = '\n'.join((HERE / name).read_text() for name in names)
            result[field] = compile_unit(source, args.ffdec, Path(temporary) / field).hex()
    OUTPUT.write_text(json.dumps(result, indent=1) + '\n')
    print(OUTPUT, {field: len(result[field]) // 2 for field, _ in UNITS})


if __name__ == '__main__':
    main()
