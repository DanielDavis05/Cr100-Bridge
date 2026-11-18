#!/usr/bin/env python3
"""Installed service supervisor and radio/pairing helpers for the Nexmon radio on Kali.

Supported boards and their radios are listed in radio_platform.py."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

from controller_state import controller_records, merge_controller, normalize_mac, normalize_state

HERE = Path(__file__).resolve().parent
DATA = Path('/var/lib/cr100-bridge')
RUN = Path('/run/cr100-bridge')
OWNERSHIP_STATUS = HERE.parent / 'firmware/nexmon-ownership/status.py'
# Ordinary command waits use subprocess deadlines. Driver GETs additionally
# need their isolated, leased workers; this is not a universal kernel bound.
STARTUP_COMMAND_TIMEOUT = 3.0


def validate_state(path):
    """Reject incomplete pairing output before changing a working household.

    A household may have no controllers: it is created on first run (ensure_household)
    and returns to that when every controller is forgotten.
    """
    state = json.loads(Path(path).read_text())
    try:
        state['bssid'] = normalize_mac(state['bssid'])
    except (KeyError, ValueError):
        raise ValueError('Invalid bssid') from None
    state = normalize_state(state)
    if state['freq'] not in (2412, 2437, 2462): raise ValueError('Invalid radio frequency')
    if not state['household'].startswith('Sonos_') or len(state['household']) > 32:
        raise ValueError('Invalid household')
    return state


def ensure_household(state_path, mac=None):
    """Create the household on first run, so the radio link runs before any pairing.

    Every pairing then happens inside the running link. Nothing can connect until a
    controller pairs: each one gets its own key then.
    """
    if state_path.exists(): return False
    import netstart_pair
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = netstart_pair.write_state(state_path, netstart_pair.household_state(mac or radio_mac()))
    log(event='household-created', household=state['household'], bssid=state['bssid'], freq=state['freq'])
    return True


def check_routes():
    routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'table', 'main'],
                                               text=True, timeout=STARTUP_COMMAND_TIMEOUT))
    network = ipaddress.ip_network('192.168.69.0/24')
    for route in routes:
        destination = route.get('dst', 'default')
        if destination == 'default' or route.get('dev') == 'sonos0': continue
        if ipaddress.ip_network(destination, strict=False).overlaps(network):
            raise RuntimeError('The upstream network overlaps 192.168.69.0/24; use a different upstream subnet.')


def nexmon_loaded():
    """Whether wlan0 runs Nexmon firmware. Kali's brcmfmac-nexmon reports only the version
    (7.45.206) to ethtool; the banner with nexmon.org reaches the kernel log at load."""
    for command in (['ethtool', '-i', 'wlan0'], ['journalctl', '-k', '-b', '-q', '--no-pager', '--grep', 'Firmware: BCM'],
                    ['dmesg']):
        try: text = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL,
                                           timeout=STARTUP_COMMAND_TIMEOUT)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired): continue
        if 'nexmon' in text.lower(): return True
    return False


def prepare_radio(channel=6):
    """Use the driver's current PHY; never unload a driver during normal startup."""
    from sonosnet_link import open_radio
    if not nexmon_loaded():
        raise RuntimeError('Nexmon firmware is not loaded. Reboot after installing the Kali Nexmon packages.')
    subprocess.run(['rfkill', 'unblock', 'wifi'], check=True, timeout=STARTUP_COMMAND_TIMEOUT)
    subprocess.run(['modprobe', 'tun'], check=True, timeout=STARTUP_COMMAND_TIMEOUT)
    subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], check=True, timeout=STARTUP_COMMAND_TIMEOUT)
    # MPC must be off before verifying: with it on, the firmware powers the idle
    # core down within seconds, and the clock/beacon queries then report not ready.
    # Clearing MPC uses the stock Nexmon iovar and sends nothing over the air.
    from nexmon_power import disable_mpc
    previous = disable_mpc()
    log(event='radio-power-save', mpc=0, previous=previous)
    verify_injection_ownership()
    subprocess.run(['iw', 'dev', 'wlan0', 'set', 'power_save', 'off'], check=True,
                   timeout=STARTUP_COMMAND_TIMEOUT)
    open_radio('mon0', channel).close()


def verify_injection_ownership():
    """Require ownership, probe NoAck, clock and hardware beacon stamps before TX."""
    remedy = ('Required Nexmon radio fixes are not loaded. Run the bridge installer to patch the '
              'supported firmware image, then reboot. The radio will remain stopped until GET 430 and '
              'GET 432/433/435/436 verify ownership, probe NoAck, the hardware clock and beacon stamping.')

    def abi(values, expected):
        return isinstance(values, dict) and all(type(values.get(key)) is int and values[key] == value
                                               for key, value in expected.items())

    def valid_beacon_offsets(values):
        if not abi(values, {'magic': 0x514d4853, 'version': 1, 'size': 48, 'status': 1}): return False
        offsets = values.get('offsets_unsigned')
        return (values.get('ready') is True and isinstance(offsets, list) and len(offsets) == 4 and
                all(type(value) is int and value == 0 for value in offsets))

    def valid_beacon_stamp(values):
        if not abi(values, {'magic': 0x51545342, 'version': 1, 'size': 80, 'capabilities': 1}): return False
        fields = ('sequence', 'calls', 'beacon_candidates', 'stamped', 'already_stamped', 'rejected_headers',
                  'ioctl_reads', 'last_valid', 'last_low', 'last_high_before', 'last_high_after',
                  'last_bss_index', 'last_prefix', 'last_length_before', 'last_length_after', 'reserved')
        if not all(type(values.get(field)) is int and 0 <= values[field] < 1 << 32 for field in fields): return False
        if values['sequence'] & 1 or values['reserved'] or values['last_valid'] not in (0, 1): return False
        if not values['last_valid']: return True  # Fresh firmware must identify itself before its first beacon.
        return (values['last_low'] <= 0xffff and (values['last_low'] & 0x81) == 0 and
                values['last_high_before'] <= 0xffff and values['last_high_after'] <= 0xffff and
                values['last_high_after'] == (values['last_high_before'] | 0x80) and
                values['last_bss_index'] <= 3 and values['last_prefix'] in (0, 4, 16) and
                38 <= values['last_length_before'] <= 512 and
                values['last_length_after'] == values['last_length_before'] + 124 + values['last_prefix'])

    try:
        for selection in ('--ownership-only', '--radio-only'):
            result = subprocess.run([sys.executable, str(OWNERSHIP_STATUS), selection, '--timeout', '3'],
                                    capture_output=True, text=True, timeout=5, check=False)
            record = json.loads(result.stdout)
            if not isinstance(record, dict): raise ValueError('Unexpected firmware diagnostic response.')
            if selection == '--ownership-only':
                fields = ('ownership',)
                valid = abi(record.get('ownership'), {'magic': 0x4f4e5243, 'version': 1, 'size': 48})
            else:
                fields = ('noack', 'clock', 'beacon_offsets', 'beacon_stamp')
                clock = record.get('clock') or {}
                valid = (abi(record.get('noack'), {'magic': 0x414e5243, 'version': 1,
                                                  'size': 40, 'capabilities': 1}) and
                         abi(clock, {'magic': 0x51465354, 'version': 1, 'size': 24, 'status': 1}) and
                         clock.get('ready') is True and type(clock.get('tsf_us')) is int and
                         0 <= clock['tsf_us'] < 1 << 64 and
                         valid_beacon_offsets(record.get('beacon_offsets')) and
                         valid_beacon_stamp(record.get('beacon_stamp')))
            if result.returncode or record.get('ok') is not True or not valid:
                errors = [record.get('error')] + [record[field].get('error') for field in fields
                                                 if isinstance(record.get(field), dict)]
                detail = next((error for error in errors if isinstance(error, str) and error),
                              'Unexpected firmware diagnostic reply from ' + selection + '.')
                raise RuntimeError(remedy + ' ' + detail)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as error:
        raise RuntimeError(remedy + ' ' + str(error)) from None


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


RESTART_FIRST = 1.0      # s before a crashed helper (DHCP, players) is started again; doubles
RESTART_MAX = 30.0       # s, the longest wait between restarts
STABLE_AFTER = 60.0      # s a helper must run before its next crash counts as the first again


def log(**record):
    print(json.dumps({'time': round(time.time(), 3), **record}), flush=True)


def supervise(state_path, config_path, run_dir, stop=None):
    """Run the radio link and, on its TAP, DHCP and the virtual players. Only the link is
    fatal (systemd restarts the group): recovery reopens the radio and calibrates its
    hardware clock before injection. DHCP or the players exiting restarts just that helper,
    after RESTART_FIRST s doubling to RESTART_MAX, while the CR100 stays associated."""
    stop = stop or threading.Event()
    run_dir.mkdir(parents=True, exist_ok=True)
    # Old wall-clock samples must not make a new, failed start look healthy.
    for name in ('link.jsonl', 'link.jsonl.1', 'players.pid'):
        (run_dir / name).unlink(missing_ok=True)
    ensure_household(state_path)
    state = validate_state(state_path)
    check_routes()
    prepare_radio({2412: 1, 2437: 6, 2462: 11}[state['freq']])
    pid_path = run_dir / 'players.pid'
    with children() as processes:
        def launch(command):
            process = subprocess.Popen(command, cwd=HERE)
            processes.append(process)
            return process

        link = launch([sys.executable, '-u', str(HERE / 'sonosnet_link.py'), '--state', str(state_path),
                       '--config', str(config_path),
                       '--tap', 'sonos0', '--addr', '192.168.69.1/24', '--log', str(run_dir / 'link.jsonl'),
                       '--run-dir', str(run_dir)])
        deadline = time.monotonic() + 15
        while not stop.is_set():
            if link.poll() is not None: raise RuntimeError('Radio link exited before sonos0 was ready')
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise RuntimeError('Timed out waiting for sonos0')
            shown = subprocess.run(['ip', '-4', 'addr', 'show', 'dev', 'sonos0'],
                                   capture_output=True, text=True, check=False,
                                   timeout=min(STARTUP_COMMAND_TIMEOUT, remaining))
            if 'inet 192.168.69.1/' in shown.stdout: break
            if time.monotonic() >= deadline: raise RuntimeError('Timed out waiting for sonos0')
            stop.wait(0.1)
        if stop.is_set(): return
        helpers = {   # name -> command; each restarts on its own
            'dhcp': ['dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null', '--interface=sonos0',
                     '--except-interface=lo',
                     '--bind-interfaces', '--listen-address=192.168.69.1', '--no-resolv', '--no-hosts',
                     '--address=/update.sonos.com/update-firmware.sonos.com/system-api.sonos.com/192.168.69.1',
                     '--local=/update.sonos.com/update-firmware.sonos.com/system-api.sonos.com/',
                     '--dhcp-range=192.168.69.50,192.168.69.99,255.255.255.0,1h',
                     '--dhcp-authoritative', '--no-ping', '--dhcp-option=3', '--dhcp-option=6,192.168.69.1',
                     '--dhcp-leasefile=' + str(run_dir / 'leases'), '--log-dhcp', '--log-facility=-', '--pid-file='],
            'players': [sys.executable, '-u', str(HERE / 'zoneplayers.py'), '--state', str(state_path),
                        '--config', str(config_path), '--iface', 'sonos0', '--listen-ip', '192.168.69.1',
                        '--base-ip', '192.168.69.11', '--leases-file', str(run_dir / 'leases')],
        }
        running = {}   # name -> {'process', 'started', 'failures', 'due' (restart time while down)}

        def start(name, failures=0):
            # The players' PID is published below before they can install their SIGHUP
            # handler. An ignored signal is inherited through exec, so a reload request
            # that early is dropped (they have not read the config yet) instead of
            # ending them. dnsmasq keeps its own SIGHUP meaning.
            shield = name == 'players' and threading.current_thread() is threading.main_thread()
            if shield: previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
            try:
                process = launch(helpers[name])
            finally:
                if shield: signal.signal(signal.SIGHUP, previous)
            running[name] = {'process': process, 'started': time.monotonic(), 'failures': failures, 'due': None}
            # The web interface signals the players to reload; it must reach the live process.
            if name == 'players': pid_path.write_text(str(process.pid) + '\n')

        for name in helpers: start(name)
        try:
            while not stop.wait(0.25):
                if link.poll() is not None:
                    raise RuntimeError(f'Radio link exited (status {link.returncode})')
                now = time.monotonic()
                for name, helper in running.items():
                    process = helper['process']
                    if helper['due'] is None and process.poll() is not None:
                        failures = 1 if now - helper['started'] >= STABLE_AFTER else helper['failures'] + 1
                        delay = min(RESTART_MAX, RESTART_FIRST * 2 ** (failures - 1))
                        helper.update(failures=failures, due=now + delay)
                        if name == 'players': pid_path.unlink(missing_ok=True)
                        log(event='helper-exited', helper=name, status=process.returncode, restart_in=delay)
                    elif helper['due'] is not None and now >= helper['due']:
                        processes.remove(process)
                        start(name, helper['failures'])
                        log(event='helper-restarted', helper=name, attempt=helper['failures'])
        finally:
            pid_path.unlink(missing_ok=True)


PAIR_ACK_TIMEOUT = 20.0   # s for a running link to take the request (it may still be starting)
PAIR_POLL = 0.25


def bridge_active():
    result = subprocess.run(['systemctl', 'is-active', 'cr100-bridge.service'], capture_output=True,
                            text=True, timeout=STARTUP_COMMAND_TIMEOUT, check=False)
    return result.stdout.strip() == 'active'


def pair_in_link(run_dir, seconds=None, clock=time.monotonic, wall=time.time, sleep=time.sleep):
    """Ask the running link to pair (link_pairing). Return False if it never answered.

    Paired controllers stay connected; on channel 6 or 11 the link visits channel 1
    between beacons while the window is open.
    """
    import link_pairing
    seconds = link_pairing.PAIR_SECONDS if seconds is None else seconds
    request_path, status_path = run_dir / link_pairing.PAIR_REQUEST, run_dir / link_pairing.PAIR_STATUS
    request = {'id': secrets.token_hex(8), 'until': wall() + seconds}
    link_pairing.write_json_atomic(request_path, request)
    try:
        def status():
            try: value = json.loads(status_path.read_text())
            except (OSError, ValueError): return None
            return value if isinstance(value, dict) and value.get('id') == request['id'] else None

        started = clock()
        current = status()
        reported = set()
        while current is None:
            if clock() - started >= PAIR_ACK_TIMEOUT: return False
            sleep(PAIR_POLL)
            current = status()
        hop = ' The bridge visits channel 1 between beacons while it waits.' if current.get('hopping') else ''
        print('Waiting for a CR100 in add-to-household setup mode. Paired controllers stay connected.' + hop,
              flush=True)
        while True:
            for mac in current.get('controllers') or []:
                if mac not in reported:
                    reported.add(mac)
                    print('Paired ' + mac + '.', flush=True)
            state = current.get('state')
            if state == 'done': return True
            if state == 'expired': raise RuntimeError('No CR100 responded within three minutes. Try pairing again.')
            if state == 'failed': raise RuntimeError('Pairing failed: %s' % (current.get('error') or 'unknown error'))
            if wall() > request['until'] + 30: raise RuntimeError('The bridge stopped reporting pairing progress.')
            sleep(PAIR_POLL)
            current = status() or current
    finally:
        request_path.unlink(missing_ok=True)


def pair(state_path, run_dir):
    """Add to the current household; preserve its controllers and their keys on failure."""
    import link_pairing
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / 'pair.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = validate_state(state_path) if state_path.exists() else None
        if previous and bridge_active():
            if pair_in_link(run_dir):
                print('Paired. The bridge kept running.', flush=True)
                return
            print('The running bridge did not take the pairing request; pausing it to pair.', flush=True)
        # First pairing, or no running link: pair on the setup channel with the bridge stopped.
        subprocess.run(['systemctl', 'stop', 'cr100-bridge.service'], check=True)
        fd, temporary = tempfile.mkstemp(prefix='.pair-', suffix='.json', dir=state_path.parent)
        os.close(fd)
        temporary = Path(temporary)
        temporary.unlink()
        try:
            prepare_radio(link_pairing.SETUP_CHANNEL)
            mac = previous['bssid'] if previous else radio_mac()
            # A new household uses the setup channel, so later pairings need no channel visits.
            command = [sys.executable, '-u', str(HERE / 'netstart_pair.py'),
                       '--iface', 'mon0', '--mac', mac,
                       '--freq', str(previous['freq'] if previous else link_pairing.SETUP_FREQ),
                       '--seconds', '180', '--state', str(temporary)]
            if previous:
                command += ['--previous-state', str(state_path)]
            with children() as processes:
                process = subprocess.Popen(command, cwd=HERE)
                processes.append(process)
                code = process.wait(timeout=220)
                if code: raise RuntimeError(f'Pairing helper exited with status {code}')
            if not temporary.exists(): raise RuntimeError('No CR100 responded within three minutes. Try pairing again.')
            paired = validate_state(temporary)
            if previous:
                for field in ('bssid', 'household', 'freq'):
                    if paired[field] != previous[field]:
                        raise ValueError('Pairing cannot change existing household ' + field)
                merged = previous
                for controller in controller_records(paired):
                    merged = merge_controller(merged, controller)
                paired = merged
            temporary.write_text(json.dumps(paired, indent=2) + '\n')
            temporary.chmod(0o600)
            os.replace(temporary, state_path)
            print('Paired. Starting the bridge.', flush=True)
        finally:
            temporary.unlink(missing_ok=True)
            if state_path.exists():
                subprocess.run(['systemctl', '--no-block', 'start', 'cr100-bridge.service'], check=True)


def controller_search(state_path, config_path, mac, mode):
    """Persist one paired controller's proactive-search policy without restarting."""
    from bridge_config import update_config_file
    mac = normalize_mac(mac)
    if mode not in ('pause', 'resume'): raise ValueError('Choose pause or resume')
    state = validate_state(state_path)
    if mac not in {record['mac'] for record in controller_records(state)}:
        raise ValueError('No such paired controller')
    enabled = mode == 'resume'

    def change(config):
        if not isinstance(config, dict): raise ValueError('Invalid bridge configuration')
        profiles = config.get('controllers')
        if profiles is None: profiles = config['controllers'] = {}
        if not isinstance(profiles, dict): raise ValueError('Invalid controller settings')
        profile = profiles.get(mac)
        if profile is None: profile = profiles[mac] = {}
        if not isinstance(profile, dict): raise ValueError('Invalid controller settings')
        profile['auto_reconnect'] = enabled

    update_config_file(config_path, change)
    log(event='controller-auto-reconnect', mac=mac, enabled=enabled)
    return {'mac': mac, 'auto_reconnect': enabled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('radio', 'run', 'pair', 'controller-search'))
    parser.add_argument('controller_mac', nargs='?')
    parser.add_argument('search_mode', nargs='?', choices=('pause', 'resume'))
    parser.add_argument('--state', type=Path, default=DATA / 'bridge-state.json')
    parser.add_argument('--config', type=Path, default=DATA / 'bridge-config.json')
    parser.add_argument('--run-dir', type=Path, default=RUN)
    args = parser.parse_args()
    if args.action == 'controller-search':
        if not args.controller_mac or not args.search_mode: parser.error('controller-search requires MAC and pause|resume')
    elif args.controller_mac or args.search_mode:
        parser.error('Controller arguments require controller-search')
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
        elif args.action == 'controller-search':
            controller_search(args.state, args.config, args.controller_mac, args.search_mode)
        else: supervise(args.state, args.config, args.run_dir, stop)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
