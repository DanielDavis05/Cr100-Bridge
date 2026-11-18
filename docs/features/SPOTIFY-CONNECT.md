# Starting Spotify Connect autoplay from the bridge

Verified on Bedroom, a Sonos ZP80 running S1, on November 3, 2026.
The Pi can activate the native Spotify Connect receiver, start one chosen track,
and let Spotify supply subsequent tracks. This works with the CR100's existing
Sonos Next command and with end-of-track continuation. It does not require the
public recommendations API or a Spotify client left running as the controller.

The working implementation is an isolated research prototype, not a deployed
change to the CR100's Play Now path. The production bridge still starts Spotify
through the finite Sonos queue.

## What was actually tested

All commands originated on the bridge Pi. The only target was Bedroom. Its
physical UUID was `RINCON_000E5800000A01400`; its separate Spotify receiver ID was
`901b97f3d7c6940b4b8f2d6a16c9858f3baffa73`.

| Experiment | Observed result |
| --- | --- |
| Public Web API play, with authorized playback scopes and a fresh Connect session | HTTP 403, `Restricted device` |
| Native Connect play using the ordinary Web API test token | HTTP 403 |
| Native Connect play using a Login5 token from the existing streaming login | HTTP 200 and the selected seed actually started |
| One-track native list with empty context URI | Next paused at the end of that list |
| Same seed, with its Spotify track URI supplied as the context | Next continued to a track absent from the request |
| Native play/transfer while Sonos was using its ordinary queue | HTTP 200 acknowledgements, but the source stayed `x-rincon-queue` |
| Receiver-scoped token exchange followed by local ZeroConf `addUser` | HTTP 200 / status 101 / spotifyError 0; source changed to `x-sonos-vli` |
| Native play after that activation | Selected seed started on `x-sonos-vli` |
| Two ordinary Sonos AVTransport.Next calls after activation | Blue in Green → Caravan → The Girl From Ipanema |
| Seed allowed to finish after seeking to its last four seconds | Blue in Green → Moanin', still PLAYING; no Next or second play command |

Only Blue in Green (`spotify:track:0aWMVrwxPNYkKmFthzmpRi`) was supplied to the
native play request. Spotify selected the other tracks. Earlier trials also
continued to Take Five and then another track on completion. These observations
establish automatic continuation, not a literal infinite-duration test.

The prototype's native login session was closed after each command. The receiver
continued independently between commands. The ordinary Sonos Next requests used
the same action as the CR100, rather than the restricted public Spotify API.
No CR100 menu or firmware was changed during these tests.

## Working sequence

### 1. Obtain the real receiver identity locally

Read `http://<coordinator>:1400/spotifyzc?action=getInfo`. Bedroom advertised:

```json
{
  "remoteName": "Bedroom",
  "deviceType": "AVR",
  "tokenType": "authorization_code",
  "clientID": "9b377073ea334637b1406f329ce005de",
  "scope": "streaming"
}
```

Use its `deviceID`, not the Sonos UUID. The public `/me/player/devices` response
omitted Bedroom; `/me/player` exposed it with `id: null` and `is_restricted: true`.
Neither result prevents native control. Resolve the physical coordinator through
the bridge's Sonos topology when integrating this beyond the fixed test target.

### 2. Establish a native authenticated control session

The bridge already had a separate streaming-capable folder login at
`/var/lib/cr100-bridge/spotify-folders-token.json`, using the existing
`spotify_folders.FOLDER_CLIENT_ID`. The prototype refreshed it through
`SpotifyFolderLibrary._access_token()` and its existing file lock.

An isolated `librespot==0.0.10` Python environment authenticated a session with
that OAuth token and `AUTHENTICATION_SPOTIFY_TOKEN`. It disabled credential
storage, audio caching, and library logging. The authenticated session's canonical
username was checked against `/me` fetched using the separately authorized Web
API test token. Its Login5-derived token successfully authorized Connect commands.
This was a control session; no local audio renderer was started.

The folder token's own `/v1/me` request returned 429, so that response was not
used to establish account identity. Whether the original streaming OAuth token
could authorize Connect commands directly was not established; the successful
control requests used the Login5 token. Ordinary Web API credentials were tested
and rejected.

### 3. Activate the Sonos receiver when necessary

If the player is using its queue or another source, first exchange the streaming
OAuth access token at `https://accounts.spotify.com/api/token` using a form body:

```text
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
requested_token_type=urn:spotify:params:oauth:authorization_code
subject_token_type=urn:ietf:params:oauth:token-type:access_token
subject_token=<existing streaming OAuth token>
client_id=<client ID associated with that streaming grant>
audience=<receiver's getInfo.clientID>
resource=urn:spotify:resources:connect
scope=streaming
```

The returned `access_token` is the receiver authorization code. Keep it in memory
and send it as `blob` in a form POST to the receiver's `/spotifyzc`:

```text
action=addUser
version=<getInfo.version>
tokenType=authorization_code
clientKey=
loginId=<verified canonical account ID>
userName=<verified canonical account ID>
blob=<receiver authorization code>
deviceName=CR100 bridge research
deviceId=<stable 40-character controller ID>
```

This handshake switched Bedroom back to the native `x-sonos-vli` source. No
password, browser cookie extraction, `resetUsers`, or URI replay was needed.
The receiver returned status 101 / ERROR-OK; confirm the physical source change
as well. Public `/me/player` briefly remained 204 after successful activation.

### 4. Start the seed with a nonempty Spotify context

Resolve an `spclient` host using `https://apresolve.spotify.com/?type=spclient`.
With the native Login5 bearer token, POST to:

```text
/connect-state/v1/player/command/from/<controller ID>/to/<receiver deviceID>
```

The tested body was:

```json
{
  "command": {
    "endpoint": "play",
    "context": {
      "uri": "spotify:track:0aWMVrwxPNYkKmFthzmpRi",
      "url": "context://spotify:track:0aWMVrwxPNYkKmFthzmpRi",
      "pages": [{"tracks": [{"uri": "spotify:track:0aWMVrwxPNYkKmFthzmpRi"}]}],
      "metadata": {}
    },
    "play_origin": {"feature_identifier": "cr100-bridge-research"},
    "options": {
      "license": "on-demand",
      "skip_to": {},
      "player_options_override": {}
    }
  }
}
```

The empty-context control trial stopped after its single track. The otherwise
equivalent nonempty track context continued automatically. The account/receiver
already had working autoplay enabled; these tests did not change that setting
or establish how to force it when disabled.

An HTTP 200 with `ack_id` is insufficient proof. Check the speaker is actually on
`x-sonos-vli` and playing the selected seed. This distinction caught the
ineffective play/transfer requests made before receiver activation.

## Reproducing the experiment

Local prototype: [native_probe.py](../../../../research/bridge-runs/spotify-autoplay-2026-11-03/native_probe.py).
The tested remote copy is `/tmp/cr100-native-probe.py`; its disposable environment
is `/tmp/cr100-native-venv`. They are not installed bridge services. Example
commands on the Pi, using the fixed Bedroom identity guards in that script:

```sh
sudo /tmp/cr100-native-venv/bin/python /tmp/cr100-native-probe.py info --native-session
sudo /tmp/cr100-native-venv/bin/python /tmp/cr100-native-probe.py activate --native-session
sudo /tmp/cr100-native-venv/bin/python /tmp/cr100-native-probe.py start --native-session --context track
sudo /tmp/cr100-native-venv/bin/python /tmp/cr100-native-probe.py next
```

`activate`, `start`, and `next` change Bedroom playback. `queue-seed`, `seek-end`,
and `cleanup` are experiment-specific actions, not proposed product controls.
The script deliberately fixes the receiver address, UUID, Spotify device ID,
and seed. Generalize target validation and error handling before integration.

## Integration implications and remaining limits

The bridge can replace Spotify Play Now's current `clear_queue → share →
play_track(1)` sequence with `activate receiver if needed → native context play`.
The CR100's normal play, pause, Next, and seek can remain Sonos actions, with Next
also honoring any pending explicit Sonos queue. The user's revised product model
below keeps explicit entries in Sonos and recommendations outside that queue.
It needs a tested handoff coordinator rather than mirroring recommendations into
Q:0. The control helper should verify the source/track and account, rather than
report success on an acknowledgement.

### Product decisions confirmed after the experiment

The user's subsequent clarification supersedes the earlier context-aware Play
Now proposal. **Autoplay Off preserves existing behavior.** With Autoplay On,
ordinary **Play Now** should start just the chosen Spotify track, then native
Spotify recommendations outside the Sonos queue. Selecting a track inside an
album does not implicitly queue the rest of that album.

An explicit **Play from here** action should put the selected occurrence and the
remainder of its browsed album/playlist into the Sonos queue, then start Spotify
continuation after that finite selection finishes when enabled. Use the actual
browse context and occurrence, not a track's album metadata. Native queue to
Spotify continuation without replaying the final seed is still unproven.

**Queue from here** should append the selected occurrence and remaining collection
without interrupting playback. Preserve duplicate occurrences, disc/playlist
order, and tracks on subsequent pages. Today's playback resolver retains track
identity but not the required browse context/absolute occurrence; that needs an
explicit validated playback intent, preserving existing controller route locks.

**Explicit Sonos queue entries have priority.** When an item is queued during
recommendations, finish the current song before switching to the first pending
explicit item. Recommendations must not be appended to Q:0. The proposed
non-Spotify boundary is to finish the explicit mixed-source queue and stop,
without resuming old Spotify recommendations. The saved Autoplay preference
remains enabled for the next Spotify listening run.

Q:0 retains already-played rows. Queue priority needs a per-run pending cursor and
queue revision; a nonzero queue length alone must not replay old rows. Keep
existing Play Now replacement semantics unless explicitly changed. Pause, Stop,
sleep timer, source changes and external takeover are not queue exhaustion.

The user wants a change applied to the current listening session **only if the
current track continues without interruption** and is willing to help listen-test
that behavior. A live toggle must not activate/restart a session, issue Play or
Seek, replace the active source, or restart the current track. If it cannot be
applied safely, save the preference for the next Play Now action and clearly
distinguish the saved preference from the active session's mode.

The confirmed native activation/start sequence above does not establish a safe
live toggle. Source research finds Autoplay as a Spotify user attribute, with
updates triggered by desktop settings; it has not established a receiver-scoped
setter. Changing that user attribute must not silently implement a per-room
control or be mistaken for a local in-memory librespot override affecting Sonos.

A candidate live-toggle test must keep the track/source identity stable and the
position progressing normally, with no pause/stop/seek/restart. Audible continuity
requires the user's listening confirmation as well as transport observations.
Testing must also establish the intended end-of-selection behavior and restore
any temporary preference. The user preference is a requirement, not a claim that
this test has passed. A read-only check initially found Bedroom paused on
`x-sonos-vli`. The user then started Breathe Deeper on Bedroom for an authorized
control trial. Spotify Desktop's Autoplay preference was temporarily switched
from On to Off at approximately 3:22 into the track. This is a broader Spotify
preference, not an established room-specific bridge setter. Observations are in
`runs/spotify-autoplay-2026-11-03/live-toggle-observation.jsonl`; final continuity,
boundary results and preference restoration are recorded below.

The user clarified that the product must be **bridge-controlled, with no changes
to Spotify's global account Autoplay preference**. The temporary desktop setting
was restored to On and its checkbox readback verified before Breathe Deeper
finished (approximately 5:54/6:12). The user reported no audible interruption.
All observed samples before the natural transition retained PLAYING, the same
source hash, and steadily advancing position. After restoration, Breathe Deeper
continued naturally to Afterthought. Because On was restored before completion,
this trial does not establish finish-current-then-stop with Autoplay Off, and it
does not establish a bridge-only live toggle. No further preference changes are
part of the planned feature.

### Bridge-controlled continuation: new metadata proof

The bridge then successfully queried the native Spotify context resolver without
touching playback or account preferences. `POST /context-resolve/v1/autoplay`
with a protobuf AutoplayContextRequest (seed context URI plus recent track URI)
returned HTTP 200, a `spotify:station:track:...` context, 50 track URIs, and a next
page. See `native-autoplay-context-access.json` and `probe_autoplay_context.py`
under the experiment directory. Before/after speaker observations retained the
same PLAYING source and duration, advancing normally by one second. This uses the
native interface, not the public Web API recommendations endpoint.

This supports a better candidate architecture for the user's preference:

1. Keep the Sonos queue exclusively for explicitly selected music.
2. Fetch Spotify-related candidates ahead of time as metadata, outside Q:0.
3. Play recommendations as finite one-track selections with an empty context,
   letting the bridge choose whether there is a next track. Natural completion
   and a bridge-selected next recommendation now pass the isolated test below.
4. At each natural boundary, service pending explicit Sonos entries first; when
   eligible, start the next Spotify candidate only if the bridge flag is On.
5. Turning Off during a bridge-owned finite track should merely disarm the next
   start. It must not restart, seek, pause or replace that current track.

The bounded sequencing prototype now passes on Bedroom. Queue-to-Connect
activation can resume an old cached track before the chosen start, and production
event handling and transition latency still need work. An existing
Spotify-app-started, receiver-managed context cannot automatically
become a bridge-managed finite session without a separately proven safe change;
defer the mode change for such a session rather than promise no interruption.

### Bridge-controlled live test: passed

The user authorized a Bedroom test of bridge-owned continuation, an explicit
non-Spotify queue item, and local Autoplay Off during the current track. Bedroom's
indexed music library and library folders were empty. The user approved a short
temporary audio file as the mixed-source substitute and subsequently authorized
clearing the existing queue.

`test_bridge_continuation.py` is a bounded isolated prototype, not an installed
service. It holds recommendations outside Q:0, plays finite one-track empty-
context selections, and uses a local Pi flag to decide continuation. It made
zero Spotify account-preference writes and zero volume commands.

The successful run on November 3, 2026 (local time) established:

| Step | Observation |
| --- | --- |
| Seed completes | Blue in Green reached 5:37 and paused naturally |
| Bridge continuation On | Bridge started Spotify-selected Overture from Whiplash |
| Explicit queue add while recommendation plays | Overture remained PLAYING on the same source; position 2:43 before/after enqueue |
| Queue priority at natural completion | After Overture finished, the generated 12-second MP3 played on `x-rincon-queue` |
| Non-Spotify queue finishes | Fixture reached 11/12 seconds, then STOPPED; no Spotify resume during an eight-second hold, with local Autoplay still On |
| Local Autoplay Off during Lujon | Same source/title and PLAYING state, position 2:04 → 2:05; zero transport commands during the change |
| Finish current then stop | Lujon reached 2:38, paused naturally, and stayed paused during a ten-second hold; no subsequent playback command |

The user confirmed **No interruption** at the local Off change. The script sent
no Play, Pause, Stop, Seek, SetURI, activation or queue command after that change.
Final readback: queue empty, Lujon paused after completion, local test flag Off.
The temporary HTTP server and control file were shut down/removed; the temporary
remote MP3 and script were removed. Production GUI/service/configuration were
not changed. The existing authorized credential may have refreshed normally.

For time, Spotify samples started in their last 45 seconds. Initial offsets and
any setup seek preceded the verified listening baseline; there was no seek
during the Off test. Transition verification took about 4.8 seconds from the
seed's first observed terminal state to verified recommendation playback, and
4.4 seconds from recommendation terminal state to verified queued-file playback.
These include observation/verification latency, not exact audible-gap measurements.
Gapless playback is not established.

An earlier WAV fixture attempt could not verify queued playback. A tagged stereo
MP3 served with HTTP byte-range support played successfully in a standalone queue
diagnostic and then in the full run. Evidence is retained rather than treating
the earlier failed verification as success. This proves actual non-Spotify Sonos
queue playback; **indexed music-library playback remains untested**.

Evidence under `runs/spotify-autoplay-2026-11-03/`:

- `bridge-controlled-result.json`: sanitized outcome summary.
- `bridge-controlled-live-test.jsonl`: complete successful sequence and samples.
- `test_bridge_continuation.py`: exact prototype source.
- `queue-fixture-diagnostic.jsonl`: verified temporary MP3 queue playback.
- `bridge-controlled-live-test-wav-attempt.jsonl`: earlier failed fixture attempt.

Stock CR100 bottom-left menus are locally constructed in MainUI.swf; no remote
extension hook for either Autoplay or Queue from here has been established.
Initial mode settings can live in the bridge web GUI. Controller softkey additions
remain a separate UI patch, or require a tested stock browse-action alternative.

This was tested on one S1 Bedroom coordinator after leaving Connect for its
ordinary queue. Group changes, hard reboots, other receivers, disabled autoplay,
albums/playlists, and production authentication lifecycle are not covered.
The native token exchange, Login5, and Connect command interfaces are private
client protocols and can change independently of the public Web API.

`StartAutoplay` and VirtualLineIn's transmission actions were inspected but were
not needed or invoked. The UPnP action named StartAutoplay has alarm-related
parameters; its name alone did not identify Spotify's recommendation mechanism.

In the initial native-session experiment, the temporary queue was removed, restoring its
original empty state. Bedroom was left paused on the native source, with its
original volume of zero. The test changed the current music; production bridge
code/configuration and the production catalog token were not changed. The existing
folder token may have been refreshed through its normal refresh path. Credentials
and authorization-code values were neither exported nor included in evidence.

## Evidence and primary references

Sanitized request results are in
[runs/spotify-autoplay-2026-11-03](../../../../research/bridge-runs/spotify-autoplay-2026-11-03/README.md),
especially `native-zeroconf-activation.json`, `native-start-after-activation.json`,
`native-activated-session-next*.json`, `native-seed-natural-completion.json`, and
`native-cleanup.json`.

- [Spotify hardware ZeroConf guide](https://developer.spotify.com/documentation/commercial-hardware/implementation/guides/zeroconf)
- [Spotifast's native Sonos control implementation](https://github.com/crmne/spotifast/blob/main/src/sonos.rs)
- [librespot Connect transfer implementation](https://github.com/librespot-org/librespot/blob/dev/core/src/spclient.rs)
- [SpotifyWebApiPython's receiver authorization-code exchange and addUser implementation](https://github.com/thlucas1/SpotifyWebApiPython/blob/master/spotifywebapipython/zeroconfapi/zeroconfconnect.py)
- [librespot-python session and Login5 implementation](https://github.com/kokarare1212/librespot-python/blob/main/librespot/core.py)

## Now Playing Autoplay icon (R13 firmware)

The bridge reports each room's Autoplay state through the inert read
`CR100_SpotifyAutoplayNow`: `off`, `armed` (Autoplay on, the current track is
the listener's own Spotify music) or `active` (the current track was started by
Autoplay). `SpotifyAutoplay.status()` answers from its run table: a run whose
current track came from `_recommend()` is `active`; the flag is set before the
speaker is told to play the recommendation and cleared when an explicit queue
row takes over. Without a run, a Spotify row in the Sonos queue reads `armed`.
External Spotify Connect sessions are never adopted and read `off`.

While a room reads `armed` or `active`, its AVTransport event carries
`r:CR100Autoplay`, so a change always reaches the controller. When the saved
preference flips, the policy loop asks every room's backend to re-publish. R13
firmware draws the icon left of Crossfade; older firmware ignores the value.

