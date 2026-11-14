# Smart devices on the CR100: implementation plan

Written 2026-11-14. This is the build brief for the "Devices" feature: Home
Assistant lights, switches, fans, scenes and scripts controlled from the CR100's
Music menu. It covers both repositories:

- `cr100-bridge` (this repo): catalogue, list serving, actions, deployment to the Pi.
- `cr100-firmware` (sibling): the Flash UI patch and a new UI-only CFW release.

Read these first. They are the evidence this plan rests on, and they name the
prototype sources to reuse:

1. `docs/research/SMART-DEVICE-UI-FEASIBILITY.md` (round one)
2. `docs/research/SMART-DEVICE-UI-FEASIBILITY-2.md` (round two; its
   recommendations are adopted below)
3. Evidence indexes under `../../research/bridge-runs/smart-devices-2026-11-14/`
   and `../../research/bridge-runs/smart-devices-followup-2026-11-14/`
   (`slider/experiment-1.md`, `lists/helpers.md`, `rooms-visual/worked-helpers.md`)
4. `docs/features/DIALOGS.md`, `docs/features/SOFTKEYS.md`, `docs/guides/deploying.md`
5. `cr100-firmware/docs/ui-events.md`, `docs/guides/changing-the-flash-ui.md`,
   `docs/guides/building-a-release.md`, `docs/guides/shipping-a-release.md`

Where this plan names a function, file or field, confirm it against the code
before relying on it. If the code contradicts the plan, follow the code, keep
the behaviour the plan asks for, and record the deviation in the delivery note.

## 1. What the user gets

A **Devices** entry in the Music menu of a room, shown only when the bridge has
a working Home Assistant connection and something to control. It appears for
rooms from either music source (Home Assistant or native Sonos). With no Home
Assistant configured, or on a bridge without this feature, the entry is absent.

Inside, for a single room whose speaker sits in HA area "Kitchen":

```
All Lights            On · 60%
Counter               On · 80%        <- light group
Ceiling               Off             <- light not in any group
Coffee Plug           On              <- switch
Extractor Fan         Off
Scenes                >               <- scenes and scripts of the area
Individual Lights     >               <- members of the groups above
Other Rooms           >               <- every other area, by floor then name
```

- **OK** on a device row switches it on or off. No dialog on success.
- **OK** on a scene or script runs it and shows "Started" for about 1.2 s.
- A **Brightness** softkey on dimmable lights (including All Lights and groups)
  and a **Speed** softkey on fans with percentage support open the existing
  centred slider, 0-100, titled with the device name, starting at the current
  value.
- Rows show an on/off bulb icon; folder rows keep the native arrow; no
  music-note icon.
- When the selected zone is a **group of rooms**, the list starts with
  **All Lights** for the whole group, then one folder per member room (each
  folder is that room's list as above, including its own All Lights), then
  Other Rooms.
- When Home Assistant drops while a list is open, the rows are replaced by one
  inert row "Home Assistant unavailable" and come back on reconnect. At the
  Music root the Devices entry disappears and returns without leaving the menu.

Device types in this version: `light`, `switch`, `input_boolean`, `fan`,
`scene`, `script`. Not in this version: covers, climate, locks, alarm panels,
buttons, sirens, valves, light colour, sensors. Do not add them.

## 2. Decisions already made (do not reopen)

| Topic | Decision | Source |
| --- | --- | --- |
| Listing | Bridge-owned ContentDirectory container with prefix `SD:`. Browsing never writes to HA. | round 1 |
| Writes | UI events over the existing `CR100_UIEvent` transport. | round 1 |
| Power | Send an **absolute** desired state taken from what the row displayed. Never send "toggle". No result read and no forced re-browse on success; the bridge publishes a container update and the list refreshes from it. | round 2, §2 §4 |
| Errors | Transport-error NOTIFY scoped to the calling controller's AVTransport subscription, then an ordered OK reset. Fallback when no matching subscription exists: the 250 ms deferred result read. One feedback mode per action, never both. | round 2, §3 |
| Slider | Variant B: thumb moves locally, 300 ms trailing timer, send newest absolute value, flush once on Back, target identity captured at open. | round 2, §1 |
| Identity | Every action carries a bridge-issued view token and a row id. The bridge re-checks both at receipt and again immediately before the HA call. Structural changes invalidate the view; ordinary state changes do not. | round 2, §5 |
| Optimistic paint | Not built. Authoritative refresh only. | round 2, §12 |
| External updates | Coalesce to at most one container update per 2 s per container with a trailing publish. Updates caused by the user's own action, failures, availability and structural changes bypass the cap. | round 2, §6 |
| Structure | Native folders. No heading rows. | round 2, §7 |
| Groups | Catalogue is the union of the member rooms' areas; root refresh on membership change. | round 2, §8 |
| Availability | Root-only 1 s poll of the availability flag, rebuild on change, cancelled on leaving the root. Open lists change through container updates. | round 2, §9 |
| Icons | Class-driven on/off bulbs using verified unused low resource ids (900/901 worked; 9001/9002 did not). | round 2, §11 |
| Softkey labels | Distinct wrapper handler per label (Brightness, Speed). | round 2, §11 |
| Scope of backend work | Shared HA connection plus smart devices. Moving Sonos rooms onto the native backend under HA is **out of scope**. | owner |

## 3. Bridge work

### 3.1 Shared Home Assistant connection

Today `HABackend` creates and owns the `HomeAssistant` client, so there is no
HA connection when the music source is native Sonos. The feature needs one
whenever HA credentials exist.

- Add a small owner (suggested `tools/ha_hub.py`) that holds one
  `HomeAssistant` client per player-service process, created from the
  configured HA URL and token regardless of the active music source.
- `HABackend` uses the hub's client instead of constructing its own. Closing or
  reloading the HA backend (including the web UI's source switch) must not
  close the shared client while HA credentials remain configured. Changing or
  removing credentials replaces or closes it.
- Find out how `bridge_config.py` and `webui.switch_source` keep the HA URL and
  token when the source is switched to Sonos. If they are stashed where the
  player service cannot read them, make them readable without changing what
  the web UI shows. Existing configs must keep working with no manual edit;
  add a config migration only if unavoidable, and test it.
- The hub also owns the registry snapshot: `config/entity_registry/list`,
  `config/device_registry/list`, `config/area_registry/list`,
  `config/floor_registry/list`, refreshed on connect and every 60 s (and on the
  registry-updated events if that is cheap). `ha_linein.py` currently pulls the
  entity and device registries itself; make it read the hub's snapshot so the
  bridge fetches each registry once. Its behaviour must not change.
- Extend `HomeAssistant.call_service` to accept a list of entity ids. Keep the
  existing signature working.
- The web UI process keeps its own connection. Do not merge processes.

All existing tests must still pass after this step, before any smart-device
code is added. Run them.

### 3.2 Catalogue (`tools/smart_devices.py`)

Pure model code, unit-testable without a network.

**Area of a room.** For an HA-source room: the area of its `media_player`
entity (entity `area_id`, else its device's area). For a native Sonos room:
find the HA Sonos device with the same RINCON id (reuse the matching already
in `ha_linein.py`: identifiers, then `configuration_url` as a seed only) and
take its area. A per-room config override `devices_area` (an HA `area_id`, or
an empty string meaning "no area") wins over both. A room with no area still
gets Devices, containing only Other Rooms.

**Which entities.** Domain in the list from §1; not `disabled_by`; not
`hidden_by`; no `entity_category`; state object present. Area of an entity:
its own `area_id`, else its device's. Entities with no area are omitted.
Without the category filter the Sonos integration's own switches (loudness,
crossfade) appear, so test that they do not.

**Light groups.** A light whose state has a non-empty `attributes.entity_id`
list is a group (this catches Hue rooms and zones as well as `platform ==
group`). Its members that are in the catalogue move into Individual Lights and
leave the main list. A group whose members are exactly all the area's lights
is dropped, because All Lights already covers it. A group whose members are in
a different area is shown in the group's own area only.

**All Lights.** Present when the area has two or more individual lights. Its
target is the explicit list of the area's individual light entity ids (not an
`area_id` target, and not the groups, so filters apply and nothing is switched
twice). State: On if any member is on; brightness is the rounded mean of the
members that are on and report brightness. For a grouped zone the top-level
All Lights targets the union of the member rooms' lights. `light.toggle` on an
area inverts each light separately, which is why only absolute on/off is sent.

**Names.** Use the registry name, else `friendly_name`. Strip a leading area
name ("Kitchen Ceiling" becomes "Ceiling" inside Kitchen) unless that leaves
nothing. Sort: All Lights, groups, lights, then other devices, each by name
(case-insensitive); then the folders in the order shown in §1. Rows fit about
24 wide characters; put the state last so truncation eats the name's tail, and
keep the title one line.

**State text.** `On`, `Off`, `On · 60%` for a dimmable light that reports
brightness (HA 0-255 mapped to 1-100, never showing 0% for a light that is
on), `On · 40%` for fans with `percentage`, `Unavailable` for `unavailable` or
`unknown`. Scenes and scripts show no state.

**Other Rooms.** Every other area with at least one catalogue entity, grouped
as floor folders when HA has more than one floor in use, otherwise flat, sorted
by floor level then name. Each area folder is that area's full list. Areas
already shown as member rooms of the current group are left out.

**Ids and tokens.** Container ids are ASCII: `SD:` (root for the addressed
zone), and children derived from area ids and a kind (`SD:a:<area_id>`,
`SD:a:<area_id>:scenes`, `SD:a:<area_id>:lights`, `SD:other`,
`SD:floor:<floor_id>`). Row ids are ASCII and opaque to the firmware:
an entity id for a single entity, a synthetic id for All Lights of an area or
of the group. Unicode names appear only in titles. Every Browse response of an
`SD:` container carries the current **view token** in a place the firmware
prototype could read (reuse the transport the round-two guard prototype used;
do not echo titles through the ASCII codec). The token encodes the bridge
session and the structural epoch of that container.

**Epochs.** The structural epoch of a container changes when its set or order
of rows changes, a row's name changes, a row's target membership changes, or
the zone's group membership changes. Power, brightness and availability
changes of a row do not change it. Sorting must therefore not depend on state.

**Row class.** Items use UPnP classes the firmware patch keys on: one for a
device that is on, one for off, one for unavailable, one for one-shot rows,
and a normal container class for folders. The class also has to tell the
firmware whether the row has a level control and which label it uses. Follow
the encoding the round-two prototypes proved (`object.item.sdOn` / `sdOff`
and the per-type softkey policies); extend it minimally.

### 3.3 Serving the list

- Route `SD:` in the household's browse path the same way `AI:` goes to
  `browse_line_in`. The request arrives at the zone's coordinator; resolve the
  current group members from the bridge's own topology, never from what the
  firmware claims.
- Paging must work (`StartingIndex`, `RequestedCount`); a house with many
  areas can exceed one page in Other Rooms.
- When HA is not connected, every `SD:` container returns the single inert
  row "Home Assistant unavailable".
- Container updates: when a state change affects a container's rows, bump that
  container's update id through the existing `ContainerUpdateIDs` eventing,
  under the 2 s coalescing rule with a trailing publish. Bypass the cap for the
  change that follows a user action from the controller, for HA
  connect/disconnect, and for structural changes. Only containers whose
  content actually changed are bumped; an unrelated `state_changed` (HA sends
  all of them) must cost one dictionary lookup, not a catalogue rebuild.
- Availability flag: expose the per-room variable the round-one probe used
  (`CR100_DevicesAvailable`) next to `CR100_RoomCapabilities`, true when HA is
  connected and the catalogue has at least one row anywhere. Advertise
  `devices.v1` in `CR100_UICapabilities`. Firmware without the patch ignores
  both; firmware with the patch on an older bridge sees neither and shows no
  entry.

### 3.4 Actions

Register with `UIEventGateway`, room scope, `mutates=True`:

| Action | Parameters | Effect |
| --- | --- | --- |
| `devices.power` | view token, row id, `on` or `off` | `turn_on` / `turn_off` on the row's target entities, in the row's domain(s) |
| `devices.level` | view token, row id, 0-100 | lights: `light.turn_on` with `brightness_pct` (0 means `turn_off`); fans: `fan.set_percentage` |
| `devices.activate` | view token, row id | `scene.turn_on` / `script.turn_on` |

Rules for all three:

- Reject before any HA call when: HA is disconnected; the view token is from
  another session or an older structural epoch; the row id is not in that
  container now; the row is unavailable; the row does not support the action.
  Re-check immediately before the HA call when the action waited in a queue.
- A row id is never trusted as an entity id. Look it up in the bridge's own
  catalogue for the zone the event arrived on (its own area(s) or Other Rooms);
  anything else is rejected. This keeps the controller from addressing
  arbitrary HA entities.
- Latest value wins toward HA: per target, at most one HA call in flight; a
  newer `devices.level` or `devices.power` for the same target replaces any
  queued one. The UPnP thread that received the event must not block on HA for
  longer than the event transport tolerates; measure and choose (the firmware
  does not read the result on the success path, so return promptly).
- After a successful call, publish the affected containers when HA's state
  change arrives. If no state change arrives within 3 s, publish anyway so the
  list shows the true state.
- Failures (rejection or HA error) go to the caller as described in §2
  "Errors", with a short message that fits the dialog: "Light not responding",
  "Home Assistant unavailable", "List changed. Select again." Store the
  result so the deferred-read fallback can fetch it.
- Log one structured line per action (action, room, row id, outcome,
  duration), without tokens.

### 3.5 Web UI and config

- Status: one line on the existing status page: Home Assistant devices
  connected or not, number of areas and devices, and for each bridge room the
  area it resolved to.
- Config: per-room `devices_area` override, and a global `smart_devices`
  switch (default on when HA credentials exist). Expose both in the web UI
  only if that fits the existing room and settings forms without redesign;
  otherwise leave them as documented config keys.
- When the source is native Sonos and no HA credentials exist, nothing about
  the web UI changes.

### 3.6 Tests

- Extend `tools/fake_ha.py` with area, floor, device and entity registries and
  with light, switch, fan, scene and script services that change state and
  emit `state_changed`. Model the real shapes recorded in round one
  (`ha/` under its evidence folder), including a Hue-style group and the Sonos
  integration's config-category switches. Do not copy the owner's real entity
  names or host into the repository; invent names.
- Unit tests for every rule in §3.2 and §3.4, including: group detection and
  collapse; All Lights membership, state and absolute commands; name
  stripping; grouped-zone catalogue; epoch changes only on structural change;
  stale-token and foreign-row rejection; latest-value-wins; coalescing with the
  bypass cases; HA disconnect and reconnect; native-Sonos room to area
  matching; source switch keeps the shared connection; no `SD:` browse ever
  calls a service.
- Add every new runtime module to `RUNTIME` in `tools/distribution.py`, and
  extend the test that guards that list if one exists.
- Full suite as in `docs/guides/deploying.md` §1 must pass.

## 4. Firmware work

Add one patch module (suggested `tools/smart_devices_ui.py`) to the
`compose_ui.py` pipeline, following `docs/guides/changing-the-flash-ui.md`:
pinned target hash, transform, branch fix-up, receipt, tests. Port the proven
prototype helpers listed at the end of the round-two report; they are patterns,
not finished code. Keep envelope bounds, session rollover and result
validation from the production `BridgeUI_*` helpers.

Build, in this order, verifying each in the emulator before the next:

1. **Entry and availability.** Devices in the Music root when
   `devices.v1` is advertised and the room's availability flag is true. Root
   refresh on group-membership change (membership-signature hook). Root-only
   1 s availability poll, cancelled on leaving the root; add the leave and
   re-entry check round two did not run.
2. **List.** Device rows from `SD:`, native folders two levels deep, blank
   decoration instead of the music note, on/off bulb icons from the row class
   with pinned low resource ids declared in the composer.
3. **Power.** OK sends `devices.power` with the absolute opposite of the state
   the row displayed, the retained row id and the view token. No result read,
   no forced browse. The identity guard separates structural invalidation from
   state refresh, so a normal state update does not force reselection; a
   structural change during the press sends nothing and shows "List changed.
   Select again."
4. **Errors.** Scoped transport-error dialog over the list, cursor back on the
   same row; deferred-read fallback.
5. **Slider.** Brightness and Speed softkeys through distinct handlers;
   variant B with 300 ms trailing coalescing, flush on Back, immutable target.
   Softkey absent on rows without a level control and in folders.
6. **One-shot rows.** "Started" for 1.2 s after `devices.activate`.
7. **Unavailable state.** Inert "Home Assistant unavailable" row; inert
   Unavailable device rows.

Then:

- Unit and static tests in `cr100-firmware/tests` for the new module, at the
  level the existing UI modules have.
- A scripted emulator acceptance run against the **real bridge code** driven
  by `fake_ha.py` (not the round-two synthetic peer), covering: entry
  shown/hidden; toggle on and off; All Lights; a group row; slider slow and
  burst; scene; failure dialog; stale-list rejection; grouped rooms; HA drop
  and return at root and in a list; an old bridge without the capability (no
  entry). Keep screenshots and logs as evidence under
  `../../research/bridge-runs/smart-devices-build-2026-11-14/`.
- Confirm no regression in the existing menus the patch touches (Music root,
  Room Settings, softkeys on queue and library lists).
- Build a new **UI-only** release per `docs/guides/building-a-release.md`,
  next release number after the latest under `build/releases/`, signed with
  the existing `build/keys/development.pem`. Run `tools/verify_release.py` and
  the full `tools/run_tests.py`. Do not modify earlier releases.

## 5. Documentation

- `cr100-bridge/docs/features/SMART-DEVICES.md`: what it does, the area rules,
  config keys, limits, troubleshooting. Plain language, for the owner.
- Update `cr100-firmware/docs/ui-events.md` (three actions, capability,
  availability flag, view token) and `docs/feature-roadmap.md`.
- Add the release's `ACCEPTANCE.md` with the emulator results and an explicit
  list of what still needs checking on the physical controller.

## 6. Git

Create branch `feature/smart-devices` in each repository from its current
HEAD. **Do not commit, push, stash or reset.** Leave the work uncommitted for
the owner to review. Do not touch the untracked files already present
(`REVIEW-FIXES-PLAN.md`, the research reports). The deployed build stamp will
therefore read "dirty"; that is accepted.

## 7. Deployment (bridge to the Pi)

Target: `kali@<pi-address>`, key-based SSH, passwordless sudo. Follow
`docs/guides/deploying.md` exactly. Conditions and order:

1. Only after the full bridge suite and the emulator acceptance pass.
2. **Compare first.** Read the running release's build stamp on the Pi. If it
   is not the current HEAD of this checkout or an ancestor of it (for example
   it was built from another worktree with features this branch lacks), do not
   deploy; record why and mark deployment blocked.
3. Check that no pairing or controller update is in progress and no other
   installer is running.
4. Back up the data directory:
   `sudo cp -a /var/lib/cr100-bridge /var/lib/cr100-bridge.bak-<timestamp>`.
5. Build the archive with `tools/distribution.py`, copy, `./install.sh --check`,
   then `sudo ./install.sh`.
6. Run every check in `docs/guides/health-checks.md`. Then verify the feature
   read-only on the Pi: HA hub connected, areas resolved for each room, and a
   Browse of `SD:` for one room returning sensible rows (compare against HA
   states read over the same connection).
7. If any health check fails and cannot be fixed forward within two attempts,
   roll back to the previous release as the guide describes and confirm the
   services are active again.

## 8. Controller package

Build and verify only. Optionally stage it on the bridge with `arm=False` as
shown in `shipping-a-release.md` so it is ready in the web UI. **Never arm it,
and never install to, reboot or otherwise write to the physical controller.**

## 9. Hard limits

- **No writes to real Home Assistant devices or real speakers.** The owner is
  asleep. No `call_service` against the real HA for any light, switch, fan,
  scene, script or media player, from the Mac or from the Pi, for testing or
  any other reason. Reads (states, registries) are fine. All behavioural
  testing uses `fake_ha.py` and the emulator.
- No writes to the physical controller beyond what the bridge restart itself
  causes.
- Emulator work uses disposable overlays; never the shared emulator disk.
- No secrets, tokens, real host names, real entity names or MAC addresses in
  either repository or in the delivery note.
- No changes outside the two repositories and the evidence folder, except the
  deployment in §7.
- No new third-party dependencies without a strong reason, recorded.

## 10. Order of work and stop rules

1. §3.1 shared connection, full suite green.
2. §3.2-§3.4 catalogue, serving, actions, with tests.
3. §4 firmware steps 1-7 against the real bridge code and `fake_ha.py`.
4. §3.5, §3.6 remainder, §5 docs.
5. Full suites in both repositories; emulator acceptance.
6. §4 release build and verification.
7. §7 deployment, §8 package staging.
8. Delivery note.

If one firmware step cannot be made to work after a serious attempt, finish
the rest, ship the release without that step (the entry, list, power and
errors are the minimum worth shipping; without them build no release), and
say so. Never weaken a test to make it pass, and never report a step as done
that was not observed working.

## 11. Delivery note

Write `docs/research/SMART-DEVICE-DELIVERY.md` last. Its **first line** must
be exactly one of:

```
STATUS: COMPLETE
STATUS: PARTIAL
STATUS: BLOCKED
```

Then, briefly: what was built; test results with counts; emulator acceptance
table (observed / not observed); deviations from this plan and why; whether
the bridge was deployed, the release directory now current on the Pi, the
previous one, the backup path and the rollback command; the path and SHA-256
of the `.upd` package and whether it is staged; and a short checklist for the
owner's first session on the physical controller (install the update, then
what to try, in order, starting with one lamp).
