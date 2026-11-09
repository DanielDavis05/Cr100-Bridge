# CR100 custom UI and firmware deployment investigation

Investigated November 2, 2026, using the owner's existing update-process records,
exact official model-2 firmware, public primary hardware references, disposable
application disks and synthetic NAND media. No physical controller update, unlock, hardware probe
or flash write was performed. A read-only compiler check on the previously
authorized Pi timed out; its services and files were not changed.
Later SSH retry and read-only controller diagnostics succeeded, as recorded below.

[RESEARCH.md](RESEARCH.md) consolidates all findings and open items. At its November
2 documentation checkpoint, the adopted next route is a separate stock **Music >
Speaker Settings** service; production integration and Pi deployment are not done.
The earlier fixture is named Speaker Controls.

## What works

| Route | Result | Remaining limit |
|---|---|---|
| Stock controller, controls under Music | Original unmodified 4.3 runtime: Speaker Controls service, room/value browser, explicit action applies simulated Bass; direct SOAP readback agrees; zero playback requests. | Fixed library wording; no native slider. Exact 4.3.2 gate matches statically; dynamic stock-4.3.2/physical acceptance and production integration remain open. |
| Patched original System Settings | Tested in 4.3 and exact 4.3.2 native runtime from the rebuilt filesystem; all four controls pass SOAP readback. | Requires loading a modified controller UI file. Physical initial access is unresolved. |
| Custom CramFS/UPD construction | Built, same package size; all 288 filesystem entries verified, only MainUI contents change; all other package records preserved. | Rebuilding changes the signed digest. |
| Custom UPD with stock trust key | Exact 4.3.2 updater rejects it with signature error 6. | HTTP mirroring does not override authentication. |
| Custom UPD with explicit lab trust key | Exact 4.3.2 updater authenticates it, writes synthetic NAND bank 1/generation 2 and completes post-install; a separate cold boot mounts that bank. | Native automatic reset is not modeled: updater returns error 17. Physical key placement and local invocation remain unresolved. |
| Temporary JFFS SWF overlay | File bind, unmount, original hash restoration and original menu restoration tested on emulator kernel. | Physical-kernel behavior and recovery remain unverified. |

This is not yet a finished installation method for an untouched physical CR100.
The unresolved dependency is initial privileged access or a separately proven
stock-update acceptance route. The emulator's research access does not supply
that access on the actual device.

## Update trust and the owner's earlier successful update

The existing [physical 4.3-to-4.3.2 record](../update-analysis/7.1-access/cr100/update-to-4.3.2.md)
shows that a local HTTP mirror and TLS 1.0 stand-in delivered the authentic signed
model-2 package successfully. The controller's update-message/manifest exchange
and final package authentication are separate layers. That earlier success does
not establish modified-package acceptance.

The exact 4.3.2 package uses RSA-1024, PKCS#1 v1.5 and MD5. Its signature covers
4,838,728 preceding bytes, including version, installation script, kernel,
CramFS/MainUI, partition tables and MCU payload. Changing the SWF invalidates
that signature. Replacing a key inside the proposed filesystem cannot change
the key used by the running updater to authenticate that same proposal.

The exact updater retains these local options:

- `-K PATH` selects a local 260-byte public-key file and still verifies RSA.
- `-f` bypasses the release comparison, while retaining authentication and
  hardware checks. It is needed in this experiment because the candidate keeps
  the source release number.
- `-F` additionally affects the MCU update decision. It is unnecessary here.
- `-t` returns after parsing/authentication, before the installation script and
  flash writer. The authentication comparisons used this verified path. The
  later isolated NAND installation test deliberately omitted `-t`.

Native results are recorded in
`runs/controller-deployment-2026-11-02/native-validation-summary.json`:

| Input | Native result |
|---|---|
| Authentic official package, default key | Success |
| Actual rebuilt custom UI package, default key | Error 6, signature verification failed |
| Same custom package, explicit generated lab key | Success |

The new signing key is laboratory material, not a vendor/device key. Neither an
old digest nor a local override option proves that a bridge can re-sign an image
for stock trust. The current work has not established a network route that both
places an accepted custom key and causes the untouched controller to select it.
Device identity and player firmware-decryption keys are different from the
vendor's firmware-signing private key; having one does not provide the other.

Normal installation also handles kernel/rootfs banks, partition metadata and an
MCU payload. Authentication success alone does not verify physical installation,
bank selection, boot integrity or recovery. The earlier generic application
emulator runs a different Linux kernel. A separate test now boots the original
CR100 kernel with emulated NAND; its results and limits follow.

## Native NAND installation and cold boot

The unchanged 4.3.2 kernel, driver, updater and installation script ran against
synthetic model-2/submodel-3/revision-3 NAND. Both original banks began at
generation 1. A privileged laboratory JFFS seed supplied the generated public
key, candidate package, loopback-only HTTP server and a startup hook that stops
the application. This bootstrap is explicitly not a physical access method.

The exact invocation was:

```sh
/bin/upgrade -f -K /jffs/lab/lab.pub http://127.0.0.1:18091/firmware.upd
```

The updater authenticated the actual custom UI package, selected bank 1 at
generation 2, wrote kernel and rootfs through native MTD, compared the matching
MCU version without programming it, and completed its original post-install
script. Both written payloads match the candidate UPD byte for byte; ECC and
generation markers validate. The manufacturing area, partition table, original
bank 0 and reserved regions remain unchanged. JFFS changed for expected runtime
and upgrade-log writes.

The final reboot returned: the minimal MCU model rejects unimplemented restart
commands 19/20. The native process therefore exits 1 with **error 17, reboot
failure**. This is not a fully successful automatic update/reset test.

After syncing and freezing that media, a separate cold boot loaded the kernel
extracted from the persisted bank. The original kernel printed **Selected boot
section 1 (generation 2)**, mounted its read-only CramFS, and exposed the modified
901,555-byte `MainUI.swf`. The extracted SWF matches the already-tested UI
prototype, SHA-256
`34d251ec355619aebdf7530cb0f8329e027f4126d679e3282784c9bd935032e6`.
The native-kernel test intentionally leaves the UI application stopped; its
interactive controls were validated separately in the exact-executable runtime.

A recovery clone changed exactly one completion-marker OOB byte in bank 1's
rootfs. A separate native cold boot then selected bank 0/generation 1 and exposed
the original 897,065-byte UI. This verifies the kernel's fallback on that fault,
not physical boot-ROM recovery or a general guarantee against failed updates.

The test uses 32 MiB synthetic NAND and an independent emulator BIOS trap ABI.
It does not execute the physical OTP boot ROM, M16 firmware or hardware reset,
nor verify the owner's actual NAND geometry or initial access. The kernel is
selected and extracted on the host between runs; automatic physical bootloader
bank loading is not claimed.

Receipts and reproduction details:
[native test results](runs/controller-native-install-2026-11-02/RESULTS.md).
The serial-model correction is documented in
[SCIF receive regression](runs/controller-native-install-2026-11-02/scif-rx-fix.md).

## Software-only initial-access follow-up

The reachable Pi's DHCP lease identifies the physical CR100 at
`192.168.69.52`. Read-only checks returned HTTP 200 for `/status/VERSION`
(`25.2-50130`), `/status/dmesg` and `/status/ps`. The boot log reports an inability
to open the initial console; the process listing shows the original controller
daemon, with no inetd, telnetd, gdbserver or updater entry. TCP 23 refused the
connection. No login, unlock, service change or physical write was attempted.

An additional plausible UI path was traced in the exact released software:
album-art code really calls `MovieClip.loadMovie`, but HTTP(S) loading explicitly
sets image-only mode and rejects SWF content. Local SWF interpretation and a
retained `UIDiagPanel.swf` loader do not establish remote delivery or execution;
that diagnostic file is absent from the stock filesystem. These observations
do not establish a supported software-only initial-access method, nor rule out
all possible vulnerabilities.

The retained hidden diagnostic menu was also traced through setup/empty-Zones
callers. It offers hardware tests, conditional burn-in and power-off, with no
package installer or service-enable action. A bounded search of public Sonos
support/release material found no distributed CR100 service package or owner
maintenance procedure. This leaves initial software access unresolved; the
deeper entry-point audit remains incomplete after an automatic cybersecurity
review rejection. The complete firmware-loading goal is not achieved.

[Software-only audit and owner-run commands](runs/controller-native-install-2026-11-02/initial-access-audit/README.md)
contain bounded physical receipts and exact loader/startup evidence. The owner
also supplied case FCC ID **SBVCR002**; its filing covers the radio module and
does not uniquely identify the host flash or service wiring.

**Follow-up (November 3):** `/unlock` is live-confirmed reachable on the
physical controller and its credential is the stored device identity key
record, obtainable from the same external NAND backup that provides owner
recovery. The strongest current route is that hardware-first composite; the
missing facts are the F3 board/chip identification gates. See
[unlock-route follow-up](runs/unlock-access-route-2026-11-03/README.md).

## Stock-firmware menu alternative

The successful original **4.3** unmodified-firmware path is:

```text
Music > Speaker Controls > Software ZP80 > Bass > Bass +2
    > Add to My Speaker Controls Library
```

The value is nonplayable. Its menu contains the explicit library action without
Play/Queue choices. That action sends
`createItem(favorite="ctl:apply:r1:bass:p2")`; only this write request changes the
simulated setting. Folder browsing, search and metadata prefetch stay read-only.
An independent SOAP query returned Bass 2, Treble 0, Loudness 1 and LED On. A
second explicit action selected Bass -10, demonstrating a second absolute value.
The log contains no Play, SetAVTransportURI, AddURIToQueue or getMediaURI request.

This uses the legacy [Sonos service-favorites action](https://docs.sonos.com/docs/add-favorites).
On this old controller, downloaded menu-label overrides did not replace the
firmware's fixed library wording. The action can therefore work without a
firmware change, but does not have the native settings presentation.

The matching 4.3.2 browse-action function was compared statically; it is not a
dynamic stock-4.3.2 or physical result. The fixture used OK-button menu prompting.
Negative `OkMenuSelection` can bypass the action menu for direct Play Now/Add End;
both preferences need acceptance tests. The exact evidence and preference caveat
are in [the stock-service notes](runs/controller-deployment-2026-11-02/bridge-only-options.md).

Production integration should expose each room's actual capability-supported
settings, include the explicit room identity in issued action IDs, use absolute
assignments, handle retry receipts and stale views, and confirm backend readback
before reporting success. The existing Sonos settings provider can serve Sonos
speakers through either backend. Generic HA speakers must retain their own
capability limits. Multiple service descriptors/accounts must coexist with
Spotify; the temporary fixture advertises just its test service.

**Follow-up:** production integration is now implemented and deployed as the
separate **Speaker Settings** Music service alongside Spotify. Exact stock
4.3.2 acceptance passed with the production service and simulated provider;
the Pi catalogue agrees with read-only ZP80 values. The controller's **OK Button
must use Show Menu**. Line-In editing, live HA and hands-on physical application
remain open. See [current findings](RESEARCH.md#speaker-settings-deployment-follow-up).

Evidence: `stock-menu-validation.json`, `bridge-controls-probe.jsonl`,
`stock-controls-explicit-action.png` and
`stock-controls-bass-two-readback-view.png` in the deployment run directory.

## Physical access and recovery findings

Exact 4.3.2 has an authenticated maintenance route and a writable JFFS startup
hook. Neither supplies initial access without the required credential or a
privileged file-write method. `/set-local-setting` is a stub in this image. No
usable arbitrary upload route or genuine CR100 identity backup was established.
The existing physical NAND/console backups in this workspace concern the ZP80,
not the handheld.

The exact controller kernel defaults to `console=ttySC0,38400`. Both serial-driver
initialization and console registration check manufacturing flag `0x02`. The
physical controller's flag value, UART pinout and connector voltage remain
unknown. Public CR100 teardown/FCC photographs identify board/connector leads,
including J8004, without a verified serial or recovery procedure. Details and
primary source links are in `hardware-recovery-references.md` in the run.

An [external-flash investigation](runs/controller-native-install-2026-11-02/external-flash-route.md)
also identifies programmer profiles for documented Samsung NAND variants and
ways to preserve physical-page/OOB data. The owner's full chip marking,
package, voltage, isolated access and independently verified read/restore path
remain missing. The separate original OTP boot ROM must be preserved; it is
not an ordinary erasable flash device.

After legitimate privileged access and recovery are established, the narrowest
first physical experiment is a temporary JFFS-hosted SWF bind over the stock UI,
followed by a controlled application restart. A complete application exit is
needed before unmounting. A Configure.dev hook has now run in the synthetic
native-kernel laboratory, but remains untested on the physical controller.
Factory reset, M16 recovery and player bootloader procedures
are not verified recovery methods for this modified handheld UI.

## Reproducible research artifacts

The earlier application/signature phase artifacts are under
`runs/controller-deployment-2026-11-02/`:

- `build_custom_cr100_update.py`: pinned original input, CramFS CRC and inode
  validation, preserved compressed blocks for unchanged files, no-op round-trip
  equality, and per-file/record receipts.
- `25.2-50130-1-2-settings-ui-lab-signed.upd`: actual custom image,
  SHA-256 `99d70a0b20d5ac0e192128debe202410d4fadb2c926fcc4bd8fd038d01a211a2`.
- `lab_sign_upd.py`, `native_check_only.py`, `fixture_http.c`: signing and
  authentication-only laboratory tests. They do not establish physical access.
- `stage_custom_runtime.py`, `control_server_4_3_2.py`,
  `cr100_hw_shim_4_3_2.c`, `version-layout.json`: filesystem/application emulation
  with unchanged exact native executable bytes and explicit helper addresses.
- `update-signatures.md`, `physical-options.md`, `bridge-only-options.md` and
  `hardware-recovery-references.md`: deeper evidence and remaining gaps.

The original running shared application VM and its writable disk were preserved;
this firmware experiment did not change the production bridge service. The
shared QEMU source `hw/char/sh_serial.c` was corrected and rebuilt for the
confirmed SCIF receive bug. Native tests used separately copied binaries with
recorded hashes. Their artifacts are under
`runs/controller-native-install-2026-11-02/`.
The viewer at `http://127.0.0.1:8876` now shows the later stock-4.3.2 Speaker
Settings acceptance fixture. The earlier patched-UI evidence remains in its run
directory; it is not the currently loaded MainUI.
