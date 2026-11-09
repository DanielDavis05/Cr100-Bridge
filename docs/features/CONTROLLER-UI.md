# Restoring the original CR100 settings UI

This remains a laboratory prototype. [Consolidated findings and open items](../research/RESEARCH.md)
record the adopted next production route: a separate **Music > Speaker Settings**
service on unchanged controller firmware. That service is not yet implemented
or deployed; the native-UI and firmware-loading work below retain separate limits.

The bridge cannot add entries to the stock System Settings menu. The controller's
`MainUI.swf` constructs that menu locally. A prototype modifies that original UI
program to add **System Settings > Speaker Settings > room**, using the retained
native Bass, Treble, Loudness and Status Light interfaces.

This is a controller-software modification. It does not require a new UI engine
or a replacement `hh.anacapad` executable for these four controls. The menus are
reconstructed bindings, rather than a single hidden feature flag.

## Reproducible prototype

`tools/patch_controller_settings_ui.py` accepts only the two original SWFs whose
hashes were examined: 4.3 / 24.1-75101 and 4.3.2 / 25.2-50130. For example, from
this repository:

```sh
python3 tools/patch_controller_settings_ui.py \
  --input ../cr100-emulator/rootfs/opt/swf/MainUI.swf \
  --output runs/settings-ui-prototype-2026-11-02/rebuilt-4.3.swf
```

It replaces `Node_populateSettings`, adds eight ActionScript helper functions,
rebuilds tag/function lengths and branch offsets, and verifies that unrelated
named functions and non-action resources remain unchanged. The original input
is preserved. The result is an unpackaged SWF, **not a signed firmware update**.

The first prototype:

- Enumerates visible compatible players and targets the chosen player's UDN.
- Uses the original centered slider and On/Off menu widgets.
- Maps Bass/Treble to physical units −10…+10. The native integer-widget API uses
  twice the physical EQ value, so wrappers divide reads and multiply writes.
- Retains the native fixed-output EQ block, including Loudness.
- Checks for a removed/unavailable room before activating a control.

Slider movement applies immediately. Closing with Back or OK flushes the native
cache; it does not undo the setting.

Evidence, SWFs, receipts, screenshots, packet capture and installation research
are under `runs/settings-ui-prototype-2026-11-02/`. Runtime validation uses a
separate qcow2 overlay and localhost viewer at `http://127.0.0.1:8875`; it does
not use the shared emulator's writable disk. No changes were deployed to the Pi
or physical controller for this prototype.

On the 4.3 emulator, all four controls have passed native SOAP dispatch and
direct software-player readback. Bass was exercised through +10 and Treble
through −10; extra wheel input stayed within range. Loudness and Status Light
were switched off and read back. A follow-up now runs the rebuilt 4.3.2
filesystem with the unchanged exact 4.3.2 native executable and version-specific
emulator helpers. All four native controls pass SOAP readback there too: Bass
+1, Treble −1, Loudness Off and LED Off. See the
[deployment investigation](../research/CONTROLLER-DEPLOYMENT.md) for current validation and
the distinction between application runtime and firmware installation.

## Backend integration

The UI sends standard native Sonos commands. The package 2 bridge routes already
handle those commands for verified Sonos speakers through either the direct
Sonos backend or the Home Assistant backend. Generic HA speakers do not acquire
Sonos-only capabilities from this UI modification.

The prototype currently shows the same four controls for every compatible room.
Before release, it needs capability-aware visibility and clear unsupported/error
behavior for generic HA rooms and partially capable players. It has not yet been
tested end to end through the live bridge, against HA, with multiple rooms, with
a room disappearing, or against a fixed-output player. The simulated player is
not evidence for physical audio behavior.

A controller modification is also avoidable if the controls live under Music.
An isolated original **4.3** stock-firmware test exposes **Speaker Controls > room > Bass**,
with nonplayable value items and an explicit SMAPI library-add action. That
action changed simulated Bass from 0 to +2, passed SOAP readback, and made no
playback requests. The fixed label remains “Add to My Speaker Controls Library”.
This is a tested alternative interaction route; production integration with
the bridge and capability providers remains unfinished.

The equivalent 4.3.2 browse-action gate matches statically; an exact-4.3.2 dynamic
stock-service test and physical acceptance are still open. The fixture used the
OK-button menu-prompting preference; direct Play Now/Add End preferences need
separate reachability checks. The four-control 4.3.2 test above uses a patched SWF.

Balance, fixed/variable output, Line-In name/icon/gain, sleep-timer extensions and
alarms remain separate UI work. Existing stock Sleep Timer is still accessible
under Music > Clock and Alarms.

## Installing on the physical controller

The promising route is a writable `/jffs` UI file with a temporary file bind mount
over `/opt/swf/MainUI.swf`, followed by a controlled app restart. This preserves
the underlying system image and original config path. A copied `-c` configuration
with a different `SWFFilePath` is another way to test the loader once local access
exists. The stock startup does not automatically select a copied configuration.

Exact 4.3.2 BusyBox successfully created and removed this file bind mount in the
disposable emulator. After unmounting and restarting, the original SWF hash and
three-entry stock System Settings menu returned. The tested prototype was then
mounted again for review. A complete app exit is needed before unmounting; an
immediate attempt while it was still exiting returned “Device or resource busy.”
This validates reversible loading on the earlier emulator kernel, not the
physical stock kernel. Physical reboot rollback and a persistent startup hook
have not been tested.

The physical blockers are still privileged file access, a verified backup and
recovery route, and stock-kernel/space validation. `/jffs/system/Configure.dev`
is a real startup hook, but no persistent hook has been installed. Persistence
should follow a successful temporary trial and an independently tested rollback.
An emulator management shell is not a physical-controller access method.

Repacking the official UPD invalidates its vendor signature. No normal custom
signed update route has been established. The available older 4.3 image also
omits these menus, so downgrading to that image does not restore them. Detailed
evidence is in `physical-install-research.txt` and
`physical-overlay-next-experiment.txt` in the run directory.

The later [native NAND test](../research/CONTROLLER-DEPLOYMENT.md#native-nand-installation-and-cold-boot)
now verifies the original updater writing the custom image to synthetic NAND,
completing post-install and mounting the installed bank on a separate cold boot
of the original kernel. Automatic reset remains unmodeled and returns error 17.
This uses an explicitly privileged JFFS laboratory bootstrap and independent
emulator boot ABI; physical access, physical boot-ROM acceptance and recovery
remain unresolved.
