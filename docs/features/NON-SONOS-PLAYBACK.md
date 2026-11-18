# Music on non-Sonos speakers

A Sonos keeps its own queue and fetches music-service audio itself. A speaker
reached through Home Assistant that is not a Sonos (a Bose SoundTouch, a
Chromecast, a DLNA renderer) has neither, so the CR100's queue actions and
music-service playback used to fail on it with UPnP error 501
(`provider_enqueue is unavailable for this room`).

For those rooms the bridge now keeps the queue itself and plays it through the
best route the speaker offers.

## What works on a non-Sonos room

| CR100 action | How the bridge handles it |
| --- | --- |
| Play Now / Add to Queue / Play Next on a Spotify track, album or playlist | Expanded to tracks and kept in a bridge queue |
| Queue screen (Q:0), Remove, Clear, jump to a track | Served from the bridge queue |
| Play, Pause, Stop, Next, Previous, Seek | Sent to the active route |
| Shuffle and Repeat | Applied by the bridge queue (and passed to Spotify on Connect) |
| Radio, Line-In, other menu items | Played as before; the bridge queue is kept, not played |

Volume, mute and the speaker's own sources stay with Home Assistant.

## Routes

The route is chosen each time the queue starts playing:

1. **Spotify Connect** (preferred). If the speaker is a Spotify Connect receiver
   signed in to the bridge's Spotify account, the bridge sends it the queue's
   tracks. Spotify streams and advances on the speaker itself; the bridge follows
   the title the speaker reports to keep the controller's queue position right.
2. **Relay**. Otherwise, if Home Assistant can play a URL on the speaker, the
   bridge fetches each track's audio (librespot for Spotify), converts it with
   FFmpeg and serves one continuous MP3 stream at
   `http://<bridge LAN address>:8404/relay/<token>.mp3`. Next, Previous and
   Seek switch the audio inside that stream.

If neither route is possible, the controller shows why (for example, the
speaker cannot play a URL, or relay playback needs the bridge Spotify sign-in).

Both routes need the bridge's Spotify playback sign-in (the same one Autoplay
uses). Spotify Premium is required.

## Finding Connect speakers

Connect receivers are discovered by mDNS (`_spotify-connect._tcp`) and matched
to rooms by the speaker's name (its ZeroConf `remoteName`, or the mDNS instance
name, against the Home Assistant friendly name). No system mDNS daemon is
needed. To pin or disable Connect for a room:

```json
"bridge_playback": {
  "connect": {
    "media_player.daniel_s_room_bose": "http://10.0.0.147:8200/zc",
    "media_player.kitchen_chromecast": false
  }
}
```

A receiver signed in to a different Spotify account is only claimed when it
supports the `authorization_code` ZeroConf flow (as Sonos does). Others, such as
SoundTouch (`accesstoken`), must be selected once from the Spotify app using the
bridge's account.

## Settings

```json
"bridge_playback": {
  "enabled": true,
  "relay": true,
  "stream_port": 8404,
  "stream_host": null
}
```

`stream_host` overrides the LAN address put in relay URLs (the radio relay's
discovery is used otherwise).

## Adding another provider

The queue and routes are provider-neutral:

- `tools/media_sources.py` defines a `MediaSource`: `owns(uri)`, `available()`,
  `expand(uri, kind)` into `QueueEntry` rows, and optionally
  `open_audio(entry)` for the relay route.
- `tools/spotify_media.py` is the Spotify source.
- `tools/bridge_player.py` holds the routes. A provider with its own native
  speaker protocol can add a route next to `ConnectRoute`; anything with audio
  works on the relay route without one.

## Limits

- Home Assistant polls some speakers slowly (SoundTouch about every 10 s) and
  may not report a position at all. On the Connect route the bridge shows the
  state it commanded until the speaker's report catches up, counts the position
  itself from the track start or seek point, and only ends the session after the
  speaker has reported something else for 30 seconds (or never reported a queued
  track within 60 seconds of Play). Measured on a Wave SoundTouch: position
  within about 2 seconds of the speaker through seek, pause and resume.
- Spotify on a SoundTouch ignores the start position in a play request when it
  already holds that track, so the bridge always follows Play with a seek.
- With Spotify Autoplay on (the provider's `autoplay` setting), Connect playback
  continues past the end of the queue with related tracks, as on Sonos. Adding
  to the queue while Autoplay plays does not interrupt it; Play Now does. After
  Next, the new title can take up to one Home Assistant poll (~10 s on a
  SoundTouch) to appear; the position counts from the skip.
- On R13 firmware, Now Playing shows the Autoplay icon for these rooms too: an
  outline infinity sign while the Connect route plays the queue with Autoplay on,
  and a filled one once the speaker plays past the end of the queue
  (`CR100_SpotifyAutoplayNow`, see the firmware's `docs/ui-events.md`).
- When Home Assistant reports a speaker as `unknown` (a SoundTouch left with no
  source), the bridge queue still accepts music and plays it through Connect.
- Each room's bridge queue and Connect session are saved to
  `/var/lib/cr100-bridge/bridge-queues.json` (`bridge_playback.state_file`), so a
  bridge restart, update or settings save resumes control of what is playing
  without a new Play.
- After Play, Seek, Next and Previous the room reports TRANSITIONING, then
  PLAYING about 2 seconds later, as a Sonos does. The CR100 restarts its
  progress bar from that second event; it does not poll position otherwise.
- The bridge signs in to Spotify playback at startup, so the first Play does
  not wait for it (about 9 seconds on a Raspberry Pi 3).
- The Connect route sends a fixed track list. Adding or removing tracks while it
  plays restarts the list in place at the current position.
- Relay playback is not synchronised between speakers, and Pause depends on how
  the speaker treats a live HTTP stream (the bridge reconnects from the saved
  position if the speaker drops it).
- Relay Seek (and resuming after a dropped stream) decodes the track from its
  start: on a Raspberry Pi expect about one second of delay per 12 seconds of
  offset. Measured on the bridge Pi: librespot plus FFmpeg produced a 5:37 track
  in 97 seconds, comfortably faster than real time.
- Home Assistant media-browser items and line-in are not queued yet on
  non-Sonos rooms; they still play directly.
