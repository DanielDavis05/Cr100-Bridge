# Bridge troubleshooting

Symptoms seen on real hardware, what caused them, and what to do. Run the
[health checks](health-checks.md) first; they tell you which layer is broken.

```sh
PI=kali@<pi-address>
CR=192.168.69.52
```

Useful logs:

```sh
ssh $PI 'sudo journalctl -u cr100-bridge -u cr100-radio -u cr100-webui --since "1 hour ago" --no-pager -o short-iso | tail -100'
ssh $PI 'sudo cr100-bridge logs'      # follow all bridge services live
```

## Controller shows "Searching for Sonos components"

Work down this list. Each item rules out a layer.

1. **Is the radio alive?** Check the Nexmon firmware (below). A crashed firmware
   leaves `cr100-bridge` "active" with no working radio.
2. **Is the controller associated and exchanging traffic?** Look at
   `/run/cr100-bridge/link.jsonl` and `ping $CR` from the Pi. If it isn't
   associated, wake it: a sleeping CR100 doesn't search on its own after a bridge
   restart.
3. **Associated, but discovery fails?** Run
   `curl --interface 192.168.69.1 -m 5 http://$CR:3400/status/uptime` from the Pi.
   If that times out while radio traffic flows, look at transmit-queue
   interference:
   - **A paired controller that is switched off.** The bridge keeps sending
     reconnect probes to it, and on unpatched firmware those probes delayed beacons
     and knocked the working controller off. The installed Nexmon patch sends
     probes without ACK retries and stamps beacons in hardware, which fixed this in
     testing. If it comes back, use **Pause reconnect attempts** on the absent
     controller's card (Controllers page) or
     `sudo cr100-bridge controller-search <mac> pause`. Pausing keeps the pairing
     and applies without a radio restart. Resume before using that controller again.
   - **Firmware power saving.** The radio service turns the firmware's MPC
     setting off at startup and after every firmware reload, separately from
     Linux `power_save`. `status.py` and the radio service log show whether it is
     verified.
4. **Link fine, but no rooms?** If Rooms shows only **CR100 Bridge**, no allowed
   speaker is reachable. See "Speaker shown as unavailable" below, and check the
   controller's component selection on the Controllers page (an empty selection
   hides everything).

Avoid rebooting the controller over and over to "fix" this. A reboot can clear
it, but it also erases the state you need to measure. Capture `link.jsonl`,
`/status/dmesg` and `/status/getassoc` first.

## Nexmon firmware crashes

Signs: `dmesg` on the Pi shows `Firmware has halted or crashed`, `wlan0`/`mon0`
disappear, and the journal shows "Network is down". One boot recorded hundreds
of crashes.

- Each crash leaves a dump in `/sys/class/devcoredump`.
- Recover with `sudo systemctl restart cr100-radio cr100-bridge`, or reboot the
  Pi if the interfaces don't come back.
- Earlier crash causes were bursts of unacknowledged unicast frames (four or
  more back to back crashed the firmware) and a use-after-free while receiving.
  The link paces transmissions to avoid the first. If crashes come back after a
  change, suspect new burst traffic first.
- Burst-injection tools (for example `aireplay-ng -9`) crash the firmware and
  renumber the phy. Don't run them on the bridge radio.

## Speaker shown as unavailable when it isn't

A speaker that answers slowly can look unavailable. An S1 ZP80 sometimes takes
about 5 s to answer `GetPositionInfo`. With the old 3 s timeout the bridge
dropped the room, reloaded the players and left controllers searching. The
read timeout is now 8 s (`sonos_backend.READ_TIMEOUT`).

Check from the Pi:

```sh
ssh $PI 'curl -sS -m 10 -o /dev/null -w "%{http_code} %{time_total}s\n" http://<speaker-ip>:1400/xml/device_description.xml'
ssh $PI 'sudo journalctl -u cr100-bridge --since "30 min ago" -o cat | grep -iE "unavailable|timeout|reload" | tail'
```

Other Sonos-backend quirks:

- GENA events from old players arrive 2–8 s late and merged, so the backend
  re-reads state after each action. A short delay after a button press is normal.
- Transport actions must go to the group coordinator: a group member reports
  PLAYING even when it isn't in charge.
- Radio streams have no Pause; the backend sends Stop.

## Web UI is slow

The Pi is rarely the bottleneck. Speaker pages wait synchronously for discovery
and status reads, and they share one lock, so one slow speaker slows every page.
Check `uptime` and the speaker response times before suspecting load.

## New controller menus don't appear

- Menus a CR100 has already loaded can stay cached until its next discovery
  refresh. Leave it awake for a minute or reboot it once.
- Room Settings, Bridge Diagnostics, Spotify track actions and the Autoplay mode
  need the custom controller firmware. A stock controller keeps the older
  **Music > Speaker Settings** workaround, which is off by default.
- Check that the controller's component selection includes the room.

## A controller firmware update fails

See the CFW project's [shipping guide](../../../cr100-firmware/docs/guides/shipping-a-release.md#if-it-fails).
In short: read `/status/jffs/upgrade.log` on the controller and the update record
on the Controllers page. A connection error before any download happened once
when updating from a release with an old TLS retry bug; retrying from the newer
release fixed it.

## Pairing

- Pairing happens on channel 1, then the controller moves to the bridge channel.
  `sudo cr100-bridge pair` waits up to three minutes. Put the CR100 in its
  add-to-household setup mode first.
- A failed attempt keeps the previous pairing and restarts the bridge.
- Keep `/var/lib/cr100-bridge/bridge-state.radio-pn.json` with the pairing state.
  Losing it causes nonce reuse and replay drops. If it's corrupt, the radio
  refuses to start rather than encrypt with a reused counter.

## Developer gotchas

- Injected frames need a radiotap header of length 8. Length 0 is silently dropped.
- Beacons must stay on the TSF grid, or a dozing CR100 misses them and declares
  the AP lost after 2 s.
- A searching CR100 sends no probe requests. It only picks an AP from unicast
  probe responses, so the bridge must probe it while it is unassociated.
- After `webui.py` reloads, its command line is just `python3`. Find it with
  `ss -ltnp | grep :8080`, not `pgrep`.
- Monitor-mode captures on the Pi include locally echoed injected frames. Their
  timestamps don't show when a frame actually went on air.
