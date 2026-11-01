#!/usr/bin/env python3
"""Installed service supervisor and radio/pairing helpers for Pi 4 on Kali."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
DATA = Path('/var/lib/cr100-bridge')
RUN = Path('/run/cr100-bridge')


def validate_state(path):
    """Reject incomplete pairing output before replacing a working household."""
    import re
    state = json.loads(Path(path).read_text())
    for field in ('bssid', 'cr100_mac'):
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', state[field]):
            raise ValueError('Invalid ' + field)
    if len(bytes.fromhex(state['network_key'])) != 16: raise ValueError('Invalid network key')
    if state['freq'] not in (2412, 2437, 2462): raise ValueError('Invalid radio frequency')
    if not state['household'].startswith('Sonos_') or len(state['household']) > 32:
        raise ValueError('Invalid household')
    return state


def check_routes():
    routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'table', 'main'], text=True))
    network = ipaddress.ip_network('192.168.69.0/24')
    for route in routes:
        destination = route.get('dst', 'default')
        if destination == 'default' or route.get('dev') == 'sonos0': continue
        if ipaddress.ip_network(destination, strict=False).overlaps(network):
            raise RuntimeError('The upstream network overlaps 192.168.69.0/24; use a different upstream subnet.')


def prepare_radio(channel=6):
    """Use the driver's current PHY; never unload a driver during normal startup."""
    from sonosnet_link import open_radio
    info = subprocess.check_output(['ethtool', '-i', 'wlan0'], text=True)
    if 'nexmon' not in info.lower():
        raise RuntimeError('Nexmon firmware is not loaded. Reboot after installing the Kali Nexmon packages.')
    subprocess.run(['rfkill', 'unblock', 'wifi'], check=True)
    subprocess.run(['modprobe', 'tun'], check=True)
    subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], check=True)
    subprocess.run(['iw', 'dev', 'wlan0', 'set', 'power_save', 'off'], check=True)
    open_radio('mon0', channel).close()


def radio_mac():
    return Path('/sys/class/net/wlan0/address').read_text().strip()


@contextlib.contextmanager
def children():
    processes = []
    try:
        yield processes
    finally:
        for process in reversed(processes):
            if process.poll() is None: process.terminate()
        deadline = time.monotonic() + 5
        for process in reversed(processes):
            try: process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def supervise(state_path, config_path, run_dir, stop=None):
    """Fail as a unit if any essential child dies; systemd restarts the group."""
    stop = stop or threading.Event()
    state = validate_state(state_path)
    check_routes()
    prepare_radio({2412: 1, 2437: 6, 2462: 11}[state['freq']])
    run_dir.mkdir(parents=True, exist_ok=True)
    # Old wall-clock samples must not make a new, failed start look healthy.
    for name in ('link.jsonl', 'link.jsonl.1', 'players.pid'):
        (run_dir / name).unlink(missing_ok=True)
    with children() as processes:
        def launch(command):
            process = subprocess.Popen(command, cwd=HERE)
            processes.append(process)
            return process

        link = launch([sys.executable, '-u', str(HERE / 'sonosnet_link.py'), '--state', str(state_path),
                       '--tap', 'sonos0', '--addr', '192.168.69.1/24', '--log', str(run_dir / 'link.jsonl')])
        deadline = time.monotonic() + 15
        while not stop.is_set():
            if link.poll() is not None: raise RuntimeError('Radio link exited before sonos0 was ready')
            shown = subprocess.run(['ip', '-4', 'addr', 'show', 'dev', 'sonos0'],
                                   capture_output=True, text=True, check=False)
            if 'inet 192.168.69.1/' in shown.stdout: break
            if time.monotonic() >= deadline: raise RuntimeError('Timed out waiting for sonos0')
            stop.wait(0.1)
        if stop.is_set(): return
        launch(['dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null', '--interface=sonos0',
                '--bind-interfaces', '--port=0', '--dhcp-range=192.168.69.50,192.168.69.99,255.255.255.0,1h',
                '--dhcp-authoritative', '--no-ping', '--dhcp-option=3', '--dhcp-option=6',
                '--dhcp-leasefile=' + str(run_dir / 'leases'), '--log-dhcp', '--log-facility=-', '--pid-file='])
        players = launch([sys.executable, '-u', str(HERE / 'zoneplayers.py'), '--state', str(state_path),
                          '--config', str(config_path), '--iface', 'sonos0', '--listen-ip', '192.168.69.1',
                          '--base-ip', '192.168.69.11'])
        pid_path = run_dir / 'players.pid'
        pid_path.write_text(str(players.pid) + '\n')
        try:
            while not stop.wait(0.25):
                for process in processes:
                    if process.poll() is not None:
                        raise RuntimeError(f'Essential bridge process exited: {process.args[0:3]} (status {process.returncode})')
        finally:
            pid_path.unlink(missing_ok=True)


def pair(state_path, run_dir):
    """Keep the old pairing on timeout/failure and restart it after the attempt."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / 'pair.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['systemctl', 'stop', 'cr100-bridge.service'], check=True)
        fd, temporary = tempfile.mkstemp(prefix='.pair-', suffix='.json', dir=state_path.parent)
        os.close(fd)
        temporary = Path(temporary)
        temporary.unlink()
        try:
            prepare_radio(1)
            mac = radio_mac()
            with children() as processes:
                process = subprocess.Popen([sys.executable, '-u', str(HERE / 'netstart_pair.py'),
                                            '--iface', 'mon0', '--mac', mac, '--freq', '2437',
                                            '--seconds', '180', '--state', str(temporary)], cwd=HERE)
                processes.append(process)
                code = process.wait(timeout=220)
                if code: raise RuntimeError(f'Pairing helper exited with status {code}')
            if not temporary.exists(): raise RuntimeError('No CR100 responded within three minutes. Try pairing again.')
            validate_state(temporary)
            temporary.chmod(0o600)
            os.replace(temporary, state_path)
            print('Paired. Starting the bridge.', flush=True)
        finally:
            temporary.unlink(missing_ok=True)
            if state_path.exists():
                subprocess.run(['systemctl', '--no-block', 'start', 'cr100-bridge.service'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('radio', 'run', 'pair'))
    parser.add_argument('--state', type=Path, default=DATA / 'bridge-state.json')
    parser.add_argument('--config', type=Path, default=DATA / 'bridge-config.json')
    parser.add_argument('--run-dir', type=Path, default=RUN)
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    def terminate(*_):
        if args.action == 'run': stop.set()
        else: raise InterruptedError('Service stopped')
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        if args.action == 'radio': prepare_radio()
        elif args.action == 'pair': pair(args.state, args.run_dir)
        else: supervise(args.state, args.config, args.run_dir, stop)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
