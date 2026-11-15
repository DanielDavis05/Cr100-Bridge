# Smart device slider stall

Investigation and fix, 2026-11-14. Evidence: `research/bridge-runs/smart-devices-slider-2026-11-14/`
(see its `INDEX.md`). Every emulator timing below is a **4.3.2 emulator timing**, with the real bridge
and `fake_ha.py` running inside the same emulated SH4 guest as the controller. They show mechanisms
and relative changes, not hardware speed.

## Cause

**Confirmed.** The CR100's stock Browse panel answers every ContentDirectory NOTIFY with its own
blocking re-browse round (a `Q:0` metadata Browse, usually followed by an `SD:` Browse). It does this
even while the Brightness slider covers the list, and it works through a burst of events one at a time
instead of merging them. The bridge sent many such NOTIFYs around a level gesture:

- Every brightness change of the light (and of a group entity following its members) is a visible
  list change.
- "Own action" changes bypass the 2 s cap.
- One refresh pass that touched several open containers of a room sent one NOTIFY per container.

While the controller is inside these rounds the wheel is starved. The steps it does process arrive
late and collapse together.

The same test window in the Pi journal includes the bridge's outgoing NOTIFY records, which the
original analysis did not use. They show bursts of NOTIFYs (often two or three within 100 ms),
followed by chains of Browse pairs, roughly one pair per NOTIFY, about 1.1 s apart, with no NOTIFY in
between. The longest chain was 27 Browses in 15.3 s.

## Before and after (emulator)

Identical inputs on R8 + HEAD bridge (**before**, session `f-r8h`; group-immediate and hesitant
from `f-r8h3`, because the first attempts never reached the slider) and R9 + the new bridge (**after**,
session `f-r9w`). Each gesture is 64 wheel steps down from 100 at 10–20 steps/s with jitter and pauses
of 0.45, 0.60 and 0.35 s (`h01`: bursts of 3–7 steps separated by 0.35–1.0 s hesitations). "Settle"
means FakeHA walks the light to its new brightness in six `state_changed` events over 1.5 s, as a light
with a transition does; "immediate" means one event. Counted from the first wheel step until Back,
22 s after the last step. **Emulator timings, not hardware timings.**

| Case | Browses during gesture | Browses until Back | CD NOTIFYs until Back | Level sends | Feedback reads | Longest gap between thumb moves (s) | Last step → final HA call (s) | Final HA level / expected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Single light, settle | 0 → 0 | 2 → **0** | 6 → 8 | 2 → 2 | 1 → 1 | 2.27 → 1.80 | 12.3 → 7.0 | 36 → 36 |
| Single light, immediate | 0 → 0 | 5 → **0** | 6 → 2 | 3 → 2 | 3 → 1 | 1.76 → 1.15 | 9.4 → **1.9** | 36 → 36 |
| All Lights, settle | 0 → 0 | 2 → **0** | 10 → 8 | 2 → 2 | 1 → 1 | 1.67 → 1.28 | 29.5 → 13.9 | 36 → 36 |
| All Lights, immediate | 0 → 0 | 5 → **0** | 6 → 2 | 7 → 2 | 3 → 1 | input stalled → 1.22 | 49.7 (wrong value) → **3.0** | **19** (expected 36) → 36 |
| Group, settle | 0 → 0 | 2 → **0** | 39 → 5 | 2 → 2 | 1 → 1 | 2.84 → 1.28 | not reached → 11.3 | **82** (expected 36) → 36 |
| Group, immediate | 0 → 0 | 4 → **0** | 2 → 2 | 2 → 2 | 2 → 1 | 2.37 → 1.43 | 6.6 → **2.5** | 36 → 36 |
| Single light, settle, hesitant (12 s gesture) | 0 → 0 | 4 → **0** | 9 → 8 | 5 → 4 | 1 → 3 | 2.89 → 3.84 | not reached (41.5 s: 68) → 9.6 | **68** (expected 36) → 36 |

Browses are counted when they complete. Thumb gaps are measured while wheel input is still arriving.
"Feedback reads" counts `CR100_DevicesFeedback` reads: per request after each send in R8, and in R9
the mode read at open plus any re-read of a mode over 5 s old.

The "before" All Lights immediate case is the owner's stall, reproduced. The gesture began while the
controller was still working through Browse rounds caused by the fixture reset's NOTIFYs (one `Q:0`
Browse took 29 s in the guest). The thumb's first visible move came **19.4 s** after the first step.
The level sends then trickled out over the next 60 s (35, 34, 27, 20, 19, 9, 3), one per Browse
round, long after the gesture had ended.

The "before" hesitant case shows the same thing on a single light. After the 12 s gesture the
controller sent 69, 68 and 67, one step about every 20 s, each after a Browse round. In the emulator
this is the physical controller's "one step per second", slowed down.

In the "before" single-light immediate case, a Browse round ran just after the gesture. The steps
queued behind it were processed late, sending a stale 42 before the final 36.

In "after", no case had a Browse while the slider was open. In the normal-close cases the held
refresh was replayed once 1.8–2.1 s after Back (one `Q:0` plus one `SD:` Browse). NOTIFYs still reach
the controller during and after the gesture. R9 holds their handling, and the bridge sends fewer: in
"immediate" cases two in total, the trailing publish after each send.

Thumb position matched the final level wherever the slider was on screen. In the "settle" cases the
emulated guest is saturated by about 25 `state_changed` events per command for three lights, each
rebuilding the catalogue. Some SetStrings then missed the controller's ~2 s deadline. The controller
showed "Device action unavailable." over the slider, and once the bridge's 10 s action budget ran out
(`device_failed`, "Light not responding"). This happened on R8 and R9 alike: a popup was on screen
after the gesture in 4 of 4 "before" and 3 of 4 "after" settle cases. It was already noted in the
delivery as an emulator effect. It is not evidence about hardware. In "after" the slider was still
open, at the user's value, once Back dismissed the popup.

The "after" hesitant gap of 3.84 s is the second send waiting on a bridge HA call that ran into the
10 s budget in the saturated guest. No Browse was involved.

One R9 settle frame (All Lights) showed a second slider at the list's stale level (100) after a
transport popup and a pushed error. Back revealed the real slider at 36. R8 showed similar stacking
under the same timeouts: after Back a slider was left at about 7%. Its cause in the stock panel code
was not isolated. R9's held list shows stale levels while a slider is open, so the final R9 also
refuses to open a slider while any refresh is held ("List changed. Select again."). Such a slider
can therefore never start from a stale value.

Scrolling the Devices list itself (12 wheel ticks, no slider): 2 row-metadata Browses
(`SD:all:lantern`, BrowseMetadata) about 9–11 s later, the same on R8 and R9. Not one per tick.

The matrix ran on R9 build `r9b`. The shipped R9 (`r9c`) adds only the open-slider refusal above.
It was rechecked on single light and All Lights immediate (0 Browses until Back, final 36 after 1.8 s
and 2.6 s, longest gaps 1.46 s and 1.33 s), plus the burst and failure checks below. The results
matched.

### Burst and failure checks on the R9 movie

| Check (R9 + new bridge) | Before (R8 + HEAD) | After |
| --- | --- | --- |
| Slider, 6 NOTIFYs 50 ms apart, 20 wheel steps | 12 Browses, first thumb move 5.3 s, longest gap 7.3 s, 7 visible changes | **0 Browses**, first move 0.08 s, longest gap 0.19 s, 22 changes |
| Slider, one pass publishing 3 containers | 7 Browses, first move 6.0 s, longest gap 10.2 s | **0 Browses**, first move 0.07 s, longest gap 0.21 s |
| Level failure behind the slider (FakeHA fails services) | "Light not responding" for about 9 s, slider back at the user's value, a Browse round behind the slider at 7.7 s | Same message and slider; the held refresh replayed 0.6 s after Back; list shows the unchanged level |


## Evidence

### Physical controller (Pi journal, read-only)

The owner's test window has 97 ContentDirectory NOTIFYs, 164 Browses and 24 actions (15 level,
9 power). Every level action completed in 549–786 ms. The NOTIFYs come in clusters: 30 single, 13
pairs, 10 triplets, one of 5 and one of 6 within 100 ms. A triplet 30 ms apart is one bridge refresh
pass publishing three containers.

After each cluster the controller issues Browse pairs (median 0.79 s between Browses, about 1.1 s per
pair) for as many rounds as there were NOTIFYs. Six chains have six or more Browses with no NOTIFY in
between: 27 Browses in 15.3 s, 17 in 9.5 s, 14 in 6.9 s, and others. These are the "pair every 1.1 s
with no SetString" stretches. They start after level or power actions because only those produce
NOTIFYs. Extract: `pi-readonly/notify-browse-timeline-relative.jsonl` (relative times, no addresses).

### Emulator: what one NOTIFY costs

Lab-only `burst` operation on R8 + HEAD bridge, Devices list open. Session `b-r8`.

| Case | NOTIFYs | Browses | Thumb with 20 wheel steps |
| --- | --- | --- | --- |
| List, 6 publishes 50 ms apart | 6 | 9 over 14.9 s (6 `Q:0`, 3 `SD:`) | |
| List, 6 publishes 1.2 s apart | 6 | 10 over 16.2 s | |
| List, 6 publishes of a container that is not on screen | 6 | 6 `Q:0` over 12.1 s | |
| Slider, no publishes (control) | 0 during input | 0 during input | first move 0.08 s, longest gap 0.29 s, 21 changes |
| Slider, 6 publishes 50 ms apart | 6 (+4 from the sends) | 12 over 46.8 s | first move **5.3 s**, longest gap **7.3 s**, 7 changes for 20 steps |
| Slider, one pass publishing 3 containers (as HEAD sends) | 3 (+2) | 7 over 30.1 s | first move **6.0 s**, longest gap **10.2 s** |

### Emulator: where the Browses come from

Two lab-only movies, each differing from R8 in a single function:

- **Wheel-hook probe (`diag8`):** `SD_userButton` reads a property on every call. Scrolling the list
  fired it on 3 of 3 wheel steps. With the slider open, 20 wheel steps fired it **0** times. The list's
  wheel hook does not run behind the slider. Session `d-hook`.
- **Stock cache handler skipped (`diagcache`):** in a Devices list the browse-cache hook returns before
  the stock `BrowsePanel_onBrowseCacheEvent` body. Six NOTIFYs then produced **0** Browses, in the list
  and behind the slider. The slider stayed fully responsive under them (first move 0.08 s, longest
  gap 0.23 s). The re-browse is driven by the stock ActionScript handler, not by the native cache.
  Session `d-cache`.

### Hypotheses

| # | Hypothesis | Verdict |
| --- | --- | --- |
| 1 | Bridge publishes repeated ContainerUpdateIDs changes while the light settles, own changes bypass the cap, and the controller re-browses behind the slider | **Confirmed**, and worse than stated: every NOTIFY costs one blocking round, one pass sent one NOTIFY per changed container, and a container that is not on screen still costs a `Q:0` Browse |
| 2 | The wheel hook (`SD_userButton` → `SD_dirty`/`SD_scheduleSnapshot`/repaint) runs while the slider is open and causes a Browse per tick | **Refuted.** The hook does not run behind the slider (0 of 20 steps). No Browse happened during any gesture without NOTIFYs. Scrolling the list itself (12 ticks) produced 2 row-metadata Browses about 10 s later in R8 and R9 alike, not one per tick |
| 3 | 300 ms trailing delay causes mid-gesture sends, each costing two blocking requests, collapsing input | **Partly confirmed, minor.** Sends do happen at natural pauses (the first pause of every plan) and each blocks the thumb for the length of its requests: longest gap 1.7–2.3 s in R8 emulator runs. It is not the stall: without NOTIFYs the thumb never lagged more than about 2 s, and on the Pi a send is about 0.15 s plus one extra GetString |
| 4 | Another loop, e.g. `browse()` → `_refresh()` → publish → Browse | **Exists, negligible.** Across every trace only 2 publishes came from a refresh inside `browse()` (one of them capped) |

## What was changed and why

### Bridge (`tools/smart_device_service.py`)

1. **Level-gesture hold.** While a `devices.level` action is in progress on a target, list updates
   caused by that target are held and published once afterwards. The hold is on from the moment the
   action is received, throughout its HA call, and until 1.5 s after the latest level action and 1.0 s
   after the target's latest state change. It is never longer than 8 s from the gesture's first
   action. Structural, availability and row-availability changes and failed actions are never held.
   The held publish is a single trailing bypass publish. The view token is never replaced by holding.
2. **One NOTIFY per room per pass.** All publishes in one pass (refresh, trailing, confirmation,
   held-publish, failure) bump their containers and then send a single ContentDirectory NOTIFY per
   room, carrying every new ContainerUpdateID. Before, a pass that changed the Devices root, a room
   folder and a group folder sent three NOTIFYs within 50 ms, costing three Browse rounds.

The bridge keeps the 2 s external cap and the immediate own-action publish. Holding is per target, so
another controller viewing the same light also sees its update after the gesture rather than during it.

### Firmware (R9, `tools/smart_devices_ui.py`, new profile `cr100-custom-4.3.2-re100-r9`)

1. **No list re-browse behind an open slider.** While a level slider is open over a Devices list, the
   patched cache-event hook still marks the list stale at once (`SD_markDirty`), but holds the stock
   handler. It remembers each distinct (room, container) event, up to 32. Closing the slider first
   flushes the final value, then replays the held events once through
   `g_BrowsePanel.OnBrowseCacheEvent`, the stock handler's own registration. A list key reaching the
   list hook also releases anything held, since such keys never arrive while the slider is open.
   Selection identity is unaffected: the slider's target is captured at open, and the list stays
   stale until its replayed refresh is repainted. While anything is held, `SD_openLevel` refuses to
   open another slider ("List changed. Select again."), because the held list's levels are stale.
2. **One request per send.** The feedback mode is read once when the slider opens, from the existing
   `CR100_DevicesFeedback` variable without a request id. It is re-read only if over 5 s old at a send.
   A level send is then a single SetString with an explicit `feedback=notify|deferred`, without the
   `CR100_DevicesFeedback:<request>` read. Deferred mode still polls `CR100_UIResult` for errors. One
   residual gap remains. If the controller's AVTransport subscription lapses within those 5 s, the
   bridge freezes `deferred` for a request the slider sent as `notify`, and that one failure is
   retained but not shown. R8's per-request read covered this. A lapse normally means the bridge or
   network went away, in which case the SetString itself fails visibly.
3. **Send spacing.** The trailing delay stays 300 ms, but a send is never scheduled within 800 ms of
   the previous one. A value that changed while a send was in flight is sent 800 ms later instead of
   at once. Back still cancels the timer and flushes immediately.

Kept: absolute values, the view-token and row-id guard, the target captured at open, transport errors
("Device action unavailable."), pushed and deferred result errors, and "List changed. Select again.".

An earlier session's uncommitted R9 draft also guarded `SD_userButton` with a slider-open flag. That
was dropped because the hook provably does not run behind the slider, and a flag left set would have
suppressed it afterwards. The same draft added `'r9': False` to R8's composition receipt, which broke
R8's release verification. It was removed and is now pinned by a test.

## What was not changed

- The stock controller's per-event re-browse in an ordinary list, with no slider open. One NOTIFY still
  costs one round there. The bridge changes reduce how many NOTIFYs arrive.
- The `Q:0,…,AI:,…` entries the bridge puts in every ContentDirectory event, and the controller's
  `Q:0` metadata Browse per event.
- Own power actions still publish on every visible change; a settling light after a power toggle can
  still send a NOTIFY per pass (now one per room, not one per container).
- The 2 s external cap and 300 ms trailing delay.
- The firmware's per-request feedback read for power and activation actions.
- Earlier releases. R6c, R7 and R8 still verify byte for byte.

## Regression checks

Acceptance cases from the delivery, rerun on the final R9 movie with the new bridge through a port of
the original runner (`lab/accept_runner.py`, run `lab/accept-r9c2/`). All five passed their machine
checks, and the decisive screenshots were viewed:

| Case | Result |
| --- | --- |
| Slider: five slow ticks 41–45 each acknowledged, burst of 20 → one write of 65, burst of 20 + Back → one write of 85, no late writes in a 22 s tail, back on the same row | passed; list shows Ceiling 85% |
| Power: boundary row, single light, All Lights, group; exact absolute calls | passed |
| Errors: notify and deferred failure, exactly one feedback mode each, Back to the same row | passed; "Light not responding" shown in both |
| Stale list: identity reorder sends nothing and shows "List changed. Select again.", explicit reselection works | passed |
| Fan speed burst | passed |

Feedback reads in the slow-tick case: R9 re-reads the mode at most once per send (here once per tick,
since the ticks are over 5 s apart). It never reads `CR100_UIResult` on success.

## Tests

- cr100-bridge: `.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -q`: **1345 tests, OK
  (2 skipped)**. Five tests are new in `tools/test_smart_device_service.py`:
  - level hold publishes once when quiet;
  - hold maximum and power actions not held;
  - hold from receipt and through an in-flight call, released on failure;
  - failed level action published and reported despite a hold;
  - one NOTIFY per room per pass.
- cr100-firmware: `tools/run_tests.py --directory tests`: **252 tests, 0 failures, 0 skipped**. The UI
  module checks the R9 bytecode:
  - only the intended helpers change, and three helpers are added;
  - the stock cache handler body is intact behind the hold;
  - the open-slider refusal is present;
  - the feedback-mode helper is used at open and at send;
  - the 300/800 ms timing is in place, and Back flushes without spacing;
  - the safety strings are retained;
  - the R9 profile is UI-only over R8;
  - R8's helpers, bytes and composition receipt are unchanged.

## R9

`cr100-firmware/build/releases/re100-cfw-20261114-r9/cr100-custom-4.3.2.upd`

- Package SHA-256 `c273cc99b147c80795ebf75c4ffb787460747628627937d5f49d13d80751f8f4`,
  5,914,269 bytes. Profile `cr100-custom-4.3.2-re100-r9`, development-signed with the same key as R8.
- `MainUI.swf` SHA-256 `24522a2f69c2423da5628d9201045e62b5ee4fe3039065df1850fbae46d4a492`, the movie
  used for the final checks.
- UI-only: `hh.anacapad`, the embedded app, TLS library, launcher, installer and updater are
  byte-identical to R8, so R8 stays a working fallback bank.
- `tools/verify_release.py` verifies R9. R8 verifies, and R7 and R6c verify with `--source`.
- A first R9 build without the open-slider refusal is kept at
  `build/_superseded/re100-cfw-20261114-r9.attempt1-before-openlevel-guard`.
- Not installed, staged or armed.

The bridge change and R9 are independent, but they are meant to ship together:

- The bridge alone stops NOTIFYs during a gesture and sends one per room per pass. A controller on R8
  still re-browses once for every NOTIFY that does arrive.
- R9 alone stops the re-browse behind the slider and halves the requests per send. NOTIFYs still pile
  up for the list after the slider closes, though only one replay is run.

The bridge-only configuration was not measured.

## Follow-up: R10 (2026-11-14/15)

On the controller, R9 with the new bridge applied a brightness value only when Enter closed the slider. The Pi
journal (read-only) showed one `GetString` and one `SetString` per gesture, both at close, and nothing during
the turn.

**Cause (reproduced in the emulator).** R9 spaced sends by comparing `getTimer` with `sentAt`, which starts at
-100000 ms to mean "no send yet". If the clock reads below about -99.5 s, `800 - (now - sentAt)` is a large
positive number, so the trailing timer is scheduled minutes away and only Back/Enter's flush sends. A lab movie
that offsets R9's clock read by -1e6 ms and reports the requested delay asked for about 480,000 ms on every
wheel step. Its pump ran only at Back, so the light changed only after Back (session `n-clockneg`). The
emulator's own clock is an ordinary elapsed-ms value (~475,000 ms), so unmodified R9 works there. One unprobed
run with the same offset still sent mid-turn, which is not explained. R10 removes the clock from the timing,
so it does not depend on the answer.

**R10 firmware** (`cr100-firmware` `2bfb6a8`, release `re100-cfw-20261114-r10`, package
`b65177716424e63d9880fda9e38103ea531297beead27dc6651a525be185467e`, UI-only over R9):

- Level timing uses timers only: 300 ms after the last wheel step, or the end of the previous send's 800 ms
  spacing timer. The feedback mode is re-read at the first send after each 5 s timer. Back/Enter still flush at
  once.
- Optimistic power rows: a press records the requested state (by room and row id) and repaints the visible rows
  before its SetString. A second press then asks for the opposite. The guess lasts 5 s, or until a newer press,
  and is kept if the reply misses the native ~2 s deadline. A first version recorded it only after a successful
  reply; in the emulator the reply often missed the deadline after the bridge had acted, and the second press
  repeated "off" (`n-opt`).
- A press made while the list refreshes is queued for up to 3 s and runs on the refreshed row if its id and view
  token match. Otherwise it shows "List changed. Select again.", and it is dropped if the selection moved.
- Devices sits directly above System Settings in the music menu.

**Bridge** (`b609f3f`, `d1874a9`): from receipt until HA reports the requested state, the request fails, or its
confirmation comes due, a power press's targets (and group members) are listed in the requested state. A stale
HA event mid-transition no longer flips the row back. A failed or rejected request lists the actual state, a
level action clears an earlier power display, and a due confirmation ends the display so the real state is
published once. The requested state is published by the service loop. A first version refreshed inside the
SetString request; in the emulator that missed the controller's reply deadline, and the "Device action
unavailable." popup swallowed the next OK (`accept-r10-superseded-sync-publish`).

**Emulator results** (emulator timings):

| Check | R9 | R10 |
|---|---|---|
| Quick second OK on a light | 0.4 s: off sent twice, light stays off (`accept-r9d`) | 0.1 s and 0.4 s: off then on (`n-opt2`); acceptance 0.4 s and 3 s: off then on (`accept-r10c`) |
| Press during a list refresh | "List changed. Select again." | Queued, then sent the opposite on the refreshed row (`n-opt` 2 s case) |
| Sustained gestures (4 plans) | — | Final value sent 0.3–1.1 s after the last step, before Back; 0 Browses with the slider open (`n-r10`) |
| Acceptance | — | power, slider, errors, stale, fan passed (`accept-r10b`) |

Emulator effects that are not R10 logic: under load SetString replies miss the ~2 s deadline and emulated HA
calls time out. A popup left on screen swallows the next OK, which made one double-press gap fail in each
acceptance run, on R9 and R10 alike. One hesitant-turn run per firmware ended at the wrong light level because
HA calls timed out after the controller had sent the right final value (`h-*`).

Tests: bridge suite 1351 tests OK (2 skipped), including six new pending-power tests. Firmware suite 258 tests,
0 skipped, including R10 scoping, no clock opcode in the level helpers, guess-before-send, queued press, menu
position and receipt tests. R8 and R9 bytes and receipts are unchanged.

### R10 checks on the physical controller

After deploying the bridge commits and installing R10:

1. Turn a light's Brightness, stop, and wait without pressing anything. The light should follow within about
   a second of stopping, and the bridge log should show `devices.level` actions during the turn, not only at
   Enter/Back.
2. Turn a light off and press OK again straight away (well under a second). The row icon should flip on each
   press, and the light should end on. The log should show `turn_off` then `turn_on`.
3. Press OK on a row just as the list updates after another change. It should act once the list is redrawn,
   with no "List changed" message, unless the row really changed.
4. The music menu should list Devices directly above System Settings.

## Check on the physical controller

The emulator cannot confirm hardware speed. After deploying the bridge change and installing R9:

1. Open a dimmable light's Brightness and turn the wheel continuously for 60+ steps, with a couple of
   short pauses. The thumb should follow throughout. The light should settle on the thumb's value within
   about 1–2 s of stopping.
2. Repeat on All Lights and on a light group. A group following its members was the largest NOTIFY
   source.
3. In the bridge log during the turn, look for `ContentDirectory.Browse` lines. There should be none
   until Back, then one pair. There should be about one NOTIFY per send plus one after the gesture,
   instead of bursts.
4. Each level send should be a SetString with no `GetString` of `CR100_DevicesFeedback:<request>` after
   it. There is one `CR100_DevicesFeedback` read when the slider opens, and another only if more than
   5 s have passed since the last.
5. Press Back after a turn: the list should show the new level within a couple of seconds.
6. Toggle a light and scroll the list straight away. Each toggle should now cost at most one Browse
   round per settling step, not three.
7. Fail a level change deliberately (for example, unplug a lamp briefly) with the slider open:
   "Light not responding" should appear, and the slider should remain.
8. Watch for "Device action unavailable." during turns. It was an emulator deadline effect here and
   should not appear on the Pi.

