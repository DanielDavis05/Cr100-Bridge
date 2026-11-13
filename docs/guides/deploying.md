# Deploying and updating the bridge

How to get a change from your checkout onto the Pi, and how to back out.
For a first install on a fresh Pi, see [Supported installation](../../README.md#supported-installation).

The examples use two shell variables. Set them once:

```sh
PI=kali@<pi-address>          # the Pi's home-network address, key-based SSH
```

## 1. Test locally

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt   # first time only
.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -q
.venv/bin/python tools/test_link.py
```

The full suite takes a few minutes. `test_link.py` also runs a private radio-capture
regression when `research/bridge-captures/` is present (see `tools/workspace.py`);
otherwise it skips that part.

## 2. Build the archive

```sh
.venv/bin/python tools/distribution.py --archive /tmp/cr100-bridge.tar.gz
shasum -a 256 /tmp/cr100-bridge.tar.gz
```

The archive is built from an explicit file list (`tools/distribution.py`), so a
new runtime module must be added to `RUNTIME` there or it will not ship. Local
configuration, tokens, captures and caches are never included. The archive embeds
a build stamp (commit, dirty flag) that the web UI and the controller's About
screen show, so commit first if you want a clean stamp.

## 3. Install on the Pi

```sh
scp /tmp/cr100-bridge.tar.gz $PI:/tmp/
ssh $PI 'rm -rf /tmp/cr100-bridge-src && mkdir /tmp/cr100-bridge-src && tar -xzf /tmp/cr100-bridge.tar.gz -C /tmp/cr100-bridge-src'
ssh -t $PI 'cd /tmp/cr100-bridge-src/cr100-bridge && ./install.sh --check && sudo ./install.sh'
```

What the installer does on an update:

- Builds a new release directory under `/opt/cr100-bridge/releases/`, checks its
  imports and dependencies, then switches the `/opt/cr100-bridge/current` symlink.
- Keeps `/var/lib/cr100-bridge/` (config, pairing key, CCMP packet-number file,
  tokens, web password) untouched.
- Restarts the services. Add `--no-start` to install without restarting.

Before you run it:

- **Don't update during pairing**, and only run one installer at a time.
- **A restart resets the radio link.** An awake CR100 reconnects within seconds;
  a sleeping one stays unassociated until someone wakes it. Changes that touch only
  config, players or the web UI usually don't need a radio restart: the web UI
  applies config by reloading the player service in place.
- If the install reports that a newly patched Nexmon firmware is not loaded yet,
  reboot the Pi once. The installer never reboots by itself.

## 4. Check it worked

Run the [health checks](health-checks.md). At minimum:

```sh
ssh $PI 'readlink -f /opt/cr100-bridge/current; systemctl is-active cr100-webui cr100-radio cr100-bridge'
```

Then confirm on the physical controller that Rooms lists your speakers and a menu
opens. A green status page alone does not prove the controller can discover rooms.

## Rolling back

The installer keeps the new release and the two most recently modified other
release directories under `/opt/cr100-bridge/releases/`. It also protects the
current and previous release targets, even when they are older. To go back:

```sh
ssh $PI 'ls -1t /opt/cr100-bridge/releases/ | head'
ssh $PI 'sudo ln -sfn /opt/cr100-bridge/releases/<previous-release> /opt/cr100-bridge/current && sudo cr100-bridge restart'
```

Or reinstall an older archive with `sudo ./install.sh`. Either way the data
directory is shared, so a rollback keeps pairing and settings. Before risky
changes, copy the data directory:

```sh
ssh $PI 'sudo cp -a /var/lib/cr100-bridge /var/lib/cr100-bridge.bak-$(date +%Y%m%dT%H%M)'
```

## Old manual launcher

Early test setups ran the bridge by hand from a home-directory tools folder.
That launcher is retired. **Never run it alongside the installed services**: both
would drive the same radio.
