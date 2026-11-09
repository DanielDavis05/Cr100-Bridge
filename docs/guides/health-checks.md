# Bridge health checks

A quick top-to-bottom check of a running bridge: Pi, radio, controller link,
speakers, then the controller itself. Every command here is read-only.

```sh
PI=kali@<pi-address>
CR=192.168.69.52     # controller address on the bridge network (see leases below)
```

The CR100 network is `192.168.69.0/24` on interface `sonos0`. The bridge is
`192.168.69.1`, and controllers usually lease `.52` and up.

## 1. Services and release

```sh
ssh $PI 'readlink -f /opt/cr100-bridge/current
  systemctl is-active cr100-webui cr100-radio cr100-bridge
  sudo systemctl show cr100-bridge cr100-radio cr100-webui -p Id -p ActiveState -p NRestarts'
```

Healthy: all three `active`, and `NRestarts` not climbing between checks.
`cr100-pair` is normally inactive. `sudo cr100-bridge status` shows the same in
long form.

## 2. Radio and Nexmon firmware

```sh
ssh $PI 'iw dev | grep -E "Interface|type"
  sudo dmesg | grep -cE "Firmware has halted|firmware crashed" 
  ls /sys/class/devcoredump 2>/dev/null
  sudo /opt/cr100-bridge/current/.venv/bin/python /opt/cr100-bridge/current/firmware/nexmon-ownership/status.py'
```

Healthy:

- `wlan0` and `mon0` both exist.
- The crash count stays at 0 and doesn't grow. **A firmware crash removes
  `wlan0`/`mon0` while `cr100-bridge` keeps running and retrying**, so systemd
  can show it as active while the link is dead.
- `status.py` shows the patched firmware version. Its ownership counters show
  injections; allocation failures should stay flat between two readings a few
  seconds apart.

## 3. Controller link

```sh
ssh $PI 'sudo tail -n 3 /run/cr100-bridge/link.jsonl
  cat /var/lib/misc/dnsmasq.leases
  ip -s link show sonos0 | tail -4'
```

`link.jsonl` is the bounded radio status log. Look for each controller's
association state and recent events (`link-*`, `associated`, `helper-exited`,
`helper-restarted`). The live journal shows periodic `"event": "stats"` lines:

```sh
ssh $PI 'sudo journalctl -u cr100-bridge --since "5 min ago" -o cat | grep "\"event\": \"stats\"" | tail -1'
```

## 4. Controller reachability (from the Pi)

The CR100 serves its own diagnostics on port 3400. Query it from the Pi, which
is the only host on its network:

```sh
ssh $PI "ping -c2 -W2 $CR | tail -1
  curl --interface 192.168.69.1 -sS -m 5 http://$CR:3400/status/uptime | sed 's/<[^>]*>//g' | tail -2
  curl --interface 192.168.69.1 -sS -m 5 http://$CR:3400/status/hh | head -c 600"
```

`/status/hh` reports the controller's MAC and software version. Other useful pages:

| Page | What it shows |
| --- | --- |
| `/status/getassoc` | Association and recent beacon RSSI history |
| `/status/dmesg` | Kernel log, including `Lost connectivity to AP` beacon-loss events |
| `/status/opt/log/anacapa.trace` | UI application trace |
| `/status/jffs/upgrade.log` | Last firmware update result |
| `/status/jffs/localsettings.txt` | Saved settings (household, nag counters) |
| `/status/ps` | Running processes |

The controller's dmesg keeps a **fixed number** of beacon-loss entries. To spot
new drops, compare the latest entry's timestamp between readings, not the count.

## 5. Speakers

```sh
ssh $PI 'sudo cat /run/cr100-bridge/players-status.json | head -c 1500'
ssh $PI 'curl -sS -m 8 -o /dev/null -w "%{http_code} %{time_total}s\n" http://<speaker-ip>:1400/xml/device_description.xml'
```

`players-status.json` shows each virtual room and whether its backend is
reachable. Old S1 players can take around 5 s to answer some reads; the backend
allows 8 s.

## 6. On the controller

The last check is the one that matters. Wake the CR100 and confirm:

1. Rooms lists your speakers. If it shows only **CR100 Bridge**, the link is up
   but none of that controller's speakers are reachable (see
   [troubleshooting](troubleshooting.md)).
2. A menu (Music, Now Playing) opens and responds.

Association or a green web UI is not proof. Sleeping controllers ignore the
first key press, which only wakes them.

## Web UI

`http://<pi-address>:8080` on your home network shows speaker source health,
each controller's status and events, and update state. It runs independently of
the radio, so it stays up when the radio fails.
