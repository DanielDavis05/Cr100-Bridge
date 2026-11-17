#!/usr/bin/env python3
"""Native helper VM for emulator labs: everything that is not the CR100.

The SH4 emulator interprets every guest instruction, so anything that runs in
it beside the controller (the bridge, a fake Home Assistant, a software
speaker, test drivers) is slow and steals time from the controller itself.
This module runs those parts in a small arm64 Linux VM at native speed (QEMU
with the macOS hypervisor) and joins it to the controller's private segment
through a local socket. The segment still has no host or LAN connection.

    build      download the pinned Alpine root filesystem and build the image
    exec       run a command in a run's helper
    clock      the helper's monotonic clock relative to the host's
    stop       power a run's helper off

`radio_emulator_lab.start` boots the helper together with the emulator for a
run prepared with `peer='host'`; see docs/research/EMULATOR-LAB.md.

The helper boots from RAM in a few seconds and keeps no state. A run's
`peer/` directory is shared with it as `/lab`, so code, logs and control files
are ordinary host files.
"""
from __future__ import annotations
import argparse, base64, gzip, hashlib, io, json, os, secrets, socket, stat, subprocess, sys, tarfile, time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace

QEMU = '/opt/homebrew/bin/qemu-system-aarch64'
VM = workspace.EMULATOR / 'vm'
KERNEL, INITRAMFS, MODLOOP = VM / 'vmlinuz-virt', VM / 'initramfs-virt', VM / 'modloop-virt'
ASSETS = VM / 'native-peer'
ALPINE = '3.24.4'
ROOTFS_URL = 'https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/aarch64/alpine-minirootfs-%s-aarch64.tar.gz' % ALPINE
ROOTFS_SHA256 = '4b14c18f7e90a063b61462ea1462a5732c154bf6adad594974fe6487cd98a8de'
PACKAGES = ['python3', 'py3-websockets', 'py3-cryptography', 'py3-pillow', 'py3-cffi', 'scapy', 'iproute2', 'tcpdump']
MANAGEMENT_MAC = '52:54:00:5e:e7:01'
AGENT_GUEST_PORT = 22346
SHARE = '/lab'

COMMON_INIT = r'''#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p /proc /sys /dev /run /tmp /.modloop
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts; mount -t devpts devpts /dev/pts; mount -t tmpfs tmpfs /run; mount -t tmpfs tmpfs /tmp
version=$(uname -r)
[ -e /lib/modules ] || ln -s /usr/lib/modules /lib/modules
modprobe virtio_blk; modprobe loop; modprobe squashfs
# The module archive is the read-only disk whose first bytes are a squashfs.
for disk in /dev/vd?; do
 [ "$(dd if=$disk bs=4 count=1 2>/dev/null)" = hsqs ] && mount -t squashfs -o ro $disk /.modloop && break
done
if [ -d /.modloop/modules/$version ]; then mount --bind /.modloop/modules/$version /usr/lib/modules/$version; fi
modprobe virtio_net; modprobe af_packet
'''

BUILD_INIT = COMMON_INIT + r'''
exec >/dev/console 2>&1
set -x
ip link set lo up; ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0; ip route add default via 10.0.2.2
echo 'nameserver 10.0.2.3' >/etc/resolv.conf
apk update && apk add --no-cache @PACKAGES@ || { echo PEER_BUILD_FAILED; poweroff -f; }
apk info -v | sort >/etc/lab-packages.txt
umount /usr/lib/modules/$version
rm -rf /var/cache/apk/* /root/.cache
cd /
find . -xdev \( -path ./proc -o -path ./sys -o -path ./dev -o -path ./run -o -path ./tmp -o -path ./.modloop -o -path ./init \) -prune -o -print \
 | cpio -o -H newc 2>/dev/null | gzip -1 >/tmp/root.cpio.gz
out=; for disk in /dev/vd?; do [ "$(dd if=$disk bs=4 count=1 2>/dev/null)" = hsqs ] || out=$disk; done
size=$(wc -c </tmp/root.cpio.gz)
printf '%-512s' "PEERROOT $size" | dd of=$out bs=512 count=1 conv=notrunc 2>/dev/null
dd if=/tmp/root.cpio.gz of=$out bs=1M seek=1 conv=notrunc 2>/dev/null
sync; echo PEER_BUILD_DONE $size; poweroff -f
'''

RUN_INIT = COMMON_INIT + r'''
modprobe 9pnet_virtio; modprobe 9p; modprobe tun 2>/dev/null
hostname lab-peer; ip link set lo up
# IPv4 only, like the controller: no neighbour discovery chatter on its segment.
echo 1 >/proc/sys/net/ipv6/conf/all/disable_ipv6; echo 1 >/proc/sys/net/ipv6/conf/default/disable_ipv6
# The private segment is always eth0, whatever order the NICs were found in.
for nic in /sys/class/net/*; do
 name=${nic##*/}
 case "$(cat $nic/address)" in
  '@MANAGEMENT_MAC@') ip link set $name name management ;;
  00:00:00:00:00:00) ;;
  *) ip link set $name name segment ;;
 esac
done
ip link set segment name eth0
ip link set management up; ip addr add 10.0.2.15/24 dev management
mkdir -p @SHARE@; mount -t 9p -o trans=virtio,version=9p2000.L,msize=262144,access=any lab @SHARE@
echo PEER_READY >/dev/console
exec /usr/bin/python3 /opt/lab_agent.py >>/dev/console 2>&1
'''

AGENT = r'''"""Authenticated command access for the helper VM (host loopback forward only)."""
import base64, hmac, json, socketserver, subprocess, time
from pathlib import Path
TOKEN = Path('@SHARE@/.agent_token')

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if self.client_address[0] != '10.0.2.2': return
        try:
            req = json.loads(self.rfile.readline(4 * 1024 * 1024 + 1))
            if not hmac.compare_digest(str(req.get('token', '')), TOKEN.read_text().strip()):
                raise ValueError('Authentication failed')
            if req.get('op') == 'clock':
                out = {'ok': True, 'monotonic': time.monotonic(), 'time': time.time()}
            elif req.get('op') == 'exec':
                p = subprocess.run(req['argv'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=float(req.get('timeout', 60)))
                out = {'ok': True, 'returncode': p.returncode,
                       'stdout': base64.b64encode(p.stdout[-262144:]).decode(),
                       'stderr': base64.b64encode(p.stderr[-65536:]).decode()}
            else: raise ValueError('Unknown operation')
        except Exception as error:
            out = {'ok': False, 'error': repr(error)[:500]}
        self.wfile.write(json.dumps(out).encode() + b'\n')

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True; daemon_threads = True

Server(('10.0.2.15', @AGENT_PORT@), Handler).serve_forever()
'''


def _fill(text, **values):
    for key, value in values.items(): text = text.replace('@%s@' % key, str(value))
    return text


def _cpio(entries):
    """gzip newc archive of (name, mode, data) owned by root."""
    out = io.BytesIO()
    def add(index, name, mode, data):
        name = name.encode() + b'\0'
        out.write(('070701' + '%08X' * 13 % (index, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0)).encode())
        out.write(name + b'\0' * (-(110 + len(name)) % 4) + data + b'\0' * (-len(data) % 4))
    for index, (name, mode, data) in enumerate(entries, 1): add(index, name, mode, data)
    add(0, 'TRAILER!!!', 0, b'')
    return gzip.compress(out.getvalue(), 1)


def _rootfs_entries(archive):
    with tarfile.open(archive) as source:
        members = {m.name.lstrip('./'): m for m in source if m.name.lstrip('./')}
        for name, m in sorted(members.items()):
            if m.isdir(): yield name, stat.S_IFDIR | m.mode, b''
            elif m.issym(): yield name, stat.S_IFLNK | 0o777, m.linkname.encode()
            elif m.islnk():
                target = members[m.linkname.lstrip('./')]
                yield name, stat.S_IFREG | target.mode, source.extractfile(target).read()
            elif m.isreg(): yield name, stat.S_IFREG | m.mode, source.extractfile(m).read()


def _qemu(initrd, extra, serial, memory='1024M'):
    return [QEMU, '-M', 'virt', '-accel', 'hvf', '-cpu', 'host', '-smp', '2', '-m', memory,
            '-display', 'none', '-monitor', 'none', '-no-reboot',
            '-kernel', str(KERNEL), '-initrd', str(initrd), '-append', 'console=ttyAMA0 rdinit=/init quiet',
            '-drive', 'file=%s,format=raw,if=virtio,readonly=on' % MODLOOP,
            '-serial', 'file:%s' % serial, *extra]


def build(force=False):
    """Build ASSETS/root.cpio.gz. Needs the network once, for Alpine packages only."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    if (ASSETS / 'root.cpio.gz').exists() and not force:
        return json.loads((ASSETS / 'manifest.json').read_text())
    archive = ASSETS / ROOTFS_URL.rsplit('/', 1)[1]
    if not archive.exists():
        data = urllib.request.urlopen(ROOTFS_URL, timeout=60).read()
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ROOTFS_SHA256:
        raise RuntimeError('Unexpected Alpine root filesystem hash: %s' % archive)
    initrd = ASSETS / 'build.initrd'
    init = _fill(BUILD_INIT, PACKAGES=' '.join(PACKAGES)).encode()
    initrd.write_bytes(INITRAMFS.read_bytes() + _cpio(_rootfs_entries(archive)) +
                       _cpio([('init', stat.S_IFREG | 0o755, init)]))
    output = ASSETS / 'build-output.raw'
    with output.open('wb') as handle: handle.truncate(768 * 1024 * 1024)
    log = ASSETS / 'build-serial.log'
    log.unlink(missing_ok=True)
    subprocess.run(_qemu(initrd, ['-nic', 'user,model=virtio-net-pci',
                                  '-drive', 'file=%s,format=raw,if=virtio' % output], log, '2048M'),
                   check=True, timeout=900)
    with output.open('rb') as handle:
        header = handle.read(512).split()
        if header[:1] != [b'PEERROOT']:
            raise RuntimeError('Helper image build failed; see %s' % log)
        handle.seek(1024 * 1024); root = handle.read(int(header[1]))
    (ASSETS / 'root.cpio.gz').write_bytes(root)
    output.unlink(); initrd.unlink()
    manifest = {'alpine': ALPINE, 'rootfs_sha256': ROOTFS_SHA256, 'packages': PACKAGES,
                'root_sha256': hashlib.sha256(root).hexdigest(), 'bytes': len(root),
                'kernel_sha256': hashlib.sha256(KERNEL.read_bytes()).hexdigest(),
                'built_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    (ASSETS / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def paths(directory):
    directory = Path(directory).resolve()
    return directory, directory / 'peer'


def start(directory, link, mac, agent_port, sockets=None, addresses=('192.168.88.80/24',)):
    """Boot the helper for this run. `link` is the SH4 emulator's hub socket."""
    build()
    state, share = paths(directory)
    share.mkdir(exist_ok=True)
    qmp = Path(sockets or state) / 'peer-qmp.sock'
    if qmp.exists():
        raise ValueError('Run already owns a helper VM; stop it first.')
    token = share / '.agent_token'
    token.write_text(secrets.token_hex(32) + '\n'); token.chmod(0o600)
    overlay = [('init', stat.S_IFREG | 0o755, _fill(RUN_INIT, MANAGEMENT_MAC=MANAGEMENT_MAC, SHARE=SHARE).encode()),
               ('opt', stat.S_IFDIR | 0o755, b''),
               ('opt/lab_agent.py', stat.S_IFREG | 0o644, _fill(AGENT, SHARE=SHARE, AGENT_PORT=AGENT_GUEST_PORT).encode())]
    initrd = state / 'peer-initrd'
    initrd.write_bytes((ASSETS / 'root.cpio.gz').read_bytes() + _cpio(overlay))
    serial = state / ('peer-serial-%d.log' % int(time.time()))
    args = _qemu(initrd, [
        '-nic', 'user,model=virtio-net-pci,mac=%s,restrict=on,hostfwd=tcp:127.0.0.1:%d-:%d' % (MANAGEMENT_MAC, agent_port, AGENT_GUEST_PORT),
        '-netdev', 'stream,id=segment,server=off,addr.type=unix,addr.path=%s,reconnect-ms=500' % link,
        '-device', 'virtio-net-pci,netdev=segment,mac=%s' % mac,
        '-fsdev', 'local,id=lab,path=%s,security_model=none' % share,
        '-device', 'virtio-9p-pci,fsdev=lab,mount_tag=lab',
        '-qmp', 'unix:%s,server=on,wait=off' % qmp], serial)
    with (state / 'peer-qemu.log').open('ab') as log:
        process = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    (state / 'peer-process.json').write_text(json.dumps(
        {'pid': process.pid, 'args': args, 'agent_port': agent_port, 'mac': mac, 'qmp': str(qmp)}, indent=2) + '\n')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Helper VM exited; see %s' % (state / 'peer-qemu.log'))
        try:
            call(directory, {'op': 'clock'}, timeout=2); break
        except (OSError, ValueError): time.sleep(.2)
    else: raise TimeoutError('Helper VM agent did not answer; see %s' % serial)
    initrd.unlink()
    for address in addresses:
        shell(directory, 'ip addr add %s dev eth0' % address)
    # Same private routes as the guest namespaces: local subnet, multicast and limited broadcast only.
    shell(directory, 'ip link set eth0 up && ip route add 224.0.0.0/4 dev eth0 && ip route add 255.255.255.255 dev eth0')
    return {'pid': process.pid, 'agent_port': agent_port, 'share': str(share)}


def call(directory, request, timeout=75):
    state, share = paths(directory)
    port = json.loads((state / 'peer-process.json').read_text())['agent_port']
    request = dict(request, token=(share / '.agent_token').read_text().strip())
    with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
        client.settimeout(timeout)
        client.sendall(json.dumps(request).encode() + b'\n')
        with client.makefile('rb') as stream:
            result = json.loads(stream.readline(4 * 1024 * 1024 + 1))
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'Helper operation failed.'))
    return result


def execute(directory, argv, timeout=60):
    result = call(directory, {'op': 'exec', 'argv': list(argv), 'timeout': timeout}, timeout + 15)
    return {'returncode': result['returncode'],
            'stdout': base64.b64decode(result['stdout']).decode(errors='replace'),
            'stderr': base64.b64decode(result['stderr']).decode(errors='replace')}


def shell(directory, command, timeout=60, check=True):
    result = execute(directory, ['/bin/sh', '-c', command], timeout)
    if check and result['returncode']:
        raise RuntimeError('Helper command failed: %s\n%s' % (command, result['stderr'][-800:]))
    return result


def clock_offset(directory, samples=9):
    """helper time.monotonic() minus host time.monotonic(), from the quickest of several round trips."""
    best = None
    for _ in range(samples):
        sent = time.monotonic(); remote = call(directory, {'op': 'clock'})['monotonic']; back = time.monotonic()
        if best is None or back - sent < best[0]: best = (back - sent, remote - (sent + back) / 2)
    return best[1]


def stop(directory):
    state, _ = paths(directory)
    record = state / 'peer-process.json'
    if not record.exists(): return {'stopped': None}
    qmp = Path(json.loads(record.read_text())['qmp'])
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(qmp))
            with client.makefile('rwb', buffering=0) as stream:
                stream.readline()
                stream.write(b'{"execute":"qmp_capabilities"}\n'); stream.readline()
                stream.write(b'{"execute":"quit"}\n'); stream.readline()
    except OSError: pass
    qmp.unlink(missing_ok=True)
    return {'stopped': str(state)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('build'); p.add_argument('--force', action='store_true')
    for name in ('exec', 'stop', 'clock'):
        p = sub.add_parser(name); p.add_argument('directory', type=Path)
        if name == 'exec': p.add_argument('argv', nargs=argparse.REMAINDER)
    a = parser.parse_args()
    if a.action == 'build': result = build(a.force)
    elif a.action == 'exec': result = execute(a.directory, a.argv[1:] if a.argv[:1] == ['--'] else a.argv)
    elif a.action == 'clock': result = {'helper_minus_host_monotonic': clock_offset(a.directory)}
    else: result = stop(a.directory)
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
