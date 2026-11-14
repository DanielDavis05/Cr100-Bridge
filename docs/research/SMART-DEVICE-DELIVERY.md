STATUS: PARTIAL

# Smart devices: delivery note

Written 2026-11-14 at the end of the build described in
`SMART-DEVICE-IMPLEMENTATION-PLAN.md`. Two agents worked on it. The first (codex) wrote the
bridge and firmware code, ran the emulator acceptance and kept a running log. It ran out of
usage before building the release. A second agent (Claude) reviewed that work, fixed one
defect, built and verified the release, deployed the bridge and wrote this note. Below,
**verified** means the second agent saw it directly. **Relayed** means it comes from codex's
log and evidence, with only spot checks.

Why PARTIAL: everything in the plan is built, deployed and staged. The one gap is the owner's
late permission to test real lights in the test room. That test was prepared but not run,
because this session's permission system refused the command that would have switched the
lights. No real light, switch, fan, scene, script or speaker was switched by either agent.

## What was built

- **Bridge** (uncommitted on `feature/smart-devices`):
  - `tools/ha_hub.py`: one shared Home Assistant connection per player process, whatever
    the music source. Also holds the entity, device, area and floor registries.
  - `tools/smart_devices.py`: the pure catalogue.
  - `tools/smart_device_service.py`: `SD:` browsing, view tokens, coalesced container
    updates, and the three actions (`devices.power`, `devices.level`, `devices.activate`).
  - Wiring in `zoneplayers.py`, `ui_events.py`, `ha_client.py` (lists of entity ids, a typed
    "no acknowledgement" error), `ha_backend.py`, `ha_linein.py`, `bridge_config.py`
    (keeps `devices_area`) and the web UI status.
  - `fake_ha.py` gained registries and device services. New tests are in five
    `test_*smart*`/`test_ha_hub` modules. The new modules are in `RUNTIME`.
- **Firmware** (uncommitted on `feature/smart-devices`):
  - `tools/smart_devices_ui.py`: the Flash UI patch, steps 1-7.
  - The R8 profile in `modern_tls.py` and `build_tls_release.py`, and the declared
    resource additions in `compose_ui.py`.
  - `tests/test_smart_devices_ui.py`, and updates to the docs (`ui-events.md`,
    `feature-roadmap.md`, `changing-the-flash-ui.md`, README).
- **Docs**: `docs/features/SMART-DEVICES.md` (owner guide) and a README line.

### Review by the finishing pass (verified)

The second agent read the diffs against plan sections 3 and 4:

- Browsing `SD:` only rebuilds the catalogue from cached registries and states and never
  calls an HA service. The single `call_service` is in the action worker.
- Every action looks up the view token and then the row id among that view's rows, which
  come from the bridge's own catalogue for the room's bridge topology. The HA call targets
  `row.targets`, never a controller-supplied id. Folders, unavailable rows and unsupported
  actions are rejected, and the check runs again immediately before the HA call.
- Power is `turn_on`/`turn_off`, taken only from `desired` in `on`/`off`. There is no
  toggle. A level of 0 on a light is `turn_off`. Fans use `set_percentage`.
- `ha_hub.py`, `smart_devices.py` and `smart_device_service.py` are in `RUNTIME`, and the
  built archive contains them.
- `pi_deployment_safety.py` does not exist in either repository, so nothing needed removing.

### Defect found and fixed

- **The R8 release could not pass its own verification.** The Devices patch receipt
  listed `added_resource_hashes` as tuples. `verify_release` compares the in-memory receipt
  with `manifest.json` as read back from JSON, where tuples become lists, so the build
  always ended in "manifest disagrees with reviewed composition: ui".
  - Fix: the receipt now uses lists (`tools/smart_devices_ui.py`), and
    `tests/test_smart_devices_ui.py` has a JSON round-trip regression.
  - The composed `MainUI.swf` bytes did not change.
  - The failed build attempt is kept under `build/_superseded/`.

### Observation, not changed

In the real house, Hue room groups are assigned to a separate area rather than to the rooms
they light. Following plan §3.2 literally, as codex's test
`test_groups_only_live_in_their_own_area_cross_area_members_keep_theirs` asserts, the test
room's main list is then only "All Lights", "Individual Lights >" and "Other Rooms >". Its
lamps sit one level down, and the group row appears under the other area. This is neither
broken nor unsafe, but the owner may prefer one of two alternatives:

- reassign those groups to their rooms' areas in Home Assistant, or
- change the rule so that members leave the main list only where their group row is shown.

The owner guide's troubleshooting table now explains the first option.

## Test results

| Suite | Result | Source |
| --- | --- | --- |
| Bridge, full (`unittest discover -s tools`) | 1338 tests OK, 2 skipped (legacy RadioPatch fixture paths) | Verified on this working tree by the supervising agent before the finishing pass; no bridge code changed afterwards (one owner-guide row only) |
| Firmware, full (`tools/run_tests.py --directory tests`) | 248 tests, 0 failures/errors, before and after the receipt fix | Verified |
| Firmware focused `test_smart_devices_ui.py` | 14 tests OK after the fix | Verified |
| `test_link.py` | Passed | Relayed |
| `verify_release.py` R8, R7, R6c | All verified (R7/R6c with `--source` pointing at the stock package, whose manifest path moved) | Verified |
| Earlier release folders | 416 files byte-identical to the pre-build snapshot | Verified |

## Emulator acceptance

Disposable QEMU SH4 overlay, exact stock 4.3.2 native, real bridge code with `fake_ha.py`
and invented devices. Evidence: `research/bridge-runs/smart-devices-build-2026-11-14/`.
All rows were run and reviewed by codex (relayed). The finishing pass re-inspected the
screenshots marked "verified".

| Case | Observed | Notes |
| --- | --- | --- |
| Entry shown, hidden on HA drop, back without navigation | Observed | |
| Absolute power: All Lights, group row, single light | Observed | Accepted from a quiet replay. The first run showed a transport modal. The finishing pass viewed the settled "Ceiling: Off" frame (verified) |
| Brightness slider, slow and burst, Back flush | Observed | |
| Fan speed | Observed | |
| Scene and script "Started" | Observed | The finishing pass viewed scene "Started" (verified) |
| Failure dialog, notify and deferred | Observed | |
| Stale list rejected, reselect works | Observed | Quiet replay |
| Grouped rooms | Observed | |
| HA drop and return inside a list | Observed | |
| Unavailable device row inert | Observed | |
| Nested folders and Back | Observed | |
| Old bridge: no Devices entry | Observed | |
| R8 regression of Music root, Room Settings, library softkeys, Queue, About | Observed | Machine checks by codex. The finishing pass reviewed every screenshot (verified): correct |
| Final emulator identity and owned shutdown | Observed | `FINAL-OWNED-SHUTDOWN.json`: QEMU stopped, ports closed, baseline image unchanged. No emulator process was running (verified with `ps`) |

Known emulator behaviour (relayed): under heavy harness polling, a few replies from the
emulated guest crossed the controller's roughly 2 s HTTP deadline, and the controller showed
"Device action unavailable" even though the action ran. Quiet replays of the same code were
clean. Watch for this on hardware.

## Real devices

| What | Status |
| --- | --- |
| Read-only checks against the real HA (registries, states) | Done, verified (see Deployment) |
| Owner-authorised light tests in the test room | **Not run.** The finishing pass resolved the room read-only to exactly one HA area with four dimmable lamps and prepared a script. The script would have sent single-lamp off/on, All Lights off/on and one brightness change through the deployed modules' UI-event → `SmartDeviceService` path, then restored the starting brightness. Its dry run worked. The live run was refused by the session's permission system, and the agent did not work around that. The lamps were never touched |
| Speaker play/pause in the test room | Not run (not needed to verify the feature) |

## Deviations from the plan

- **Source switch reconnects HA.** A web UI source switch re-executes the player process,
  so the shared HA connection reconnects. Within one process, closing a backend keeps the
  connection. (relayed)
- **Feedback mode is fixed by the bridge at receipt.** The controller reads
  `CR100_DevicesFeedback:<request>` after SetString. Activations always use deferred mode,
  so "Started" appears only after HA confirms. (relayed)
- **Grouped zones use folder ids `SD:r:<hash>`, one per member room.** (relayed)
- **One sender for all actions.** A single worker sends every HA call, which is stricter
  than one call in flight per target. A lost HA reply holds the affected targets until the
  reply arrives ("Device busy"). (relayed)
- **The Devices availability flag needs a paired controller,** so the deployment check
  inferred availability from the bridge's own status instead (connected, 383 devices).
- **R7/R6c verification needs `--source`.** Their manifests point at a stock-package path
  that has moved.

## Deployment (verified)

- **Ancestry check.** The Pi ran `release-review-pKgFJjIm`, stamp `8c46c386`, clean. That
  commit is an ancestor of this checkout's HEAD `83264bb5`, so the deploy went ahead.
- **Pre-checks.** `cr100-pair` was inactive. Both controller update records were
  `expired`, the bridge update `active` was null, and no installer was running.
- **Backup:** `/var/lib/cr100-bridge.bak-20261114T164605Z`. A recursive compare with the
  live directory showed no differences.
- **Install.**
  - Archive SHA-256 `a7461780e616d7ebe6ec6303f6c628a63c12eb288e72a3c010e842602202edb8`,
    stamp `83264bb5-dirty`.
  - `./install.sh --check` passed. `sudo ./install.sh` succeeded.
- **Current release:** `/opt/cr100-bridge/releases/release-rj31PaPS`.
  **Previous:** `release-review-pKgFJjIm`.
- **Health checks**, all passed:
  - All three services `active` with `NRestarts=0`, including after staging.
  - `wlan0` and `mon0` present, 0 firmware crashes, no devcoredump. Nexmon status ok with
    0 allocation failures.
  - The controller is associated, has its lease, answers ping and port 3400, and was not
    rebooted.
  - All 7 virtual rooms answer `device_description.xml` with 200.
  - No tracebacks in the journal.
- **HA connection.** HA connected about 50 s after the restart. Before that, name
  resolution failed and the client retried every 5 s.
- **Feature checks, read-only:**
  - Status: enabled and connected, 10 areas, 383 devices. All 7 rooms resolved to their
    expected areas.
  - A Browse of `SD:` for the test room returned All Lights, Individual Lights and Other
    Rooms. Individual Lights held four lamps at "On · 85%". HA states read on a separate
    read-only connection show those four lamps on at brightness 216/255, which is 85%.
  - Sonos configuration switches and disabled entities were correctly absent.
  - Other Rooms listed floor folders. No `smart-device-action` log lines appeared, so no
    action ran.

Rollback:

```sh
ssh <pi> 'sudo ln -sfn /opt/cr100-bridge/releases/release-review-pKgFJjIm /opt/cr100-bridge/current && sudo cr100-bridge restart'
```

## Controller package

- **Release:** `cr100-firmware/build/releases/re100-cfw-20261114-r8/`, which includes
  `ACCEPTANCE.md`.
- **Package:** `cr100-custom-4.3.2.upd`, 5,914,269 bytes, SHA-256
  `769f6d7d5db3ae7002a5c253484540b9f04fc33a78d08382f165f408b227dbaa`.
- **UI-only.** Only `MainUI.swf` changed, so also `rootfs.cramfs` and the package; the app
  and sidecar are identical to R7. `MainUI.swf` SHA-256 `50e32e1b…59ad5` equals the preview
  accepted in the emulator.
- **Staged for the controller that runs R7,** with `arm=False`: status `staged`,
  `force_prompt` false, no offer expiry. Staging replaced that controller's expired R7
  record, and the bridge dropped its stored copy of the R7 package, which stays available
  in `build/releases`.
- **Not armed and not installed.** Nothing was written to the controller.

## Owner's first session on the physical controller

1. In the web UI's Controllers page, review the staged R8 package (the vendor-signature
   warning is expected) and select Install. On the CR100, open Online Updates and confirm.
2. After the reboot, check that About My Sonos System shows "Release: R8 (2026-11-14)".
3. Open the test room, then Music. **Devices** should be at the bottom.
4. Open Devices, then Individual Lights, and press OK on one lamp. It should turn off with
   no dialog, and the row should change to Off within a second or two. Press OK again to
   turn it back on.
5. On that lamp, press Brightness, move the slider a few steps, then Back. Check the lamp
   and the row agree.
6. Try All Lights off and on, then a group row in another area via Other Rooms.
7. Try a scene only if you know what it switches. It should show "Started".
8. Watch for "Device action unavailable" dialogs during normal presses. Report any you see.
9. Confirm Music Library, Queue softkeys and Room Settings behave as on R7. If anything is
   wrong, R7 is still in the other bank.
