# Ruffle preview spike

Time-boxed research spike, 2026-11-15, about two hours. Question: can the CR100's own `MainUI.swf` run
in the browser under [Ruffle](https://ruffle.rs) well enough to show a live preview of a controller's
Spotify menu inside the bridge web UI?

**Short answer: yes.** On top of the existing POC's ActionScript host, a small Music layer makes the
stock UI browse a Spotify service: Music → Spotify → Playlists and Back again, with the controller's
own panels, fonts, highlight and transitions. The data is a hard-coded tree served through the same
`BrowseCacheMgr` calls `hh.anacapad` answers. What remains is engineering, not open questions:
feeding the tree from the bridge, a room model for the Music key, 4.3.2 support and web-UI
packaging. **Recommendation: a staged go.** First run a short spike on the data channel, getting the
bridge's own menu into `BrowseCacheMgr`. If that works, build the Ruffle preview. Keep a hand-drawn
HTML preview only as the fallback for users who haven't uploaded firmware (see the end).

## Prior work: the offline setup POC

An earlier proof of concept already runs the stock **4.3** `MainUI.swf` in Ruffle 0.7.1:
`/Users/daniel/Documents/fw/research/cr100-emulator/ruffle/`, outside this repository. It covers the
whole out-of-box flow (welcome, pairing, registration with the character picker, usage data, Setup
Complete), with a Playwright smoke test. It solved several problems this spike first approached
differently:

- **The host is written in ActionScript.** `bootstrap.as` (compiled into the SWF with JPEXS)
  defines `RinconSystem`, `RinconHWMon`, `Household` and `UpnpDeviceDiscovery`, listener buses,
  timer-driven asynchronous operations and registration ops. State lives in AS, so host semantics
  need no JavaScript round trip.
- **Sonos's TextField extensions** are emulated as AS properties and methods: `selectedLine`,
  `visibleLines`, `textLineHeight`, `getTextAtLine`. My scan missed the properties, and they were the
  cause of the missing selection highlight below.
- **Input** comes from an AS `Key` listener that maps browser keycodes to `HardwareKey(...)`.
- **Fonts** ship as a separate `cr100-fonts.swf` built from the firmware TTFs (`FontSource.java`)
  and passed to `fontSources`. This avoids the TTF renaming used below.
- **AS → JS** goes through `fscommand`, which is one-way. A Ruffle quirk is documented: a same-frame
  wizard transition runs synchronously, so one callback is adapted to return its next state.

Its scope ends at Setup Complete; the Rooms and music interface are outside it. The rows below
marked *(POC)* come from re-testing with its `bootstrap.as`.

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
- The *(POC)* re-test uses the POC's own inputs: the stock **4.3** SWF (`126c3e9f…a2ef`), its rootfs
  resources and its `cr100-fonts.swf`, still under Ruffle nightly-2026-11-07.
- A static `http.server` on 127.0.0.1 served the page to the desktop app's browser pane.

## What renders, step by step

| Stage | Result |
| --- | --- |
| 1. Stock SWF, no help | Loads without errors. Draws the chrome: background art, battery and Wi-Fi icons, scrollbar, softkey bar. **No text, no menu.** With no `RinconSystem` global, the boot code takes a built-in desktop path straight to the `ZoneMenu` frame. AVM1 calls on undefined objects are silent no-ops, so Ruffle logs **no** missing-native errors at all. The only warnings concern fonts. |
| 2. Fonts | 205 of the 244 text fields use **device fonts** (not embedded outlines): `standard 07_55/07_65/09_55/09_65` (Craig Kroeger's pixel fonts) and `Luxi Sans` (regular and bold). These map to the TTFs in `/opt/fonts/swffontmap.txt`. Ruffle's `fontSources` registers a font under its TTF **full name**, so `luxisr.ttf` became "Luxi Sans Regular" and missed. It has no flag for bold device-font variants either. Fix: copies whose name tables are rewritten to the exact SWF names, plus bold-flagged copies of `stan0965`/`stan0765`. After that there are no font warnings. The POC's font SWF (`cr100-fonts.swf`) is the cleaner route and also worked with this nightly. |
| 3. Strings | All UI text comes from about 1,580 `STR_*`/`RHHSTR_*` globals that the native host injects from `en-US.xml`. Injecting them from the firmware's own XML via one prepended DoAction made the **"Rooms" title and "Settings" softkey render correctly** in the right fonts. |
| 4. Native proxies | A prepended DoAction installs objects for the five host globals. Each object's `__resolve` logs the access and forwards it. Ruffle honours `__resolve` for property reads **and** method calls. With `RinconSystem` defined, boot takes the real on-device path (household ID, `searchForZonePlayers`, then `ZoneMenu`). |
| 5. JS round trip | `flash.external.ExternalInterface` **does not exist for SWF 6 code** in Ruffle, as in Flash, where `flash.*` is SWF 8+. Workaround that worked: a tiny SWF 8 helper movie of our own as the root. It loads `MainUI` into `_level1` and puts a function on its own `_level0` timeline that calls `ExternalInterface.call`. SWF 6 code calls `_level0.Spike_host(...)` and gets JavaScript's return value **synchronously**. Ruffle does **not** share `_global` between movies of different SWF versions, and `_level0` is the only channel found. The POC shows host logic is better kept in AS; a synchronous JS channel matters only for live bridge data. An untested alternative for that is AS `XML`/`LoadVars` requests to the bridge itself. |
| 6. Input | Physical buttons arrive through a host-called AS function, `HardwareKey(name, "down"/"up")`. Names: `OK`, `Back`, `Music`, `Zones`, `SoftButton1–3`, `ScrollWheel`, `Up`, `Down`, transport and volume keys. The wheel distance comes from a host extension of the built-in object, `Key.getScrollWheelDist()`. Through `ExternalInterface.addCallback` on the helper, keys and wheel reach the UI: click sounds are requested and the wheel moves the browse selection. |
| 7. Music menu | Pressing Music does nothing without a selected room. Room data are native ZoneGroup objects with their own methods, not emulated here. Navigating to the `Browse` panel directly draws the **real CR100 Music root**: Music Library, **Spotify** (supplied by the shim's `Household.getMediaServerAt`), Line-In, Clock and Alarms, System Settings, plus the "View Clock"/"View Queue" softkeys and battery/Wi-Fi state from the shim. |
| 8. Selection *(POC)* | With only the step-1–7 shim, the wheel moved the selection index but no highlight was drawn and OK did nothing. Rebuilt on the 4.3 SWF with the POC's `bootstrap.as` unchanged plus a small Music layer: a fake Spotify media server, a `BrowseCacheMgr` serving a hard-coded tree, and `__resolve` traps that report missing members. Now the **selection highlight draws and Down moves it onto Spotify.** |
| 9. Entering Spotify *(POC)* | OK first did nothing. Wrapping the browse functions in AS tracers showed why: `getSelectedChildNode` calls `getChildOfNode(this.browseStack.top(), line)`, and `browseStack` is created with `new BrowseStack()`, **a host class** the SWF never defines. Without it `top()` returned undefined, and the child lookup fell through to the native cache with an empty UDN. A 12-line AS `BrowseStack` (`push`/`pop`/`top`/`clear`/`length`) fixed it. OK then runs `primeThenPopulate` → `primeCache` → `onPrimed`, which my layer fires after 300 ms. |
| 10. Spotify list *(POC)* | The service panel opened with the "Spotify" header but empty rows. Service lists use **Sonos's virtual-list TextField.** The panel sets `List_txt.OnFetchData = TextField_doFetchData` and `List_txt.node = node`, then calls `List_txt.setVL(childCnt, scrollPos)`. The field is expected to call `OnFetchData(first, n)`, which asks `node.doFetchData` (= `Node_FetchCDNodeChildrenEx`) for a `{labels, labelCnt}` chunk and returns it through `this.setVLText(labels, first, count)`. `labels` is one string of `\n`-separated titles. Emulating `setVL` (fetch every row) and `setVLText` (place lines into `text`) in AS made **Playlists, Albums, Artists, Liked Songs** render. OK on Playlists shows its three playlists, and Back returns. |

Rendering itself is not the problem. Ruffle draws the vector art, bitmaps, pixel fonts and panel
transitions of this SWF faithfully as far as the spike went. Every gap was a missing host service.

Two input notes. Synthetic `KeyboardEvent`s dispatched from page script (on the player, its canvas,
its hidden input, `document` or `window`) did not reach this Ruffle nightly. A real click on the player
followed by a real key press did. The POC's `Key` listener route needs a focused player.

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
- **Extensions to Flash built-ins**, which a stock player lacks:
  - `Key.getScrollWheelDist()`, `Key.setPowerScrollDist()`.
  - `TextField` properties `selectedLine`, `visibleLines`, `textLineHeight`, plus the methods
    `getTextAtLine()`, `getEllipsisIndex()`, `clearFirstLineForegroundColors()`, `addListener()`,
    `removeListener()`, and the virtual list `setVL()`/`setVLText()` with the `OnFetchData` callback.
  - `String.prototype.compareNoCase()` and `displayLength`.

  The POC emulates most of these in AS; the Music layer adds the virtual list. A name-based static
  scan misses the properties, because the SWF only reads them.
- **Host classes constructed with `new`**, invisible to a scan of global reads:
  - `BrowseStack` (browse navigation) and `Scroller` (scrollbar thumb, driven as a TextField
    listener; cosmetic).
  - `CustomerRegistrationOp`, `PostUpdateMgr`, `RinconUpdateMgr` (the POC emulates these).
  - `ShareManager`, `HWDiag`, `ProvisionTrialAccountOp`, `ProvisionCredentialedTrialAccountOp`,
    `MigrateTrialAccountOp`, `CheckCredentialsOp`.
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
own SMAPI service, and starts from the POC.

| Work | Estimate | Notes |
| --- | --- | --- |
| Already done in the POC: AS host scaffolding, listener buses, async ops, TextField and String extensions, key input, string injection, font SWF, smoke test | — | Its `build.py` imports the patch helpers from `cr100-firmware`'s pre-move path. It also hard-codes the 4.3 rootfs. |
| Extraction pipeline from the user's uploaded `.upd`: `MainUI.swf`, the six fonts and both `en-US.xml` files; font SWF; serving Ruffle | 2–3 days | `online_updates._mainui_from_cramfs` already reads `opt/swf/MainUI.swf` from the update's cramfs and needs generalising to other paths. The font SWF needs a JDK plus JPEXS at build time, or a Python replacement. Compiling `bootstrap.as` can happen once at release time, since the injected tags don't depend on the user's SWF. |
| 4.3.2 and CFW builds | 1–2 days per family, then upkeep | The POC targets 4.3. 4.3.2 is the controller firmware the bridge supports. |
| Done in this spike's Music layer: media server, `BrowseCacheMgr` contract with priming, `BrowseStack`, virtual-list TextField | — | `poc/music.as`, about 100 lines of AS. |
| A minimal room model, so the Music key works without jumping straight to the Browse panel | 2–4 days | The Music key needs a current zone group: ZoneGroup/ZonePlayer objects with methods, built in AS. A preview could also keep opening the Browse panel directly and skip this. |
| `BrowseCacheMgr` over the bridge's own Spotify menu | 2–4 days | The data shapes and call order are now proven. The open part is the data channel: AS `XML`/`LoadVars` requests to the bridge, which can't be made synchronous, are untested. So is JS through the SWF 8 helper. `primeCache` → `onPrimed` is asynchronous, which suits a fetch on prime with synchronous reads from an AS cache afterwards. |
| Integration in `tools/webui`: CSP, MIME types, size | 1–2 days | See below. |

**Total: roughly 1.5–2.5 weeks, and no remaining step is a research question.** The data channel is
the main risk, and the next spike should take it first. Maintenance comes on top: every CFW build
changes the SWF (the repo already ships patched variants), and the preview would have to track each
build the user uploads.

Web UI costs that apply whatever the route:

- **Size:** `ruffle_web_bg.wasm` is 14.2 MB (5.0 MB gzipped) plus a 680 KB loader. The Pi serves it
  once per browser cache.
- **CSP:** the current policy (`default-src 'self'`, no inline styles) would need at least
  `script-src 'self' 'wasm-unsafe-eval'` to compile WebAssembly. Ruffle's shadow root contains inline
  `<style>` elements (observed), which `style-src 'self'` would block. Whether the audio worklet
  needs blob URLs is untested. `tools/webui.py` also needs
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
3. Injection of the strings and host layer, and packaging of the font SWF, happen locally on the
   user's own copy, for preview only.

Ruffle itself is MIT/Apache-2.0 and could be vendored with its license files. The host layer
(`bootstrap.as`, the Music layer, any helper movie) is our own code and can ship precompiled.

## Recommendation

**A staged go for the Ruffle preview, starting from the POC.**

| | Ruffle + real `MainUI.swf` | Hand-drawn HTML preview |
| --- | --- | --- |
| Fidelity | The controller's own panels, fonts, highlight, icons and transitions | Close: same 320×240 layout and pixel-font metrics |
| Shows real bridge output | Once the data channel feeds `BrowseCacheMgr` | Directly: render `spotify_menu.menu_for_controller` / SMAPI `getMetadata` results |
| Behaves like the controller | Yes: real navigation, Back, softkeys | Only as far as it is re-created |
| Works without the user's firmware | No: needs an uploaded `.upd` | Yes |
| Firmware-version coupling | Medium: the host layer targets host APIs, and only the strings/hooks injection touches the SWF | None |
| Web UI impact | 5 MB gzipped wasm, CSP loosened for WebAssembly and Ruffle's styles | None beyond existing CSS/JS |
| Effort | ~1.5–2.5 weeks from the POC, plus upkeep | ~1–3 days |

Before this follow-up, the case against Ruffle was that getting real menu content into it meant
reimplementing an unknown slice of `hh.anacapad`. That slice is now known and small for browsing: a
media server, five `BrowseCacheMgr` calls, a stack class and a virtual-list text field. Real
behaviour is what makes a live preview worth more than a picture.

The steps:

1. **Data channel spike (1–2 days):** serve the bridge's Spotify menu to `music.as`'s
   `primeCache`, using AS `XML`/`LoadVars` from the bridge's own origin. If that can't work, use JS
   through the helper movie.
2. If it works, port the POC host and Music layer to 4.3.2 and wire the extraction from the uploaded
   `.upd` and the web UI integration.
3. Keep a minimal HTML list as the fallback when no firmware has been uploaded.

Stop at step 1 if neither channel works without contortions. In that case, build the HTML preview
instead.

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
- `poc/`: the re-test on top of the POC. `music.as` is the Music layer and `hooks-music.as` opens the
  Browse panel. `build_music.py` compiles the POC's `bootstrap.as` + `music.as` with the POC's JPEXS
  copy (`/Users/daniel/Documents/fw/scratch/ruffle-oobe-tools/ffdec`) into
  `site/MainUI-poc-music.swf`. Open `site/poc.html`, click the player, then use the arrow keys and
  Enter.
  `hooks-music.as` also wraps the browse functions in `fscommand` tracers, which is how the
  `BrowseStack` and `setVL` gaps were found.
- `shots/`: `01-stock.jpg` (chrome only), `02-strings.jpg` (title and softkey text),
  `03-device-mode-rooms.png` (on-device boot path), `04-music-menu-spotify.png` (Music root with the
  shim's Spotify entry), `05-poc-selection-spotify.png` (highlight on Spotify),
  `06-poc-spotify-list.png` (the service list) and `07-poc-playlists.png` (second level).

Run `serve.sh`, then open
`http://127.0.0.1:8931/index.html?swf=helper8-l1.swf`. In the page console, `spikePlayer.nav('Browse')`
opens Music, and `spikeWheel(±1)` and `spikeKey('OK')` drive input. `spikeCallTable()` lists the
native calls made so far.
