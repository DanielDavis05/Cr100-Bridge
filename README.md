# CR100 Bridge

Use a Sonos CR100 controller with modern Sonos speakers through Home Assistant.
A Raspberry Pi presents a SonosNet link and virtual rooms to the CR100. Home
Assistant supplies the real speakers, playback controls, and music library.

This repository contains the runtime, virtual player implementation, protocol
fixtures, web interface, installer, and systemd units. An emulator checkout,
Sonos firmware image, and the author's configuration are not needed.

## Supported installation

- **Raspberry Pi 4 Model B, 64-bit Kali Linux, systemd, Python 3.11 or newer.**
- Ethernet or a separate network adapter for the home network and internet.
  The built-in `wlan0` is dedicated to the CR100; ordinary Wi-Fi on that radio
  cannot be the installation/SSH connection or upstream connection.
- Internet access to Kali's package repositories and PyPI; root/sudo access.
- Home Assistant with the desired media players. For Sonos queue/library features,
  configure the Sonos integration in Home Assistant first.

Raspberry Pi OS, 32-bit Kali, and other Pi models are not supported by this installer.
The recorded working radio setup is Pi 4/Kali 2026.3, kernel
`6.12.34+rpt-rpi-v8`, `brcmfmac-nexmon-dkms 6.12.2`, and Nexmon firmware 7.45.206.
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
virtual environment. It does not perform a full OS/kernel upgrade.

If matching headers are unavailable, update Kali's kernel and headers, reboot,
and rerun. If newly installed firmware is not loaded, the installer starts the
web UI and reports the radio failure. Reboot to load the new driver/firmware;
the enabled services will start automatically. It never reboots the Pi itself.

Open the printed **`http://<pi-address>:8080`** URL from your home network:

1. Set the web interface password.
2. Add your Home Assistant URL and long-lived access token under Home Assistant.
3. Choose the speakers to show on the CR100 under Speakers.
4. Under Controllers, choose **Pair controller**, then put the CR100 into its
   add-to-household setup mode. Pairing waits up to three minutes and the bridge
   starts automatically after success. The radio supports one controller.

The web UI runs before pairing and stays available if the bridge/radio fails.
This is a headless setup: the installer prints the URL; it does not launch a
desktop browser. The web interface is intended for a trusted home network.

The isolated controller network uses `192.168.69.0/24`. An upstream network using
that subnet is rejected. DHCP is bound to `sonos0` and advertises no DNS server
or default gateway; the home network's DHCP/DNS configuration is untouched.

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

```sh
sudo cr100-bridge status
sudo cr100-bridge logs
sudo cr100-bridge pair       # blocks until pairing succeeds or times out
sudo cr100-bridge restart
sudo cr100-bridge stop
```

A failed pairing attempt retains the previous pairing and restarts that bridge.
Replacing a successful pairing creates a new household/key. No default network
key, live controller state, HA token, or Spotify credential is shipped.

## Updates and existing manual installations

Rerun `sudo ./install.sh` from the newer checkout. Dependencies and imports are
checked in a new release directory before switching the application symlink.
Existing configuration, passwords, tokens, and pairing state are retained.
Previous releases remain under `/opt/cr100-bridge/releases/` for recovery.
Only one installer may run at a time; do not update during pairing.

For an existing manual setup, stop its launcher before starting these services.
The installer deliberately does not import `/home/kali` configuration or secrets.
To retain an existing pairing/configuration, install with `--no-start`, then copy
the intended files into `/var/lib/cr100-bridge/` as root. Update token paths in
`bridge-config.json` (relative paths resolve beside that file), apply mode 0600,
and start `cr100-webui.service` and `cr100-bridge.service`. Do not run the old
launcher and installed services together.

For Spotify, enable it under Spotify, supply your own Spotify app's Client ID,
register `http://127.0.0.1:8888/callback` as its redirect URI, then use Sign in.
Follow the UI's instruction to paste the redirected address back into the page.
Sonos still needs a Spotify account linked through the Sonos app for playback.

## Development and portable archive

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -q
.venv/bin/python tools/test_link.py
python3 tools/distribution.py --archive /tmp/cr100-bridge.tar.gz
```

The archive uses an explicit file list and excludes local configuration, keys,
tokens, research captures, logs, and caches. Extract it and run `install.sh` on
the Pi. The same builder prepares the installed release. Tests use local fake
Home Assistant/Spotify services. An optional private radio capture regression
is skipped when its capture is absent; synthetic link tests still run.

The bundled player under `tools/zp80/` originated in the local CR100 emulator
project. It includes the software player, Netstart helper, synthetic public key,
and UPnP XML/image fixtures needed at runtime; no emulator is launched.
