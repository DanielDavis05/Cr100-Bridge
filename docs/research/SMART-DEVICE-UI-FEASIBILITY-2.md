| Experiment | Result | Status |
| --- | --- | --- |
| 1. Sliders | B with 300 ms trailing coalescing reduced twenty burst steps to one write, while Bass, A and C each serialized twenty writes. | observed in emulator |
| 2. Toggle stages | No result read/no forced Browse saved one SOAP read and about 0.25–0.43 s of median visible latency against the same optimized fixture. | observed in emulator |
| 3. Failures | Original and deferred result dialogs worked; a caller-scoped transport-error NOTIFY plus ordered OK showed the error without broadcasting it to another subscriber. | observed in emulator |
| 4. Repeated presses | Raw toggle turned the same row On then Off; two absolute On requests left it On, and adjacent-row requests retained separate identities. | observed in emulator |
| 5. Mutable lists | Native index retention can send an action to a different device than the pixels show; retained displayed identity plus epoch/bridge guards prevented the tested races. | observed in emulator |
| 6. Update stress | Thirty distinct notifications arrived in 9.949 s without sampled blanking; clean controls support a conservative 2 s external-update cap. | observed in emulator |
| 7. Folders/headings | Two-level folders, parent selection, child softkeys/refresh and a non-selectable leading heading worked; folders are the simpler production choice. | observed in emulator |
| 8. Groups | Availability, Browse and actions address the coordinator; membership context and a membership-change root hook are needed for all-room catalogs. | observed in emulator |
| 9. Availability | The open root needs an explicit refresh trigger; a 1 s poll worked at the root, while open lists needed a real CD update. | observed in emulator |
| 10. One-shot/risky rows | A 1.2 s Started acknowledgement worked, and native confirmation defaulted to Cancel with zero actions on Cancel/Back. | observed in emulator |
| 11. Appearance | Blank decorations, native folder arrows, class-driven on/off bulbs and distinct-handler Speed labels worked. | observed in emulator |
| 12. Optimistic update | Local text/icon paint and correlated reconciliation worked, including stale-data rejection; the measured paint-first path has a command-latency tradeoff. | observed in emulator |

Investigation: November 14, 2026. All timings below are **4.3.2 emulator timings**, including emulation, synthetic services and bounded framebuffer sampling—not hardware or real-HA performance predictions. Evidence is indexed in [the follow-up directory](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/INDEX.md). The first-round scripts/builder and optimized V3 fixture were reused in four separate owned overlays, with unchanged stock native executable SHA-256 `fe4f159b…d830`. No production code or existing release was modified. No physical controller, speaker, Pi or real HA was accessed. No commits were made.

A copied launcher initially retained its old output path and briefly booted the previous disposable overlay. It was stopped before guest commands/experiments; the seed export predates that incident. Every reported measurement uses a fresh follow-up clone. See `toggle/launcher-path-incident.json`. Diagnostic attempts with readiness, clock, emitter or control-connection problems are retained and excluded explicitly; raw packet totals include those attempts.

## Recommendation: build slider B with 300 ms trailing coalescing

Keep the thumb local, restart one 300 ms idle timer on movement, send the newest **absolute** value with the device identity captured at open, and cancel/flush once on Back. Use SetString only per value and the error path below. This is coalescing, not asynchronous SOAP: a pump still blocks briefly.

Two normal twenty-step bursts produced **one write each**, with final arrival **2.015–2.051 s** after the last input. Bass took **20 writes / 10.478 s**, A **20 / 12.143 s**, and C **20 / 9.778 s**. B’s final thumb arrived in **1.08–1.61 s** across those two bursts. Its separate immediate-Back case sent one final value, arriving after **3.205 s**, returned to the same row and had no late timer writes in a 22 s tail. C’s queued write could be retargeted by a changed binding; an exclusive lease prevented that case, but did not remove its serialization or native-EQ aliasing costs.

## Recommendation: use absolute intents, notification-driven refresh and scoped errors

Use a retained **displayed/pending** row identity and frozen view token; send absolute desired state via SetString without a foreground result read or forced re-browse. Publish the authoritative container change after mutation. At 24 rows, median visible bounds were **2.88–3.25 s**, versus **3.34–3.65 s** for the original path. Browse began a median **81 ms after the on-wire NOTIFY**. Do not compute from a newly fetched cache object and assume it matches the pixels.

For failure, target the calling controller’s active AVTransport subscription with a body override, then an ordered OK reset. This showed the message over Devices, left global transport state clear and sent no error to a second callback subscriber. Keep the tested 250 ms deferred retained-result reader as a fallback when that notification path is unavailable; it showed failures in **1.99–2.30 s** but added about **0.5 s** to successful redraws. The scoped cold-session failure appeared in **2.08–2.57 s**; that is not evidence it is faster than a warmed deferred read. Choose one feedback mode per action so foreground-verified one-shot/confirmation paths do not also show a duplicate pushed error.

**Keep the authoritative path as the initial power-control default.** Strict optimistic paint is feasible, but the tested 250 ms paint-first variant delays bridge intent receipt by about **1.0 s**. In matched controls its visible median was **0.62–0.94 s**, versus **4.08–4.45 s** without optimism, while intent receipt moved from **0.74–0.99 s** to **1.76–2.01 s**. Prioritize the earlier device command for this feature. Removing the fixed delay improved dispatch but did not prove the requested paint-before-native-call ordering. The optimistic alternative also needs explicit acknowledgement/revision and recovery semantics; its successful safety experiments do not make that production contract automatic.

## 1. Baseline Bass and brightness variants

The baseline was opened through **Room Settings → Devices Lab → Music Equalization → Bass**. Bass was reset to −10 before each pattern, preventing range clamping from hiding burst writes. Every path received five unit packets one second apart and twenty unit packets in one FIFO write, exactly as round one.

| Path | Slow: writes / mean gap / final tail | Burst: writes / rate during drain / final tail |
| --- | --- | --- |
| Native Bass | 5 / 1.075 s / 0.634 s | 20 / 2.01 per s / 10.478 s |
| A: send without result read | 5 / 1.055 s / 0.976 s | 20 / 1.71 per s / 12.143 s |
| B: 150 ms leading | 5 / 1.111 s / 1.030 s | 5 / 1.44 per s / 3.799 s |
| B: 150 ms trailing | 5 / 1.047 s / 1.082 s | 2 / 0.72 per s / 2.475 s |
| **B: 300 ms trailing** | **5 / 1.044 s / 0.959 s** | **1 final write / 2.015–2.051 s across two runs** |
| C: native integer setting | 5 / 1.100 s / 0.689 s | 20 / 2.12 per s / 9.778 s |

Bass/A/C showed write-paced stair steps. B moved locally between short send stalls; its accepted 300 ms burst first motion was bounded at 0.036–0.597 s. Eighteen finite normal/guarded cases matched actual fixture state and calibrated thumb position within one pixel. This does not establish success under transport failure. Back flushed B’s newest value once; A/C drained twenty writes before closing. Per-device titles/initial values **Ceiling 50** and **Kitchen 73** were verified at open.

C used real `setIntSetting("Bass/Master", value*2)` and flush after a binding event. During an artificial 1.2 s pending write, rebinding let a queued value 97 change Kitchen while Ceiling’s slider remained open. An exclusive target lease rejected the rebind and kept both subsequent writes on Ceiling. The prototype also issued a same-value native write at open to seed cache; its necessity was not isolated. These costs favor B.

Evidence: [slider section, metrics and frames](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/slider/experiment-1.md), `slider/compact-metrics.json`, `final-value-checks.json`, `runs/`, source-phase receipts and captures.

## 2. Toggle stage measurements

Five accepted trials per path at both 8 and 24 rows alternated new/original order against the same optimized fixture. Five more 24-row trials measured deferred success reads. An initial 8-row attempt was replaced: before pixels still said On while cache/action context said Off, leaving identical before/after pixels. It is race evidence, not a latency sample.

Median lower–upper measurement bounds, seconds after key injection:

| Rows/path (n=5 each) | Bridge receives event | Complete NOTIFY on wire | Browse request | Response server flush | New text |
| --- | --- | --- | --- | --- | --- |
| 8 / no read, no force | 0.56–0.75 | 1.06–1.83 | 1.79–1.98 | 2.15–2.35 | 3.00–3.36 |
| 8 / original | 0.53–0.75 | 1.28–1.98 | 2.19–2.45 | 2.52–2.77 | 3.26–3.59 |
| 24 / no read, no force | 0.54–0.74 | 0.98–1.81 | 1.76–1.97 | 2.13–2.36 | 2.88–3.25 |
| 24 / original | 0.56–0.77 | 1.32–2.01 | 2.16–2.37 | 2.57–2.76 | 3.34–3.65 |
| 24 / deferred result | 0.57–0.77 | 1.31–1.84 | 2.24–2.43 | 2.65–2.85 | 3.37–3.70 |

The bridge’s earlier publication-call entry was logged around 0.91–1.20 s after key input. It is distinct from actual wire publication: asynchronous serialization/delivery followed it. Same-packet-clock NOTIFY→Browse medians were **64/81 ms** for new 8/24-row paths, **338/300 ms** for originals and **503 ms** for deferred reads. Do not attribute the whole publish-log→Browse gap to native cache reaction.

Host key/ACK and frame windows were calibrated with before/after guest uptime. Packet/guest clock offsets were bounded causally by matched SOAP request/entry/response brackets within each trial; all 25 intersections were valid, but wire bounds are intentionally broad (constant-offset model within one trial). These are measurement bounds, not confidence intervals. Several owned VMs ran concurrently; the matched comparisons matter more than absolute cross-run differences.

Evidence: [stage metrics](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/toggle/experiment-2-metrics.json), [wire method and timings](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/toggle/wire-publication-method.md), raw `e2-*` frames/timing, `events.jsonl`, `packet-summary/`, and the independent `slider/audit-toggle/` checks.

## 3. Toggle failure

A handler failure caused zero device-state writes in every tested path. `SD_sendSilent`’s original error branch showed the message in **1.35–1.80 s** and Back restored Desk Plug. A 250 ms deferred result read showed it in **1.99–2.30 s**, also returning to the same row. A no-read transport-error notification showed it in **1.20–1.67 s**, remained visible **9.83–10.43 s**, and auto-returned to the row.

The scoped follow-up chose only the caller-IP-matching AVTransport SID, delivered ERROR then OK in sequence, and left the room’s global error empty. The native UI displayed the popup despite the immediate reset. A second synthetic callback got only its initial OK, no error. This is one native UI plus a negative callback sink, not two physical-controller acceptance. The cold-session scoped popup appeared in **2.08–2.57 s**, including the fresh-session read.

Evidence: [failure/routing metrics](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/toggle/experiments-3-4-metrics.json), `e3-fail-{orig,nr}/`, `e3-fail-deferred-accepted/`, [scoped popup/return frames](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/toggle/e3-scoped-error/), `e3-scoped-error-return.png`, `other-notifies.jsonl`, and `probe/round2_peer_scoped.py`.

## 4. Repeated presses

Accepted host key-command starts were 0.24–0.26 s apart for the same row and 0.41–0.44 s apart for different-row OK/wheel/OK. Injection/ACK brackets place both FIFO injections within one second (same-row spacing bounded at approximately 0.201–0.463 s; different-row 0.370–0.586 s). Bridge arrivals were 0.90–1.15 s apart because synchronous sends serialized queued input.

| Requests | Same row result | Two adjacent rows result |
| --- | --- | --- |
| Two raw toggles | On then Off; list ends Off | Desk and Fan both On |
| Two explicit On requests from unchanged Off snapshot | On twice; list stays On | Desk and Fan both On |

The controlled cases held publication for 1.5 s; all four were rerun with normal 50 ms publication and gave the same outcomes. Each accepted case sent exactly two unique requests. Prefer absolute desired state from the retained displayed/pending snapshot: cache state can precede pixels. With optimistic display, a second press deliberately computes from the latest pending value instead (experiment 12).

Evidence: [request/state metrics](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/toggle/experiments-3-4-metrics.json), `e4-*-seq/`, `e4-*-default/`, and log envelopes. Incomplete threaded key/snapshot attempts are excluded.

## 5. List mutation and identity

Starting with Delta at index 3, insertion above highlighted Charlie; removing Alpha highlighted Echo; removing Delta highlighted Echo; reversing the list highlighted Echo. Native selection is numerical, not device identity. While Delta’s slider was open, insertion changed the eventual highlighted row to Charlie, but the wheel write still targeted Delta and Back returned to the shifted index.

The direct negative race is decisive: certain-before/after frames still showed **Delta**, but unguarded OK sent **Charlie’s ID** and changed Charlie. A retained post-repaint ID/title plus invalidation epoch fixed the tested cases: at 0.61 s after insertion it still sent Delta; at a 2.09 s ambiguous transition it sent no action and asked for reselection. A hidden server rename also failed the bridge title check without mutation.

Build a frozen identity/view guard and revalidate it on the bridge. The tested policy conservatively treats every invalidation as structural; production should distinguish structural epochs from power-state revisions. Prefer an ASCII opaque issued-view token over echoing arbitrary Unicode titles through the existing ASCII codec.

Evidence: [mutation/race section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/lists/experiment-5.md), `guard-race-clock-analysis.json`, `05s-unguarded-400ms/`, guarded race folders and `runtime-events.jsonl`.

## 6. Rapid updates

The strict trial delivered **30 distinct, ordered wire notifications in 9.949 s**, all HTTP 200, final title #29. The cache coalesced them to **4 direct Browses + 2 metadata Browses**. All three wheel moves survived; no near-empty frame appeared in 45 samples. This cannot exclude flicker between sampled frames. That movie incorrectly polled availability inside Devices, so its 2.1–7.1 s input delays are confounded and are not attributed solely to notifications.

Clean controls used a root-only poll, quiet reset and identical inputs:

| Spacing | Wire events | Browse traffic | Worst sampled input upper bound |
| --- | --- | --- | --- |
| No external updates | 0 | 0 | 0.59 s |
| 1 s | 10 in 9.245 s | 6 direct + 2 metadata | 3.04 s |
| 2 s | 5 in 8.029 s | 5 direct + 3 metadata | 1.61 s |

Each retained all inputs and had no sampled blanking. Recommend **2 s minimum spacing for coalesced external state updates**, with a final trailing publication. Bypass that cap for user-action confirmation, failures, availability and structural changes. This is a conservative recommendation from three inputs per rate, not a universal threshold. A selection-decoration optimization did not remove metadata requests and is not a proven improvement.

Evidence: [stress/control section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/lists/experiment-6.md), `06-template-320ms/`, clean controls, `flood-analysis.json`, `quiet-analysis.json`, `packet-final/`. Earlier generators that coalesced/stretched thirty updates are excluded.

## 7. Nested lists and headings

Native folders worked through **Devices → Scenes → Evening Scenes** and **Devices → Other Rooms → Other Lab**, plus Individual Lights. Back restored the parent folder row at both levels. Child scene rows blanked Brightness; child lights showed it. A child-container revision refreshed text without leaving or moving the cursor.

An inert heading was still selectable. A custom direction-aware skip made a **leading** heading non-selectable at fresh open, Up/Down and OK. Arbitrary middle/end/consecutive headings were not tested. Recommend native folders and no heading rows in the first implementation; headings offer little benefit while requiring extra selection logic.

Evidence: [nested/heading section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/lists/experiment-7.md), `07p-*` and `07o-*` screenshots and fixture/builder sources.

## 8. Grouped rooms

Selecting Other Lab ungrouped addressed its player; grouping it under Devices Lab made availability, Browse and actions address the **coordinator**. The former root hook missed same-coordinator membership changes for the whole ≥6.6 s sample window. A membership signature hook fixed dissolve/form visibility in **0.71–1.36 / 0.91–1.49 s**.

The native group exposes `GroupID`, `numZonePlayers` and `getZonePlayerAt(i).UDN`. An event carrying group ID plus both member UDNs worked; enumeration was unsorted. Build the catalog from the union of current member HA areas, deduplicating areas and using room folders when grouped. Keep each room’s All Lights inside its room folder. Bind group/member context in a bridge-issued view token; the coordinator endpoint alone does not identify a device’s owning area. Validate member sets against authoritative bridge topology.

Evidence: [group section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/rooms-visual/experiment-8.md), group screenshots, stale/fixed membership trials and `experiment-8-metrics.json`.

## 9. HA availability

Changing the availability flag alone left the open root stale. An identical topology NOTIFY also failed to trigger a semantic group-change callback. A single **nominal 1 s poll** read the flag and rebuilt only on change; Devices had disappeared and returned at the respective 3 s checks without navigation. The final variant restricts that unchanged root branch to the root; experiment 6 verified that it issues no polling RPC inside Devices. Its stop/cancel-on-leave hook is implemented, but had no dedicated leave/re-entry cancellation acceptance trial.

An open list needed a real CD update to replace rows with **Home Assistant unavailable** and restore them on reconnect. The unavailable row was inert and the bridge rejected writes. Poll + invalidate/forcePoll alone, with actual notifications suppressed, did not replace old rows within 3 s. Use the availability poll for the root and container events for open lists.

Evidence: [availability section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/lists/experiment-9.md), accepted `09p-root-*`, `09g-list-*` frames and final root-only movie. No real HA was contacted.

## 10. Scenes, scripts and confirmation

Both one-shot rows sent a verified activation and showed **Started** with a requested 1,200 ms timeout. Sampled visible duration was **0.989–1.501 s** for Scene and **1.022–1.548 s** for Script; the same row remained selected. Build this brief acceptance acknowledgement; do not imply that a script has finished or has a persistent power state.

`BridgeUI_confirm` opened from Risky Pump with **Cancel selected**. Cancel and Back each sent zero UI/action requests in separately marked intervals and returned to the same row. Positive Confirm sent exactly one toggle. Capture immutable device/room/view context before opening and validate again on execution.

Evidence: [one-shot/confirmation section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/rooms-visual/experiment-10.md), Started sequences, confirmation screenshots and `experiment-10-metrics.json`.

## 11. Appearance and Speed

Existing BLANK decoration removed music notes; native folders retained their arrow. Custom state bulbs also worked: cached classes `object.item.sdOff/sdOn` selected gray/yellow vector frames. Toggling Ceiling changed only its text/icon. The attempt completed within **32.6 minutes**. Use class metadata, not localized caption parsing.

Identical shapes at resource IDs 9001/9002 rendered the no-play fallback; IDs **900/901** worked. The exact dictionary limit was not measured. Allocate verified unused low IDs and declare the exact sprite/shape additions in the composer. Sharing one handler for Brightness/Speed reproduced a stale label; distinct wrapper callbacks fixed it.

Evidence: [appearance section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/rooms-visual/experiment-11.md), blank/folder/Speed PNGs, class-icon toggle frames and resource receipts. No arbitrary per-row artwork fetch is needed.

## 12. Optimistic paint and reconciliation

The local virtual-list overlay changed both the full row title and on/off bulb without a Browse. Five retained-identity trials put first visible change at **0.56–1.08 s**, median **0.62–0.94 s**. Every first changed frame preceded bridge receipt. A separate ActionGetTime diagnostic bracketed a synthetic-clock GetString, recorded the timer immediately before native SetString and reported it afterward. With calibration and rounding allowances, the completed optimistic frame preceded **native call entry by at least 0.236 s**. This is stronger than merely seeing paint before the request reached the bridge.

The ordinary authoritative/250 ms modes form the matched five-trial comparison, using the same 8-row guarded icon movie, fixture and 0.5 s handler delay. The zero-gap row is a bounded three-trial diagnostic from a later boot with post-send timing instrumentation:

| Mode | Runs | First changed pixels, median bounds | Bridge intent receipt, median bounds |
| --- | --- | --- | --- |
| Authoritative | 5 | 4.08–4.45 s | 0.74–0.99 s |
| Optimistic, 250 ms post-repaint gap | 5 | 0.62–0.94 s | 1.76–2.01 s |
| Optimistic, no fixed gap | 3 | 0.58–0.94 s | 1.36–1.57 s |

The zero-gap pre-call timer/frame intervals overlapped in all three diagnostics (lower margins −0.168/−0.103/−0.075 s). Paint-before-receipt still held, but strict paint-before-call was **inconclusive**, not disproved. Do not remove the gap and claim the strict ordering was measured. These matched controls, rather than experiment 2’s different roughly-three-second fixture, establish the latency tradeoff.

Success reconciled without changing the intended display. Rejection after an earlier successful On first painted Off, then restored actual **On** with an error message and the same row. Rapid same-row presses alternated absolute pending states; different-row requests kept distinct captured IDs. Genuine old DIDL data—sequence 11 while 12 was pending and after settlement—did not overwrite the newer On; an old error caused no obsolete popup. Equal-sequence older-view and NaN metadata also left the sampled row unchanged. A reorder race retained Ceiling’s ID; the bridge rejected its old view rather than changing newly indexed Desk. Unlike experiment 5's explicit-reselection guard, this variant automatically rearms after repaint; its accepted safety case also depends on frozen-view rejection by the bridge. Automatic rearming alone is not proven to match visible pixels in every race.

This needs an explicit correlation contract, not optimistic caption guessing: canonical device/room identity, frozen view token, absolute desired state, session/intent identity, authoritative state revision and correlated acknowledgement/rejection. Retain high-water state after settlement. The experiment encoded metadata in custom class suffixes; it did not implement an existing production schema. Sequences remained monotonic across views within its session; reset/other-session behavior needs defined epoch scoping. Contradictory payloads with identical sequence/version cannot be ordered. Lost-confirmation timeout/recovery and complete malformed-metadata validation were not acceptance-tested. These are reasons to keep the simpler authoritative default, while retaining the measured optimistic helpers for a deliberate later choice.

Evidence: [optimistic section](../../../../research/bridge-runs/smart-devices-followup-2026-11-14/rooms-visual/experiment-12.md), `rooms-visual/opt/metrics.json`, retained trials, stale/rejection/race frames, native-entry timer analysis and matched controls. The view guard rejects at receipt in this fixture; execution-time revalidation after an intervening delayed context change was not separately demonstrated.

## Tested helpers to carry into the firmware patch

These are proven patterns, not drop-in production code; retain the original envelope bounds, session rollover and result validation.

| Helpers/pattern | Proven role |
| --- | --- |
| `SL_get`, local `SL_set`, `SL_pump`, `SL_finish` | Local slider state, trailing timer, one sender, immutable target and close flush. |
| `SD_brightness` opening structure | Per-device title/initial value and proxy/widget construction at open. |
| `Global_CallOnceAfterDelay` and returned `cancel()` | Delayed/coalesced work and cancellation; callback still blocks on SOAP. |
| `SD_sendOnly`, `SL_sendOnly` | Existing UI-event envelope with no per-value result read. |
| `SD_sendSilent`, `SD_readDeferred` | Verified/error-only dialogs; do not schedule a result read if send returned null. |
| `SD_userButton`, `SD_acceptSnapshot`, `SD_markDirty`; `OG_*` | Retained identity and invalidation epoch: SD requires explicit reselection after ambiguity; OG rearms after repaint and additionally relies on bridge view rejection. |
| `SD_headingNoop`, `SD_skipHeading` | Inert/skip behavior for a leading heading only; omit initially rather than generalize untested cases. |
| Scoped `createCDNode` and child softkey policies | Custom action leaves, nested folders and per-type labels. |
| `SD_startPoll`, root-only `SD_pollAvailability`, `SD_stopPoll` | Root flag polling and rebuild-on-change worked; root-only scope worked. Cancel-on-leave is implemented without a dedicated acceptance trial. |
| `RV_groupMembers`, `RV_groupKey`, `RV_params` | Group/member enumeration, membership root refresh and action context. |
| `RV_activate`, `RV_confirm`, `RV_confirmDone` | Started acknowledgement and captured-context confirmation. |
| Scoped `Browsepanel_setClassDecorations` wrapper | Blank or class-driven on/off decorations, preserving stock folders. |
| `OP_init`, `OP_info`, `OP_sync`, `OP_redraw`, `OP_schedule`, `OP_afterPaint`, `OP_sendNext`, `OP_showError`; `Node_FetchCDNodeChildrenEx`/`OP_originalFetch` wrapper | Full-title plain label-chunk overlay, correlated reconciliation and deferred error display; see experiment 12’s conditions. |

Do not carry the failed selection-decoration optimization, the native-Bass brightness alias, or a generic heading skip as if fully accepted. Sources and inventories: `slider/experiment-1.md`, `lists/helpers.md`, `rooms-visual/worked-helpers.md`, and `rooms-visual/opt/build_opt.py`.

## Changes required by these measurements

- Bind every command/slider to immutable device identity and a frozen room/group view. Native indices and current-cache titles are insufficient; reject stale/renamed/removed bindings before mutation. Use opaque ASCII view tokens for Unicode labels; revalidate identity/view immediately before a queued mutation as well as at receipt.
- Keep catalog/view epochs separate from ordinary state revisions. A blanket dirty-on-every-update guard unnecessarily requires reselection after normal power changes.
- Build the 300 ms coalescing slider and flush on Back. Timer callbacks do not create asynchronous transport; avoid an untagged shared native-integer target.
- Use all group members’ HA areas, membership-change root refresh and a root-only availability trigger. Open-list availability transitions require CD events.
- Coalesce external updates at a conservative 2 s minimum; keep user-confirmation/failure/availability notifications immediate.
- Use folders rather than heading rows initially; use brief Started for one-shot actions and explicit default-Cancel confirmation for risky ones.
- Custom icons need a small, pinned resource patch with unused low IDs. Dynamic labels need the tested distinct wrapper handlers.
- If optimistic display is enabled later, it requires a new explicit intent/confirmation contract: absolute desired state, session/intent identity, monotonic per-device authoritative revision, separate view epoch, correlated rejection and bounded recovery. Old data cannot clear a newer pending intent. The throwaway class suffix is a transport probe, not an existing production schema.
- Only the synthetic, finite cases above are accepted. Full malformed-data validation, all heading arrangements, physical performance and real HA/Sonos execution remain outside these measurements.
