#!/usr/bin/env python3
"""Run the tools test suite in parallel, with fast HTTP server shutdown.

Usage: python tools/run_tests.py [tools directory] [--workers N] [--timeout SECONDS]

Each test module (tools/test_*.py, and tools/cr100_flash/tests/test_*.py when present)
runs in its own process, several at once. Tests already bind ephemeral ports and use
temporary directories, so modules are independent.

Inside each process, socketserver's default serve_forever() poll interval drops from
0.5 s to 0.01 s before any test code runs. shutdown() otherwise waits up to one poll
per server, and the integration tests start and stop several loopback servers per
test. Callers that pass their own interval keep it. Production code is not changed.

A module with no unittest tests (test_link.py is a script) is listed, not failed.
Exit status is 1 if any module fails or times out.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
from pathlib import Path
import re
import subprocess
import sys
import time

POLL_INTERVAL = 0.01
NO_TESTS = 5   # unittest's exit status when a module defines no tests

CHILD = r'''
import socketserver, sys, unittest
original = socketserver.BaseServer.serve_forever
def serve_forever(self, poll_interval=%r):
    return original(self, poll_interval)
socketserver.BaseServer.serve_forever = serve_forever
sys.path.insert(0, '.')
unittest.main(module=None, argv=['unittest', sys.argv[1]])
''' % POLL_INTERVAL


def modules(tools):
    found = [path.stem for path in sorted(tools.glob('test_*.py'))]
    flash = tools / 'cr100_flash' / 'tests'
    found += ['cr100_flash.tests.' + path.stem for path in sorted(flash.glob('test_*.py'))]
    return found


def run_module(tools, module, timeout):
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-c', CHILD, module], cwd=tools, capture_output=True,
                                text=True, timeout=timeout)
        code, output = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired as error:
        code = None
        output = ((error.stdout or b'').decode(errors='replace') + (error.stderr or b'').decode(errors='replace')
                  + '\nTimed out after %d s' % timeout)
    ran = re.search(r'^Ran (\d+) tests?', output, re.M)
    skipped = re.search(r'skipped=(\d+)', output)
    return {'module': module, 'code': code, 'ran': int(ran.group(1)) if ran else 0,
            'skipped': int(skipped.group(1)) if skipped else 0,
            'seconds': time.monotonic() - started, 'output': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('tools', nargs='?', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--workers', type=int, default=os.cpu_count() or 4)
    parser.add_argument('--timeout', type=float, default=600, help='seconds allowed for one module')
    parser.add_argument('--slowest', type=int, default=5, help='list this many of the slowest modules')
    args = parser.parse_args()
    tools = args.tools.resolve()
    selected = modules(tools)
    if not selected: parser.error('No test modules in %s' % tools)

    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max(1, args.workers)) as pool:
        futures = [pool.submit(run_module, tools, module, args.timeout) for module in selected]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    wall = time.monotonic() - started

    failed = [r for r in results if r['code'] not in (0, NO_TESTS)]
    empty = sorted(r['module'] for r in results if r['code'] == NO_TESTS)
    for result in sorted(failed, key=lambda r: r['module']):
        status = 'timed out' if result['code'] is None else 'exit %d' % result['code']
        print('===== FAILED %s (%s) =====\n%s' % (result['module'], status, result['output'].rstrip()), flush=True)
    print('Slowest modules:')
    for result in sorted(results, key=lambda r: -r['seconds'])[:args.slowest]:
        print('  %6.1fs  %4d tests  %s' % (result['seconds'], result['ran'], result['module']))
    if empty: print('No unittest tests (run separately): %s' % ', '.join(empty))
    print('%d tests in %d modules (%d skipped), %.1fs with %d workers: %s'
          % (sum(r['ran'] for r in results), len(results), sum(r['skipped'] for r in results), wall,
             args.workers, 'FAILED (%d modules)' % len(failed) if failed else 'OK'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
