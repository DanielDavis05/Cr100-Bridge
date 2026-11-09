# Radio and global alarms

The bridge supplies the CR100's existing **Music > Radio** menu using the public
RadioTime/TuneIn directory. It does not add TuneIn as another Music-menu account.
No TuneIn login or borrowed partner identity is required.

## Listening and browsing

Radio includes station and show favorites, Stations/Shows/Hosts search, saved
Local Radio, Music, Talk, Sports, Location, Podcasts, and Radio Settings. Genre
folders are alphabetical; station discovery defaults to the provider's Most
Popular list. More Stations/Shows choices fetch additional pages only when
opened. Back preserves a browsing snapshot rather than reranking the screen.

ZIP selection resolves the ZIP to coordinates because the public API ignores
direct ZIP parameters. City browsing uses the provider's geographic tree.
Explicit saved locality takes precedence over IP-based detection.

Show pages expose available live listening choices, archived episodes, artwork,
and schedules. Search returns the public API's bounded matches. Hosts search
returns associated programs; it does not invent separate host profiles.
Episode progress can be saved and resumed on a seek-capable playback backend.

Live stations use a direct broadcast route that preserves the existing music
queue. Finite episodes use ordinary track/queue behavior. Group selection,
volume/mute, Stop/Play, and the sleep timer retain their normal roles. Live
Pause stops the source when real timeshift pause is unavailable; Play returns
to live audio. The bridge does not claim live seeking or recording.

Existing Spotify Autoplay configuration and provider instances are retained.
Spotify continuation works with Radio installed; selecting Radio manually or
from an alarm relinquishes the prior Spotify continuation. Selecting Spotify
again starts its normal autoplay behavior. Installing an update preserves the
existing bridge configuration instead of copying the example over it.

## Artwork and metadata

The bridge publishes station identity separately from the current song and
program. Current album covers are displayed with title, artist, and album
information, including the controller's existing enlarged artwork view.
Matching supplied track artwork wins; a cached MusicBrainz/Cover Art Archive
lookup can fill a missing cover when a recording/release match is unambiguous.
Episode/show artwork, station logos, and a neutral image provide explicit
fallbacks when an actual album cover cannot be identified.

Player/stream metadata is the primary live source. Supplementary TuneIn data
respects provider cache headers and TTLs. Old station/song responses cannot
replace the active session's metadata or artwork. Images pass through the
controller-reachable JPEG cache rather than requiring the old controller to
decode modern remote image formats or HTTPS connections.

## Radio management

The bridge web UI has **Radio** and **Alarms** entries. Radio manages location,
station order, language, quality, reconnect, history, episode resume, Podcasts
and By Language visibility, and supplementary cover lookup. Podcasts and album
artwork are enabled by default. Album artwork has no disable setting.

Favorites and custom stations can be added, renamed/reordered, and removed.
Custom station editing includes a bounded URL test that never starts room
playback. Export/import operates on versioned Radio data. Refresh and preference
reset preserve favorites/custom stations; cache clearing does not delete alarms.
Deleting a custom station referenced by an alarm requires handling that alarm
explicitly. Removing a directory favorite leaves its alarm reference valid.

Direct MP3/AAC streams are supported. Sonos also accepts compatible WMA. For a
renderer without WMA or HLS support, the FFmpeg proxy converts the selected
stream to continuous MP3. HLS containers are detected independently of TuneIn's
codec labels; MP3 carried in HLS (for example WTMC) uses the proxy for Sonos.
The installer includes FFmpeg. Conversion is started on demand,
uses opaque stream URLs, and has bounded concurrent processes and cleanup.

## Native controller UI adaptation

The firmware itself inserts favorite/search/local entries into Radio. The
native adapter accounts for those entries rather than returning duplicates.
A narrow, version-checked SWF adaptation groups station/show favorites into
**Favorites**, enables the original ZIP/city dialog in bridge households, and
adds explicit native setting-choice actions. Album artwork
and Now Playing use the original firmware's rendering widgets.

Create a separate adapted SWF from an original supported input:

```sh
python3 tools/patch_native_radio_ui.py \
  --input /path/to/original/MainUI.swf \
  --output /path/to/disposable/MainUI.swf
```

The tool accepts the examined original 4.3 and 4.3.2 hashes, verifies unrelated
functions/resources remain unchanged, and writes a receipt beside the output.
It never overwrites the original or installs firmware. This is not a signed
Sonos update. Controller installation and laboratory loader constraints are
documented in [CONTROLLER-DEPLOYMENT.md](../research/CONTROLLER-DEPLOYMENT.md).

## Global alarms

Alarms belong to the household, independently of Radio. The global store,
scheduler, native AlarmClock adapter, web API, and execution ownership are
shared. Radio is the first media provider. A later Spotify adapter supplies
source descriptions and fresh playback requests through the same provider
registry; it does not need another database or scheduler.

Alarm records store canonical provider/content references and optional account
references. OAuth tokens and temporary resolved stream URLs are excluded.
Definitions survive provider outages. Native/web operations edit the same
SQLite database; web Stop/Snooze requests enter a durable command queue consumed
by the player process.

The scheduler handles local time/IANA timezone, recurrence, a five-minute
missed-occurrence grace window, DST, duration, cancellation, and nine-minute
snooze. Spring-forward nonexistent local times are skipped; fall-back repeated
times fire once. A durable occurrence journal prevents repeat dispatch after
restart. Uncertain renderer acknowledgments are reconciled rather than replayed
blindly. A timer only stops playback still owned by its alarm, including when
the user manually selects the same source again.

Radio references resolve current streams at firing time. Source failure uses
the actual native Sonos buzzer or a locally served WAV on a compatible reachable
renderer. The public static `/alarm-chime.wav` asset contains no account data.
Configure `alarms.chime_url` if the renderer needs a different reachable URL.
An unavailable speaker cannot sound an alarm; the run status records failure.

Global web source selection is provider-driven. The original CR100 alarm picker
contains hardcoded service choices: adding a future provider to the registry
does not automatically add its native picker screen. Radio uses the original
saved-station picker; a future provider can use the web editor and a separately
verified native picker adaptation.

## Configuration and state

```json
{
  "radio": {
    "enabled": true,
    "port": 8402,
    "state_file": "radio-state.json",
    "stream_port": 8403
  },
  "alarms": {
    "enabled": true,
    "state_file": "alarms.sqlite3",
    "timezone": "America/New_York"
  }
}
```

Paths are relative to `bridge-config.json`. Radio state uses atomic replacement
and a cross-process file lock. Alarm state uses SQLite transactions. Retain
these state files during upgrades. Normal preference/favorite changes do not
restart the CR100 wireless link.

The native Radio endpoint is advertised on the controller-facing address.
`radio.stream_host` can override the renderer-reachable LAN address used for
converted audio. Its HTTP listener binds all interfaces by default; configure
`radio.stream_bind` and `radio.stream_port` for another binding. Do not use an
address reachable only from the controller's TAP network for real speaker audio.

Authenticated web operations are available at `/api/radio`, its operation routes,
and `/api/alarms`. The same existing bridge session and request-header rules
protect them. A second web process does not start another alarm scheduler.

## Verification

Development validation on November 3, 2026 passed the 793-test full suite,
followed by the final native adapter/firmware checks and three Radio HTTP
integration checks. Spotify Autoplay has 40 existing tests and five additional
Radio/alarm coexistence checks. An isolated portable copy passed 100 Radio and
alarm checks without importing source from the research workspace.

The test suites cover typed API errors inside HTTP 200 responses, lazy paging,
cache control, persistent state, concurrent processes, actual authenticated
HTTP management, direct Sonos/HA adapters, global alarm provider extension,
native SOAP favorites/alarms, artwork delivery, stale responses, and real
FFmpeg WMA-to-MP3 conversion when FFmpeg is available.

```sh
.venv/bin/python -m unittest discover -s tools -p 'test_radio*.py'
.venv/bin/python -m unittest discover -s tools -p 'test_alarm*.py'
.venv/bin/python -m unittest discover -s tools -p 'test_native_radio.py'
node --check tools/webui/app.js
```

Native visual validation uses an independent emulator disk/overlay, ports,
process files, logs, and private network identities. It does not modify or stop
the user's other emulator instance. See [TUNEIN-RADIO-PLAN.md](../research/TUNEIN-RADIO-PLAN.md)
for the full feature and acceptance checklist.
The native screenshots and test boundaries are recorded in
[RADIO-ACCEPTANCE.md](../research/RADIO-ACCEPTANCE.md).
