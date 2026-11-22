# Interactive CR100 4.3.2 flash tool

The default profile handles a **future UI-only update** for the owner's model-2 CR100 at
`192.168.69.52` (`00:0e:58:02:6f:a9`). It uses the Kali Pi's `sonos0` address
`192.168.69.1`. It will refuse the package that is already active. Build and
sign a new UPD before using the `flash` command.

The tool checks that the lab-signed UPD changes only `/opt/swf/MainUI.swf` in
the stock 4.3.2 root filesystem. It requires unchanged stock installer,
kernel, partition tables and MCU record. The lab **public** key goes into the
Pi bundle; the signing private key stays on the workstation. The stock key on
the controller is not replaced.

The separate `combined-release` profile is pinned to the exact
`projects/cr100-firmware/build/release/cr100-custom-4.3.2.upd` artifact. It permits
exactly two reviewed changes: `MainUI.swf` and the one-byte `/bin/upgrade`
signature-policy patch. After boot, that updater accepts later packages even
when their signature check fails. The initial install still uses the current
stock updater and the matching development public key.

## Make a bundle on the workstation

Run from `/Users/daniel/Documents/fw` and choose a new output name for each
package. The output directory must not already exist. Input locations follow
`tools/workspace.py`; use `CR100_WORKSPACE`, `CR100_BRIDGE_RUNS`,
`CR100_STOCK_UPD`, or `CR100_FIRMWARE` for another workspace layout:

```sh
PYTHONPATH=projects/cr100-bridge/tools python3 -m cr100_flash.make_bundle \
  --candidate /absolute/path/to/new-ui-lab-signed.upd \
  --output /Users/daniel/Documents/fw/research/bridge-runs/future-ui-flash-bundle-01
scp -rp /Users/daniel/Documents/fw/research/bridge-runs/future-ui-flash-bundle-01 \
  kali@10.0.0.170:/home/kali/
```

For the exact combined release, add `--profile combined-release` and use
`/Users/daniel/Documents/fw/projects/cr100-firmware/build/release/cr100-custom-4.3.2.upd`
as the candidate. The bundle builder selects its development public key and
checks both hashes against the reviewed release. A rebuilt package has a new
hash and requires a separate review.

The bundle is private (`0700` directory, `0600` files) and contains its own
file hashes. Keep the bundle private because it contains the temporary root
access artwork. It does **not** contain the signing private key.

## Run on the Pi

```sh
ssh kali@10.0.0.170
cd /home/kali/future-ui-flash-bundle-01
python3 -m cr100_flash.flash check
python3 -m cr100_flash.flash flash
```

`check` is offline. `flash` confirms the exact MAC and release, asks for
`READY` after stable cradle power is set, then advertises **Install firmware**.
Select that room on the CR100 and open Now Playing; press Enter at the Pi prompt.
The artwork initially is harmless. The script waits for that fetch before
arming one root-shell artwork image. It removes the temporary room and address
once the root listener opens and restores the normal UI.

Next it copies read-only helpers to controller RAM, checks MCU version,
captures NAND twice and 22 diagnostic/settings files into a new private run
directory, and checks the live NAND boot selection and partition table. It
requires both boot/system areas and their read-status sidecars to match between
captures; changes confined to the live JFFS settings area are recorded. It
compares the active root filesystem hash so it cannot reinstall an already
active image. It then invokes the stock updater's **verification-only** mode
(`-t -f -K`) and stages a launcher guarded by that run's current boot section
and generation. The install uses `-f -K`; it never uses uppercase `-F`.

Before any flash write, the script shows the candidate hash and destination
bank. Type the displayed `FLASH <eight hex digits>` phrase to authorize that
one launch. Keep the Pi and controller powered until the controller restarts.
The script will observe an uncertain launch result but will never retry it.
After the reboot, it checks the controller upgrade log for the expected bank,
generation and completed writes, then asks you to confirm the UI menu.

An optional read-only rehearsal runs all preflash checks, then closes the
temporary root listener:

```sh
python3 -m cr100_flash.flash preflight
```

The later `flash` command repeats the fresh backups and all checks; it does
not reuse a prior boot-state result. Each run is saved under
`/home/kali/cr100-flashes/<UTC-time>-<id>/`. Copy a successful run directory
to private workstation storage for a second copy. The NAND files include
software read-error placeholders and are diagnostic captures, **not** exact
chip-level recovery images. The tool does not capture M16 internal firmware
or radio EEPROM. A hardware programmer is needed for a full raw recovery
image. UI behavior still needs the on-device menu check after reboot.

The successful Nov 4 install used a one-shot section-1/generation-10 launcher.
This tool generates a new launcher from the live section/generation on every
run. The existing package used that day is bundled at
`research/bridge-runs/physical-upd-2026-11-04/future-flash-bundle-20261104` as an offline example; it
is already active on the controller and is not a new update to install.

The generated bank-0/generation-11 launcher was also exercised in a disposable
emulator copy of the installed NAND: it rejected stale state, then wrote the
opposite bank at generation 12 with matching kernel and root filesystem. This
does not test the complete interactive Pi workflow on the physical controller.
