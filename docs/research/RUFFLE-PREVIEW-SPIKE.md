# Ruffle preview spike

Time-boxed research spike, 2026-11-15, about two hours. Question: can the CR100's own `MainUI.swf` run
in the browser under [Ruffle](https://ruffle.rs) well enough to show a live preview of a controller's
Spotify menu inside the bridge web UI?

**Short answer:** it runs, and with a shim it draws the real Music menu with an entry served from
JavaScript. A live Spotify preview is technically possible, but it means emulating a real part of
`hh.anacapad` in JavaScript and re-patching every firmware build's SWF. The spike also hit an
unresolved input failure one level before the Spotify list. **Recommendation: no-go for now;** build the
hand-drawn HTML preview (see the end).

Nothing was added to `tools/webui` or `dist/`, and no Sonos file was copied into the repository. The
test page, the throwaway tools and the screenshots live in a session scratch directory (see
[Reproducing](#reproducing)).

## Setup

- Ruffle **nightly-2026-11-07**, self-hosted web build (Ruffle publishes only dated nightlies), zip
  SHA-256 `427125b3…69ee`, matching GitHub's published digest. Version string
  `0.8.0-nightly.2026.11.7`, renderer `wgpu-webgl`.
- SWF: the stock **4.3.2** `MainUI.swf` (SHA-256 `25bfa76d…ab1e1`, one of the two inputs that
  `tools/patch_controller_settings_ui.py` accepts), taken from an extracted 4.3.2 rootfs outside this
  repository. It is FWS (uncompressed), SWF version 6, AVM1. The stage is **320×240 at 12 fps** (not
  240×320) with 20 root frames.
- Device fonts and `en-US.xml` string tables from the same rootfs (`/opt/fonts`, `/opt/resources`).
- A static `http.server` on 127.0.0.1 served the page to the desktop app's browser pane.

## What renders, step by step

| Stage | Result |
| --- | --- |
| 1. Stock SWF, no help | Loads without errors. Draws the chrome: background art, battery and Wi-Fi icons, scrollbar, softkey bar. **No text, no menu.** With no `RinconSystem` global, the boot code takes a built-in desktop path straight to the `ZoneMenu` frame. AVM1 calls on undefined objects are silent no-ops, so Ruffle logs **no** missing-native errors at all. The only warnings concern fonts. |
| 2. Fonts | 205 of the 244 text fields use **device fonts** (not embedded outlines): `standard 07_55/07_65/09_55/09_65` (Craig Kroeger's pixel fonts) and `Luxi Sans` (regular and bold). These map to the TTFs in `/opt/fonts/swffontmap.txt`. Ruffle's `fontSources` registers a font under its TTF **full name**, so `luxisr.ttf` became "Luxi Sans Regular" and missed. It has no flag for bold device-font variants either. Fix: copies whose name tables are rewritten to the exact SWF names, plus bold-flagged copies of `stan0965`/`stan0765`. After that there are no font warnings. |
| 3. Strings | All UI text comes from about 1,580 `STR_*`/`RHHSTR_*` globals that the native host injects from `en-US.xml`. Injecting them from the firmware's own XML via one prepended DoAction made the **"Rooms" title and "Settings" softkey render correctly** in the right fonts. |
| 4. Native proxies | A prepended DoAction installs objects for the five host globals. Each object's `__resolve` logs the access and forwards it. Ruffle honours `__resolve` for property reads **and** method calls. With `RinconSystem` defined, boot takes the real on-device path (household ID, `searchForZonePlayers`, then `ZoneMenu`). |
| 5. JS round trip | `flash.external.ExternalInterface` **does not exist for SWF 6 code** in Ruffle, as in Flash, where `flash.*` is SWF 8+. Workaround that worked: a tiny SWF 8 helper movie of our own as the root. It loads `MainUI` into `_level1` and puts a function on its own `_level0` timeline that calls `ExternalInterface.call`. SWF 6 code calls `_level0.Spike_host(...)` and gets JavaScript's return value **synchronously**. Ruffle does **not** share `_global` between movies of different SWF versions, and `_level0` is the only channel found. |
| 6. Input | Physical buttons arrive through a host-called AS function, `HardwareKey(name, "down"/"up")`. Names: `OK`, `Back`, `Music`, `Zones`, `SoftButton1–3`, `ScrollWheel`, `Up`, `Down`, transport and volume keys. The wheel distance comes from a host extension of the built-in object, `Key.getScrollWheelDist()`. Through `ExternalInterface.addCallback` on the helper, keys and wheel reach the UI: click sounds are requested and the wheel moves the browse selection. |
| 7. Music menu | Pressing Music does nothing without a selected room. Room data are native ZoneGroup objects with their own methods, not emulated here. Navigating to the `Browse` panel directly draws the **real CR100 Music root**: Music Library, **Spotify** (supplied by the shim's `Household.getMediaServerAt`), Line-In, Clock and Alarms, System Settings, plus the "View Clock"/"View Queue" softkeys and battery/Wi-Fi state from the shim. |
| 8. Spotify list | **Not reached.** The wheel moves the selection index (the UI asks for row 1's properties), but no highlight is drawn and OK on Spotify does nothing. The likely cause is missing host extensions to `TextField` (below) or unanswered calls such as `RinconSystem.FormatString`. Not diagnosed within the time box. |

Rendering itself is not the problem. Ruffle draws the vector art, bitmaps, pixel fonts and panel
transitions of this SWF faithfully as far as the spike went. Every gap was a missing host service.

## The native surface

`MainUI.swf` reads five globals that only the host defines. A static scan of the stock 4.3.2 SWF
(member names used on each global and never defined in the SWF) gives:

| Host object | Methods used | Properties used | Role |
| --- | --- | --- | --- |
| `RinconSystem` | 63 | ~10 (`IsDiagBuild`, `dealerLock`, `storesAlive`, `showContributing`, `OkMenuSelection`, …) | strings/formatting (`formatString`, `FormatString`, error strings), settings, tracing, version info, power, language |
| `Household` | 37 | ~18 (`currentGroupID`, `numMediaServers`, `totalZonePlayers`, `associatedZPUDN`, `AlarmClock`, `MusicServiceDiscovery`, …) | rooms/players/groups, media servers (music services appear here), listeners |
| `BrowseCacheMgr` | 20 | `lastUpnpResult` | the browse cache: counts, row properties, prime/poll, listeners |
| `RinconHWMon` | 14 | `batteryBars`, `wifiStrengthBars`, `isDocked` | battery, Wi-Fi, wheel parameters, click sound, standby |
| `UpnpDeviceDiscovery` | 2 | — | listeners |

On top of these:

- **Objects returned by the host have methods of their own.** ZonePlayer/ZoneGroup objects (UPnP
  services `AVTransport`, `RenderingControl`, `ContentDirectory`; `getCachedUPnPState2`,
  `createUpnpOp`, …) and the `AlarmClock` object. Media-server objects, by contrast, are plain data:
  `Title`, `UDN`, `hasMusic`, `isStubAccount`, `isDock`, `isPandora`, `isLastFM`,
  `canCacheTopLevelChildren`.
- **Extensions to Flash built-ins**, which a stock player lacks: `Key.getScrollWheelDist()`,
  `Key.setPowerScrollDist()`, and `TextField` methods `getTextAtLine()`, `getEllipsisIndex()`,
  `clearFirstLineForegroundColors()`.
- **Host → AS calls:** `HardwareKey(name, flag)` and events fired on registered listener objects
  (`Household.addListener`, `BrowseCacheMgr.addListener`/`addIndexListener`, `addZPListener`,
  `addMSListener`, battery and Wi-Fi listeners). The real host delivers browse results
  asynchronously through these.

### Calls observed on the boot → Music path

Logged live through the shim, in on-device mode, from boot to the Music root:

- **Boot:** `RinconSystem.getHouseholdId`, `restartedForLanguageChange`, `isTraceEnabled`,
  `setServiceErrorHandler`, `fileExists`, `getLocalIPAddr`, `FormatString`, `traceObject`,
  `addPowerManagementListener`; reads of `IsDiagBuild`, `dealerLock`, `storesAlive`.
  `Household.searchForZonePlayers`, `getNumZoneGroups`, `addListener`, `AlarmClock.addRunAlarmListener`;
  reads of `currentGroupID`. `RinconHWMon.setScrollWheelParams`, `addBatteryStateListener`,
  `addBatteryListener`, `addWifiStrengthListener`; reads of `batteryBars`, `wifiStrengthBars`.
- **Opening Browse (Music root):** `Household.lookupZoneGroup`, `getZonePlayerAt(0,"ANY")`,
  `getMediaServerAt`, `lookupMediaServer`, `getAssociatedZP`, `getCurrentMasterZP`, `addZPListener`,
  `addMSListener`, `removeListener`; reads of `totalZonePlayers`, `numMediaServers`, `associatedZPUDN`.
  `BrowseCacheMgr.getChildProperty`, `addListener`, `addIndexListener`.
  `RinconSystem` reads of `OkMenuSelection`, `showContributing`.
- **Inside a service (static reading of `Node_FetchCDNodeChildrenEx`, `createCDNode`,
  `createNodeForMediaServer`):**
  - `BrowseCacheMgr.getContainerTotalMatches(udn, parentId, doBrowse, first)` returns a count.
  - `getChildProperty(udn, parentId, index, prop)` returns one property; prop 0 = title, 1 = id,
    2 = `upnp:class`.
  - `getPropertyOfChildren(udn, parentId, first, count, prop, formatter)` returns
    `{labels: [...], labelCnt}`.
  - Also `lastUpnpResult`, then `primeCache`/`pollMS`/`invalidateContainer` with listener events for
    asynchronous fills.

The browse half is a clean data API, and these are exactly the shapes a JavaScript cache over the
bridge's SMAPI `getMetadata` (`tools/smapi_service.py`) could return. The host half (rooms, players,
transport) is the expensive part, and the Music menu will not open without a room.

## Effort estimate for a real preview

Assumes the preview shows browse menus only (no playback) for one firmware family, fed by the bridge's
own SMAPI service.

| Work | Estimate | Notes |
| --- | --- | --- |
| Extraction pipeline from the user's uploaded `.upd`: `MainUI.swf`, the six fonts and both `en-US.xml` files; font renaming; serving Ruffle | 2–3 days | `online_updates._mainui_from_cramfs` already reads `opt/swf/MainUI.swf` from the update's cramfs. It needs generalising to other paths. |
| Instrumenting the SWF at serve time (strings, proxies, input, navigation) plus the SWF 8 helper | 2–3 days | The spike's builder reuses the AVM1 helpers in `tools/patch_controller_settings_ui.py`, and the injection is a single prepended DoAction with no relocation. Production needs hash-gating per firmware build, like the existing patchers. |
| Shim core: proxies, a synchronous call bridge, a listener registry for JS → AS events | 2–3 days | Listener objects can't cross `ExternalInterface`. AS keeps a table and passes IDs; JS calls back through `addCallback`. |
| Minimal household model: one fake room/group with the methods the Music path touches | 3–5 days | Methods-bearing objects must be built on the AS side and backed by JS data. About 45 `Household` members, plus ZoneGroup/ZonePlayer methods. |
| `BrowseCacheMgr` over SMAPI `getMetadata`, including priming and asynchronous fill events | 2–4 days | The data shapes are known (above). |
| Host built-in extensions and the selection/OK failure | 1–5 days, uncertain | `TextField.getTextAtLine`/`getEllipsisIndex`/`clearFirstLineForegroundColors` need approximations. AVM1 `TextField` has no line API to build on. |
| Integration in `tools/webui`: CSP, MIME types, size | 1–2 days | See below. |

**Total: roughly 2–4 weeks, with the last line the main risk.** Maintenance comes on top: every CFW
build changes the SWF (the repo already ships patched variants), and the preview would have to track
each build the user uploads.

Web UI costs that apply whatever the route:

- **Size:** `ruffle_web_bg.wasm` is 14.2 MB (5.0 MB gzipped) plus a 680 KB loader. The Pi serves it
  once per browser cache.
- **CSP:** the current policy (`default-src 'self'`, no inline styles) would need at least
  `script-src 'self' 'wasm-unsafe-eval'` to compile WebAssembly. Whether Ruffle's shadow-DOM styling and
  audio worklet work under `style-src 'self'` without blob URLs is untested. `tools/webui.py` also needs
  a `.wasm` → `application/wasm` MIME entry.
- **Hidden tabs:** Ruffle ticks on `requestAnimationFrame` and only switches to background ticking on a
  `visibilitychange` event. A page that loads hidden draws nothing until it becomes visible. This cost
  the spike some time and is harmless for a real user, but it matters for any automated test.

## Licensing route

Shipping `MainUI.swf`, its fonts or its strings with the bridge is out: they are Sonos's code and
data. The pixel fonts are Craig Kroeger's (miniml.com), and Luxi Sans is Bigelow & Holmes's with a
license that forbids modified copies. The workable route is the one the firmware panel already
supports:

1. The user uploads their own `.upd`, which the firmware panel already accepts and verifies.
2. The bridge extracts `opt/swf/MainUI.swf`, `opt/fonts/*` and `opt/resources/{cr100,shared}/en-US.xml`
   from that image's cramfs at run time. These are kept only in the bridge's local state, never in
   `dist/` or the repository.
3. Instrumentation and font renaming happen locally, on the user's own copy, for preview only.

Ruffle itself is MIT/Apache-2.0 and could be vendored with its license files. The helper SWF and shim
would be our own code.

## Recommendation

**No-go for Ruffle now; build a hand-drawn HTML preview instead.**

| | Ruffle + real `MainUI.swf` | Hand-drawn HTML preview |
| --- | --- | --- |
| Fidelity | Pixel-exact chrome and fonts, as far as the shim reaches | Close: same 320×240 layout and pixel-font metrics, list rows from the bridge's own menu data |
| Shows real bridge output | Only once household and browse are emulated (weeks) | Directly: render `spotify_menu.menu_for_controller` / SMAPI `getMetadata` results |
| Works without the user's firmware | No: needs an uploaded `.upd` | Yes |
| Firmware-version coupling | High (patched per SWF build) | None |
| Web UI impact | 5 MB gzipped wasm, CSP loosened for WebAssembly | None beyond existing CSS/JS |
| Effort | ~2–4 weeks plus ongoing upkeep | ~1–3 days |

The Spotify page needs "what will this menu look like on the controller", and a faithful list of
titles in the controller's frame answers that. The Ruffle route's extra value is pixel-exact chrome,
which the spike showed Ruffle can deliver once strings and fonts are supplied (screenshots below). But
getting real menu content into it means reimplementing a slice of `hh.anacapad`.

If it is revisited, the cheapest worthwhile step is narrower than a live preview: an optional
"controller view" that renders the **Music root** (as the spike already does) from the user's uploaded
firmware. Leave descending into services to the HTML preview.

## Reproducing

All artefacts are in this session's scratch directory, which a reboot can clear:
`/private/tmp/claude-501/-Users-daniel-Documents-fw-projects-cr100-bridge--claude-worktrees-charming-jang-d2a75e/3d0acf2a-b5d3-4ee2-af0d-c02851cc2d0a/scratchpad/`.

- `site/`: the test page. `index.html`, `page.js` (the Ruffle config), `tap.js` (a console tap plus a
  timer-driven rAF for hidden panes) and `shim.js` (the JS native shim, a fake Spotify tree and the
  keyboard map). Also the built SWFs, symlinks to the rootfs SWF and fonts, and renamed font copies.
- `tools/`:
  - `swfscan.py`: tags, fonts and undefined globals.
  - `pathscan.py`: native names reachable from a function-name pattern.
  - `rootscan.py`: root-timeline actions.
  - `renamefont.py`: rewrites a TTF name table for Ruffle.
  - `build_instrumented.py`: strings, proxies, input and navigation injected into a stock SWF. It uses
    this repo's `tools/patch_controller_settings_ui.py` helpers.
  - `build_helper8.py`: the SWF 8 `ExternalInterface` helper.
- `shots/`: `01-stock.jpg` (chrome only), `02-strings.jpg` (title and softkey text),
  `03-device-mode-rooms.png` (on-device boot path) and `04-music-menu-spotify.png` (Music root with the
  shim's Spotify entry).

Run `serve.sh`, then open
`http://127.0.0.1:8931/index.html?swf=helper8-l1.swf`. In the page console, `spikePlayer.nav('Browse')`
opens Music, and `spikeWheel(±1)` and `spikeKey('OK')` drive input. `spikeCallTable()` lists the
native calls made so far.
