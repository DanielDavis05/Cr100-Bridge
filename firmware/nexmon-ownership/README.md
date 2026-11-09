# BCM43455c0 radio firmware support

This package targets one exact Kali `firmware-nexmon0.2` image with Nexmon
`7.45.206 / 2.2.2-552-gb8c6-2`. It fixes injection packet ownership, removes ACK
expectation from valid injected unicast probe responses, exposes the coherent radio
clock, and enables native hardware timestamping for validated injected beacons.
The host bridge uses the same hardware-clock epoch and100-TU beacon grid.

The packaged binary is byte-identical to the reviewed hardware-stamping trial
image. Packaging does not establish long-term controller reliability: the earlier
NoAck-only and host-aligned software-beacon variants still lost connectivity.
Physical validation must compare loss-event identities and actual controller UI
behavior. A fixed-size dmesg ring can retain nine records while replacing old losses
with new ones, so a constant record count is insufficient.

## Ownership and probe NoAck

The stock `wl_send` path saves its next host packet, clears PKTLINK at offset0x16,
and performs native-to-OS accounting before its filter. The package restores that
stock entry point and intercepts the later filter to:

1. Allocate an independent packet with202 bytes of TX headroom.
2. Copy only radiotap/frame bytes, retaining fresh allocator metadata.
3. Free the normalized source exactly once.
4. Mark a valid unicast probe-response clone with native packet-tag0x1000.
5. Hand the clone to the existing injector and return consumed=1.

Ordinary traffic delegates to the original filter. Malformed raw lengths and
allocation failures free/consume the normalized source without injecting. A
nonzero PKTLINK at this call site is counted and delegated to stock handling.
The probe classifier checks a complete24-byte header plus12-byte fixed body,
unprotected/unfragmented form, nonzero unicast address fields and a nonzero interval.
Other frame bytes and allocator metadata remain unchanged.

The exact7.45.206 builder maps tag0x1000 at packet+0x18 to omission of
MacTxControlLow0x80. That is the ACK-expectation bit on this target. Bit zero selects
a short descriptor format; older-chip bit-zero ACK definitions do not apply.
The injector ignores radiotap NoAck/retry-control fields, so this native tag is
necessary. Native data ACK/retry and completion paths remain intact.

## Native hardware beacon timestamping

The RAM builder pointer at0x19849c calls a narrow wrapper around the unchanged
native Thumb function0x1a63ed. It always forwards all eleven arguments and returns
the exact original result. It modifies a packet only when all guards match:

- Dedicated monitor mode, hardware up, and SCB equal to the verified
  `wlc->band->hwrs_scb` injection context.
- Native sendctl single-frame/no-key arguments, FIFO1, TX-parameter tagbit4 set,
  and header-ready tag0x80 not yet set.
- Complete38..512-byte beacon, normal management flags, zero duration, broadcast
  destination, matching nonzero unicast SA/BSSID, no fragment,100-TU interval,
  first SSID IE and a complete bounded IE sequence.
- After the native call, a new header-ready tag, an exact124-byte descriptor plus
  optional4/16-byte TSO prefix, the original retained frame pointer, a long
  descriptor, and a still-valid beacon.

The only functional packet write is `descriptor[4] |= 0x80`, setting
MacTxControlHigh0x80. All other descriptor bits, ACK policy, TSO bytes and802.11
frame bytes are preserved. Already-stamped packets receive no write. Probes,
data, association frames, malformed beacons and nonmatching native callers keep
the original path. No packet is retained or read after enqueue/free: exact native
callers0x25c98/0x1a6d30 still access it synchronously after this wrapper returns.

The target microcode uses hardware TSF plus a per-BSS offset when this high bit
is enabled. All four offsets were measured as zero before the controlled trial,
and GET435 retains a fixed read-only check. The wrapper reports descriptor byte100
low two bits as its BSS index. It does not write shared-memory clock offsets or
configure a separate hardware beacon queue. Software scheduling can still delay a
beacon; its transmitted timestamp is now assigned through the native hardware path.

## Exact binary constraints

| Item | Value |
| --- | --- |
| Canonical package image SHA256 | `37b46819372d624e9b8153d164857dc983b696919b0899bf8034a1bad65bf832` |
| Previous ownership image | `54484fd4e0c2afc4eeb87e554dc80b76dab26356b7792a3928be6cd93e959cf4` |
| Previous NoAck/clock image | `e88ca542742c49321f2d5d7624e646ef16b5cf830697cebdd7770f548ebd62a9` |
| Packaged target SHA256 | `ce38c2e189815a0495615ab4ecad979be8f01b234cd5b3e9c1cfe90edf32677d` |
| Image length |624943 bytes |
| Pristine7.45.206 SHA256 | `7fe369f7fdfded9eeceb8e438f7346e58634741b326d0715ad1b15275e6fa3a4` |
| Firmware RAM base |0x198000 |
| Existing Nexmon region |0x22c7f0..0x2307f0 |
| Overlay |0x22d100..0x22d8d0,2000 bytes |

Five canonical ranges change: overlay, send pointer0x2037c0, filter branch0x1a1f78,
ioctl pointer0x20e264 and builder pointer0x19849c. Existing native code, injector,
sendframe, compressed microcode and reclaim boundary are unchanged. The reserved
overlay tail must match the pristine image. Whole-image hashes and exact preimage
bytes guard every supported route; private experimental hashes are not accepted
by the released manifest.

## Installation and loaded-firmware checks

The main installer applies `manifest.json` after Kali's Nexmon packages and before
activating the bridge release. `apply.py` accepts the exact canonical image, either
explicit previous-image upgrade route, or an already-patched target. It validates
all byte preconditions and the common complete target hash before atomic
replacement. Firmware board-name/alternatives symlinks remain intact. Backups and
receipts are private under `/var/lib/cr100-bridge/firmware-backups/`.

```sh
sudo python3 firmware/nexmon-ownership/apply.py --check
sudo python3 firmware/nexmon-ownership/apply.py --apply
# Reboot/reload through the managed radio recovery path to load the disk image.
sudo python3 firmware/nexmon-ownership/status.py --ownership-only
sudo python3 firmware/nexmon-ownership/status.py --radio-only
```

`--check` does not write. `--apply` is idempotent and refuses unknown images. Package
upgrades can replace the disk image; the installer can reapply only supported
inputs. A disk hash does not identify the firmware already running in the radio.

`--ownership-only` preserves GET430 identification. `--radio-only` reads the fixed
NoAck/clock/offset/stamp diagnostic APIs, requiring exact signatures, versions,
sizes and capability/status fields. Fresh zero stamp counters identify capability
before the first eligible beacon; subsequent GET436 counts/high-bit values verify
that the native builder actually enabled stamping. They do not prove airtime or
room discovery. The existing exact MPC GET tuple(GET262,32bytes,`mpc\0`) remains
available to the startup power helper; arbitrary iovars are rejected.

Driver calls run in a child process. Parent diagnostic waits are bounded to the
requested timeout plus at most0.2seconds of cleanup. A secure inherited exclusive
lease at `/run/cr100-bridge/firmware-query.lock` prevents replacement children while
a timed-out child is still in an uninterruptible kernel call, even after the original
caller exits. It is never explicitly unlocked or unlinked. A socket timeout or
SIGKILL does not itself cancel such a callback. The host clock reader has its own
persistent helper and lifetime lease.

## Fixed diagnostic APIs

All responses are little-endian and have no reset/write action. Short/null buffers
return-14 untouched; larger buffers retain trailing bytes. Commands other than
430/432/433/435/436 delegate to the existing Nexmon handler. There is no ROM reader,
arbitrary-memory query, packet payload snapshot or old passive GET434 observer.

### GET430 ownership counters(48bytes)

Twelve uint32 words: magic0x4f4e5243(CRNO),version1,size48,seen,cloned,source_freed,
alloc_fail,malformed,host_link_nonzero,forwarded,ioctl_reads,clone_free_on_error.
`source_freed` counts originals, not later TX-clone completion. With allocation
failures alone, `seen == source_freed == cloned + alloc_fail`. Compare deltas:
successful socket sends do not reveal firmware allocation drops.

### GET432 probe NoAck counters(40bytes)

Ten uint32 words: magic0x414e5243(CRNA),version1,size40,capabilities1,probe_seen,
probe_tagged,probe_rejected,probe_alloc_fail,other_frames,ioctl_reads. Capability
bit zero identifies the narrow clone-marking behavior. These are pre-injection
classification counters, not ACK/completion counters.

### GET433 coherent hardware clock(24bytes)

Six uint32 words: magic0x51465354(TSFQ),version1,size24,status,low,high. Status1
means coherent/up; status0 means unavailable and zero clock words. The helper
checks hardware-up and uses the read-only native TSF helper at Thumb0x1a96ed.
At most three samples accept a newer pair only if high words agree and low does
not move backward, handling low-word wrap and rejecting repeated mixed samples.
Only caller-owned outputs are written.

### GET435 per-BSS clock offsets(48bytes)

Four uint32 header words: magic0x514d4853(SHMQ),version1,size48,status; followed by
four uint64 offsets. Status1 requires identical complete snapshots; status0 returns
all-zero offsets. The getter reads only16 fixed halfwords at SHM byte offsets
0xeee,0xef0,...,0xf0c, twice(32native reads maximum), with hardware-up guards before
each read and after sampling. There is no address/index/count input. Native
Thumb0x1bdf0d writes only the indirect read-address selector, flushes it and reads
the selected halfword; it never writes SHM/clock data.

### GET436 hardware beacon stamp capability/counters(80bytes)

Twenty uint32 words, in order:

| Words | Fields |
| --- | --- |
|0..3| magic0x51545342(BSTQ),version1,size80,capabilities1 |
|4..11| sequence,calls,beacon_candidates,stamped,already_stamped,rejected_headers,ioctl_reads,last_valid |
|12..19| last_low,last_high_before,last_high_after,last_bss_index,last_prefix,last_length_before,last_length_after,reserved0 |

Only qualifying beacon metadata is sampled. A stable sequence is even; GET436
tries at most three times, returning-16 untouched if busy. For a valid sample,
high_after equals high_before OR0x80, BSS index is0..3, prefix0/4/16, and
length_after equals length_before+124+prefix. No MAC, packet pointer, key, IV/CCMP,
public timestamp body or application payload is exposed.

## Build and validation

`build.py` uses reviewed official Nexmon ARM GCC5.4.1(macOS or Linux x86), verifies
exact input/pristine hashes and preimage bytes, and records relative source/build
commands. A complete Nexmon plugin rebuild is unnecessary for this fixed overlay.
Repeat the optional `--upgrade-from` to include both supported previous images:

```sh
python3 firmware/nexmon-ownership/build.py \
  --base /path/to/reviewed-base.bin \
  --nexmon /path/to/reviewed/nexmon \
  --upgrade-from /path/to/previous-ownership.bin \
  --upgrade-from /path/to/previous-radio.bin
python3 -m unittest discover -s firmware/nexmon-ownership -p 'test_build.py' -v
```

The image is reproducible across output directories with this toolchain, and
identical whether or not optional upgrade routes are included. Each upgrade has
its own exact input hash/preimages and shares the canonical target hash. Duplicate
or unsupported upgrade inputs are rejected. The released manifest includes only
the two previous images listed above.

Build products include image, byte-patch manifest, source/build hashes, ELF/map and
disassemblies under `build/`. The manifest avoids distributing a full vendor image.
Tests locate external inputs through `CR100_NEXMON_BASE` and `CR100_NEXMON_SOURCE`.

Nine build checks include exact image/route reconstruction, output-directory
reproducibility, guarded hashes/ranges and C ASan/UBSan harnesses for ownership,
probe-only NoAck, coherent TSF, fixed SHM bounds and the beacon wrapper. The wrapper
harness checks TSO0/4/16, frame/context/pointer guards, zero-length/short headers,
unchanged probes/data/association frames, exact native result/arguments and a single
descriptor-byte change. The identical trial image also passed exact ARM execution
with eleven-argument sentinels and memory hooks verifying those writes, plus the
real native SHM-selector path. These checks establish construction/ownership,
not the controller's long-term physical reliability.
