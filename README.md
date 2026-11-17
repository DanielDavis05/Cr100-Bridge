# Slop Notice

This is a long-running project of mine, but this version of the codebase was largely created using AI coding tools.

The original version required a ZP80 running outdated, exploitable firmware. This version runs on a Raspberry Pi 3B+ or 4 and includes a web UI for configuration.

I plan to go through the codebase and clean it up and add proper documentation, starting with this README. Until that work is complete, please refrain from using this version.

# CR100 Bridge

Use a Sonos CR100 controller with modern Sonos speakers, directly or through Home
Assistant. A Raspberry Pi presents a SonosNet link and virtual rooms to the CR100.
The speaker source, Sonos players on the home network or Home Assistant, supplies
the real speakers, playback controls, and music library.

This repository contains the runtime, virtual player implementation, protocol
fixtures, web interface, installer, and systemd units. An emulator checkout,
Sonos firmware image, and the author's configuration are not needed.

The [consolidated findings, evidence index and open items](docs/research/RESEARCH.md) distinguish
deployed features, stock-controller tests, patched-UI and synthetic-NAND results,
and physical validation gaps. Custom controller firmware uses **System Settings
> Room Settings**. The older **Music > Speaker Settings** workaround is disabled
by default and remains an explicit compatibility option for stock firmware.

## Guides

- [Deploying and updating](docs/guides/deploying.md): build an archive, install it on the Pi, roll back
- [Health checks](docs/guides/health-checks.md): confirm the Pi, radio, controller link and speakers are working
- [Troubleshooting](docs/guides/troubleshooting.md): known symptoms and their fixes
- [Home Assistant devices](docs/features/SMART-DEVICES.md): Music → Devices controls, area mapping and configuration (requires Devices-capable controller firmware)
- Feature notes are in [docs/features](docs/features/); research records are in [docs/research](docs/research/).

## Supported installation

- **Raspberry Pi 4 Model B or 3 Model B+, 64-bit Kali Linux, systemd, Python 3.11 or newer.**
- Ethernet or a separate network adapter for the home network and internet.
  The built-in `wlan0` is dedicated to the CR100; ordinary Wi-Fi on that radio
  cannot be the installation/SSH connection or upstream connection.
- Internet access to Kali's package repositories and PyPI; root/sudo access.
- Sonos speakers (S1 or S2) on the home network, or Home Assistant with the desired
  media players. Through Home Assistant, the Sonos queue and library need its Sonos
  integration.

Raspberry Pi OS, 32-bit Kali, and other Pi models are not supported by this installer.
The recorded working radio setup is Pi 4/Kali 2026.3, kernel
`6.12.34+rpt-rpi-v8`, `brcmfmac-nexmon-dkms 6.12.2`, and Nexmon firmware 7.45.206.
The Pi 3 Model B+ has the same BCM43455c0 radio and, on that Kali release, the same
kernel, driver and byte-identical firmware image, so it takes the same patch. Its
slower CPU and 1 GB of memory have not yet been through a controller soak.
Supported boards and the radio each uses are listed in
[`tools/radio_platform.py`](tools/radio_platform.py), which the installer reads.
Fresh installation on physical hardware still needs acceptance testing; offline
tests cannot validate a board's driver, packet injection, or CR100 pairing.

## Install from a local checkout

Copy or clone this repository onto the Pi, enter its directory, then run:

```sh
./install.sh --check
sudo ./install.sh
```

Preflight checks the OS, board, architecture, upstream connection, and subnet.
The installer installs the Kali Nexmon driver/firmware, matching kernel headers
when available, DHCP/network tools, libpcap for pairing, and a dedicated Python
virtual environment. It applies the reviewed packet ownership, probe delivery,
and hardware clock fixes to the exact
supported Nexmon package image, preserving the firmware symlinks and a private
original backup. Unknown firmware hashes are rejected. It does not perform a full
OS/kernel upgrade.

If matching headers are unavailable, update Kali's kernel and headers, reboot,
and rerun. If newly installed firmware is not loaded, the installer starts the
web UI and reports the radio failure. Reboot to load the new driver/firmware;
the enabled services will start automatically. It never reboots the Pi itself.
The radio service verifies ownership, probe NoAck support, the hardware clock,
beacon clock offsets, and hardware timestamp support in running firmware through
read-only GET 430/432/433/435/436 before injection.
A patched file on disk alone is insufficient;
reboot after the first firmware patch so the driver loads it.

Open the printed **`http://<pi-address>:8080`** URL from your home network:

1. Set the web interface password.
2. Under Speaker source, choose Sonos speakers directly or Home Assistant. For
   Home Assistant, add its URL and a long-lived access token.
3. Choose the speakers to show on the CR100 under Speakers.
4. Under Controllers, choose **Pair controller**, then put the CR100 into its
   add-to-household setup mode. Pairing waits up to three minutes and the bridge
   starts automatically after success. Use **Add controller** to pair another
   CR100 with the same household. Existing controllers and their settings are kept.

The web UI runs before pairing and stays available if the bridge/radio fails.
This is a headless setup: the installer prints the URL; it does not launch a
desktop browser. The web interface is intended for a trusted home network.

The isolated controller network uses `192.168.69.0/24`. An upstream network using
that subnet is rejected. DHCP is bound to `sonos0` and advertises the bridge at
`192.168.69.1` as DNS so update hostnames resolve locally. It advertises no default
gateway; the home network's DHCP/DNS configuration is untouched.

## GitHub and streamed installation

The repository is currently local. Once hosted, clone it and use the same
installer, or stream the script while supplying the repository URL:

```sh
# Replace OWNER/REPOSITORY and VERSION with the published repository and tag/commit.
curl -fsSL https://raw.githubusercontent.com/OWNER/REPOSITORY/VERSION/install.sh \
  | sudo bash -s -- --repo https://github.com/OWNER/REPOSITORY.git --ref VERSION
```

`--ref` accepts a branch, tag, or commit (default `main`). Use the same release
for the script URL and source reference. The script can also install an explicit
local source using `--source /path/to/cr100-bridge`. `--no-start` installs and
enables services without starting them in the current boot.

## Services and files

| Item | Purpose |
| --- | --- |
| `cr100-webui.service` | Settings on port 8080, independent of pairing/radio |
| `cr100-radio.service` | Verify Nexmon, create `mon0`, disable radio power saving |
| `cr100-bridge.service` | Supervise the continuous radio link, DHCP, virtual players |
| `cr100-pair.service` | Pair a CR100; pause the bridge and restart it afterward |
| `/opt/cr100-bridge/current` | Symlink to the installed application release and venv |
| `/var/lib/cr100-bridge/` | Configuration, pairing key/state, tokens, web password data |
| `/run/cr100-bridge/` | Leases, player PID, bounded radio status log |
| `/etc/NetworkManager/conf.d/99-cr100-bridge.conf` | Reserve only the bridge interfaces |

Services use root for raw radio sockets, TAP/address management, DHCP, and player
reload signalling. Configuration and credentials are created with mode 0600 in
a mode 0700 data directory. Logs go to journald; the web UI's radio log rotates
at 2 MiB with one backup. Packet capture is not enabled by the installed services.
Firmware originals and application receipts are retained privately under
`/var/lib/cr100-bridge/firmware-backups/`. The small patch manifest, source, bounded
read-only diagnostic, and installation details are in
[`firmware/nexmon-ownership`](firmware/nexmon-ownership/README.md); complete vendor
firmware images and compiler outputs are excluded from bridge archives.

The radio reserves CCMP packet numbers durably in
`/var/lib/cr100-bridge/bridge-state.radio-pn.json`. Keep this file with the pairing
state: its private high-water marks prevent nonce reuse and replay drops across
clock changes, restarts, and power loss. Corrupt or unwritable counters stop the
radio before it encrypts a frame. Unexpected radio/TAP worker failures also reach
the supervisor instead of leaving a live-looking bridge with a dead worker.
The firmware enables hardware timestamps on validated injected beacons; probe
responses retain their native hardware timestamps. The host schedules beacons
on the hardware clock's 100-TU grid across bridge restarts. Startup verifies
that the four per-BSS clock offsets are zero on this dedicated radio. Clock
queries run in one isolated helper with bounded parent waits; stale calibration
or a lost radio stops the link for supervised recovery. A replacement clock
reading must remain usable through the next refresh; a marginal reading cannot
prematurely expire an otherwise good calibration. Acquisition probes use
the native NoAck packet flag so an absent controller cannot consume unicast
retries intended for acknowledged traffic. Encrypted data keeps normal ACKs.
Radio startup also disables and verifies the firmware's minimum power consumption
(MPC) setting, separately from Linux Wi-Fi power saving. This is reapplied after
a firmware reload. Its fixed verification transaction and firmware diagnostics
run in isolated workers with inherited leases, so a timed-out driver request
cannot accumulate replacement workers across bridge restarts. Disabling MPC
improved a measured controller TCP handshake,
but searching later recurred with MPC still off; it does not establish that
controller discovery is working or resolve every radio-link failure.
Automatic reconnect is enabled by default and can stay enabled while a paired
controller is switched off. **Pause reconnect attempts** remains an optional
diagnostic control on each controller card. It applies without restarting the
bridge and pauses unsolicited acquisition probes only; incoming controller
traffic and replies remain enabled. Pairing and chosen components are kept.

Long-run physical radio validation is still in progress. Earlier versions with
probe NoAck and software-built beacon timestamps still lost beacons after roughly
14–16 minutes. Hardware beacon timestamping and the calibration expiry fix are
under observation; they are not yet a confirmed complete radio fix. Verify room
discovery and menus on the physical controller; association or
a green bridge status alone does not establish that they work. Controller
diagnostic logs retain a fixed number of loss entries, so compare their event
timestamps rather than their count when testing for new drops.

The same control is available over SSH with
`sudo cr100-bridge controller-search <controller-mac> pause`; use `resume`
in place of `pause` to enable automatic acquisition again.

```sh
sudo cr100-bridge status
sudo cr100-bridge logs
sudo cr100-bridge pair       # blocks until pairing succeeds or times out
sudo cr100-bridge restart
sudo cr100-bridge stop
```

A failed pairing attempt retains the previous pairing and restarts that bridge.
Additional controllers share the existing household, channel, and network key;
pairing briefly pauses the bridge for all controllers. Pairing a known controller
does not add a duplicate. Older single-controller state files remain supported.
No default network key, live controller state, HA token, or Spotify credential is shipped.

The radio tracks each controller's association, power saving, buffered traffic,
and link status independently. The Controllers page shows each controller's own
status and recent events. Multi-controller radio behavior is covered by offline
tests; simultaneous operation with physical CR100s still needs acceptance testing.
Under Controllers, use **Choose components** on a controller to select its speakers
and About entries. **Use household selection** follows every globally enabled
component, including newly added speakers. Turn it off to keep an explicit
selection; selecting nothing hides all components from that controller. Unavailable
saved choices are retained for when their speaker or network returns. Bonded About
entries, such as a Sub, also require one of their owning speakers to be visible.
Saving applies the selection through the player service; existing radio pairings
are retained. A CR100 may keep cached entries until its next discovery refresh.

The Speakers page controls the household's available speakers; a controller's
selection only narrows that list. The speaker source and available Music services
remain shared. Spotify menus, pinned folders, and entry locks also have separate
settings for each controller.
If none of a controller's selected speakers are reachable, **CR100 Bridge** appears
in Zones as a read-only diagnostic device. It remains reachable even with no rooms
configured, using a stable endpoint on the controller network. When a speaker
returns, the bridge goes back to its normal hidden About entry. The bridge's
About details show its build and home-network address. Explicit component choices
still apply: hiding the bridge, or choosing no components, also hides this fallback.
Playback and grouping still belong to the physical speakers: controlling a visible
speaker that is grouped can affect playback across its physical group.

Selections are stored under `controllers[mac].visible_components` in
`bridge-config.json`. Omit the field or use `null` to inherit the household, `[]`
to hide all, or a list of stable component keys for a custom selection. Backed
speakers use `sonos:<UUID>` or `ha:<entity>`, simulated rooms use `sim:<slot>`,
bonded hardware uses `sonos:<physical UUID>`, and the bridge uses `cr100-bridge`.
Renaming a room keeps its component identity and controller selections.

## Controller firmware and Online Updates

Under **Controllers**, upload a signed CR100 `.upd`, review its internal target
version, then select **Install**. The selected controller installs it through
**System Settings → Online Updates**, including genuine firmware downgrades.
The optional **Force update prompt** is available on known firmware 4.3 only;
stock 4.3.2 must use the manual path because its incompatibility screen hides
Online Updates. Cancel, expiry, and update-service restart release the temporary
compatibility requirement.

With no armed controller firmware, Online Updates runs the bridge's full native
update demonstration with custom confirmation text and progress. Bridge release
download and installation remain a stub. See [Online Updates](docs/features/ONLINE-UPDATES.md)
for staging, recovery, supported UI customization, and validation limits.

## Updates and existing manual installations

Rerun `sudo ./install.sh` from the newer checkout. Dependencies and imports are
checked in a new release directory before switching the application symlink.
Existing configuration, passwords, tokens, and pairing state are retained.
Previous releases remain under `/opt/cr100-bridge/releases/` for recovery.
Only one installer may run at a time; do not update during pairing.

For an existing manual setup, stop its launcher before starting these services.
The installer deliberately does not import configuration or secrets from a home directory.
To retain an existing pairing/configuration, install with `--no-start`, then copy
the intended files into `/var/lib/cr100-bridge/` as root. Update token paths in
`bridge-config.json` (relative paths resolve beside that file), apply mode 0600,
and start `cr100-webui.service` and `cr100-bridge.service`. Do not run the old
launcher and installed services together.

For Spotify, enable it under Spotify, supply your own Spotify app's Client ID,
register `http://127.0.0.1:8888/callback` as its redirect URI, then use Sign in.
Follow the UI's instruction to paste the redirected address back into the page.
Sonos still needs a Spotify account linked through the Sonos app for playback.

Under Spotify → Playback, **Autoplay similar music** is Off by default. Off keeps
the existing finite Sonos queue and playback APIs: selected music stops when the
queue finishes, assuming Repeat is Off. Enabling Autoplay lets the bridge fetch
related Spotify tracks and start them one at a time after your selection ends.
Recommendations stay outside the Sonos queue. Music explicitly added to that
queue takes priority after the current recommendation finishes; after queued
music from another source ends, playback stops rather than resuming Spotify.

Long sessions follow Spotify's recommendation pages and fetch ahead as the buffer
runs low. Autoplay filters tracks already played in the current listening run,
including Spotify tracks explicitly played from the queue. Explicit queued
duplicates remain playable. If the station runs out of unseen tracks, playback
stops instead of cycling through earlier recommendations. Starting a new
selection starts a new listening run.

Autoplay needs the separate **Connect Spotify playback** authorization, shared
with Spotify folders. Use the same Spotify account as the main connection. The
bridge uses that connection's streaming permission for native playback control;
it does not change Spotify's global account Autoplay preference. Direct, verified
Sonos playback is the tested route; other speaker backends are not covered by
the native Autoplay proof.

Changing only Autoplay saves without restarting the players. Turning it Off
during a bridge-controlled track lets that track finish and suppresses the next
recommendation. A session started independently in the Spotify app stays under
its existing control; a safe mode change for that session is deferred until the
next bridge-started selection. Track transitions can have a short gap.

Under Spotify → Controller menus, choose a paired controller to edit its own
menu: rename or reorder entries, pin folders, playlists, albums/singles or
tracks, and lock any entry. The controller fixes Search first with the name
Search; its lock remains configurable. Other entries can be renamed and reordered.
Save the menu to apply it. A locked entry remains
visible; opening it shows “This feature has been locked by your administrator”.
The lock covers that entry and everything reached through it. The same music
can still be reached through another unlocked entry. Lists already cached on
the controller can remain visible until its next refresh.

To mirror real Spotify folders, use Connect Spotify folders and sign in to the
same account as the main Spotify connection. This separate authorization is
shared with bridge-controlled Autoplay. After approving, Spotify redirects to
`http://127.0.0.1:8898/login`; an error page is expected. Copy the whole address
from the browser and paste it into the folder sign-in field, then finish sign-in.
Select folders from the nested tree to pin them. Folder contents follow Spotify;
editing the controller menu does not change your Spotify library.

Folders refresh automatically every 300 seconds by default, and Refresh folders
requests an update immediately. If Spotify is unavailable, the last complete
folder tree remains available. Spotify's public Web API does not expose folders,
so folder mirroring uses a read-only private client endpoint that Spotify may
change. Playlist, album and track browsing uses the existing Web API connection.

## Development and portable archive

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -q
(cd firmware/nexmon-ownership && ../../.venv/bin/python -m unittest discover -p 'test_*.py' -q)
(cd tools && ../.venv/bin/python -m unittest discover -s cr100_flash/tests -t . -p 'test_*.py' -q)
.venv/bin/python tools/test_link.py
python3 tools/distribution.py --archive /tmp/cr100-bridge.tar.gz
```

The archive uses an explicit file list and excludes local configuration, keys,
tokens, research captures, logs, and caches. Extract it and run `install.sh` on
the Pi. The same builder prepares the installed release. Tests use local fake
Home Assistant/Spotify services. An optional private radio capture regression
is skipped when its capture is absent; synthetic link tests still run. Optional
Nexmon build and flash-package fixtures are skipped with a reason when their
external workspace inputs are absent. Flash tooling stays outside the archive;
its tests run from the checkout.

### Playback backends

Each virtual room is played by a backend: Home Assistant (`tools/ha_backend.py`) or
Sonos players over their local UPnP (`tools/sonos_backend.py`, `sonos_content.py`,
`sonos_upnp.py`; tests against `fake_sonos.py`). The web interface's Speaker source
page switches between them (config `backend`); the other source's rooms are kept
under `stashed_sources` and return when it is chosen again.
`tools/backends.py` defines what a backend provides: a `Backend` (one connection,
with optional album art and Music menu content) and a `Room` per player, which
reports state and carries out plain actions (play, seek, join, ...). The shared
code turns those into the CR100's UPnP, so a backend never handles SOAP or DIDL
itself. `tools/content.py` holds the Music menu's paging and object registry.

To add a backend: subclass `Backend` and `Room` in a new module with a
`load(settings, config_dir, log)` function, register it in `backends.BACKENDS`
under its name and config section, and add the module to `distribution.py`.
Rooms then name it as `{"backend": "<name>", "id": "<room id>"}`; the existing
`{"entity": ...}` form still means Home Assistant. `tools/test_backends.py`
drives a household through an in-memory backend and is a template for tests.
The web interface knows the two sources by name (`webui.SOURCES`); a new backend
also needs its speaker listing there.

Rooms expose cached routing bindings and operation-specific capabilities in
`tools/room_capabilities.py` and `Room.operation_capabilities()`. These describe
implemented bridge commands, separately from the descriptive About identity.
Transport actions and play modes follow the current source and availability;
unsupported setters return UPnP error 501 before changing simulated player state.
The stock CR100 can still show settings for which no command route exists.

Home Assistant rooms use their entity registry platform, integration scope and
`supported_features`. Sonos queue and provider routes require a Sonos entity;
generic players use HA's media browser with its original media IDs and types,
without claiming a Sonos queue. Grouping requires compatible integration scopes
and support on both rooms. A cached About model or IP never enables native
commands. HA disconnects disable controls while retaining identity, and reconnects
refresh controls even when the entity state is unchanged.

See [package 1 implementation and pending live checks](docs/research/BACKEND-FEASIBILITY.md#package-1-implementation)
for the supported routes, assumptions and validation details.

`NativeSettingsRoom` shares native settings dispatch and fixed-output capability
gating between the HA and direct Sonos room implementations. Configuration
persistence, room selection and stable slot allocation live in
`tools/bridge_config.py`, shared by the player and web interface.

Selected Sonos rooms also share native sound/device settings through
`tools/sonos_settings.py`: bass, treble, Master loudness, independent LF/RF balance,
status LED, supported fixed output, input name/icon and speaker-owned sleep timers.
This works through either playback backend when a verified physical Sonos endpoint
is reachable. HA transport, normal volume and grouping remain HA-owned. Generic
HA rooms do not gain these native controls.

### Music service extensions

Music catalogues and playback backends are separate extension points.
`tools/smapi_service.py` owns the anonymous SMAPI transport, strict SOAP parsing,
service descriptors, request context and HTTP lifecycle. Spotify and Speaker
Settings implement their own catalogue/actions and safe error mapping on that
base; the diagnostic `SmapiStub` keeps its test catalogue separately.

To add a catalogue, implement the `MusicService` contract and register a factory
in `tools/music_service_registry.py`. Factories receive settings and a
`ServiceContext` with the config path, listener address, logger and room supplier.
Return an unstarted service; `MusicServices` combines discovery/accounts and
routes owned playback items. Add new runtime modules to `distribution.py`.
The contract is `descriptor()`, `media_servers()`, `owns(uri, metadata)`,
`start()` and `close()`. A playable provider also implements
`resolve_playback(uri, metadata)` returning `(provider_uri, media_type)`.
Services exposing `controller_resolver` receive the requester's `controller_ip`
in that resolver call; the separate `on_locked` notice hook is optional.

A provider can optionally expose a `playback_policy` and select it through
`playback_policy_for(uri, metadata)`. Policies may handle resolved `play_provider`
operations, observe successful `queue_added` operations, and receive validated
room/coordinator actions through `before_action` and `after_action`. Declining a
play operation retains the existing backend route. Policies own their worker
lifecycle through `start()`/`close()`; queue-encoding adapters remain pure.
Spotify Autoplay uses these hooks rather than adding Spotify-specific dispatch
to the player or the Sonos encoding registry.

Spotify retains precedence over an explicitly configured diagnostic stub. The
legacy Speaker Settings catalogue requires explicit opt-in. An optional
`music_services` list replaces that selection; listing a provider explicitly
enables it unless `enabled: false`. Each provider needs a distinct service ID
and listener port:

```json
"music_services": [
  {"provider": "spotify", "id": 240, "port": 8400, "autoplay": false}
]
```

Spotify setup and menu edits support both config formats. Each catalogue still
needs its own upstream API/authorization and setup integration.

For finite direct Sonos queue playback, register an adapter on
`SonosBackend.playback_adapters`. Its `prepare(uri, kind)` returns ordered
`QueueItem(uri, metadata)` candidates without contacting or changing a speaker.
The full plan is validated before a Play Now command clears the queue. Spotify
share-link encodings and regional fallback live in `tools/provider_playback.py`;
another service supplies its own native Sonos encoding. HA forwards provider
URIs/types to its integration. Advertising a catalogue alone does not make its
items playable on a speaker.

### Radio and global alarms

The original **Music > Radio** menu uses the public RadioTime/TuneIn directory,
with favorites, search, local/genre browsing, Podcasts, and Radio Settings.
Playback publishes current metadata and cached album artwork while live radio
preserves the music queue. The web UI manages Radio and household alarms; the
shared alarm scheduler supports additional source providers later.

To show the alarm clock with **Snooze** and **Stop Alarm** on a CR100, edit the
alarm on that controller, set **Advanced > Show Clock on this controller > Yes**,
then save. Repeat on each controller that should show the alarm.

Clock and Alarms uses native Sonos timezone rules for the controller, with current
daylight-saving transitions. Manual time persists as a household clock offset
shared by the controller and alarm scheduler. Enabling Internet time returns to
the Pi's synchronized system clock; neither setting changes the Pi's OS clock.
Clock edits preserve the remaining duration of sleep timers and running alarms.

Existing Spotify Autoplay settings and playback hooks are preserved. Radio
selection, including an alarm, releases the previous Spotify continuation;
selecting Spotify again retains its normal autoplay behavior. See [RADIO.md](docs/features/RADIO.md)
for configuration, the supported native menu adaptation, and verification.

### Room Settings and stock-controller compatibility

Custom firmware provides **System Settings > Room Settings** for native EQ,
balance, loudness, status light, supported Line-Out mode and Line-In name/level
controls. Sleep Timer remains under **Music > Clock and Alarms**. These controls
use the shared native settings backend and do not need a music-service menu.

The older **Music > Speaker Settings** service is disabled by default. For an
unmodified controller, explicitly enable its compatibility catalogue:

```json
"speaker_settings": {"enabled": true}
```

Alternatively, add `{"provider": "speaker_settings"}` to an explicit
`music_services` list. Its default port is **8401** and service ID is **244**;
optional `host`, `port`, `id` and `name` overrides remain supported.

On stock firmware, set **Controller Settings > OK Button > Show Menu**, then
open **Music > Speaker Settings > room > setting**, select a value and choose
**Add to My Speaker Settings Library**. That fixed controller caption applies
the setting. Browsing is read-only; reopen the setting folder to refresh its
current-value marker. The catalogue offers EQ, balance presets, status light,
supported Line-Out mode and Sleep Timer, but no Line-In name/level editor.

Settings support is discovered from the physical description, service contracts
and successful native reads. Getters use background snapshots; setters validate
arguments and confirm readback. Sleep timers use the real coordinator, while
sound/device/input settings address the physical room. Fixed output and uncertain
fixed-mode writes block volume/EQ changes until readback confirms the mode.
Line-In gain writes use explicit native ranges when provided. Verified ZP80
inputs also support the controller's ten native gain choices when their service
description omits those ranges; other unknown input ranges remain unavailable.
See [package 2 details and live checks](docs/research/BACKEND-FEASIBILITY.md#package-2-implementation).

The Sonos backend reads state itself after every action and every 30 s, besides
events: a ZP80 on S1 11.16 sends its events 2-8 s late and merged. It needs the
Pi to accept incoming connections from the players (event callbacks, an ephemeral
TCP port unless `sonos.event_port` is set); without them it polls every 3 s.
Spotify through Sonos needs the account linked in the Sonos app.

The bundled player under `tools/zp80/` originated in the local CR100 emulator
project. It includes the software player, Netstart helper, synthetic public key,
and UPnP XML/image fixtures needed at runtime; no emulator is launched.

### About My Sonos System

Each selected Sonos zone reports its real model, software/build and hardware
versions, serial number and home-network IP in the CR100's existing About entry.
Explicitly bonded Subs, surrounds and stereo partners appear there too, without
becoming selectable rooms. Grouping an unselected room with a selected room does
not add that room's hardware to About. This works with the direct Sonos source
and with Sonos entities identified through Home Assistant's registries. The Pi
must be able to reach the Sonos products' local port 1400 for their full details.

Identity reads run in background workers and retain the last successful details
during temporary failures; missing information is shown as unavailable. Playback
discovery and topology keep the bridge's common compatible firmware profile, so
real S1/S2 version differences in About do not trigger the CR100's update checks.
The bridge advertises `24.1-00000` for both minimum and legacy compatibility,
allowing controllers on firmware 4.3 (`24.1-75101`) as well as 4.3.2. The
discovery software identity and capabilities retain the selected player profile.

Non-Sonos players are labelled **Bridge Virtual Component** with the bridge build.
An additional **CR100 Bridge** entry appears only in About, showing the bridge's
tag/short commit, full Git build details and upstream IP. The upstream address
follows the route to the real speakers or Home Assistant, rather than the
isolated controller network. These informational entries use separate HTTP ports
on the existing controller-network address and do not consume room address slots.

Build identity is stamped into installed releases and portable archives, so it
remains available without Git. Untagged checkouts show `dev` and the leading
eight commit characters; modified builds include `-dirty` in their build details.

### Line-In

Both speaker sources support connected Sonos line-in inputs. Include the source
speaker under Speakers so the CR100 can discover its input in **Music > Line-In**.
Select an input while controlling the room where it should play; the source can
be that room or another Sonos product in the same system. The existing queue is
retained. Now Playing shows the input name and source room. The Play/Pause key
stops and resumes the live input, with no track progress or seeking. Grouped rooms
follow their coordinator.

Cable changes and input names come from the Sonos AudioIn service. The Pi must
accept event callbacks from the Sonos products, including when using Home
Assistant. Home Assistant's Sonos integration and registries identify the source
products; playback commands still go through Home Assistant. Other Home Assistant
media players continue to use their normal playback controls.
