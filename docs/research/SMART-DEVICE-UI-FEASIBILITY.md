| Question | Answer | Status |
| --- | --- | --- |
| A1 | `Node_populateTopLevelChildren` can add Devices using a room-specific bridge property, and visibility followed room selection with an explicit root-refresh hook. | observed in emulator |
| A2 | A new ContentDirectory prefix and firmware-side custom node/OK dispatch worked; browsing, prefetch and scrolling produced zero action requests. | observed in emulator |
| A3 | A silent-success UI event updated the selected row in place in approximately 4.1–4.9 seconds in the 24-row fixture. | observed in emulator |
| A4 | An external `ContainerUpdateIDs` notification caused automatic re-browse and retained the cursor/scroll position, with updated text 1.6–2.0 seconds after publication in the isolated follow-up. | observed in emulator |
| A5 | A custom Brightness softkey worked and blanked/reappeared as the highlighted row changed. | observed in emulator |
| A6 | The centered slider accepted a custom title, 0–100 range and callback; five slow unit wheel events made five callbacks, a 20-event burst made 20, and Back retained the row. | observed in emulator |
| A7 | Seven single-line rows, fixed class icons and native paging worked with 1,000 entries; title capacity depends on glyph width, and a native capacity/slowness ceiling was not established. | observed in emulator |
| B1 | Real registries provide entity/device area inheritance, area-to-floor joins and filtering fields; both HA helper groups and integration-native light groups must be considered. | observed on real HA |
| B2 | All three light services accept `target.area_id`, and area toggle independently inverts each selected light rather than choosing a uniform room state. | from source/documentation |
| B3 | A real Radio Browser item resolved to an external unsigned AAC URL, while other sources can produce relative, signed HA URLs; native Sonos playback remains untested. | observed on real HA |
| B4 | All 14 sampled Sonos device records had local port-1400 configuration URLs, but these are discovery hints requiring identity/availability validation. | observed on real HA |

Investigation: November 14, 2026. Evidence is under
[`research/bridge-runs/smart-devices-2026-11-14/`](../../../../research/bridge-runs/smart-devices-2026-11-14/).
The controller tests used the **unchanged exact 4.3.2 native executable**
(`fe4f159b…d830`) and a throwaway patched R7 movie (`35faa0e9…f827`).
The VM owns a fresh read-only baseline clone and writable qcow2 overlay, restricted
loopback management forwards and an unconnected private network. Its two rooms
and all device state were synthetic. The VM was stopped after testing. Only the disposable overlay was booted; no physical controller or real speaker
was accessed or changed. The Pi was used only to read the installed HA configuration, without changing files,
processes or services. No real HA service calls were sent. Production bridge and
firmware code and existing releases were left unchanged.

The scripts, movie, source receipts, complete packet capture, framebuffer images,
redacted HA responses and logs are retained. Entry points are `lab.py`, `nav.py`,
`measure.py`, `external_update.py`, `static/build_probe.py`, `probe/findings.md`
and `ha/ha-findings.md` inside that evidence directory. The first session used preserved `probe/smart_devices_peer-v1.py`; a second
session used `probe/smart_devices_peer-v3.py` to isolate container updates with
optimized 24-row generation. A3 and large-catalog timings were not rerun with
that optimization. These are feasibility probes, not a feature release.

## A1 — Music root and room-dependent visibility

The root builder is `Node_populateTopLevelChildren` (stock definition `0xb16a7`),
installed by `createRootNode`. The probe adds a root CD node after reading
`SystemProperties.getString("CR100_DevicesAvailable")` on the current selected
master. A value of `"1"` enables it; other/missing values hide it. This follows
the existing `Restored_roomCapabilities` / `CR100_RoomCapabilities` pattern.

Switching between **Devices Lab** and **Other Lab**, with different property
values, showed and hid Devices. The probe includes a hook in
`BrowsePanel_onZoneGroupsChanged` to repopulate the root when the selected master
changes. Static inspection found the stock root branch only refreshes for
certain dock/visibility changes, so merely adding a capability read to the root
builder is insufficient. HA disconnect/reconnect while remaining on an open root
was not tested; it needs a corresponding refresh trigger. Both tested rooms were
ungrouped; member-versus-coordinator area selection was not tested.

Evidence: [enabled root](../../../../research/bridge-runs/smart-devices-2026-11-14/devices-root.png),
[other-room root](../../../../research/bridge-runs/smart-devices-2026-11-14/a1-other-room-root.png),
[`static/root-actions.txt`](../../../../research/bridge-runs/smart-devices-2026-11-14/static/root-actions.txt),
[`probe/patch-receipt.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/probe/patch-receipt.json).

## A2 — Bridge-owned rows and custom OK

`createRootCDNode` browsed `SD:devices` on the synthetic player's ordinary
ContentDirectory. The firmware factory recognised `SD:item:NNNN`, decorated the
returned node as `object.item.smartDevice` with an `onOK` callback, and dispatched
it before ordinary play/enqueue handling. Two OK presses produced exactly two
`smart.test.toggle` UI events through the production `UIEventGateway`; neither
caused playback. Server metadata alone cannot install callbacks: the firmware
node factory and OK dispatch must recognise the new identity/class. The existing
Radio `settings.item` / `changeSetting` route is another source precedent.

Before the first explicit UI event, capture frames **0–2320** contain eight CD
reads, including four Devices reads, prefetch and traversal into a later page.
There are **zero `CR100_UIEvent` requests, zero toggle/brightness handlers and zero
playback writes** in that interval. The full capture also contains zero AVTransport
playback writes. An unrelated `CR100_CFWBuild` report is separately classified;
it is not a device action. Browse, node construction and prefetch stayed read-only.

Evidence: [`probe/run-analysis.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/probe/run-analysis.json),
[`probe/packet-summary-final/`](../../../../research/bridge-runs/smart-devices-2026-11-14/probe/packet-summary-final/),
[`runtime-events.jsonl`](../../../../research/bridge-runs/smart-devices-2026-11-14/runtime-events.jsonl),
[`vm/network-1794634063.pcap`](../../../../research/bridge-runs/smart-devices-2026-11-14/vm/network-1794634063.pcap).

## A3 — In-place state text and silent success

On the highlighted third item, **Desk Plug: On → Off → On** changed without
leaving Devices or moving its cursor/scroll position. The callback awaited the
existing verified UI-event result, invalidated `SD:devices` and called
`BrowseCacheMgr.forcePoll`. Changing only the temporary child's `dc:title` would
not suffice: paged list text comes from the native browse cache.

Measured key-delivery-to-visible-text bounds were **4.608–4.934 s** for Off and
**4.056–4.338 s** for On. These are two trials against a **24-row synthetic catalog**,
not hardware or real-HA latency predictions. Frame sampling brackets the last old
and first fully updated row and includes input/capture timing uncertainty.

Silent success is practical and was observed. `SD_sendSilent` temporarily uses
`BridgeUI_deferredOwner`, already supported by `BridgeUI_report`, to retain the
message instead of opening a popup. It still calls the original `BridgeUI_send`
and verifies the retained result; its error branch displays the message. Success
showed no dialog. The error branch was inspected, not runtime-tested.

Evidence: [`a3-toggle-off/`](../../../../research/bridge-runs/smart-devices-2026-11-14/a3-toggle-off/),
[`a3-toggle-on/`](../../../../research/bridge-runs/smart-devices-2026-11-14/a3-toggle-on/),
[`static/frame-timing-analysis.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/static/frame-timing-analysis.json),
`static/build_probe.py` and `runtime-events.jsonl`.

## A4 — External container updates

**Container-specific invalidation works.** In the isolated follow-up, the list
was open with Desk Plug highlighted, `SystemUpdateID` stayed **7**, and only
`ContainerUpdateIDs` advanced from **SD:devices,11 → SD:devices,12**. The controller
automatically re-browsed, changed **Desk Plug: On → Off**, and retained selection
index 2 and scroll position 0. No key press, UI action or explicit firmware
invalidation was involved. Both GENA and `GetSystemUpdateID` readback were configured
to keep the global ID fixed; captured notifications independently confirm it.

In the isolated capture, notification frame **711** triggered Browse **720** after
**0.0616 s**, with the complete 24-row response **750** after **0.8286 s**. The
`GetSystemUpdateID` override was source-checked but not exercised by the controller.

The optimized V3 24-row fixture took **1.635–2.001 s** from the selected-room
publication-log entry to fully updated framebuffer. Desk Plug and All Lights
changed in the same captured frame (old008 → new009). Bounds use pre/post
host/guest-uptime calibration and include publication/delivery; they are not exact
wire-send times. Packet absolute timestamps have an offset from host wall time
and were not directly subtracted from host key/frame times.

The original V1 test changed both update IDs. Its notification caused Browse
after **0.121 s**, complete response after **1.904 s**, and visible text after
**2.952–3.369 s** from publication. That establishes the normal update path, but
only the second run isolates the container ID. One early follow-up attempt ran
before the list opened and is excluded (`a4-container-only/`).

A separate shrink from 1,000 to 24 entries retained the selected row and changed
the scrollbar. Source restores **numeric index and scroll position**, not entity
identity: insertions, reordering and removal can change what that index denotes.
Those cases were not exercised.

Evidence: [`a4-container-only-open/`](../../../../research/bridge-runs/smart-devices-2026-11-14/a4-container-only-open/),
[`static/container-only-frame-timing-analysis.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/static/container-only-frame-timing-analysis.json),
[`static/container-only-notify-proof.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/static/container-only-notify-proof.json),
[`probe/packet-container-only/`](../../../../research/bridge-runs/smart-devices-2026-11-14/probe/packet-container-only/),
`vm/network-1794635482.pcap`, `runtime-peer-isolation.log`, `probe/peer-v3-source-receipt.json`;
original `a4-external-on/`, `a7-shrink-to24/` and `static/frame-timing-analysis.json`.

## A5 — Dynamic softkey

The middle key displayed **Brightness**, opened its custom callback, blanked on
Ceiling and reappeared on Desk Plug when the highlight moved. Ceiling was
*deliberately* the unavailable test row; this probe did not implement HA capability
classification. The node supplies local `softbuttonPolicies` and
`softbuttonScrollPolicies`, returning `{label, handler}` objects.

A source-level subtlety: the scroll updater compares handler identity, so changing
only the label while retaining the same handler can miss a redraw. Returning a
blank label with a null handler worked. Arbitrary label/handler bindings need
custom firmware and are not supplied by ContentDirectory metadata alone.

Evidence: [blank key](../../../../research/bridge-runs/smart-devices-2026-11-14/a5-ceiling-blank-real.png),
[Brightness key](../../../../research/bridge-runs/smart-devices-2026-11-14/a5-desk-brightness-real.png),
`static/patch-api-actions.txt` and `static/build_probe.py`.

## A6 — Centered slider and wheel traffic

The existing `createSettingsBar` / `CenteredSlider` accepted **Ceiling Brightness**,
endpoints **0 / 100**, initial value 50 and a plain ActionScript get/set proxy.
Five unit wheel packets spaced one second apart produced **five callbacks**,
values 51–55. Twenty unit packets delivered in one 60-byte FIFO burst produced
**20 callbacks**, values 56–75: no coalescing occurred in this test. Their first-to-last
span was **14.348 s**, and the last callback arrived **15.264 s** after injection
(mean intercallback spacing 0.755 s), because each callback synchronously sends
and reads a UI-event result.

A single large-distance event clamped to 100, and a negative event clamped to 0,
with one callback each. Extra outward detents at either bound caused none. Back
caused no setter call and returned to the same Desk Plug row/scroll position.
There were 27 brightness callbacks in total. These are emulated wheel-input
counts; physical fast-spin aggregation remains untested. Source calls once per
native ScrollWheel event using its accumulated distance.

Direct network writes on every callback create a visible backlog. Local thumb
updates plus coalesced absolute writes, or applying on release/close, need a
separate implementation/acceptance experiment. Existing delayed-call helpers are
a source lead, not a measured fix.

Evidence: [slider](../../../../research/bridge-runs/smart-devices-2026-11-14/a6-slider-open.png),
[Back result](../../../../research/bridge-runs/smart-devices-2026-11-14/a6-back-same-row.png),
`a6-slow5-wheel.json`, `a6-burst20-wheel.json`, bound-test receipts,
`probe/inject_wheel.py` and `probe/run-analysis.json`.

## A7 — Row layout and catalog size

The ordinary CD browser displayed **seven single-line title rows**. It did not
display the supplied creator, artist, album or artwork as secondary/right-aligned
text or arbitrary row images. Built-in per-row class decorations do exist: the
fixture's wire class produced a music-note icon. Source offers TRACK/NOPLAY/BLANK
decorations and container arrows; custom HA-domain icons or independent state
columns would require additional assets/mapping or renderer changes. Locally
changing the child class does not replace the cached wire-class decoration.

There is no universal character limit. The wide sample displayed **24 complete
W characters and a clipped 25th**, while **64 i characters plus `: Off` (69 total)**
fit fully. The wide row remained clipped after five seconds; no marquee or
ellipsis was observed. Font metrics confirm 11-pixel W and 3-pixel i advances.
The text field is about 275 pixels wide; the captured usable title span was
about 270 pixels.

A **1,000-entry container worked**, including selection of item 607 and return to
item 7. Native Browse requested **500 rows** initially, then 500 at index 601
(returning the remaining 399). Complete SOAP bodies were 271,751 and 216,894 bytes.
Those responses took 17.433 and 13.334 s; **16.494 and 12.346 s were spent generating
rows in the V1 Python fixture**, which repeatedly read its control file. Thus this
establishes paging and reachability, not an intrinsic native slowness threshold.
A native maximum/capacity ceiling is **not answered**: larger catalogs and an optimized 1,000-row run were not performed; the V3
follow-up used 24 rows only. Initial-open sampling ended before completion, so no
key-to-open latency is claimed. The source's separate eager-helper 10,000-row cap
is not evidence that the normal virtual-list path accepts that many efficiently.

Evidence: [`a7-page-607-loaded.png`](../../../../research/bridge-runs/smart-devices-2026-11-14/a7-page-607-loaded.png),
[`a7-wide-focused/033.png`](../../../../research/bridge-runs/smart-devices-2026-11-14/a7-wide-focused/033.png),
[`a7-long-titles.png`](../../../../research/bridge-runs/smart-devices-2026-11-14/a7-long-titles.png),
`static/row-width-analysis.json`, `static/text-layout.json` and `probe/run-analysis.json`.

## B1 — Registries, areas, floors and groups

Read-only access to real **HA 2026.10.2** succeeded: 4,817 entity records, 384
devices, 26 areas and five floors. The relevant full-list fields are:

| WebSocket command | Fields used |
| --- | --- |
| `config/entity_registry/list` | `entity_id`, `platform`, `unique_id`, `device_id`, `area_id`, `entity_category`, `hidden_by`, `disabled_by`, `name`, `original_name`, `icon`, `config_entry_id`. |
| `config/device_registry/list` | `id` (join from entity `device_id`), `area_id`, `parent_device_id`, `configuration_url`, `identifiers`, `disabled_by`; no entity category, hidden-by or direct floor field. |
| `config/area_registry/list` | `area_id`, `name`, `floor_id`, `aliases`, `icon`, `labels`, `picture`. |
| `config/floor_registry/list` | `floor_id`, `name`, `level`, `aliases`, `icon`. |

Effective area is non-null entity `area_id`, otherwise the device's area; a child
device without an area inherits its parent device's area. A missing assignment
stays unassigned. Floor comes from effective area → `floor_id`, not the entity
name. All sampled Sonos entities inherited device areas; live override assignments
were not changed/tested. Precedence is confirmed by the matching released
[entity helper](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/helpers/entity_registry.py#L2647)
and [device helper](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/helpers/device_registry.py#L4775).
`via_device_id` is not the child-area inheritance field.

`entity_category` is null/config/diagnostic. Source permits `hidden_by`
integration/user and `disabled_by` config_entry/device/hass/integration/user, or
null; state availability must be checked separately. Modern light Group helpers
use `light.*`, registry `platform="group"`, and state `attributes.entity_id` member
IDs. This HA had no such helpers, but **two Hue-native group lights** had platform
`hue` and member lists of four/five entities. Therefore platform=group alone misses
real groups, and universal member discovery cannot be promised. A group's own
area does not restrict its members' areas.
[Group source](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/group/light.py#L137).

Evidence: [`ha/registry-summary.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/ha/registry-summary.json),
[`ha/native-light-groups.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/ha/native-light-groups.json),
full `ha/config-*-list.json` responses, `ha/get_states.json` and
`ha/source-2026.10.2/manifest.json` (pinned commit `8d7fd8b68677c2585be36ec86043308f403875b8`).

## B2 — Area service targets and mixed toggle

All three services accept this target shape, with `turn_on` replaced by
`turn_off` or `toggle` as appropriate:

```json
{"id":1,"type":"call_service","domain":"light","service":"turn_on","target":{"area_id":"kitchen"},"service_data":{}}
```

`area_id` may be a string or list. The [WS handler](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/websocket_api/commands.py#L262)
forwards the target; the [resolver](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/helpers/target.py#L196)
expands areas; [LightEntity.async_toggle](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/light/__init__.py#L683)
inverts each entity's own state. Mixed on/off becomes off/on, rather than uniform
all-off or all-on. Group entities can have their own aggregate state semantics.
Area expansion excludes hidden/config/diagnostic entries, and execution also
filters unavailable entities.

No service call was sent to real HA. The repository FakeHA assumes media-player
entity targets, so a disposable subclass added area/light handling and exercised
the extracted release toggle method over loopback WS. Mixed toggle, turn_on and
turn_off behaved as above, leaving another area's light alone. This is a source-backed
simulation, not live device acceptance. All Lights should choose an absolute
aggregate desired state; group/member overlap and cross-area membership also
need an explicit aggregation policy.

Evidence: [`ha/fake-area-light-results.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/ha/fake-area-light-results.json),
`ha/fake_area_light_probe.py` and `ha/ha-findings.md`.

## B3 — Resolving HA media for native Sonos

A read-only resolution of Radio Browser station “Christmas FM Non-stop!” returned
`{"url":"https://xmas-node1.zip.wf/live.aac","mime_type":"audio/aac"}`. This is an
external broadcaster URL, not HA; it has no HA signature or expiry parameter and
requires no HA bearer token. [Radio Browser source](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/radio_browser/media_source.py#L64)
returns the resolved station URL. This supplies a plain URL candidate for native
playback, but codec/TLS/network compatibility and actual SetAVTransportURI playback
were **not tested**, in accordance with the no-speaker-write rule.

Other providers differ. Local media can resolve to relative
`/media/<source-dir>/<path>?authSig=<JWT>` URLs pointing at HA after making them
absolute. The [resolver](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/media_source/http.py#L85)
allows relative URLs; [URL processing](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/media_player/browse_media.py#L36)
signs protected HA paths, with a default **24-hour** lifetime. External URLs pass
through. Those local-file/signing details are from source, not a live local-file
resolve. Surprisingly, this version accepts an `expires` field but does not pass
it to URL processing. Radio resolution also records a provider analytics click:
resolve at explicit play time, not during browse/prefetch.

The extra HA Music folder therefore needs a resolver/URL adapter: resolve opaque
`media-source://` identities at play time, make HA paths absolute using a
speaker-reachable origin, retain signatures and account for source/MIME handling.
HA's own [Sonos implementation](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/sonos/media_player.py#L508)
already performs processing that bypassing HA service calls will no longer supply.

Evidence: [`ha/radio-resolve.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/ha/radio-resolve.json),
`ha/radio-root.json`, `ha/real-reads.json` and `ha/ha-findings.md`.

## B4 — Sonos configuration URL

All **14** sampled Sonos media-player device records had
`http://<local-IPv4>:1400/support/review` configuration URLs; registry identifiers
included the Sonos RINCON identity. The matching [Sonos entity source](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/sonos/entity.py#L104)
constructs that URL from `self.soco.ip_address`. The existing `ha_linein.py` already
uses the device-registry/identity join and literal port-1400 address parsing.

It is a useful discovery seed, not a reliable permanent/current address contract:
registry entries can be stale, absent or offline and addresses can change. No
listed speaker was contacted, so independent identity, reachability and DHCP
change behavior were not observed. Refresh and validate native identity and
coordinator routing before selecting the native backend. Preserve HA entity/area
identity separately from the playback coordinator, and retain HA connectivity and
registry/state subscriptions independently of `HABackend`'s current ownership and
close lifecycle.

Evidence: [`ha/registry-summary.json`](../../../../research/bridge-runs/smart-devices-2026-11-14/ha/registry-summary.json),
`ha/config-device_registry-list.json`, `ha/real-reads.json` and `ha/ha-findings.md`.

## Changes or constraints forced on the planned mechanism

- Keep the bridge-owned CD container and existing UI-event transport: neither
  needs replacement based on these experiments. Add explicit firmware node/OK
  dispatch and root refresh for room and HA-connection changes.
- Refresh native cached list data after actions/external changes. Define stable
  ordering and selection-by-identity behavior if insertions/reordering must keep
  the same entity highlighted; stock restoration preserves numeric position.
- Coalesce/debounce absolute brightness writes, or apply at release/close. The
  measured synchronous per-detent UI-event path creates a long burst backlog.
- Use aggregate absolute on/off for All Lights. Define group/member overlap and
  cross-area effects; scenes/scripts need activate/run semantics rather than a
  universal power toggle.
- Resolve room areas from HA entity/device identity, independently of native
  playback group coordination. Grouped-room area selection needs another test.
- Put state in the title for the existing row renderer. Separate right-aligned
  state or arbitrary domain icons would require further UI changes; budget title
  width so state stays visible. A native
  large-catalog capacity/performance ceiling remains unmeasured.
- Add play-time HA media resolution, absolute/signed URL handling and a supported
  source policy when mounting HA's browser. A resolved URL alone is not a Sonos
  playback guarantee.
- Treat `configuration_url` as an address seed with native identity validation;
  preserve a separate HA connection lifecycle when Sonos playback leaves
  `HABackend`. No physical playback or controller acceptance is established here.
