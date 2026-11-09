# Native CR100 Radio acceptance evidence

The bridge serves TuneIn/RadioTime through the controller's existing **Music > Radio** provider (`Household.radioUDN`, native service 254). It does not add a second music service. The original 4.3 firmware UI ran in a separate SH4 emulator; these images are captures of its actual 320×240 framebuffer.

## Accepted views

- [Music menu](docs/radio-validation/music-menu.png): the existing Radio entry.
- [Radio menu](docs/radio-validation/radio-root.png): Favorites, Search, Local Radio, Music, Talk, Sports, Location; Podcasts and Radio Settings continue below the visible rows.
- [Radio Settings](docs/radio-validation/radio-settings.png): native location, station information, ordering, language, stream quality and reconnect controls.
- [Now Playing](docs/radio-validation/now-playing.png): station, current programme, matching artist/title and album cover.
- [Enlarged artwork](docs/radio-validation/enlarged-artwork.png): the original native artwork view and matching caption.

Native interaction checks also confirmed:

- [Search](docs/radio-validation/search-types.png) preserves Stations, Shows and Hosts.
- [Favorites](docs/radio-validation/favorites.png) separates Radio Stations and Radio Shows; recent and continue-listening extensions appear below them.
- [City picker](docs/radio-validation/city-picker.png) reuses the original popup. Selecting Boston sent the actual `R_RadioLocation` write and durably saved region `r102`; [Local Radio (Boston)](docs/radio-validation/local-radio-boston.png) reflects that selection.
- An explicit Stream Quality choice sent `SystemProperties.SetString` with the dedicated action token, changed the durable store from `auto` to `low`, survived a peer restart, changed to `best`, and returned to `low`. The final explicit write invalidated the exact native Settings root; Back immediately displayed `Stream Quality: low`. The [settings screen](docs/radio-validation/radio-settings.png) shows the refreshed value. Favorites remained empty throughout these settings writes.

The normal and enlarged views use the production `RadioPlayback.play`, `refresh` and `values` paths and the production native protocol adapter. The guest serves the resulting real JPEG through the bridge artwork route. Metadata XML was not authored separately to imitate the result.

## Fixture boundary

The native UI and bridge code are real. The acceptance guest's directory responses and room backend are deterministic fixtures (`tools/radio_native_guest.py`): Radio Lab Jazz / Evening Jazz, one show and episode, and a short location hierarchy. No physical speaker or external audio stream was used, so the screenshots do not establish audible reception or upstream station availability.

The artwork and recording identity are public source data: Miles Davis, **So What**, **Kind of Blue**, MusicBrainz recording `b9c6bcfa-89eb-4b01-83b2-49327ee2e896`. The approved front image comes from [Cover Art Archive](https://coverartarchive.org/release-group/8e8a594f-2175-38c7-a871-abb68ec363e7). The production ArtStore resized the matching image for the controller; the minimal guest cache was prewarmed with those output bytes. Public API client/catalog tests and runtime HTTP tests cover the upstream parsing, resolution and artwork fetch paths separately.

## Firmware adaptation

`tools/patch_native_radio_ui.py` accepts only the known original 4.3 and 4.3.2 SWF hashes and writes a separate output. It modifies the Radio root, explicit Radio settings row handling, and the original ZIP/city selector gate. Other functions and resources are compared before and after and must remain unchanged. Only the copied emulator SWF was installed for this test. No physical controller or existing emulator was modified. This helper is not a signed controller firmware updater.

Settings are changed only after an explicit native OK action using the original player's `SystemProperties.setString` binding and the bridge's dedicated `CR100_RadioAction` variable. Directory browsing remains read-only; these action tokens neither play audio nor create favorites.

## Reproduction and retained evidence

The lab helpers require the full research workspace, its stopped emulator template, original firmware, SH4 kernel, QEMU and native screenshot tooling. They are not Raspberry Pi runtime dependencies. Original firmware is not included in the portable bridge archive. The firmware patch tests skip with an explicit fixture reason when those research inputs are absent.

The retained private run is `runs/tunein-native-20261104-0100/` in the research workspace. It owns an immutable baseline copy, writable qcow2 overlay, private QEMU hub 97, loopback ports 22621/22622, unique MACs and synthetic household `Sonos_TUNEIN_RADIO_LAB`. The isolated VM was synced and stopped after acceptance; its guest bridge/controller processes ended with it. No separate lab viewer was left running. Its logs, disk and additional screenshots remain in that run for local investigation. No other emulator disk, socket or process was addressed or stopped. A final read-only inventory confirmed the separate Spotify controller-actions emulator and other viewers were still running; old process IDs from the initial inventory had changed. Authentication material and guest state are deliberately not copied into this document or `docs/radio-validation/`.

Run native adapter and guarded patch checks from `tools/`:

```sh
python3 -m unittest -v test_native_radio test_patch_native_radio_ui
```

The adapter checks real local HTTP/SOAP responses, persistent station/show favorites, native encoded IDs, ZIP/city location wires, explicit settings mutation, and stable playback URIs. Patch checks both supported original firmware fixtures and reject modified or already patched input.
