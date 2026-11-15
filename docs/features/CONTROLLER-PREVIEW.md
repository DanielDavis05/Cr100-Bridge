# Controller preview on the Spotify page

Spotify → Controller menus shows the chosen controller's Spotify menu in the controller's own
interface: its `MainUI.swf`, run in the browser by [Ruffle](https://ruffle.rs) and browsing the
bridge's running Spotify service as that controller. It shows the saved menu, its renamed and
reordered entries, shortcuts, playlists and tracks, and a locked entry's administrator message in
the controller's own popup. Background: [Ruffle preview spike](../research/RUFFLE-PREVIEW-SPIKE.md).

Without uploaded firmware the page keeps its drawing of the controller screen, which also shows
unsaved edits. The drawing's caption says why the real interface is not shown.

## Using it

1. Upload controller firmware **4.3.2** (`25.2-50130`) on any controller's Firmware panel under
   Controllers. Staging it is enough; nothing is installed.
2. Open Spotify → Controller menus and choose a controller. The preview opens on the Music menu.
3. Click the screen, then use <kbd>↑</kbd> <kbd>↓</kbd>, <kbd>Enter</kbd> (or <kbd>→</kbd>) to
   open, <kbd>Esc</kbd> (or <kbd>←</kbd>, <kbd>Backspace</kbd>) to go back, <kbd>1</kbd>–<kbd>3</kbd>
   for the soft keys.
4. After **Save this menu**, go back to the Music menu and open Spotify again. The players reload
   after a save, so the first try can show "restarting" for a few seconds.

Restart preview reloads the interface. Only browsing works: there is no room, playback, queue or
keyboard, and Search opens its categories rather than the controller's keyboard.

## How it works

**No Sonos file ships with the bridge.** `tools/controller_preview.py` reads, from the root
filesystem of a stored upload (`controller-updates/<sha256>.upd`, newest first, the chosen
controller's own upload before others):

- `opt/swf/MainUI.swf`, used only if its SHA-256 is in `SUPPORTED` (today the stock 4.3.2 build
  `25bfa76d…ab1e1`);
- the six device fonts in `opt/fonts`;
- `opt/resources/shared/en-US.xml` and `opt/resources/cr100/en-US.xml`.

`online_updates.CramFS` is the bounded exact-path reader that also finds a package's build
literal. The build goes to `controller-preview/<key>/` next to the config, with an `index.json`
that remembers each upload's result, so each upload is read once. Previews whose upload is gone
are deleted. The key changes with the MainUI hash and the host layer.

The build is injection only, as in the earlier offline-setup proof of concept:

- One DoAction sets the ~1,570 `STR_*` strings as `_global`s, as the native host does.
- One DoAction holds the **host layer**: `controller_preview_host/bootstrap.as` (the native
  objects, Sonos's TextField and String extensions, key input) and `music.as` (a household with
  one music service, `BrowseCacheMgr` fed by SMAPI `getMetadata` over AVM1 `XML.sendAndLoad`, a
  `BrowseStack`, the virtual-list TextField, provider faults to the stock popup).
- `hooks.as` opens the Music (Browse) panel whenever the stock UI shows Rooms, since there is no
  room model.

Both go in front of the first original DoAction, the hook before the first frame ends. Every
original tag must survive byte-for-byte or the build fails. Fonts get name tables matching the
SWF's device-font names (`standard 07_55` … `Luxi Sans`), with bold-flagged copies.

The host layer is compiled once by a developer, not on the bridge:

```bash
python3 tools/controller_preview_host/build_host.py --ffdec /path/to/ffdec.jar
```

It needs a JDK and JPEXS and writes `host-actions.json`. A test fails when the `.as` sources and
the compiled file disagree.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /api/spotify/preview/<mac>` | Whether a preview exists, the movie and font URLs, and the SMAPI URL for that controller |
| `GET /api/spotify/preview-files/<key>/MainUI.swf`, `…/fonts/<name>.ttf` | The built files; signed in only |
| `POST /api/spotify/preview/<mac>/smapi?t=<token>` | One SMAPI call forwarded to the running Spotify service |

The SMAPI route:

- forwards only `getMetadata`, `getMediaMetadata`, `getExtendedMetadata` and `search`;
- names the controller explicitly: the bridge sends `X-Bridge-Preview-Controller: <mac>`, which
  `SmapiService` accepts only from the bridge's own address and only for those calls. The service
  then uses that controller's menu, gives the preview an identity of its own, and sends no lock
  notice to the real controller;
- returns SOAP faults as HTTP 200, because `XML.sendAndLoad` drops the body of any other status;
- is protected against cross-site requests by a token bound to the signed-in session, since
  Ruffle's `XML.addRequestHeader` does not send `X-Bridge`.

The player process publishes the service address as `spotify_smapi` in `players-status.json`.

## Ruffle and the page

Ruffle is vendored in `tools/webui/vendor/ruffle` (pinned build, hashes and update steps in
`VENDORED.md`) and loads only when a preview is shown. Its `.wasm` is 14 MB, served as
`application/wasm` with a day's cache. The page's CSP adds `'wasm-unsafe-eval'` to `script-src`
and the hashes of Ruffle's two fixed shadow-root `<style>` blocks to `style-src`; nothing else is
loosened. `allowScriptAccess` is off.

Ruffle ticks on `requestAnimationFrame`, so a preview in a hidden tab draws nothing until the tab
is shown.

## Limits and follow-ups

- One firmware family: a new MainUI build needs its host layer checked and its hash added.
- Lists fetch up to 1,000 rows, 100 per request, and render at once.
- Unsaved edits show only in the drawing; the live preview shows what was saved.
