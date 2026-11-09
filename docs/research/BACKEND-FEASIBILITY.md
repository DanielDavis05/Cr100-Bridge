# CR100 feature feasibility across playback backends

Research date: November 2, 2026. The initial assessment compared the source tree, including
the in-progress Line-In work, with Home Assistant 2026.10.4 interfaces and current
SoCo/local-Sonos implementations. It is a feasibility assessment, not a claim
that every proposed feature is implemented or hardware-tested. That initial
assessment contacted no real speakers and changed no production code. The later
package 1/package 2 implementation, deployment and read-only physical checks
below supersede its initial status. [RESEARCH.md](RESEARCH.md) indexes current
findings and all remaining work, including the adopted, not-yet-implemented
stock **Music > Speaker Settings** service.

Detailed source notes:
[direct Sonos](../../../../research/bridge-runs/backend-feasibility-2026-11-02/sonos.md),
[HA with Sonos](../../../../research/bridge-runs/backend-feasibility-2026-11-02/ha-sonos.md), and
[HA with other speakers](../../../../research/bridge-runs/backend-feasibility-2026-11-02/ha-generic.md).

## Conclusion and the meaning of HA support

A room's capabilities should depend on the actual player, integration and media
source, rather than the selected backend's name. Sonos-specific native operations
can be supported for a Sonos speaker under either backend and omitted/rejected
for a non-Sonos HA room.

There are two different HA configurations:

1. **HA API only:** ordinary media-player actions, Sonos-specific HA actions, and
   associated number/switch/select entities. This can supply EQ, sleep timers and
   partial alarm editing, but does not expose every operation needed for parity.
2. **HA plus local Sonos extension:** HA continues to own ordinary playback/group
   commands. A Sonos extension handles missing native operations using the real
   speaker's local API. This can approach the same Sonos feature coverage as the
   direct backend, subject to model/firmware support and local connectivity.

The second configuration already has a read-only precedent in
`tools/ha_linein.py:78-103` and `tools/sonos_queue_art.py:95-117`: HA registry
identity resolves the physical Sonos device, then the bridge reads AudioIn or
ContentDirectory directly. Generalizing it to native settings is bridge work;
it does not inherently require a custom HA integration.

## Feasibility matrix

These cells describe possible implementations, not current completed support.
"Bridge" means storage/scheduling/content owned by the bridge. "Adapter" means
an optional provider for a positively identified device/integration. Native
features still require actual device/model/API support.

| Feature | Direct Sonos | HA with Sonos | HA with a non-Sonos player |
|---|---|---|---|
| Transport, volume, mute, seek, next/previous | Native; basic path exists | HA actions; basic path exists | Matching advertised HA features; not all entities implement each action |
| Shuffle/repeat | Native | HA actions | Integration capability or a bridge-owned queue |
| Enqueue / play next | Native queue | HA Sonos enqueue | Requires MEDIA_ENQUEUE; PLAY_MEDIA alone is insufficient |
| Enumerate/select/remove native queue entries | Native; much already exists | Existing sonos.get_queue/play_queue/remove_from_queue | No generic HA contract; integration adapter or an explicitly bridge-owned queue |
| Move/reorder queue entries | Native queue operation | Local Sonos extension | Provider-specific queue API; bridge queue when owned; otherwise unavailable |
| Save current queue | Native saved playlist | Local Sonos extension | Needs native queue enumeration or bridge queue ownership; current-track metadata is insufficient |
| Create/delete/overwrite saved playlists | Native saved-queue APIs | Local Sonos extension | Bridge-owned named playlists; provider playlist editing needs its own API |
| Rename a Sonos saved playlist | Legacy UpdateObject path; verify modern firmware | Same local extension/verification | Bridge-owned playlist rename is straightforward; vendor playlist rename is conditional |
| Bass/treble/loudness | Native real-device controls | HA number/switch entities or native extension | Matching device entities/API only; no universal numeric EQ contract |
| Stereo balance | LF/RF controls where supported | HA balance number or native extension | Integration-specific; cannot substitute master volume |
| Status LED / physical speaker name | DeviceProperties | HA LED switch / native rename | Integration-specific; a persistent bridge room alias works universally |
| Fixed/variable line-out | Only physical models with this output capability | Local Sonos extension with same model gate | Only a device adapter offering the real function; otherwise unavailable |
| Sonos Line-In selection/routing | Supported input models; basic current path exists | Current HA path plus Sonos input metadata | No generic x-rincon-stream support; local source selection is a different feature |
| Line-In name/icon/gain | Native AudioIn actions where accepted | Local Sonos extension | A vendor input adapter may offer equivalents; no generic Sonos-input control |
| Line-In compression/autoplay | Firmware-specific settings/actions; validate | Same native extension | No generic equivalent; audio relay/transcoding would be a separate feature |
| Sleep timer set/cancel/countdown | Native speaker timer and remaining-time reads | HA set/clear actions plus native countdown read | Bridge timer issuing supported pause/stop, or integration-native timer |
| Alarm create/edit/delete/recurrence/music/room/volume/duration | Native stored alarms | HA edits a limited existing-alarm subset; extension provides full native editor | Bridge-managed scheduler, conditional on target playback/volume/media |
| Snooze/running alarm/CR100 alarm screen | Native state/actions where supported plus protocol translation | Same native extension | Bridge-owned running-alarm state and snooze; exact behavior requires protocol work |
| Clock/timezone/date formats | Shared bridge service; optional verified native clock mapping | Same | Same; independent of speaker brand |
| Library browse/search and alphabetical jump | Native catalogs plus bridge translation/indexing | HA Sonos browser/search plus translation | BROWSE_MEDIA/SEARCH_MEDIA or bridge/provider library; indexes need a known catalog |
| Reindex configured Sonos library / compilation preference | Native indexer operations | Local Sonos extension | No generic HA library administrator; new bridge/provider indexer needed |
| List/remove Sonos NAS shares | Native documented paths | Local Sonos extension | Not applicable to unrelated players; a separate shared library may be configured |
| Add authenticated NAS share | Conditional: setup/credential protocol remains to be established | Same native investigation | Separate library-manager/SMB work; not a generic media-player action |
| Radio URL favorites/custom stations | Native favorites where suitable, or bridge catalog | Native extension or bridge catalog | Bridge catalog if the target can play the resolved stream |
| Music-service catalogs/accounts/likes/bookmarks/stations | Provider adapters/authentication | Same provider work; HA playback can remain | Provider support and target-compatible playback required; Sonos URIs are not portable |
| Grouping / Party Mode | Real Sonos groups within a compatible system | HA Sonos grouping | GROUPING plus compatible actual devices; no general cross-brand synchronization |
| Custom notifications/search input/ratings command UI | Bridge UI/protocol layer | Same | Same virtual-controller capability; action availability remains target-specific |

The native queue, rendering and sleep operations are evidenced by
[SoCo core](https://raw.githubusercontent.com/SoCo/SoCo/master/soco/core.py).
Native alarm persistence is evidenced by
[SoCo alarms](https://raw.githubusercontent.com/SoCo/SoCo/master/soco/alarms.py).
Library and favorites operations are covered separately by
[SoCo music library](https://raw.githubusercontent.com/SoCo/SoCo/master/soco/music_library.py)
and [SoCo favorites](https://raw.githubusercontent.com/SoCo/SoCo/master/soco/favorites.py).
Legacy-only methods remain conditional until checked against real device service
descriptions and successful action/readback tests.

## What HA already exposes for Sonos

The version-pinned sources show the following useful routes:

- `number.set_value` on the associated Sonos bass, treble or balance entity.
- `switch.turn_on/off` for loudness, crossfade and status light where available.
- `sonos.set_sleep_timer` and `sonos.clear_sleep_timer`.
- Sonos alarm switches and `sonos.update_alarm` for time, volume, enabled and
  linked-zones changes to an existing alarm.
- Existing queue get/play/remove actions and ordinary enqueue/play/clear actions.

Full alarm create/delete and editing recurrence, music, duration or room need
native augmentation. Queue reordering and saved-playlist editing also lack stock
HA Sonos actions. Status-light entities may be disabled by default; the bridge
should discover enabled/available controls or use an explicitly available native
route, not silently alter HA registry settings.

Sources: [number controls](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/sonos/number.py),
[switch controls](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/sonos/switch.py),
[Sonos actions](https://raw.githubusercontent.com/home-assistant/core/2026.10.4/homeassistant/components/sonos/services.py),
and [action implementations](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/sonos/media_player.py).

## What generic HA does not guarantee

Generic media_player is not a universal Sonos queue or hardware-settings API.
Its flags describe optional controls, including enqueue, browsing, searching and
grouping. They do not define a universal list/remove/reorder queue interface,
saved-playlist editor, numerical EQ, physical LED or alarm scheduler.
See [the released feature enum](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/media_player/const.py)
and [the integration contract](https://contributors.home-assistant.io/core/entities/media-player/).

This does not mean a non-Sonos device can never have EQ or an input selector.
Those can be mapped when that integration exposes matching controls. A sound-mode
preset is not automatically equivalent to bass/treble/balance. Selecting an AVR's
own input is not equivalent to distributing a Sonos analog source to other rooms.
Starting one URL on several devices is not synchronized grouping.

For richer non-Sonos queues, a separate optional Music Assistant adapter is a
credible route: its API has queue item enumeration and queue-to-playlist support.
It is not generic HA functionality or a current project dependency; a full queue
adapter may require its own API rather than only HA's exposed actions.
[Music Assistant API](https://www.music-assistant.io/api/).

## Shared services that can work on all three cases

These are proposed bridge implementations:

1. Persist household clock settings, bridge playlists, radio bookmarks, timers
   and non-native alarms independently of the backend.
2. Prefer speaker-owned alarms/timers on Sonos. Those execute without an active
   bridge connection. Bridge scheduling for other devices requires bridge/HA
   availability and needs a defined missed-deadline policy.
3. Persist IANA timezone and recurrence semantics; handle DST gaps/duplicates,
   restarts and clock changes. A HA timer helper alone is insufficient: HA
   documents that timer.finished does not fire on startup for a timer that
   expired while offline. [HA timer behavior](https://www.home-assistant.io/integrations/timer/).
4. Resolve IncludeLinkedZones at fire time. Map bridge alarm IDs/virtual room IDs
   to native Sonos alarm IDs/physical UUIDs, without duplicating the same household
   merely because it is reachable through both backend types.
5. Publish alarm-list versions, running-alarm information and controller events;
   scheduling audio alone does not activate the stock CR100 snooze/clock UI.
6. Store stable provider IDs, media types, DIDL and resolvers. Re-resolve media for
   each target. `bridge.invalid` display metadata, Sonos account URIs and expiring
   stream URLs must not be treated as portable playable playlist content.
7. Save a whole existing queue only when an adapter can enumerate it. A bridge
   queue may be introduced with explicit ownership/sequencing, but it cannot
   reconstruct a third-party app's queue from the currently playing track.
8. A fallback chime on a generic player needs an accessible, decodable audio URL;
   Sonos's buzzer URI is not a universal media source.

## Proposed per-room architecture

One room should expose operation-specific capabilities, settings/ranges, readback
and the provider responsible for each operation. The provider can be generic HA,
an HA number/switch, shared bridge services, or a verified Sonos extension.

Direct Sonos and HA-Sonos rooms reuse one Sonos feature implementation. A pure
HA room without a Sonos binding gets only generic capabilities and optional
adapters appropriate to its own integration. Wrapper/universal entities need an
explicit validated device mapping if HA does not identify them as platform=sonos.

Native identity comes from the HA entity/device registry and physical Sonos UUID,
with endpoint/model verification, rather than a friendly name. The existing
registry resolver rejects the bridge's own synthetic players. The HA client
already supports arbitrary action domains (`ha_client.py:133-138`), so number
and switch operations do not need a new network transport.

Dispatch each action once through its chosen provider. Keep ordinary HA
transport/group ownership while native extras perform readback and refresh the
controller state. Route queue/timer/play-mode operations to the current group
coordinator, physical EQ/LED/output settings to the device, and line-in settings
to the input source. Group compatibility is more specific than backend='ha'.

Advertise only actual capabilities where the old UI permits. Some settings may
remain visible because the current virtual player profile identifies every room
as a ZP80; fully hiding every irrelevant menu needs a separate firmware/profile
audit. Unsupported setters should return an explicit fault, not default success.

## Findings before package 1

This table records the original audit. The capability, generic HA catalog,
unsupported-command and grouping issues are addressed by package 1 below;
native settings were subsequently implemented in package 2; broader content
invalidation remains later work. Line numbers refer
to the audited snapshot.

| Issue | Evidence | Required change |
|---|---|---|
| HA queue methods assume Sonos | `ha_backend.py:193-195,214-222,241-267`; `ha_content.py:89-104` | Split generic playback from native/provider queue adapters |
| HA catalog roots assume Sonos IDs | `ha_content.py:26-45` | Build provider roots and retain opaque IDs/media types |
| Capability model covers too few operations | `ha_backend.py:170-174`; several calls lack matching feature checks | Add per-operation and settings capabilities, including integration identity |
| SOAP fallthrough can report success without an effect | `backends.py:429-471`; `zp80_peer.py:351-439` | Explicit dispatch outcomes and unsupported errors |
| Native client service list is incomplete | `sonos_upnp.py:33-41` | Add/discover AlarmClock, DeviceProperties and SystemProperties |
| Balance channels are flattened | `sonos_upnp.py:141-152`; `zoneplayers.py:107-113` | Retain LF/RF values and read/write actual stereo balance |
| Content updates mainly track Q:0 | `sonos_backend.py:328-334` | Invalidate/notify changed playlists, favorites, shares, library and alarms |
| Group compatibility is only checked by backend object | `zoneplayers.py:493-500` | Require a compatible native grouping provider/system |

## Boundaries and implementation order

Multiple CR100s, live radio-channel changes, controller provisioning and mesh
behavior belong to the radio/household layer, not to either playback backend.
Diagnostics can combine bridge/HA information and Sonos data where supported.
Actual Sonos firmware/account setup remains a distinct platform function; a
generic local SOAP client does not recreate every historical provider's account
enrollment, ratings or stations. S1/S2 grouping restrictions and physical model
capabilities remain in force. [Sonos system interfaces](https://docs.sonos.com/docs/components-and-interfaces).

Suggested delivery order:

1. Correct capability/identity dispatch and non-Sonos queue handling; eliminate
   fake-success setters and validate unsupported-operation behavior in the stock UI.
2. Share Sonos settings across both paths: real EQ/balance/loudness/LED, native
   sleep timer/readback, model-qualified output and input settings.
3. Implement native queue reorder, saved-playlist operations and refresh events;
   give generic players bridge playlists without claiming arbitrary native queue
   editing. Validate playlist rename on actual target firmware.
4. Implement the complete alarm facade and mapping, using native Sonos schedules
   plus a persisted bridge scheduler for non-native targets.
5. Add broader content/provider adapters, radio/library management and optional
   richer queue engines. Keep authenticated NAS-share addition and legacy-only
   time/compression APIs as explicit validation/research items.

## Package 1 implementation

Implemented on 2026-11-02: cached command bindings, operation capabilities,
backend-specific dispatch and rejection of unsupported writes. This is the
foundation for later Sonos settings, playlists and alarms; those features have
not been added in this package.

### About work reviewed and retained

The existing real About implementation was reviewed before changes. Its cached
identity inventory, registry fallbacks, bonded-device entries, bridge build stamp
and upstream IP remain the source of descriptive information. About reads do not
perform device discovery. Discovery/topology still use the common CR100-compatible
firmware persona. Bonded devices and the informational bridge entry do not become
playback rooms.

`RoomBinding` is a separate cached routing identity. It records the registry
platform/device, optional validated physical Sonos UUID/endpoint and grouping
scope. A model name, display firmware or About IP cannot authorize a command.
A recognized HA Sonos entity can retain HA Sonos service routes when its native
endpoint is unavailable; this does not grant it a direct native binding.

### Implemented routes

Every operation also requires current availability. Direct Sonos uses cached
device/coordinator state and transport actions. HA uses its connection state,
entity availability, registry platform and `supported_features`.

| Route | Direct Sonos | HA with Sonos entity | HA with non-Sonos entity |
|---|---|---|---|
| Play, pause, stop, seek, next/previous | Current native source/actions | HA feature flags; stop may use supported pause | Same generic HA feature checks and fallback |
| Master volume and mute | Native | HA feature flags | HA feature flags |
| Shuffle and repeat | Queue source | HA feature flags | HA feature flags |
| Browse and play catalog items | Existing Sonos catalog/provider routes | Existing Sonos browser/provider routes | HA media browser, retaining opaque IDs and media types |
| Browse/play/add/remove current queue | Existing native queue routes | Sonos-only services; play/enqueue feature gates | Rejected; no generic native queue adapter exists |
| Clear current playlist | Native queue | HA clear-playlist flag | HA clear-playlist flag only |
| Bridge Spotify/provider queue routes | Existing Sonos resolver | Sonos entity plus play-media/enqueue flags | Rejected; requires a future target-specific resolver |
| Sonos Line-In | Healthy connected input | Validated Sonos source plus play-media support | Rejected |
| Join/unjoin groups | Same discovered Sonos backend topology | Grouping flag and matching registry provider scope | Grouping flag and matching registry provider scope |
| EQ, balance, loudness, LED, fixed output, sleep/alarm, saved queue and device setup writes | Rejected pending later packages | Rejected pending later packages | Rejected pending an appropriate adapter |

Live Line-In withdraws pause/seek/next/previous/shuffle/repeat where appropriate
and reports normal play mode without destroying queue preferences. Partial
shuffle/repeat changes are checked before either HA service is called. Group
volume/mute checks all known members for support before sending writes; this
cannot make network failures during a multi-device write atomic.

SOAP contract validation runs before dispatch. Unsupported real-backend mutations
return UPnP 501 without first changing the standalone peer's URI, EQ, clock or
other simulated state. Per-channel volume/mute writes are rejected rather than
changing master volume. Existing virtual compatibility preferences are retained
explicitly. Read-only compatibility responses remain so the stock menus can open;
some unsupported settings may still be visible in the unmodified CR100 UI.
Existing controller-specific Spotify authorization and notices retain priority
over routing checks.

HA disconnection withdraws operations and notifies listeners while retaining
cached state and identity. Reconnection notifies again even if `get_states`
returns identical data. Registry refresh failures retain the previous binding;
an explicit registry removal withdraws it. Direct Sonos read failures withdraw
operations until successful state reads/events recover the device.

### Assumptions and pending live tests

No live HA instance was available. Fake HA WebSocket/registry/browser/service
tests cover both Sonos and non-Sonos entities, feature gates, SOAP rejection,
group boundaries and identical-state reconnects. The following still require
real installations:

- Verify registry access permissions and `platform`/`config_entry_id` values on
  the installed HA version. Generic grouping conservatively requires a shared
  platform and config entry. Valid groups spanning entries may need an explicit
  integration adapter rather than a broader automatic rule. A Sonos config entry
  is only a provider scope: it can discover multiple systems and does not prove
  shared household or S1/S2 compatibility. HA's actual join result remains
  authoritative until a future adapter supplies verified native grouping domains.
- Exercise real Sonos and generic HA transport, volume, mute, shuffle/repeat and
  clear-playlist controls, including feature-flag omissions and unavailable/off
  transitions. Confirm the intended stop-to-pause fallback on devices without stop.
- Check HA Sonos queue response formats, `MEDIA_ENQUEUE` behavior, provider
  replace/add/next, retained queue selection and Line-In coordinator routing.
  Generic entities must never issue `sonos.*` calls or accept Sonos catalog IDs.
- Browse at least two generic integrations with opaque IDs and distinct media
  types. Confirm each entity can play its own returned references and that a
  Sonos library/provider item cannot be sent to a generic target without a resolver.
- Test HA startup offline, disconnect/reconnect with unchanged states, registry
  updates/removal and native endpoint outages. About should retain cached details
  while command availability changes; recovery should refresh the controller.
- Test legitimate groups and rejected mixed-platform/cross-entry groups on HA.
  Universal/wrapper entities do not acquire Sonos privileges from names or model
  labels; a validated mapping adapter remains future work.
- On a physical CR100, check transport/play-mode refresh during radio, queue and
  Line-In playback, error presentation for unsupported settings, and stale menus
  after outages. Package 1 does not hide every unsupported stock settings screen.
- Validate the broader direct Sonos model/firmware matrix and native failure
  recovery. The read-only Pi check below covers one S1 ZP80, not S2 hardware or
  physical grouping/playback changes.

### Validation record

The test Pi was already running the latest About-info release. Candidate
tests run from a separate `/tmp/cr100-package1-*` tree using the installed Python
environment; the installed release, configuration and production services have
not been changed. A candidate household listens on loopback with discovery
disabled. A read-only backend probe uses the configured physical speaker for
topology, state and identity reads; unsupported setters go only to the candidate
loopback SOAP endpoints and never to the physical speaker.

The real speaker was identified as **Sonos ZP80**, firmware **57.23-74170**.
The probe verified real About firmware, unchanged compatibility firmware,
transport actions matching cached capabilities, and rejection of `SetBass` and
`SaveQueue` without simulated or physical mutation.

Validation results:

- Full local suite: **300 tests passed** in 183.871 seconds using
  `.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -q`.
- Isolated Pi suite: **62 tests passed** in 33.733 seconds across
  `test_capability_routing`, `test_ha_capabilities`, `test_sonos_capabilities`,
  `test_about_system`, `test_sonos_identity` and
  `test_spotify_controllers.PlaybackHTTP`.
- Real Pi/ZP80 read-only checks: real About identity and compatibility profile,
  cached transport capabilities, and unsupported-command rejection.
- Both portable-distribution tests also passed after adding this linked document
  to the package payload.
- `git diff --check` passed. No live HA validation or physical CR100 interaction
  was performed during the initial candidate tests.

Package 1 was subsequently deployed on 2026-11-02 to
`/opt/cr100-bridge/releases/release-package1-acp2kz_u`. Application readiness checks
passed, protected configuration/pairing/web settings hashes were unchanged, and
the previous About release remains available for rollback. Live bridge SOAP
queries returned the ZP80's real firmware/hardware and current transport actions.
The radio configuration and dependencies were not updated. Package 2 development
uses a separate working-tree snapshot; it was not part of this deployment.

Local probe source and detailed logs are retained under
`runs/package1-2026-11-02/` (an ignored research-output directory).

## Package 2 implementation

Backend implementation on 2026-11-02: one shared native Sonos settings provider serves direct
Sonos rooms and verified HA Sonos rooms. Ordinary HA playback, Master volume and
grouping still use HA. Non-Sonos HA rooms and Sonos registry entries without a
validated physical endpoint cannot acquire native settings from their About data.

**Controller UI integration is incomplete.** A user check after deployment showed
only Controller Settings, Online Updates and About in System Settings. Analysis of
the exact official 4.3.2 (`25.2-50130`) MainUI SWF confirmed that
`Node_populateSettings` at `0xbc29d` constructs exactly those three entries.
The older manual's ZonePlayer settings/EQ/LED/line-out menus are not linked there.
This is a firmware menu omission, not a failed backend route or a capability bit
that a reboot can refresh. The earlier package completion report overstated
controller-visible availability; automated SOAP tests proved command routes only.

Clock and Alarms still includes Sleep Timer (`0xb6bc0`). Native settings callbacks
remain in the SWF, but network ContentDirectory IDs/classes cannot install their
local callbacks or recreate those screens. A stock-compatible Music-menu command
interface using folders and bridge-owned item URIs is a separate implementation;
it is not present in the deployed package. Restoring the original System Settings
entries would require controller UI/firmware changes. Evidence is retained in
`runs/settings-menu-2026-11-02/firmware-visibility.txt`.

### Supported settings and routing

| Feature | Route and qualification |
|---|---|
| Bass and treble | Addressed physical speaker, native getter/setter pair and successful read; integer range -10 to 10 |
| Loudness | Addressed physical speaker, Master channel only |
| Balance | Independent physical LF/RF volume channels, range 0 to 100; never substitute or alter Master volume |
| Per-channel mute | Separate LF/RF values when both native channels and their actions are proven |
| Status LED | Addressed physical speaker, native On/Off enum |
| Fixed output | Require successful `GetSupportsOutputFixed=1`, native readback and setter support; fixed/variable legacy API only |
| Sleep timer | Verified current native group coordinator, even if it is not a selected bridge room; no fallback to an unverified member |
| Input name and icon | Addressed physical input source, including with the cable disconnected when AudioIn is supported |
| Input gain | Native readback where available; writes require explicit left/right SCPD ranges. The tested ZP80 provides none, so its gain writes remain unavailable |

Sleep durations must be less than 24 hours. An empty string cancels; `0:00:00`
requests immediate native sleep and must never be converted to cancellation.
Remaining time is interpolated with a monotonic clock from cached native reads;
the speaker owns execution, including while the bridge or HA is offline. Native
sleep generation is included in CR100 transport events.

Fixed output suppresses volume, mute, EQ and balance controls. A fixed-mode write
whose result cannot be confirmed also blocks these controls until a native
fixed-output read resolves the uncertainty. That guard is separate from the
displayed value: a failed read never invents a new confirmed mode. Rebinding to a
different endpoint invalidates the old device's snapshot and safety generation.
The legacy boolean does not implement modern Port Pass-Through mode.

### Cache, readback and failure behavior

Only selected room keys register with the settings worker; their verified native
coordinators are included for sleep-timer reads. A separate bounded worker keeps
settings discovery and polling off the playback/registry workers and CR100 HTTP
getter paths. Device description UDN/manufacturer, same-origin service locations,
SCPD action support, channel support and successful native reads qualify controls.
Descriptive model labels and advertised actions alone do not grant writes.

Cache getters return copies. CR100 settings responses return before the ordinary
now-playing/position path, so settings reads do not trigger playback network I/O.
Rendering events preserve independent channels and real EQ/output values.
Existing About identities, compatibility versions and informational endpoints are
unchanged. Input edits refresh the existing Line-In source-menu cache.

Setters validate all arguments before the native write, serialize against refresh
reads and confirm the requested result through native readback. Unsupported,
failed or unconfirmed operations return UPnP 501 without staging simulated state.
A binding or coordinator change during readback invalidates the result. Cache
staleness/native failures withdraw settings until recovery; known fixed-output
safety gates survive same-device uncertainty. Ordinary network writes cannot be
rolled back if a device applied them before a timeout, which is why the response
reports failure and waits for confirmed state.

### Validation and pending live checks

No live HA instance was available. Automated coverage uses fake HA/native Sonos,
real loopback HTTP/SOAP and injected read/write failures. It covers model/action
gates, generic HA rejection, physical/coordinator separation, LF/RF independence,
cache-only getters, validation, confirmed readback, uncertain fixed mode, stale
snapshots and endpoint/coordinator races. The final frozen release passed **359
local tests** in 202.478 seconds. Tests ran from the immutable distribution copy
so concurrent Now Playing edits in the shared checkout could not change the
release under validation. The snapshot includes the current Now Playing work and
its regression tests. The same snapshot passed **129 focused Pi tests** in 77.687
seconds, including settings, capability/identity routing, About, controller policy,
Now Playing and HA backend regressions. The real-ZP80 probe confirmed cached
settings/transport/About readback with **zero physical setters sent**.

Read-only research on the real ZP80 confirmed its native actions, ranges, current
EQ/channel values, LED, fixed-output support, timer fields and input attributes.
No real speaker setting was changed during this work. Native contract evidence,
the read-only candidate probe and logs are retained under
`runs/package2-2026-11-02/`.

Package 2 was deployed to
`/opt/cr100-bridge/releases/release-package2-q46bsd73` on 2026-11-02, retaining
package 1 at `/opt/cr100-bridge/releases/release-package1-acp2kz_u` for rollback.
The update refused to proceed unless the active release and its core source
hashes still matched the expected package 1 installation. Application readiness
passed and configuration, pairing and web-settings hashes were unchanged.
Read-only checks through the live virtual player's SOAP endpoints confirmed real
EQ, LF/RF channels, LED, output support/state, input attributes/level, sleep
generation, transport actions and the existing About firmware. No physical setter
was sent. The new radio process had not heard controller packets at the final
check, so controller wake/reassociation and hands-on UI checks remain unverified.

Pending validation and limits:

- With real HA, verify registry permissions/bindings, group coordinator identity
  including unselected entities, reconnects, and native endpoint changes. Ordinary
  HA transport/grouping must remain HA-owned while these extras use native UPnP.
- On a physical CR100, exercise native sleep start/cancel/countdown,
  timer-generation refresh and fault messages. Sound/device/input screens and
  balance sliders are absent from stock 4.3.2 System Settings; their physical UI
  checks require a separately implemented Music route or installed UI modification.
  Setter behavior has been tested against fake devices; physical writes require
  a deliberate manual check when the user returns.
- Verify S2 and more physical models. Output-fixed support is gated by native
  reads rather than product names; the Port's third Pass-Through mode remains
  outside the legacy boolean facade.
- Native settings refresh every 30 seconds. External app changes may take that
  long to appear; this batch does not add settings-specific GENA subscriptions.
- The existing conservative direct-Sonos room availability rule requires both
  device and coordinator reachability. Physical settings can therefore be
  withdrawn when only the coordinator is offline; per-provider availability can
  be refined later.
- Generic HA EQ/LED/input adapters, bridge-owned sleep timers for non-Sonos rooms,
  input compression/autoplay and unqualified Line-In gain writes are future work.
  Queue reorder/saved playlists and full alarms remain later packages.

## Native-menu restoration and older firmware research

Initial read-only research on 2026-11-02 identified the bindings needed for
native-style menu restoration. Subsequent emulator prototypes below verified four
controls, while safe physical installation remains unestablished. The initial
static research itself changed no firmware, controller settings or Pi services.

Exact 4.3.2 analysis found retained generic slider/choice-menu widgets and native
RenderingControl integer/boolean APIs, including bass, treble, balance and loudness
keys. DeviceProperties retains the LED menu interface. New ActionScript menu nodes
reconnect these pieces; the subsequent prototype below verified room selection,
sliders and choice menus with SOAP readback. This
requires reconstructing bindings and navigation, rather than changing one feature
flag or merely re-enabling a single array item.

Follow-up prototype: [restoring the original controller UI](../features/CONTROLLER-UI.md) now
adds Speaker Settings inside System Settings in an isolated 4.3 emulator. Bass,
Treble, Loudness and Status Light use retained native widgets and have passed
SOAP dispatch plus direct simulated-player readback. The native EQ widget API
uses twice the physical setting; the prototype translates this so the displayed
range remains −10…+10. Follow-up tests now also run the rebuilt 4.3.2 filesystem
with its unchanged exact native executable; all four controls pass simulated
SOAP readback. The exact updater accepts the custom package only when given the
lab public key, and rejects it under stock trust. See the
[deployment investigation](CONTROLLER-DEPLOYMENT.md). A later original-kernel
test writes the custom image to synthetic NAND and cold-boots bank 1/generation
2; automatic reset is unmodeled and returns error 17. Physical access remains
unresolved. A stock-firmware Speaker Controls service also passed an explicit Bass
action without playback, with fixed library-action wording. This
prototype has not been deployed to a physical controller or the Pi, and still
needs capability-aware visibility before integration with generic HA rooms.

The physical installation problem is separate: the system image is cramfs, normal
updates verify Sonos signatures, and the exact CR100 updater includes an
older-version rejection. A modified UPD cannot retain the original signature.
Writable `/jffs` and a developer startup hook exist, but no established privileged
file-write path to the physical controller was found. Emulator shell access and
ZP80 access are not evidence of CR100 access. A signed older image would still need
hardware-compatibility and downgrade-policy validation.

| Firmware/media candidate | Verified result |
|---|---|
| CR100 4.3, embedded `24.1-75101` | Still downloadable from the [official 5.2 companion URL](https://update-firmware.sonos.com/firmware/Gold/28.1-83251-v5.2-pcyakr-RC4/28.1-83040-1-2.upd), 4,834,793 bytes; its misleading filename is `28.1-83040-1-2.upd`. It has the same three-item Settings menu and does not restore the missing controls. |
| CR100 4.3.2, `25.2-50130` | Current official image remains downloadable; used for exact UI/native/updater analysis. |
| Pre-4.3 model-2 candidates | No verified usable image recovered. Known/inferred older CDN candidates returned 404; a model-2 Wayback query returned no matches on one host and timed out on the other. This is a bounded search, not proof that no older backup exists. |
| Preserved `10.20-75300-1-27.upd` | Downloaded and hash-verified. Its actual compatibility record is manufacturer 1/model 1; its rootfs contains a player daemon and no handheld/SWF assets. It is not a CR100 update or UI donor despite the unusual filename. |
| Original 2005, 2010 and 2011-labelled setup CDs | Downloaded, hashed and statically extracted. They contain desktop installers or download bootstrappers, not CR100 firmware. No installers were executed. |

Research evidence and reproducible receipts are retained under
`runs/settings-restore-2026-11-02/`: `restoration-feasibility.txt`,
`older-cr100-firmware-findings.json`, `older-cr100-url-checks.json`,
`historical-media-receipt.json` and `model27-donor/inspection.json`.
