# Emulator lab: only the controller on the emulated CPU

2026-11-15. Evidence and a working example: `research/bridge-runs/native-peer-lab-2026-11-15/`.

The SH4 emulator interprets every instruction, so anything that runs in the guest beside the
controller is slow and takes time away from the controller. Until now a lab session ran the real
bridge, `fake_ha.py`, the software speaker, the wheel driver and the framebuffer sampler in the guest,
all in Python. A run prepared with `peer='host'` keeps only the controller there.

## Where each part runs

| Part | Before | Now |
| --- | --- | --- |
| CR100 app (`hh.anacapad`) and its launcher | SH4 guest | SH4 guest |
| Bridge harness, fake HA, software speaker | SH4 guest, `peer` namespace | Native arm64 helper VM (`tools/lab_peer_vm.py`) |
| Bridge code staging | tar + `compileall` + upload + extract in the guest | Plain copy into the run's `vm/peer/` directory, which the helper mounts as `/lab` |
| Trace log, `control.json`, `summary.json` | Guest files, read and written through the guest agent | Host files in `vm/peer/` |
| Screen captures and thumb sampling | Guest Python reading the app's memory | QEMU memory dump on the host (`Screen`) |
| Keys and wheel steps | Guest Python per key, guest Python wheel driver | Host writes into a pipe; a busybox `wget` in the guest copies it to the keypad FIFO (`Input`) |
| Boot-time software speaker and `peer` namespace | Started on every boot | Removed from the boot hook (`provision_host_peer`) |

Still in the guest besides the controller: the lab agent (idle until the host asks it to run a
command or store a file), the control service that launches and supervises the app, the `wget` relay,
and the system's own `init`, `syslogd`, `klogd` and `getty`.

## How the private segment works

The emulator's private hub keeps the controller's NIC. Its second port is no longer a guest NIC but a
local socket (`-netdev stream`) to the helper VM, whose `eth0` carries the peer MAC and the addresses
`192.168.88.80`-`.82`. It is a real Ethernet link, so SSDP multicast, Netstart broadcast, the
controller's source address and GENA callbacks behave as before. Neither VM's private NIC has a host
or LAN backend. Both management NICs are QEMU user networks with `restrict=on` and loopback forwards.

The helper VM is Alpine 3.24 with Python 3.14 on the Alpine kernel already in
`research/cr100-emulator/vm/`. `lab_peer_vm.py build` downloads the pinned Alpine root filesystem
(SHA-256 checked) and installs the packages once; the image is `vm/native-peer/root.cpio.gz`. It boots
from RAM in about 2.5 s and keeps no state. Its package versions are Alpine's, not the bridge's pins
(websockets 16.0, cryptography 47.0.0, Pillow 12.2.0).

## Timings (same seed disk, same R10 movie, same slider case)

| Phase | All in the guest | Controller only |
| --- | --- | --- |
| Boot until the guest agent answers | 61.0 s | 46.3 s |
| Private network ready | 11.7 s | 13.9 s |
| Stage the bridge code | 12.4 s | 1.5 s |
| Stop the previous bridge and app | 50.5 s | 2.2 s |
| Bridge harness ready | 81.9 s | 0.4 s |
| Controller's first frame | 44.4 s | 43.7 s |
| Rooms populated and Devices list open | 103.8 s | 19.9 s |
| Slider case `g02-ceiling-immediate` | 164.5 s | 56.8 s |
| **Cold boot to finished case** | **531.3 s** | **184.8 s** |

About 104 s of the new total is the guest booting and the controller starting, which is the emulated
controller itself. The case ended in the same state both times (thumb at 214.77 then 141.77, ceiling
light 92, group 201).

The controller is also less disturbed during a gesture:

| During the 64-step gesture | All in the guest | Controller only |
| --- | --- | --- |
| Bridge's Browse handling | 469 ms average | 1 ms |
| NOTIFY delivery | 519 ms average | 18 ms |
| Last thumb movement after the last wheel step | 1.89 s | 0.04 s |
| Worst wheel-step lateness | 57 ms | 5 ms |
| Level commands sent during the turn | 2 | 4 |

**Earlier emulator timings are not comparable with new ones.** Reports such as
`SMART-DEVICE-SLIDER-STALL.md` measured a guest that was also running the bridge. The controller's
own speed in the emulator is unchanged and is still not hardware speed.

## Using it

```python
import radio_emulator_lab as lab
lab.prepare(vm, seed, control_port, agent_port, peer='host', peer_agent_port=port)
lab.start(vm)                    # emulator and helper VM
lab.provision_host_peer(vm)      # once per overlay, then stop and start again
lab.peer_shell(vm, 'python3 /lab/harness.py >/lab/runtime.log 2>&1 &')
screen = lab.Screen(vm, '/crroot432').locate()   # after each controller start
keys = lab.Input(vm, '/crroot432').attach()      # after each controller start
keys.key('music'); keys.wheel(-1); screen.capture('now.png')
lab.stop(vm)
```

`lab/labtools.py` in the example run wraps this for the smart-devices harness: `stage_bridge`,
`start_bridge`, `start_controller`, `press` (a key, then wait until the screen stops changing),
`events` (the trace, read on the host) and `peer_now` (the trace clock without a round trip).
The existing harness file runs unchanged because the helper links `/opt/cr100-lab/smart-devices` to
`/lab`.

## Limits

- `Screen` reads the physical pages that backed the display file when `locate()` ran. A capture was
  compared with the control service's and was identical. If the guest ever moves those pages (memory
  pressure), reads go stale; `locate()` again or compare against `lab.capture`.
- `Input` and `Screen` use one-client sockets with a short connection per call, so scripts take turns.
- macOS with QEMU and its hypervisor is required for the helper VM. A run prepared without
  `peer='host'` behaves as before.
- The acceptance runner's own fixed waits between keys are unchanged, so its cases are bounded by
  those waits and by the controller, not by the lab any more.

## Ported slider-lab scripts

`run_case.py`, `double_case.py`, `burst_case.py` and `accept_runner.py` (with `accept_bridge_lab.py`
unchanged) run on the new helpers in the example lab. The acceptance runner still runs two things in
the guest per case: starting the controller app and a shell check of its executable's path and hash.

| Acceptance case (R10, working-tree bridge) | All in the guest (slider lab) | Controller only |
| --- | --- | --- |
| double | 321 s (failed at the 1.5 s gap on an emulated HA timeout) | 126 s, passed |
| power | 388 s | 119 s, passed |
| errors | 273 s | 106 s, passed |
| slider | 464 s | 182 s, passed |
| fan | 255 s | 96 s, passed |
| stale | 303 s | 148 s, passed |

The other acceptance cases (`entry`, `oneshot`, `grouped`, `list_availability`, `entity_unavailable`,
`folders`) were not run here.
