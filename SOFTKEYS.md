# Custom music services and the stock CR100 softkeys

Isolated runtime investigation, November 3, 2026.

A custom SMAPI service can influence which built-in softkeys appear. In Now
Playing it can also change the library key's caption through the service name.
The examined stock UI does not expose arbitrary three-button labels and callback
bindings through SMAPI metadata or a presentation map.

## New-instance tests

The tests used an independent baseline copy and writable qcow2 overlay, the
original 4.3 executable and MainUI.swf, and synthetic service/player fixtures.
No other emulator disk, physical controller, Pi, account or speaker was modified.

| Context | Observed bottom row / action |
|---|---|
| Custom service root, Search highlighted | blank / blank / View Queue |
| Playable playlist or track highlighted | blank / Add to Queue / View Queue |
| Now Playing, library-capable track | blank / Softkey Lab / View Queue |
| Service name changed to Bridge Beta | Middle caption became Bridge Beta |
| Press middle library key | Stock add/remove/crossfade menu, with the service name inserted |
| Press right Now Playing key | Opened the stock queue screen |
| Program-radio source with working ratings recipe | Ratings / Softkey Ratings / View Queue |
| Press Ratings | Service-defined Turn on / Turn off choices |
| Choose Turn on | SMAPI `rateItem(id="bt:radio", rating="1")`, then custom result popup |
| Custom search category Enter code | Custom heading; Shift / Cancel / Search controls |
| Type a, press Search | Actual SMAPI `search(id="sk:input", term="a", ...)` received |

![Custom service with a playable selection](runs/softkeys-2026-11-03/browse-playable-selected.png)

![Middle key named through the advertised service name](runs/softkeys-2026-11-03/fresh-nowplaying.png)

![Custom search heading and stock keyboard controls](runs/softkeys-2026-11-03/keyboard-with-input.png)

## Firmware explanation

`createNodeForMediaServer` selects `setStandardMusicSoftbuttonPolicies` for an
ordinary custom service. Those policies are Flash functions. `SBS_addToQueue`
uses `STR_AddToQueue` and `BrowsePanel_AddToQueue`; prefix scrolling uses another
fixed label/handler. `createCDNode` builds a new local object using specific
native browse-cache properties, rather than copying a remotely supplied
softkey/callback object into the UI.

`NowPlayingPanel_intEnableButtons` sets the right key to `STR_ViewQueue` /
`NowPlayingPanel_viewQueue`. Its library key uses
`RinconSystem.stringForLibraryOpsKey(CurrentTrackURI)` and the stock
`NowPlayingPanel_loadLibraryMgmt` handler. Native 4.3 wrapper `0x46df60` calls
`0x4270e0`, which extracts the URI's service ID and returns the matching service
name. Thus the middle caption cannot be renamed independently of the service,
and changing that name does not change the handler.

The ratings branch uses a fixed bottom caption, `STR_PandoraRatingsKey`, with
`sonosApiRatings`. Presentation strings supply choices inside its menu. The
previous [working ratings experiment](runs/interactive-emulator-2026-11-01/README.md)
was repeated successfully in this new instance, using a new service ID 244 and
the name Softkey Ratings. Both strings and presentation documents were fetched.
The working hierarchy includes `PresentationMap > Match > Ratings`; both reported
URIs were `x-sonosapi-radio:bt:radio?sid=244&flags=0`, with matching account metadata
and consistent simulated player state. `working_recipe_probe.py` and
`radio-command.json` preserve the exact new-instance fixture and command.

The result popup displayed **Bridge received your choice.**, selected by the
service's `messageStringId="DONE"`. Its stock timeout is four seconds. The
earlier independently written fixture did not expose Ratings; its missing Match
wrapper and inconsistent source/track state make those failed attempts unsuitable
as evidence that stock ratings are unavailable.

![Stock Ratings softkey with service-defined choices](runs/softkeys-2026-11-03/ratings-menu.png)

![Service-defined rating result](runs/softkeys-2026-11-03/rating-result.png)

All ten examined label setters, custom-service node builders and softkey policy
functions match 4.3.2 after normalizing function-relative branch targets. The new
runtime test itself used 4.3. See `version-comparison.json` below.

## Evidence and instance

All files are in [runs/softkeys-2026-11-03](runs/softkeys-2026-11-03/): screenshots,
synthetic packet capture, fixture versions, requests, original runtime SWF,
native disassembly and `version-comparison.json`. Read-only hardware-breakpoint
inspection confirmed a program-radio source returned `SONOS_PROGRADIO`; the
debugger was detached and its breakpoints removed afterwards.

Verified runtime hashes:

```text
MainUI.swf  126c3e9fa13169a3749c835c0ce3c485fb47e34ac7404f63d770943deef6a2ef
hh.anacapad ab24be0fa5b0ecf6369842c30b4f51f10dbf17f91921e608c7d3401bfe263321
```

Viewer: [http://127.0.0.1:8889/](http://127.0.0.1:8889/). Its own control/setup
ports are 22741/22742. The manifest records the template identities retained
inside its unconnected private QEMU hub and the new per-run setup token.

```sh
python3 tools/radio_emulator_lab.py runs/softkeys-2026-11-03/vm capture /tmp/softkeys-screen.png
python3 tools/radio_emulator_lab.py runs/softkeys-2026-11-03/vm key music
```
