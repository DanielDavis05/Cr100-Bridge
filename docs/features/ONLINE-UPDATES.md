# Online Updates

The bridge routes the CR100's original **System Settings → Online Updates**
to the firmware explicitly armed for that paired controller, or — when no
controller firmware is armed — checks GitHub (`DanielDavis05/Cr100-Bridge`,
`master` branch, falling back to the repository's default branch) for a newer bridge commit. When a newer commit is available,
the bridge offers it as a Sonos system/player update (model 1 only) while
leaving controller firmware unchanged. Without an armed controller package or
available bridge commit, a fresh inert manifest retains the current player
persona and controller version while omitting controller firmware, so the exact
v2 native guard reports no update. An empty UpdateItem would instead display a
native connection error.

The implementation is present in this checkout. This document does not assert
that a physical controller has been flashed with custom firmware.

## Upload and offer controller firmware

Open **Controllers** in the authenticated web UI. Each paired controller has one
**Firmware** row showing what the controller runs and any update under way.

1. Select **Upload .upd** and choose the controller's `.upd` file, or select
   **Use firmware on bridge** when an administrator has placed an image at
   `firmware/controller.upd` beside the bridge configuration file. A new image at
   that path becomes available without a code update. Uploads are limited to
   16 MiB. The bridge verifies the package and reads its embedded internal
   version; the filename does not determine the target.
2. A verified package is offered to that controller immediately. If the vendor
   signature cannot be verified, or the package is custom firmware for a
   controller that still looks stock, the browser asks once before offering it.
   Declining discards the upload.
3. On the CR100, open **System Settings → Online Updates**, then keep the
   controller docked and follow the native confirmation. The controller
   downloads the unchanged firmware and invokes its own installer. A stock
   controller requires vendor-signed firmware; for a first custom install use
   **Install custom firmware** in the same row (see
   [STOCK-BOOTSTRAP.md](STOCK-BOOTSTRAP.md)).

The web UI reports upload progress and validation errors inline and refreshes
pending-update status. The offer carries the uploaded package's SHA-256
identity; if another administrator replaces the upload in between, the stale
request is rejected.

**Cancel update** removes the offer for that controller. It is available before
the download starts. It does not stop a controller after its installer has begun.

An interrupted firmware HTTP delivery marks that selection failed and reports
the error inline; upload the firmware again to retry. A late failure from an old
transfer cannot fail a replacement package staged for the same controller.

## Optional force prompt and returning to normal control

The web UI never requests a force prompt; it is available through the API only
(`force: true` on `/api/controllers/<mac>/firmware/install`). It requires known
firmware 4.3 (`24.1-*`).
It is disabled for 4.3.2 (`25.2-*`), later versions, other unsupported versions,
and unknown firmware. The API also rejects those forced requests without
raising a compatibility requirement. Manual installation remains available.

On supported 4.3 it changes only the selected controller's advertised
compatibility requirement long enough to request the stock prompt.
Other controllers keep their normal compatibility range. It does not bypass
the controller's signature checks or turn a confirmation into an automatic installation.

This limit follows an observed stock 4.3.2 failure: forcing its compatibility
requirement displays "Your software no longer supports this CR100" and reduces
Settings to About My Sonos System, Factory Reset Controller, and Turn Off
Controller. Online Updates is missing in that state. A force prompt on 4.3.2
would therefore hide the very updater the user needs. Leave 4.3.2 compatible and
use **System Settings → Online Updates** through the manual path. Persisted
forced selections for unsupported or unknown firmware are released during
recovery rather than restoring that mismatch.

The temporary requirement is a one-shot prompt lease lasting at most **120
seconds**, within the armed install's own expiry. It is released as soon as the
controller starts `CheckForUpdate`, before the later confirmation. That timing
matters: cancelling a native update confirmation does not send a separate HTTP
cancellation callback. Keeping the higher requirement until firmware download
would leave a user who chose Cancel facing repeated update-required prompts.

After the check acknowledges a supported 4.3 prompt, the package stays armed for
a manual retry through Online Updates, while ordinary controller use becomes compatible
again. The web UI distinguishes the requested prompt from a currently active
prompt and explains the manual retry. A released prompt is described as no
longer active; release through expiry or restart is not called an acknowledgement.
Web cancellation, prompt-lease expiry, update failure/expiry, download start,
and update-service restart also restore
the ordinary compatibility requirement. A restart retains valid staged
firmware for a later manual check; it does not reactivate the old forced prompt.

An uploaded, unarmed package expires after seven days. An armed selection
normally expires after one hour; download tracking has a separate 30-minute
window. Expiry stops offering the selection and clears the force requirement.
Upload and review the firmware again when the UI requests a fresh upload.

Reconnecting is not evidence of installation. For a different-version attempt,
the bridge marks completion after delivery starts and that same controller later
reports the selected target, provided the known versions at arming and download
start both differed from that target. A missing baseline does not prove a
transition. A controller reporting its old version, or a different lower
version, is not marked complete merely because it reconnected. Such reports do
not renew the force-prompt lease: checking acknowledges it, and its 120-second
limit still applies. Reporting the selected target before download leaves the
selection armed and does not mark the update complete.

## Upgrades, downgrades, and identical versions

The package must identify Sonos manufacturer 1, handheld model 2 (CR100),
and a valid internal version, and contain the controller kernel/root filesystem.
Player firmware and malformed packages are rejected. Modified or unsigned
packages show a warning and require explicit consent tied to the reviewed
package before installation can be armed. That consent persists for delivery.
The bridge records the complete package's SHA-256 and rechecks its structure
and identity before arming and delivery. The controller's own installer
performs its normal authentication and hardware compatibility checks.

The stock CR100 supports selecting a different older version. When that older
target reaches its native installer handoff, the application adds uppercase
`-F`. That is the stock downgrade route, and it retains signature checks. The
web checkbox controls whether to show a prompt; it is separate from this native
downgrade flag.

An identical-version selection shows an inline warning that the stock update
flow may skip reinstalling it. **Install** remains available without an extra
checkbox or confirmation. The bridge arms and offers the selected package with
its real embedded version; it does not fabricate a different version. Reporting
that version before download leaves the selection armed for an install attempt.
After downloading, ordinary target-version reports leave an identical-version
attempt in `downloading`, available for another download/check until cancellation
or expiry. The stock version cannot distinguish custom builds.

V3 adds package-specific installed-movie evidence. Validation extracts the single
canonical `cfw3:<64 lowercase hexadecimal digits>` literal from the staged
package's actual `/opt/swf/MainUI.swf`, storing it as `cfw_build_id`. The newly
started movie reports it through `SetString('CR100_CFWBuild', ...)`. Only the
paired controller with a downloading selection, exact expected build marker,
and reported target version can complete that selection. A label such as `v3`,
a marker from a different image, another controller, or a version-only report
cannot do so. V2 packages without a marker remain honestly unverified and
retryable. This is a cooperative startup receipt on the paired controller
network, not a cryptographic device-attestation protocol.

The original native UI updates the controller before selected players. A proven
controller completion therefore retains a separate, bounded authorization for
virtual-player recovery under that same current record/token. An exact installed
movie marker or a validated different-version transition can support this lease;
a legacy same-version completed flag cannot. Ordinary checks still return an
inert no-update manifest and completed packages cannot be downloaded. A cached
old recovery manifest contains model 1 only, avoiding another same-version
controller handoff. The lease expires at the earlier original offer/download
deadline; cancellation, rearming or replacement invalidates it at every phase.
Controller-scoped ZoneGroupTopology notifications provide that recovery item in
`AvailableSoftwareUpdate`, including the first notification after boot. The
native recovery waits for this event before fetching its saved manifest. Actual
download/completion changes refresh notifications; merely armed or unarmed
packages leave this field empty and do not create an automatic prompt.

Installed v2 controllers can also select virtual players while planning a
controller update. Their exact active controller token authorizes no-op player
sessions: only the paired source, emitted internal URL, currently visible UDN,
Flags 0/2, and empty ExtraOptions are accepted. The normal ordered
HELLO/DOWNLOAD/FLASHWRITE/REBOOT exchange refreshes accepted players' BootSeq and
discovery at the current common software persona. It does not advance the bridge
version, call a real backend, reboot the Pi, or complete the controller receipt.
Only accepted participants are required, including a subset or a later selected
player sharing an IP. Cancellation, rearming, expiry, disappearing players and
source rebinding invalidate the relevant session. A retry uses a fresh internal
transport identity so an old socket cannot modify it.

This bridge compatibility path preserves v2's explicit model-match controller
guard and native eligibility checks. Omitting the player image alone is not a
safe replacement: the stock planner can fall back to generic image information.

## Messages on the CR100

The local `GetOnlineUpdateMessage` reply supplies text appropriate to the
selected path:

| Path | Introduction/confirmation | Choices |
| --- | --- | --- |
| Official controller firmware | Identifies a vendor-signed Sonos image, its internal version and the CR100 as the target | Install official firmware / Keep current firmware |
| Custom controller firmware | Shows the known release (for example R15) alongside the internal version; explains that the first installation from stock needs the bridge's one-time **Install custom firmware** setup | Install custom firmware / Keep current firmware |
| Unverified controller firmware | States that the bridge cannot identify the package as official or a custom release | Install unverified firmware / Keep current firmware |
| Controller downgrade | Names the firmware origin and older target in the introduction and confirmation | Install older firmware or the origin-specific choice / Keep current firmware |
| Bridge update | Identifies the CR100 Bridge as the target and says the controller firmware stays unchanged | Update bridge / Later |
| Cancelled/expired selection | The staged update is no longer available | Back to settings only |
| Selected version already installed | Controller update introduction; target firmware and docking instruction | Install firmware / Keep current firmware |
| Another bridge update is active | Another controller is updating the bridge | Back to settings only |

The customizable choice labels are menu rows selected with the physical OK
button. They do not rebind the three physical soft keys. The original firmware
continues to supply checking, progress phases, caution, error, and completion
wording. Both intro header and body must be present for the optional intro.
Headers/choice labels have 40-byte text bounds and bodies 511-byte bounds after
the terminating NUL is reserved; visible wrapping still needs to fit the screen.

The bridge reads the package itself. A verified Sonos signature identifies official
firmware. For a self-describing custom release, the bridge verifies the project's
signature on its release label and checks that its build ID matches the one in
the packaged controller UI. A future build signed with the same project key can
therefore display its release during upload and installation on the same bridge.
Earlier custom packages use their known build IDs for a release label. The stock controller's normal updater cannot install a development-
signed custom package on its own. Its first custom installation uses the bridge's
guided one-time setup; later custom releases can use Online Updates.

The service deliberately advertises hostname-based `UpdateURL` values. The
stock application skips `GetOnlineUpdateMessage` for certain literal numeric
HTTP prefixes, including `http://192.168.` and `http://10.`. A direct private-IP
update URL can therefore use the stock generic confirmation without fetching
the custom text. Hostnames resolve locally as described below.

## Bridge self-updates and demonstration mode

When no controller `.upd` is armed for the requesting controller, `CheckForUpdate`
queries GitHub (`DanielDavis05/Cr100-Bridge` `master`, configurable via `"updates":
{"repo": "...", "branch": "..."}` in `bridge-config.json`) via
`tools/bridge_updater.py` and compares the remote commit SHA against the running
build's `build-info.json`:

1. If a newer commit exists, the manifest offers a `model="1"` system update
   without any `model="2"` image, and `system_version` matches the controller's
   own version so the controller leaves its own firmware untouched.
2. Selecting **Update bridge** on the CR100 drives `BeginSoftwareUpdate` and
   TCP port 4444 (`HELLO`, `DOWNLOAD`, `FLASHWRITE`, `REBOOT`):
   - `DOWNLOAD` fetches the GitHub commit archive once per job, verifies its
     structure, and stamps `build-info.json`.
   - `FLASHWRITE` stages a new `/opt/cr100-bridge/releases/release-*` tree,
     builds its `.venv`, verifies imports, updates unit/CLI files, and
     atomically flips `/opt/cr100-bridge/current` while keeping older releases
     for rollback.
   - `REBOOT` advances `BootSeq` and software version, notifies the controller,
     and restarts `cr100-webui.service` and `cr100-bridge.service` right after
     `DoPostUpdateTasks` (with a 5 s fallback timer).

The explicit laboratory `UpdateService.check(..., demonstration=True)` path
retains the simulated stub behavior (`stub=True`) for offline tests without
network requests or disk changes. Real Sonos/Home Assistant speakers are never
firmware-update targets. Controller model-2 firmware is left unchanged on both
paths: bridge manifests omit model 2.
Only an active, armed controller package selection emits a model-2 image,
including when that selected package has the same version as the controller.
This also prevents a custom controller that supports same-version reinstall
from treating an unarmed current-version placeholder as an installation offer.
Stock clients can use both the generic `UpdateItem.Version` and the manifest's
`system_version` as controller fallbacks when model 2 is absent. On the
bridge path both defaults use the requesting controller's known current
version. Setting only the manifest header is insufficient: the native stock
4.3.2 control still attempted a controller handoff in that case. With both
defaults equal to its current version, it reported no update when the explicit
model-1 image was also unchanged. The model-1 image and coordinator still carry
the bridge update's actual player target. When the controller version is
unknown, the bridge keeps the existing generic defaults rather than inventing
a controller version. Armed controller packages retain their existing generic
version, header and explicit model-2 target.

The web UI's Bridge updates section reads the coordinator receipt and shows the
latest/active status, target commit, phase, percentage, and errors. Its
internal simulated player version (`34.16-*`) is incremented per bridge update
so the CR100 triggers native discovery/reboot completion; the actual Git
commit identity is tracked in `build-info.json` and shown in About My Sonos
System.

## Local DNS and listeners

The managed bridge's `dnsmasq` helper serves **only `sonos0`**, bound to
`192.168.69.1`. DHCP advertises that address as the controller's DNS server.
It uses `--no-resolv` and `--no-hosts`, and supplies local address mappings for:

```text
update.sonos.com
update-firmware.sonos.com
system-api.sonos.com
```

Those names resolve to `192.168.69.1` on the controller network. This does not
change the main LAN's DNS, the host's resolver configuration, or its hosts file.
It does not create a wildcard mapping for all Sonos domains or forward other
DNS names through the host resolver.

| Port | Listener/use |
| --- | --- |
| UDP/TCP 53 | Private `sonos0` DNS mappings |
| TCP 80 | Controller-specific manifests and unchanged staged firmware |
| TCP 443 | Local TLS 1.0-compatible `GetOnlineUpdateMessage` endpoint |
| TCP 4444 | Native player-update command/progress sessions on virtual component addresses |

The existing virtual-player SOAP listeners on TCP 1400 and administrative web UI
on TCP 8080 keep their separate roles. The administrative web UI refuses access
from the controller network. The legacy TLS listener is limited to the private
controller-side address; its generated local certificate is not the vendor's
firmware signing key.

On the first installation of this DNS-aware bridge version, a CR100 with an old
DHCP lease can still hold its earlier DNS configuration. **Renew its lease or
reconnect the controller's network connection** so it receives
`192.168.69.1` as DNS. A browser using the Pi's ordinary LAN connection does not
exercise this private DNS path.

## Authorization, state, and evidence

Uploads, Install, Cancel, and status reads use the existing authenticated web
session; writes also require the `X-Bridge` request header. Uploads stream to a
bounded temporary file before validation. Staging is keyed by paired controller
MAC, while update manifests/packages are bound to that controller's resolved
source identity and unpredictable selection token. Another controller cannot
reuse the offer or fetch its package by copying the URL. Administrative status
does not expose those tokens or internal package paths. This is isolation on the
bridge's paired private network, not an assertion of a new cryptographic
controller-authentication handshake.

State lives in `controller-updates/` beside `bridge-config.json`: controller
receipts in `state.json`, verified content-addressed packages, a local TLS
certificate/key under `tls/`, and separate virtual-player compatibility or bridge
demonstration receipts in `bridge.json`.
Pairing state remains in its separate file. State updates use locking and atomic
replacement so the web UI and player process share one selection. Failed or
corrupt state cannot permanently raise controller compatibility requirements.

Implementation entry points are [webui.py](../../tools/webui.py),
[online_updates.py](../../tools/online_updates.py),
[update_service.py](../../tools/update_service.py),
[update_transport.py](../../tools/update_transport.py), and the managed DNS command
in [bridge_service.py](../../tools/bridge_service.py). The offline tests cover
authenticated HTTP staging, signature/model/version validation, controller
isolation, lifecycle/expiry, temporary-prompt recovery, update protocol framing,
and serialized bridge jobs. See [test_webui_updates.py](../../tools/test_webui_updates.py),
[test_online_updates.py](../../tools/test_online_updates.py), and the update service
and transport tests beside them.

The disposable [update_acceptance_fixture.py](../../tools/update_acceptance_fixture.py)
supports production-handler host regressions and a native emulator peer. Run
`python3 tools/update_acceptance_fixture.py --self-test` for the host matrix.
Serving requires a dedicated `--state-dir`; it creates simulated rooms plus the
Bridge/About component with no connected backend. For a private native lab, use
explicit `--bind`, `--controller-address`, `--controller-mac`, `--household`,
`--soap-port 1400 --http-port 80 --tcp-port 4444 --ssdp`, and the actual reviewed
`--package`. Unverified packages require `--allow-unverified`. Package delivery is
refused by default so acceptance can intercept the original installer handoff.
`ready.json` records listener/component addresses; `snapshot.json` records
sanitized controller and player receipts, persona, and BootSeq. This fixture does
not patch firmware or intercept the installer itself. A native run must retain
its own exact executable/package hashes and handoff evidence; passing host
regressions does not establish native acceptance.

## Production integration acceptance in the emulator

Tests against the production update manager, service, serializer, and update
command handlers established these results with the original controller
application:

| Stock controller | Observed result | Evidence |
| --- | --- | --- |
| 4.3.2, manual downgrade | Custom confirmation, native `-F` installer handoff, then an authenticated download accepted by the stock installer's check-only mode | `22-stock432-production-intro.png`, `23-production-downgrade-upgradeurl.txt`, `24-production-authenticated-check-only.json` |
| 4.3, optional force prompt | Actionable native prompt; checking released the compatibility floor before the lease expired; Cancel returned to rooms while the firmware stayed armed for manual retry | `30-stock43-force-zones.png`, `33-stock43-after-force-check.json`, `35-stock43-forward-confirm.png`, `36-stock43-confirmation-cancel.png`, `37-stock43-cancel-floor-status.json` |
| 4.3, bridge demonstration | Both the room and Bridge/About components ran the native update command session; the common simulated version advanced from `34.16-37101` to `34.16-37102`; the controller showed **Update complete** and kept firmware `24.1-75101` | `42-stock43-production-bridge-check.png`, `44-production-bridge-progress-*.png`, `45-production-bridge-result-state.json`, `47-production-bridge-post-reconnect.png` |
| 4.3.2, unsafe forced compatibility mismatch | Retirement notice and Settings without Online Updates; this test established why production now refuses this force request | `05-stock432-forced-floor-later.png`, `07-stock432-forced-settings.png` |

These files are in the separate emulator workspace under
`cr100-emulator/research_update/production-2026-11-03/evidence/`. Restoring the
normal compatibility floor admitted the unchanged controller back into rooms;
that is distinct from completing a firmware installation.

The emulator's SH4 Python lacks `_ssl`, so its native TLS 1.0/AES256-SHA request
used a research TLS relay carrying the production service's serialized message.
Separate loopback tests exercise the production TLS listener and generated
certificate. The emulator result does not claim a direct connection to that
Python TLS listener.

Controller firmware tests intercepted the flash boundary. Check-only
authentication and native installer handoff do not establish physical flashing,
auxiliary MCU programming, or reboot into the selected version. Earlier research
also established stock 4.3/4.3.2 custom confirmations and forward/reverse handoff;
the production tests above are the acceptance evidence for this integration.
