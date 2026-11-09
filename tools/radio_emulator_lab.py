#!/usr/bin/env python3
"""Disposable CR100 Radio lab; never opens an existing emulator disk writable.

Each run owns an immutable baseline copy, qcow2 overlay, loopback forwards,
sockets, token, private QEMU hub, and evidence directory. The original firmware
runs in the guest; screenshots are the real framebuffer, not an HTML recreation.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, os, secrets, shutil, socket, subprocess, sys, time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
EMULATOR = WORKSPACE / 'cr100-emulator'
DEFAULT_BASE = EMULATOR / 'vm/sh4-fallback/rootfs-fast-input-ready.ext4'


def _json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(directory, baseline=DEFAULT_BASE, control_port=22621, agent_port=22622):
    directory = Path(directory).resolve()
    if directory.exists():
        raise ValueError('Choose a new run directory; existing evidence is never replaced.')
    if Path(baseline).resolve().name in ('rootfs.ext4', 'controller-4.3-working.ext4'):
        raise ValueError('Use a stopped immutable template, never a working emulator disk.')
    for port in (control_port, agent_port):
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
    _json(directory / 'manifest.json', manifest)
    return manifest


def config(directory):
    directory = Path(directory).resolve()
    m = json.loads((directory / 'manifest.json').read_text())
    if m.get('directory') != str(directory):
        raise ValueError('Run manifest directory mismatch.')
    return directory, m


def start(directory):
    d, m = config(directory)
    if (d / 'qmp.sock').exists():
        raise ValueError('Run already owns a QMP socket; inspect or stop it first.')
    unique = m['identity_bootstrapped']
    macs = [m['management_mac'], m['controller_mac'], m['peer_mac']] if unique else [
        '52:54:00:12:34:56', '02:00:00:00:01:00', '02:00:00:00:00:80']
    args = ['/opt/homebrew/bin/qemu-system-sh4', '-M', 'r2d', '-cpu', 'sh7785', '-m', '128M',
            '-display', 'none', '-monitor', 'none', '-nic',
            'user,model=rtl8139,mac=%s,restrict=on,hostfwd=tcp:127.0.0.1:%d-:22345,hostfwd=tcp:127.0.0.1:%d-:22346' %
            (macs[0], m['control_port'], m['agent_port']),
            '-kernel', str(EMULATOR / 'vm/sh4-fallback/zImage'),
            '-append', 'printk.time=0 root=/dev/sda console=ttySC1',
            '-drive', 'file=%s,format=qcow2,if=ide,index=0' % (d / 'overlay.qcow2'),
            '-chardev', 'socket,id=labconsole,path=%s,server=on,wait=off,logfile=%s' %
            (d / 'console.sock', d / ('serial-%d.log' % int(time.time()))),
            '-serial', 'null', '-serial', 'chardev:labconsole',
            '-qmp', 'unix:%s,server=on,wait=off' % (d / 'qmp.sock'),
            '-netdev', 'hubport,id=cr100radio,hubid=97', '-device', 'rtl8139,netdev=cr100radio,mac='+macs[1],
            '-netdev', 'hubport,id=zp80radio,hubid=97', '-device', 'rtl8139,netdev=zp80radio,mac='+macs[2],
            '-object', 'filter-dump,id=radiocapture,netdev=cr100radio,file=%s' %
            (d / ('network-%d.pcap' % int(time.time())))]
    with (d / 'qemu.log').open('ab') as log:
        process = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    _json(d / 'process.json', {'pid': process.pid, 'args': args})
    return {'pid': process.pid, 'agent_port': m['agent_port'], 'control_port': m['control_port']}


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
        client.connect(str(d / 'console.sock')); client.sendall(b'\r')
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


def stop(directory):
    d, _ = config(directory)
    execute(directory, ['/bin/sync'])
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(d / 'qmp.sock'))
        with client.makefile('rwb', buffering=0) as stream:
            stream.readline()
            stream.write(b'{"execute":"qmp_capabilities"}\n'); stream.readline()
            stream.write(b'{"execute":"quit"}\n')
    return {'stopped': str(d)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('prepare'); sub.add_parser('start'); sub.add_parser('stop'); sub.add_parser('provision')
    p = sub.add_parser('exec'); p.add_argument('--namespace', choices=('cr100', 'peer')); p.add_argument('argv', nargs=argparse.REMAINDER)
    p = sub.add_parser('put'); p.add_argument('source'); p.add_argument('destination')
    p = sub.add_parser('key'); p.add_argument('name')
    p = sub.add_parser('capture'); p.add_argument('destination')
    a = parser.parse_args()
    if a.action == 'prepare': result = prepare(a.directory)
    elif a.action == 'start': result = start(a.directory)
    elif a.action == 'stop': result = stop(a.directory)
    elif a.action == 'provision': result = provision_identity(a.directory)
    elif a.action == 'exec': result = execute(a.directory, a.argv[1:] if a.argv[:1] == ['--'] else a.argv, a.namespace)
    elif a.action == 'put': result = put(a.directory, a.source, a.destination)
    elif a.action == 'key': result = key(a.directory, a.name)
    else: result = capture(a.directory, a.destination)
    print(json.dumps(result, indent=2))

if __name__ == '__main__': main()
