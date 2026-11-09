# CR100 Bridge findings, evidence and open items

The documentation checkpoint was completed November 2, 2026, before implementing
the requested stock-firmware **Music > Speaker Settings** route. The source
implementation and stock 4.3.2 emulator acceptance are now verified; the Pi
deployment is recorded below. Existing package 1/package 2 deployments and laboratory
prototypes remain distinguished below. The initial documentation phase made no
device, firmware or runtime changes.

## Reading the evidence

“Implemented” describes source behavior; “deployed” identifies a recorded Pi
release. Neither establishes every physical UI or speaker write. Stock application
emulator tests use original **4.3** software unless explicitly stated otherwise.
Exact **4.3.2** tests of the four native settings controls use a **patched SWF**,
the unchanged native executable and emulator helpers. Native NAND tests use the
original kernel/updater, synthetic storage and an already-privileged laboratory
bootstrap. They do not establish physical access.

Detailed `runs/` receipts are local research artifacts, normally ignored by Git
and excluded from portable runtime archives. Links identify retained evidence;
an installed Pi does not necessarily contain those files. The [October 31 MVP
note](RESEARCH-MVP-2026-10-31.md) preserves the earlier radio observations and
corrected cipher interpretation. Its old scope, hardware candidates and open
choices are historical; current support is described in [README](../../README.md).

## Documentation and evidence index

| Topic | Summary and detailed evidence |
|---|---|
| Installation, Pi/radio, services, backends, Spotify, About and Line-In | [README](../../README.md) |
| Backend coverage, package 1 routing and package 2 settings, deployments and live checks | [BACKEND-FEASIBILITY](BACKEND-FEASIBILITY.md); [direct Sonos](../../../../research/bridge-runs/backend-feasibility-2026-11-02/sonos.md), [HA Sonos](../../../../research/bridge-runs/backend-feasibility-2026-11-02/ha-sonos.md), [generic HA](../../../../research/bridge-runs/backend-feasibility-2026-11-02/ha-generic.md) |
| Stock text messages, search keyboard, choices, ratings, confirmations, playlists and alarms | [DIALOGS](../features/DIALOGS.md), [original-runtime results](../../../../research/bridge-runs/interactive-emulator-2026-11-01/README.md), [native/parser notes](../../../../research/bridge-runs/interactive-research-2026-11-01/native/NOTES.md) |
| Original native settings UI, units, guards and reversible loading | [CONTROLLER-UI](../features/CONTROLLER-UI.md), [prototype receipt](../../../../research/bridge-runs/settings-ui-prototype-2026-11-02/validation-summary.json), [overlay rollback](../../../../research/bridge-runs/settings-ui-prototype-2026-11-02/overlay-validation.json) |
| Stock Music action, custom package/signature tests, physical loading boundary | [CONTROLLER-DEPLOYMENT](CONTROLLER-DEPLOYMENT.md), [stock design/exact gates](../../../../research/bridge-runs/controller-deployment-2026-11-02/bridge-only-options.md), [stock runtime receipt](../../../../research/bridge-runs/controller-deployment-2026-11-02/stock-menu-validation.json), [patched 4.3.2 runtime](../../../../research/bridge-runs/controller-deployment-2026-11-02/custom-runtime-validation.json) |
| Native synthetic NAND install, cold boot and specific fallback test | [RESULTS](../../../../research/bridge-runs/controller-native-install-2026-11-02/RESULTS.md), [installer contract](../../../../research/bridge-runs/controller-native-install-2026-11-02/installer-contract.md), [NAND selector](../../../../research/bridge-runs/controller-native-install-2026-11-02/NAND-SELECTOR.md), [JFFS audit](../../../../research/bridge-runs/controller-native-install-2026-11-02/jffs-preinstall-audit.md), [SCIF correction](../../../../research/bridge-runs/controller-native-install-2026-11-02/scif-rx-fix.md) |
| Native emulator hardware/boot boundaries | [board compatibility](../../../../research/bridge-runs/controller-native-install-2026-11-02/board-compatibility.md), [independent boot ABI](../../../../research/bridge-runs/controller-native-install-2026-11-02/boot-abi/BOOT-ABI.md), [minimal HH/MCU model](../../../../research/bridge-runs/controller-native-install-2026-11-02/hh-mcu-contract.md), [boot-ROM provenance](../../../../research/bridge-runs/controller-native-install-2026-11-02/cr100-bootrom-provenance-audit.md) |
| Physical read-only diagnostics, startup/loader boundaries and blocked audit | [initial-access audit](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/README.md), [entry points](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/software-entrypoints.md), [SWF loading](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/ordinary-swf-loading.md), [diagnostic UI](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/diagnostic-ui.md), [maintenance search](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/first-party-maintenance-documentary-check.md) |
| Owner hardware/FCC, conditional external backup and recovery | [external-flash route](../../../../research/bridge-runs/controller-native-install-2026-11-02/external-flash-route.md), [owner/teardown comparison](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/owner-fcc-teardown-cross-check.md), [FCC exhibits](../../../../research/bridge-runs/controller-native-install-2026-11-02/initial-access-audit/sbvcr002-photo-findings.md), [hardware references](../../../../research/bridge-runs/controller-deployment-2026-11-02/hardware-recovery-references.md) |
| Older firmware/media | [findings](BACKEND-FEASIBILITY.md#native-menu-restoration-and-older-firmware-research), [image receipts](../../../../research/bridge-runs/settings-restore-2026-11-02/older-cr100-firmware-findings.json), [media receipts](../../../../research/bridge-runs/settings-restore-2026-11-02/historical-media-receipt.json) |
| Spotify discovery/playback, paging and queue art | [initial service](../../../../research/bridge-runs/spotify-sonos-2026-10-31/README.md), [paging diagnosis/fix](../../../../research/bridge-runs/spotify-fixes-2026-10-31/README.md), [discovery](../../../../research/bridge-runs/stub-discovery-2026-10-31/README.md) |
| Per-controller Spotify menus/locks and folders | [current behavior](../../README.md#updates-and-existing-manual-installations), [menu deployment](../../../../research/bridge-runs/spotify-menu-2026-11-02/deployment.json), [live folder proof](../../../../research/bridge-runs/spotify-folder-read-2026-11-02/README.md) |
| About/capability/native-settings deployments | [About](../../../../research/bridge-runs/about-system-2026-11-02/deployment.json), [package 1](../../../../research/bridge-runs/package1-2026-11-02/deployment.log), [package 2](../../../../research/bridge-runs/package2-2026-11-02/deployment.log), [read-only ZP80 contracts](../../../../research/bridge-runs/package2-2026-11-02/zp80-contracts-readonly.json) |
| Line-In and Spotify Connect Now Playing | [stock 4.3 Line-In UI](../../../../research/bridge-runs/line-in-validation/validation.json), [Now Playing diagnosis/live verification](../../../../research/bridge-runs/now-playing-sync-2026-11-02/README.md) |

## Findings and their limits

| Finding | Verified scope | Limit |
|---|---|---|
| Bridge/radio/installer | Current runtime supports one controller on Pi 4/Kali 64-bit with Nexmon; existing physical pairing/link and deployed services have receipts. | Fresh installation, board/driver injection and pairing acceptance remain hardware checks. Other Pi models, Pi OS and 32-bit Kali are unsupported. |
| Real About data | Selected Sonos rooms show native model/build/hardware/serial/IP and explicitly bonded components; bridge identity is informational. | Cached descriptive identity never grants commands or changes the compatible discovery persona. |
| Package 1 routing | Deployed `release-package1-acp2kz_u`: operation gates, generic HA media references, grouping boundaries and explicit unsupported-command errors. | No live HA; broader physical S1/S2 coverage remains open. |
| Package 2 native settings | Deployed `release-package2-q46bsd73`: shared verified Sonos provider, confirmed readback, fixed-mode uncertainty guards and independent LF/RF channels through either backend. | Real ZP80 checks were read-only with zero physical setters. HA playback/grouping remains HA-owned. Commands do not imply stock menu visibility. |
| Stock System Settings | Exact 4.3.2 has Controller Settings, Online Updates and About only. Available 4.3 also omits the older speaker menus. Sleep Timer remains under Music > Clock and Alarms. | SOAP capabilities, remote DIDL callbacks and rebooting cannot create local menu bindings/sliders. |
| Stock Music action | Earlier unmodified **4.3** fixture applied simulated +2 then -10. The new production service also passed **unmodified 4.3.2** UI acceptance: Spotify coexistence, browse-only zero writes/playback, explicit Add applies Bass +2 once, then native current-value refresh. | Fixed “Add to My Speaker Settings Library” caption; custom override failed. Physical controller interaction and real speaker setters remain unverified. |
| Patched native UI | Isolated 4.3 and exact-native-executable 4.3.2 runs passed Bass, Treble, Loudness and LED SOAP/readback. EQ widget units are translated by two. | Patched SWF/simulated peer only; capability filtering, live bridge/HA, multiroom/fixed-output/disappearance and physical loading untested. |
| Temporary SWF overlay | Exact BusyBox bind/unbind restored original hash/menu on earlier application-emulator kernel; full app exit precedes unmount. | No physical stock-kernel, reboot rollback or persistent-hook validation. Copied config is not selected automatically. |
| Package authentication | Same-size rebuilt CramFS/UPD changes intended SWF contents only. Exact updater rejects stock-key authentication with error 6; explicit lab key passes authentication. | `-K` requires prior local key placement/invocation. HTTP mirror and earlier authentic physical update do not bypass vendor trust. |
| Native NAND | Original updater/kernel wrote synthetic bank 1/generation 2 and completed post-install; separate cold boot mounted it; invalid completion-marker clone fell back to bank 0. | Updater reset returned **error 17**, not success. Privileged JFFS seed, synthetic BIOS/storage, host kernel extraction and stopped UI are explicit boundaries; no physical ROM/reset acceptance. |
| Physical diagnostics | Pi-mediated VERSION/dmesg/ps HTTP 200: `25.2-50130`; TCP23 refused; no maintenance daemon/updater in bounded listing. | Read-only only. Missing serial banner does not reveal full manufacturing flags. No CR100 root/ROM/NAND/identity backup, unlock or flash write established. |
| Ordinary loading leads | Unlock authenticates device credential; local-setting endpoint is a stub; image-only HTTP loader rejects SWFs; diagnostic SWF absent. Hidden menu is hardware diagnostics/burn-in/power-off. | No supported initial-access route or public service package established. Automated cybersecurity review stopped deeper entry-point audit; full firmware-loading goal is blocked/unachieved. |
| Hardware/recovery | Owner: SBVCR002, hardware `1.2.3.3-1`. FCC filing is shared radio-module approval. Different-revision teardown supplies conditional leads only. | Actual chip/package/voltage/geometry, UART wiring, electrical access and restore are unknown. Existing captured ROM is ZP80, not CR100; programmer lists do not prove CR100 recovery. |
| Older images/media | 4.3 does not restore menus; model-27-named donor is model-1/player firmware; examined setup CDs contain desktop installers/bootstrap downloads. | No usable pre-4.3 model-2 image recovered. Bounded CDN/Wayback results are not exhaustive absence. |
| Stock interaction surfaces | Original 4.3 exercised keyboard/search, read-only choices, service-radio ratings/result message, add/remove, playlist/alarm submissions and sleep requests. | Ratings require service-radio context; removal No is local and wording fixed. Mock playlist/alarm submissions do not prove persistence/scheduling/readback. No arbitrary forms/sliders/info panel established. |
| Text notification | Non-OK AVTransport description rendered arbitrary text over browser; wrapping/newlines/dismissal checked; 4.3.2 handlers statically match. | No generic buttons/input or dismissal callback. Selected-room automatic hook, Now Playing transition and physical 4.3.2 delivery remain open. |
| Spotify paging/menus/folders | Stable bounded search fixed request loop: first 50 raw slots maximum, filtered. Controller menus/locks and folder sync have deployment records. Separate client grant read real folder membership/order. | Private read-only folder endpoint may change; original live sample was not nested. Locks apply to a menu route, not all paths to the same music; controller caches can persist. |
| Connect Now Playing | Package 2 already contains song-identity/metadata/art-key fix. Real/virtual track transitions, physical cover GETs passed; user confirmed CR100 metadata updates. | Older prepared patch must not be reapplied. Like/Unlike/Add-to-playlist still needs UI/service/account/write integration. |
| Line-In | Both backends route verified Sonos inputs. Stock 4.3 UI exercised shared bridge code with in-memory source/destination. Input name is needed in transport and track metadata. | Emulated playback is not physical sound validation. Real HA/coordinator, cable/source/destination and event acceptance remain open. |

Package 1's frozen release passed 300 local/62 focused Pi tests; package 2 passed
359 local/129 focused Pi tests. Those historical results do not test the proposed
Music service. Full settings/routing contracts, failure semantics and manual gaps
remain in [BACKEND-FEASIBILITY](BACKEND-FEASIBILITY.md).

## Adopted implementation: Music > Speaker Settings

Implement a **separate bridge-owned service named Speaker Settings**, alongside
Spotify and its accounts/controller policies. “Speaker Controls” identifies the
earlier fixture. Production implementation is in `tools/speaker_settings.py` and
`tools/music_services.py`, integrated through `tools/zoneplayers.py`. This route
uses unchanged controller firmware.

The first menu exposes capability-supported Bass, Treble, Loudness, Status Light,
fixed/variable Line-Out, Balance presets and Sleep Timer presets/cancellation.
Line-In name/icon/gain editing remains separate. It is enabled by default on port
8401/service ID 244, with optional `speaker_settings` configuration overrides.
The local full suite passed **390 tests**; real HTTP fake-device integration
exercises native settings through both backend classes. No live HA was available.
Stock 4.3.2 screenshots and read-only/action receipts are under
`runs/speaker-settings-2026-11-02/emulator/`.

Use **room > supported setting > absolute value**. Catalog, metadata, search,
artwork and paging are read-only. A nonplayable preview/value exposes the stock
**Add to My … Library** action; only explicit SMAPI
`createItem(favorite=issued-context)` applies it. A service-name change may change
the fixed caption; a custom “Apply” label must not be promised.

Reuse existing provider capabilities and readback. Recheck availability, policy,
capability and room binding at apply time; generic HA acquires no invented Sonos
settings. Fixed-output/uncertain-mode guards stay authoritative. Issued context
binds room/provider identity, operation, absolute value, revision and expiry.
Consumed-context receipts make retries idempotent; stale, expired, rebound and
unissued actions fail. Never infer a target from the current playback room or
broadcast settings. Success requires confirmed backend readback.

Integer/boolean controls use lists. If text entry is included, stock room-bound
search produces a validated read-only preview, followed by the explicit action;
it is not a command language. Existing balance/output/input/timer backend support
does not itself prove a new Music-menu presentation.

Stock 4.3.2 **OK Button > Play Now** was tested: selecting a nonplayable value
does nothing, makes no playback request and sends no setter. There is no
separate Options softkey. Users must select **Music > System Settings >
Controller Settings > OK Button > Show Menu** for this route; the bridge cannot
override it. Applying settings causes zero playback calls. See the
[exact gates/acceptance recipe](../../../../research/bridge-runs/controller-deployment-2026-11-02/bridge-only-options.md).

## Open items and completion criteria

“Future package” items are separate features, not prerequisites for the Music
route. Physical acceptance and firmware loading retain their own status.

| ID | Status | Remaining work/evidence |
|---|---|---|
| M1 | Implemented/tested | Descriptor/account aggregation preserves Spotify/controller policy; distinct service IDs/ports and independently decoded account events tested. |
| M2 | Implemented/tested | Cached capability-filtered catalogs bind to configured backend rooms; unsupported/unavailable rooms show informational rows. |
| M3 | Implemented/tested | Expiring issued actions, absolute values, stale/rebinding rejection, bounded retry receipts and confirmed readback tested; HTTP reads remain nonmutating. Balance is two writes and can fail partially. |
| M4 | Stock 4.3.2 acceptance verified | Coexistence, fixed Add caption, zero browse writes/playback, one explicit mutation and native current-value refresh passed. Direct Play Now does nothing for these values: Show Menu is a required controller preference. Physical acceptance remains M7. |
| M5 | Simulated coverage passed | Direct Sonos and HA-Sonos backend classes through native fake-device HTTP, generic HA withdrawal, fixed-output restore, coordinator/member routing, outages/rebinding and retry races tested. Live HA/model coverage remains H1/H2. |
| M6 | Deployed/verified | Frozen archive passed 76 focused Pi tests, then deployed to release-speaker-settings-kju9fbkx. Readiness, both services, real ZP80 catalogue agreement and protected-data hashes passed. Previous package 2 release retained; no physical setters sent. |
| M7 | Partial physical acceptance | User navigated the physical 4.3.2 controller and applied Status Light Off to the Bedroom ZP80; createItem returned 200 and native LED readback is Off. Other settings/audio remain untested. Temporary AP loss/search/reconnect occurred; both bridge and controller application PIDs stayed unchanged. |
| U1 | Prototype only | Native patched UI capability/error handling and live bridge/HA, multiroom, fixed-output/disappearance tests; balance/output/input/extended sleep/alarms UI is separate. |
| U2 | Physical dependency | Legitimate controller access and owner backup/recovery before temporary loading; stock-kernel bind/space, controlled restart, original-hash rollback and reboot test before persistence. |
| F1 | Blocked/unachieved | No software-only initial access/custom-firmware route. Deeper audit was rejected by automated cybersecurity review; requires newly documented legitimate service access/route or approved review environment. November 3: `/unlock` is now live-confirmed reachable on the physical unit and its credential is identified as the stored device identity key record; authenticated-access acquisition now converges on the F3 hardware-backup dependency. See the [unlock-route follow-up](../../../../research/bridge-runs/unlock-access-route-2026-11-03/README.md). |
| F2 | Emulator boundary | Automatic MCU reset/programming, OTP acceptance, host-independent bank loading, actual geometry and full native UI/radio/display/power remain unverified. Error 17 is not automatic update success. |
| F3 | Hardware facts missing | Owner-board/chip/package/voltage identification, passive UART pinout, isolated access, repeated full page/OOB reads, immutable original backup and independent restore demonstration. Preserve OTP separately; synthetic media is never an owner backup/image. |
| F4 | Research open | Pre-4.3 model-2 donors/public service package search bounded. Model-1 images, old account flags and FCC lab telnet provide no owner loading procedure. |
| H1 | Live HA unavailable | Registry permissions/bindings, platform/config-entry assumptions, real feature flags, opaque references, queue formats, grouping, reconnects and endpoint/coordinator changes. Generic rooms must never call Sonos. |
| H2 | Physical coverage pending | S2/additional models, unsupported faults/stale menus, fixed/variable mode and channels. Legacy boolean does not implement Port Pass-Through. |
| H3 | Known limits/future | Settings poll every 30 s without specific GENA; external changes can lag. Current direct-Sonos availability can withdraw physical settings when only coordinator fails; per-provider refinement remains open. |
| H4 | Physical timer/input pending | Sleep start/cancel/countdown/generation/coordinator, Line-In cable/name/icon/events and source/destination through real HA/direct Sonos. Gain writes need explicit ranges absent on tested ZP80. |
| P3 | Future package | Queue reorder, persistent saved queue/save/rename/delete/overwrite, invalidation/events and actual firmware rename. Generic queue ownership/enumeration must be explicit. |
| P4 | Future package | Native alarm mapping and non-Sonos persisted scheduler: linked rooms, timezone/DST, missed deadlines/restarts, versions/running state and snooze/clock UI. Mock submission is not scheduling. |
| P5 | Future package | Generic HA EQ/LED/input, bridge timers, Music Assistant/richer queues, library/provider management, compression/autoplay, authenticated NAS shares and legacy time APIs need contracts/validation. |
| S1 | Coverage/known limits | Live nested folders, authorization expiry/private-endpoint changes and persistence/recovery. Initial live sample proves membership/order only. Search is bounded to 50 raw slots; missing source art can remain 404. |
| S2 | Future feature | Like/Unlike/Add-to-playlist: current Spotify identity, stock menu, account choice/write permission and acknowledgments. Service-radio ratings is not a general Now Playing menu. |
| D1 | Integration open | Per-player ordered notification/reset and selected-room context; automatic browse-to-dialog, Now Playing transitions and physical 4.3.2 delivery unverified. No arbitrary remote dialog/forms API found. |
| R1 | Radio reliability open | Physical controller intermittently loses AP connectivity and searches/reconnects. It happened around the user's LED change and earlier before that request; logs do not prove the LED action caused it. No bridge/controller process crash or restart occurred. Investigate separately from menu semantics. Fresh platform/pairing coverage and broader radio provisioning remain separate. |

M1–M6 are now implemented and verified within the scopes above. This does not
complete the separate firmware-loading or physical-acceptance goals.

## Speaker Settings deployment follow-up

On November 2, the frozen candidate was deployed to
`/opt/cr100-bridge/releases/release-speaker-settings-kju9fbkx`, retaining
`release-package2-q46bsd73` for rollback. The archive SHA-256 is
`ba80dd35f613d29de72d9608fb430ba2ccd44547f766244a0fed52dac3abd31e`.
The full local suite passed 390 tests; after the emulator exposed the required
Show Menu preference, the final guidance-only update passed 19 targeted local
tests and the exact final archive passed 76 focused Pi tests. Pairing,
configuration and web-settings hashes were unchanged.

Live read-only verification found Spotify (240/8400) and Speaker Settings
(244/8401) together. The Bedroom ZP80 catalogue reports all seven supported
controls; Bass, Treble and LED values agree with independent native queries.
No physical setting or playback command was sent by automated deployment
validation. Subsequently the user applied Status Light Off successfully from the
physical controller. The reported return to Searching was a temporary AP loss,
with no process restart; radio reliability remains open. Real HA and other
hands-on physical setting/audio checks remain open items.

Receipts: [deployment and validation report](../../../../research/bridge-runs/speaker-settings-2026-11-02/README.md),
[stock 4.3.2 acceptance](../../../../research/bridge-runs/speaker-settings-2026-11-02/emulator/acceptance-receipt.json).
The deployed documentation is the source-freeze snapshot; this local addendum
records the subsequent rollout outcome. Runtime source hashes match that snapshot.

## Initial-access route follow-up (November 3, 2026)

A read-only recheck confirmed the physical CR100 at `192.168.69.52`
(`25.2-50130`, TCP23 refused). First-ever requests to its `/unlock` endpoint
returned `405` (GET), `403` (invalid-length `hash=`) and `400` (missing field),
each matching a distinct traced failure branch, with no observable state
change: the authenticated maintenance route is live-confirmed reachable.

Consolidating documented evidence, the CR100 `/unlock` credential is the
controller's stored **0x2c4-byte device identity key record** — submitted as
hex and compared, not challenged-and-signed, so extracting it suffices. The
exact stock rootfs helper `grab_upgrade` confirms shipped controllers carry
that private key certificate in the manufacturing flash area, where the ZP80
audit located the same record format. Therefore the already-scoped external
NAND backup procedure (`external-flash-route.md`) is now the single acquisition
point for both the owner backup and the legitimate unlock credential: raw read
→ record extraction → authenticated `/unlock` → root telnet → the previously
emulator-verified temporary JFFS SWF overlay and `-K` custom-update
invocation. The remaining hard dependency for that route is the F3 physical
facts (actual chip marking/package/voltage, isolated access, validated read
and restore), not any network mechanism.

The pure-software argument-injection alternative (manifest URL text reaching
unquoted `/var/run/upgradeurl` expansion, ZP80-style) remains recorded but
depends on the preserved rejected-audit scope plus an undocumented CR100
key-file source; it needs the approved review environment. Bounded community
documentation search supplied no published CR100 unlock credential or owner
service procedure. Downgrade validation closed separately in
[cr100-emulator/research_update](../../../../research/cr100-emulator/research_update/README.md)
(stock 4.3.2 accepted a modified manifest with automatic `-F`; authentic 4.3
package passed `-t -F`), strengthening the post-access recovery position.
Full assessment and probe receipts:
[unlock-route follow-up](../../../../research/bridge-runs/unlock-access-route-2026-11-03/README.md).

## Flash geometry and credential-storage follow-up (November 3, 2026, later)

**Correction to the follow-up above.** Exact 4.3.2 `hh.anacapad` loads the
0x2c4-byte `/unlock` record from a 0x344-byte **file** `<dir>/private.key`
(record + 0x80-byte tail), and has a matching writer. It is not read from the
MDP or `/dev/mtd/0`. A caller of the writer is consistent with on-device RSA-1024
key generation, but that is unverified. The trace stopped when the automatic
auto-mode classifier denied the next disassembly step. As with the earlier
review rejection, it was not retried by another route. Whether a raw NAND backup
contains the credential therefore depends on the unresolved `<dir>` (JFFS
vs volatile) and the regeneration behavior. Treat this as open until the owner's
image is inspected offline or an approved review environment exists. The backup's
recovery value is unchanged.

**F2 narrowed.** The stock layout consumes exactly 1,004 good blocks, which
equals the Samsung K9F2808 datasheet's guaranteed minimum N<sub>VB</sub>. With a
geometry-parameterized copy of the emulator NAND (Samsung `EC 73`, 16 MiB), the
exact kernel/updater installed the custom UPD into bank 1 and skipped factory bad
blocks in both banks. This held with 4 bad blocks and with the datasheet worst
case of 20 (zero spare). Separate cold boots selected bank 1/generation 2 and
exposed the modified 901,555-byte MainUI. Bad blocks, MDP, table and bank 0 were
byte-unchanged. The updater still ends with error 17 (unmodeled MCU reset).
Details, hashes and reproduction:
[flash-geometry run](../../../../research/bridge-runs/flash-geometry-16mib-2026-11-03/README.md). The owner's
actual chip/voltage/package (F3) remains the first physical dependency.
