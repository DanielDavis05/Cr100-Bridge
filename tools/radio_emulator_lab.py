#!/usr/bin/env python3
"""Disposable CR100 Radio lab; never opens an existing emulator disk writable.

Each run owns an immutable baseline copy, qcow2 overlay, loopback forwards,
sockets, token, private QEMU hub, and evidence directory. The original firmware
runs in the guest; screenshots are the real framebuffer, not an HTML recreation.

A run prepared with `peer='host'` keeps only the controller in the SH4
emulator. The other end of the private segment (bridge, fake services, test
drivers) runs at native speed in the helper VM from `lab_peer_vm.py`, and
`provision_host_peer` trims the guest's boot to the controller's own needs.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, os, secrets, shutil, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace
import lab_peer_vm
EMULATOR = workspace.EMULATOR
DEFAULT_BASE = EMULATOR / 'vm/sh4-fallback/rootfs-fast-input-ready.ext4'
RELAY = ('10.0.2.100', 7777)      # guest-side address of the host input pipe (management NIC only)
FRAME_BYTES, PAGE = 153600, 4096  # 320x240 RGB565; SH4 page size
KEYS = {'soft1': '1', 'soft2': '2', 'soft3': '3', 'ok': 'o', 'back': 'b', 'music': 'm', 'zones': 'r', 'play_pause': 'p',
        'previous': '<', 'next': '>', 'stop': '.', 'volume_up': 'V', 'volume_down': 'v', 'mute': 'x', 'up': 'U',
        'down': 'D', 'motion': '!', 'release': '~'}
HELD = {'previous', 'next', 'volume_up', 'volume_down', 'mute'}


def _json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(directory, baseline=DEFAULT_BASE, control_port=22621, agent_port=22622, peer='guest', peer_agent_port=None):
    directory = Path(directory).resolve()
    if peer not in ('guest', 'host'):
        raise ValueError("peer is 'guest' (software speaker inside the emulator) or 'host' (native helper VM).")
    if peer == 'host' and peer_agent_port in (None, control_port, agent_port):
        raise ValueError('A host peer needs its own agent port.')
    if directory.exists():
        raise ValueError('Choose a new run directory; existing evidence is never replaced.')
    if Path(baseline).resolve().name in ('rootfs.ext4', 'controller-4.3-working.ext4'):
        raise ValueError('Use a stopped immutable template, never a working emulator disk.')
    for port in (control_port, agent_port) + ((peer_agent_port,) if peer == 'host' else ()):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    if control_port == agent_port:
        raise ValueError('Control and setup ports must differ.')
    directory.mkdir(parents=True, mode=0o700)
    destination = directory / 'baseline.ext4'
    # APFS clone when available; fall back to a regular independent copy.
    copied = subprocess.run(['cp', '-c', str(baseline), str(destination)], capture_output=True)
    if copied.returncode:
        shutil.copyfile(baseline, destination)
    destination.chmod(0o444)
    subprocess.run(['/opt/homebrew/bin/qemu-img', 'create', '-f', 'qcow2', '-F', 'raw',
                    '-b', str(destination), str(directory / 'overlay.qcow2')], check=True,
                   stdout=subprocess.DEVNULL)
    token = secrets.token_hex(32) + '\n'
    (directory / 'lab_token').write_text(token)
    (directory / 'lab_token').chmod(0o600)
    manifest = {'version': 1, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'directory': str(directory), 'baseline_source': str(Path(baseline).resolve()),
                'baseline_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                'control_port': control_port, 'agent_port': agent_port,
                'household': 'Sonos_TUNEIN_RADIO_LAB', 'private_hub': 97,
                'management_mac': '52:54:00:97:34:56',
                'controller_mac': '02:00:00:97:01:00', 'peer_mac': '02:00:00:97:00:80',
                'identity_bootstrapped': False}
    if peer == 'host':
        # Unix socket paths are limited to about 100 bytes; run directories are longer.
        short = Path(tempfile.gettempdir()) / ('cr100lab-' + hashlib.sha256(str(directory).encode()).hexdigest()[:10])
        manifest.update(peer='host', peer_agent_port=peer_agent_port, sockets=str(short), host_peer_provisioned=False)
    _json(directory / 'manifest.json', manifest)
    return manifest


def _sockets(d, m):
    path = Path(m.get('sockets', d))
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def config(directory):
    directory = Path(directory).resolve()
    m = json.loads((directory / 'manifest.json').read_text())
    if m.get('directory') != str(directory):
        raise ValueError('Run manifest directory mismatch.')
    return directory, m


def start(directory):
    d, m = config(directory)
    s, host = _sockets(d, m), m.get('peer') == 'host'
    if (s / 'qmp.sock').exists():
        raise ValueError('Run already owns a QMP socket; inspect or stop it first.')
    (s / 'link.sock').unlink(missing_ok=True)
    unique = m['identity_bootstrapped']
    macs = [m['management_mac'], m['controller_mac'], m['peer_mac']] if unique else [
        '52:54:00:12:34:56', '02:00:00:00:01:00', '02:00:00:00:00:80']
    relay = []
    if host:
        # Host-driven input and screen reads (see Input and Screen): a byte pipe into the
        # guest on the management NIC, and a second QMP socket for memory reads.
        for name in ('input.sock', 'mem.sock'): (s / name).unlink(missing_ok=True)
        relay = ['-chardev', 'socket,id=inputrelay,path=%s,server=on,wait=off' % (s / 'input.sock'),
                 '-qmp', 'unix:%s,server=on,wait=off' % (s / 'mem.sock')]
    args = ['/opt/homebrew/bin/qemu-system-sh4', '-M', 'r2d', '-cpu', 'sh7785', '-m', '128M',
            '-display', 'none', '-monitor', 'none', *relay, '-nic',
            'user,model=rtl8139,mac=%s,restrict=on,hostfwd=tcp:127.0.0.1:%d-:22345,hostfwd=tcp:127.0.0.1:%d-:22346%s' %
            (macs[0], m['control_port'], m['agent_port'], ',guestfwd=tcp:%s:%d-chardev:inputrelay' % RELAY if host else ''),
            '-kernel', str(EMULATOR / 'vm/sh4-fallback/zImage'),
            '-append', 'printk.time=0 root=/dev/sda console=ttySC1',
            '-drive', 'file=%s,format=qcow2,if=ide,index=0' % (d / 'overlay.qcow2'),
            '-chardev', 'socket,id=labconsole,path=%s,server=on,wait=off,logfile=%s' %
            (s / 'console.sock', d / ('serial-%d.log' % int(time.time()))),
            '-serial', 'null', '-serial', 'chardev:labconsole',
            '-qmp', 'unix:%s,server=on,wait=off' % (s / 'qmp.sock'),
            '-netdev', 'hubport,id=cr100radio,hubid=97', '-device', 'rtl8139,netdev=cr100radio,mac='+macs[1]]
    if host:
        # The peer is not a guest NIC: the hub's second port is a local socket to the helper VM.
        args += ['-netdev', 'stream,id=peerlink,server=on,addr.type=unix,addr.path=%s' % (s / 'link.sock'),
                 '-netdev', 'hubport,id=zp80radio,hubid=97,netdev=peerlink']
    else:
        args += ['-netdev', 'hubport,id=zp80radio,hubid=97', '-device', 'rtl8139,netdev=zp80radio,mac='+macs[2]]
    args += ['-object', 'filter-dump,id=radiocapture,netdev=cr100radio,file=%s' %
            (d / ('network-%d.pcap' % int(time.time())))]
    with (d / 'qemu.log').open('ab') as log:
        process = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    _json(d / 'process.json', {'pid': process.pid, 'args': args})
    result = {'pid': process.pid, 'agent_port': m['agent_port'], 'control_port': m['control_port']}
    if host:
        deadline = time.monotonic() + 20
        while not (s / 'link.sock').exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Emulator did not open the peer link; see %s' % (d / 'qemu.log'))
            time.sleep(.1)
        try:
            result['peer'] = lab_peer_vm.start(d, s / 'link.sock', macs[2], m['peer_agent_port'], sockets=s)
        except Exception:
            # Never leave half a lab running: without its peer the emulator is of no use.
            lab_peer_vm.stop(d); process.terminate()
            for name in ('qmp.sock', 'console.sock', 'link.sock', 'input.sock', 'mem.sock'): (s / name).unlink(missing_ok=True)
            raise
    return result


def provision_identity(directory):
    """Bootstrap only the owned guest using its serial console and fresh token.

    No setup token is read from another emulator. This first boot uses the
    template MACs solely inside its unconnected private QEMU hub; subsequent
    boots use the run's distinct MACs and household identity.
    """
    d, m = config(directory)
    if m['identity_bootstrapped']:
        raise ValueError('This run already has its isolated identity.')
    sys.path.insert(0, str(EMULATOR / 'network'))
    import console
    import shlex
    replacement = {'52:54:00:12:34:56': m['management_mac'],
                   '02:00:00:00:01:00': m['controller_mac'],
                   '02:00:00:00:00:80': m['peer_mac'],
                   'Sonos_EMULATED_CR100': m['household']}
    payload = ("from pathlib import Path\nimport os\nreplacements=" + repr(replacement) + "\n"
        "for name in ['/etc/init.d/S98cr100lab','/etc/init.d/S99cr100','/opt/cr100-lab/lab_net.py',"
        "'/opt/cr100-control/control_server.py','/crroot/jffs/localsettings.txt']:\n"
        " p=Path(name);value=p.read_text()\n"
        " for old,new in replacements.items():value=value.replace(old,new)\n"
        " p.write_text(value)\n"
        "p=Path('/opt/cr100-lab/lab_token');p.write_text(" + repr((d / 'lab_token').read_text()) + ")\n"
        "os.chmod(str(p),0o600)\nos.sync()\nprint('RADIO_IDENTITY_READY')\n")
    command = '/usr/bin/python3 -c ' + shlex.quote('import base64;exec(base64.b64decode(' +
        repr(base64.b64encode(payload.encode()).decode()) + '))')
    # Serial echo contains the new run's private bootstrap material. Restrict
    # evidence files, discard echoed command text, and never return that output.
    for path in d.glob('serial-*.log'): path.chmod(0o600)
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(_sockets(d, m) / 'console.sock')); client.sendall(b'\r')
        reply = console.receive(client, 15)
        if b'login:' in reply and not console.PROMPT.search(reply):
            console.paced(client, b'root\r'); reply += console.receive(client, 15)
        if not console.PROMPT.search(reply): raise RuntimeError('Owned guest console is not ready.')
        console.paced(client, command.encode() + b'\r')
        reply = console.receive(client, 45)
        if b'RADIO_IDENTITY_READY' not in reply: raise RuntimeError('Guest identity provisioning did not complete.')
    m['identity_bootstrapped'] = True
    _json(d / 'manifest.json', m)
    return {'identity_provisioned': True, 'household': m['household'], 'requires_restart': True}


def provision_host_peer(directory):
    """Once per overlay: the guest's boot starts the controller's namespace only.

    The stock lab boot hook also makes a `peer` namespace and starts the Python
    software speaker in it. With a host peer neither exists, so the hook would
    otherwise skip the network setup the controller needs. Restart afterwards.
    """
    import re
    d, m = config(directory)
    if m.get('peer') != 'host': raise ValueError('This run was not prepared with a host peer.')
    if m.get('host_peer_provisioned'): raise ValueError('This run is already provisioned.')
    read = lambda path: base64.b64decode(call(directory, {'op': 'get', 'path': path})['data']).decode()
    def write(path, text, mode):
        call(directory, {'op': 'put', 'path': path, 'mode': mode, 'data': base64.b64encode(text.encode()).decode()})
    hook = read('/etc/init.d/S98cr100lab')
    lean = hook.replace('if [ "$cr100_radio" = 1 ] && [ "$peer_radio" = 1 ]; then', 'if [ "$cr100_radio" = 1 ]; then')
    lean = re.sub(r'\n\s*if \[ -f /run/cr100-network-lab\.ready \] && \[ -f /opt/cr100-lab/zp80_peer\.py \]; then\n.*?\n\s*fi\n',
                  '\n', lean, flags=re.S)
    network = read('/opt/cr100-lab/lab_net.py')
    single = re.sub(r",\s*'peer':\([^)]*\)", '', network)
    if lean == hook or 'zp80_peer' in lean.split('stop)')[0] or single == network or "'peer'" in single.split('BASE=')[0]:
        raise RuntimeError('Guest boot files are not the expected lab hook; nothing was changed.')
    write('/etc/init.d/S98cr100lab', lean, 0o755)
    write('/opt/cr100-lab/lab_net.py', single, 0o644)
    execute(directory, ['/bin/sync'])
    m['host_peer_provisioned'] = True
    _json(d / 'manifest.json', m)
    return {'host_peer_provisioned': True, 'requires_restart': True}


def call(directory, request, timeout=75):
    d, m = config(directory)
    request = dict(request, token=(d / 'lab_token').read_text().strip())
    with socket.create_connection(('127.0.0.1', m['agent_port']), timeout=5) as client:
        client.settimeout(timeout)
        client.sendall(json.dumps(request).encode()+b'\n')
        with client.makefile('rb') as stream:
            result = json.loads(stream.readline(4 * 1024 * 1024 + 1))
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'Guest operation failed.'))
    return result


def execute(directory, argv, namespace=None, timeout=60):
    result = call(directory, {'op': 'exec', 'argv': argv, 'namespace': namespace, 'timeout': timeout})
    return {'returncode': result['returncode'],
            'stdout': base64.b64decode(result['stdout']).decode(errors='replace'),
            'stderr': base64.b64decode(result['stderr']).decode(errors='replace')}


def put(directory, source, destination, mode=0o644):
    return call(directory, {'op': 'put', 'path': destination, 'mode': mode,
                           'data': base64.b64encode(Path(source).read_bytes()).decode()})


def control(directory, operation):
    _, m = config(directory)
    with socket.create_connection(('127.0.0.1', m['control_port']), timeout=5) as client:
        client.settimeout(30)
        client.sendall(json.dumps(operation).encode()+b'\n')
        with client.makefile('rb') as stream:
            header = json.loads(stream.readline(65537))
            if not header.get('ok'):
                raise RuntimeError(header.get('error', 'Control failed.'))
            data = stream.read(header.get('size', 0))
    return header, data


def key(directory, name):
    return control(directory, {'key': name})[0]


def capture(directory, destination):
    header, data = control(directory, {'key': 'snapshot'})
    sys.path.insert(0, str(EMULATOR))
    from viewer import convert
    Path(destination).write_bytes(convert(data))
    return header


def qmp(directory, command, **arguments):
    """One QMP command to this run's emulator (for example pmemsave or quit)."""
    d, m = config(directory)
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(_sockets(d, m) / 'qmp.sock'))
        with client.makefile('rwb', buffering=0) as stream:
            stream.readline()
            stream.write(b'{"execute":"qmp_capabilities"}\n'); stream.readline()
            stream.write(json.dumps({'execute': command, 'arguments': arguments}).encode() + b'\n')
            while True:
                reply = json.loads(stream.readline() or b'{"return":{}}')
                if 'event' not in reply: return reply


def stop(directory):
    d, m = config(directory)
    if m.get('peer') == 'host': lab_peer_vm.stop(d)
    execute(directory, ['/bin/sync'])
    qmp(directory, 'quit')
    s = _sockets(d, m)
    for name in ('qmp.sock', 'console.sock', 'link.sock', 'input.sock', 'mem.sock'): (s / name).unlink(missing_ok=True)
    return {'stopped': str(d)}


class Screen:
    """The controller's published framebuffer, read from guest RAM by QEMU itself.

    A capture through the guest's control service costs emulated CPU time for
    every frame. Here the guest is asked once (a short shell command) which
    physical pages back the display file the controller maps; after that each
    read is a QEMU memory dump on the host and the guest does not run any code
    for it. Call locate() again after the controller app restarts.
    """
    def __init__(self, directory, crroot='/crroot'):
        self.directory, self.crroot = directory, crroot
        d, m = config(directory)
        self.sockets, self.pages, self.lock = _sockets(d, m), None, threading.Lock()

    def locate(self):
        query = ("for pid in $(pidof hh.anacapad); do line=$(grep ramdisk/tmp/cr100-fb.raw /proc/$pid/maps | head -1); "
                 "[ -n \"$line\" ] || continue; "
                 "dd if=/proc/$pid/pagemap bs=8 skip=$((0x${line%%-*} / 4096)) count=38 2>/dev/null | xxd -p | tr -d '\\n'; break; done")
        raw = bytes.fromhex(execute(self.directory, ['/bin/sh', '-c', query])['stdout'].strip())
        entries = [int.from_bytes(raw[i:i + 8], 'little') for i in range(0, len(raw), 8)]
        if len(entries) != 38 or not all(entry >> 63 for entry in entries):
            raise RuntimeError('Controller display is not mapped yet.')
        self.pages = [(entry & ((1 << 55) - 1)) * PAGE for entry in entries]
        return self

    def read(self, offset=0, length=FRAME_BYTES):
        """`length` framebuffer bytes from `offset` (row * 640 + column * 2)."""
        if self.pages is None: self.locate()
        first, last = offset // PAGE, (offset + length - 1) // PAGE
        data, index, scratch = b'', first, self.sockets / ('frame-%d.bin' % os.getpid())
        # One short connection per read: the socket serves one client at a time,
        # and several scripts of a session take turns on it.
        with self.lock, socket.socket(socket.AF_UNIX) as client:
            client.connect(str(self.sockets / 'mem.sock'))
            with client.makefile('rwb', buffering=0) as stream:
                def command(name, **arguments):
                    stream.write(json.dumps({'execute': name, 'arguments': arguments}).encode() + b'\n')
                    while True:
                        reply = json.loads(stream.readline())
                        if 'error' in reply: raise RuntimeError(reply['error'].get('desc', 'QMP error'))
                        if 'return' in reply: return
                stream.readline(); command('qmp_capabilities')
                while index <= last:
                    run = 1   # physically consecutive pages come out in one dump
                    while index + run <= last and self.pages[index + run] == self.pages[index] + run * PAGE: run += 1
                    command('pmemsave', val=self.pages[index], size=run * PAGE, filename=str(scratch))
                    data += scratch.read_bytes(); index += run
        return data[offset - first * PAGE:][:length]

    def capture(self, destination):
        sys.path.insert(0, str(EMULATOR))
        from viewer import convert
        Path(destination).write_bytes(convert(self.read()))



class Input:
    """Keypad and wheel events written to the controller's input FIFO from the host.

    A busybox `wget` in the guest copies one endless response from the host
    pipe into the FIFO, so no interpreter runs in the guest per event and the
    host clock decides the timing. attach() again after the controller's
    launcher restarts (it owns the FIFO's reading end).
    """
    def __init__(self, directory, crroot='/crroot'):
        self.directory, self.fifo = directory, crroot + '/dev/hhkbd'
        d, m = config(directory)
        self.path = _sockets(d, m) / 'input.sock'

    def _pipe(self):
        # One short connection per event; the guest's side of the pipe stays open between them.
        client = socket.socket(socket.AF_UNIX); client.connect(str(self.path))
        return client

    def attach(self):
        with self._pipe() as client:
            execute(self.directory, ['/bin/sh', '-c',
                'test -f /run/input-relay.pid && kill $(cat /run/input-relay.pid) 2>/dev/null; '
                'setsid wget -q -O %s http://%s:%d/ </dev/null >/dev/null 2>&1 & echo $! >/run/input-relay.pid' % ((self.fifo,) + RELAY)])
            client.settimeout(30); seen = b''
            while not seen.endswith(b'\r\n\r\n'):   # the guest's request; a stale one from an earlier relay may precede it
                chunk = client.recv(4096)
                if not chunk: raise RuntimeError('Input relay closed.')
                seen += chunk
            client.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n')
        return self

    def key(self, name):
        if name in ('wheel_up', 'wheel_down'): return self.wheel(-1 if name == 'wheel_up' else 1)
        with self._pipe() as client:
            client.sendall(KEYS[name].encode() + b'\0\0')
            if name in HELD:
                time.sleep(.08); client.sendall(KEYS['release'].encode() + b'\0\0')

    def send(self, packets):
        """Raw three-byte packets in one write, for bursts that must reach the FIFO together."""
        with self._pipe() as client: client.sendall(packets)

    def wheel(self, steps):
        """One wheel event of `steps` detents (negative is the direction the original driver calls up)."""
        with self._pipe() as client:
            client.sendall(b'w' + int(steps).to_bytes(2, 'little', signed=True))


def peer_execute(directory, argv, timeout=60):
    """Run a command in the native helper VM of a `peer='host'` run."""
    return lab_peer_vm.execute(directory, argv, timeout)


def peer_shell(directory, command, timeout=60, check=True):
    return lab_peer_vm.shell(directory, command, timeout, check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--host-peer', type=int, metavar='AGENT_PORT', help='run the peer natively; loopback port for its agent')
    sub.add_parser('start'); sub.add_parser('stop'); sub.add_parser('provision'); sub.add_parser('provision-host-peer')
    p = sub.add_parser('peer-exec'); p.add_argument('argv', nargs=argparse.REMAINDER)
    p = sub.add_parser('exec'); p.add_argument('--namespace', choices=('cr100', 'peer')); p.add_argument('argv', nargs=argparse.REMAINDER)
    p = sub.add_parser('put'); p.add_argument('source'); p.add_argument('destination')
    p = sub.add_parser('key'); p.add_argument('name')
    p = sub.add_parser('capture'); p.add_argument('destination')
    a = parser.parse_args()
    if a.action == 'prepare': result = prepare(a.directory, peer='host', peer_agent_port=a.host_peer) if a.host_peer else prepare(a.directory)
    elif a.action == 'provision-host-peer': result = provision_host_peer(a.directory)
    elif a.action == 'peer-exec': result = peer_execute(a.directory, a.argv[1:] if a.argv[:1] == ['--'] else a.argv)
    elif a.action == 'start': result = start(a.directory)
    elif a.action == 'stop': result = stop(a.directory)
    elif a.action == 'provision': result = provision_identity(a.directory)
    elif a.action == 'exec': result = execute(a.directory, a.argv[1:] if a.argv[:1] == ['--'] else a.argv, a.namespace)
    elif a.action == 'put': result = put(a.directory, a.source, a.destination)
    elif a.action == 'key': result = key(a.directory, a.name)
    else: result = capture(a.directory, a.destination)
    print(json.dumps(result, indent=2))

if __name__ == '__main__': main()
