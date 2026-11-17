#!/bin/bash
# Raspberry Pi / Kali installer. Also works via: curl <raw-install-url> | sudo bash -s -- --repo <https-url> --ref <tag-or-sha>
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [--source DIR] [--no-start]
       curl <raw-install-url> | sudo bash -s -- --repo <https-url> [--ref <tag-or-sha>]
       ./install.sh --check

Supports Raspberry Pi 4 Model B and 3 Model B+, 64-bit Kali, systemd, and an upstream
connection that does not use wlan0. Onboard wlan0 is reserved for the CR100.
--check       Read-only platform/network preflight; no installation.
--no-start    Install and enable services without starting them now.
--source DIR  Install a local source tree (default: directory containing this file).
--repo URL    Fetch a Git repository when using the streamed installer.
--ref REF     Branch, tag or commit to fetch (default: main).
EOF
}
die() { echo "Error: $*" >&2; exit 1; }
SOURCE='' REPO='' REF=main CHECK=0 START=1
while (($#)); do
  case "$1" in
    --source|--repo|--ref)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in --source) SOURCE=$2 ;; --repo) REPO=$2 ;; --ref) REF=$2 ;; esac
      shift 2 ;;
    --check) CHECK=1; shift ;;
    --no-start) START=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ -z "$SOURCE" || -z "$REPO" ]] || die 'Choose --source or --repo.'
[[ $(uname -s) == Linux ]] || die 'Only Kali Linux on a supported Raspberry Pi is supported.'
# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == kali ]] || die 'Only Kali is supported. Raspberry Pi OS support is planned separately.'
[[ $(uname -m) == aarch64 ]] || die 'Use a 64-bit Kali image (aarch64).'
# Early gate for the streamed installer, which runs before any source exists.
# tools/radio_platform.py is the registry; test_install.py keeps this list equal to it.
SUPPORTED_BOARDS='raspberrypi,4-model-b raspberrypi,3-model-b-plus'
[[ -r /proc/device-tree/compatible ]] || die 'This machine has no device tree; a supported Raspberry Pi is required.'
MODEL=$(tr -d '\0' < /proc/device-tree/model)
BOARD=$(tr '\0' '\n' < /proc/device-tree/compatible | head -n 1)
[[ " $SUPPORTED_BOARDS " == *" $BOARD "* ]] || die "Unsupported board: $MODEL ($BOARD)"
[[ -d /run/systemd/system ]] || die 'Boot Kali with systemd before installing.'
command -v ip >/dev/null || die 'Install iproute2 before running preflight.'
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die 'Python 3.11 or newer is required.'

check_uplink() {
  local route device has_uplink=0 client=${SSH_CONNECTION%% *}
  while IFS= read -r route; do
    device=$(awk '{for (i=1;i<=NF;i++) if ($i=="dev") print $(i+1)}' <<< "$route")
    [[ -z "$device" || "$device" == wlan0 ]] || has_uplink=1
  done < <(ip -4 route show default)
  ((has_uplink)) || die 'Connect Ethernet (or a second adapter) first; wlan0 is needed for the CR100.'
  if [[ -n "$client" ]]; then
    route=$(ip route get "$client")
    [[ " $route " != *' dev wlan0 '* ]] || die 'This SSH connection uses wlan0. Reconnect using the wired/second-adapter address first.'
  fi
  if ip -4 route show default | awk '{for (i=1;i<=NF;i++) if ($i=="dev" && $(i+1)=="wlan0") found=1} END {exit !found}'; then
    die 'A default route still uses wlan0. Disconnect its Wi-Fi connection before installing.'
  fi
  python3 - <<'PY'
import ipaddress, json, subprocess
network = ipaddress.ip_network('192.168.69.0/24')
for route in json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'table', 'main'])):
    dst = route.get('dst', 'default')
    if dst != 'default' and route.get('dev') != 'sonos0' and ipaddress.ip_network(dst, strict=False).overlaps(network):
        raise SystemExit('The upstream network overlaps 192.168.69.0/24. Change the upstream subnet first.')
PY
}
SSH_CONNECTION=${SSH_CONNECTION:-}
check_uplink
echo "Platform preflight passed: $MODEL / Kali / aarch64"
((CHECK)) && exit 0
((EUID == 0)) || die 'Run with sudo: sudo ./install.sh (or pipe to sudo bash).'
exec 9>/run/cr100-bridge-install.lock
flock -n 9 || die 'Another CR100 installer is running.'
if [[ -z "$REPO" ]]; then
  if [[ -z "$SOURCE" ]]; then
    SCRIPT=${BASH_SOURCE[0]:-}
    [[ -n "$SCRIPT" && -f "$SCRIPT" ]] || die 'For a piped installer, supply --repo https://... and optionally --ref.'
    SOURCE=$(cd "$(dirname "$SCRIPT")" && pwd)
  fi
  SOURCE=$(cd "$SOURCE" && pwd)
  [[ -f "$SOURCE/tools/distribution.py" ]] || die 'The source is not a complete CR100 bridge checkout.'
else
  [[ "$REPO" == https://* ]] || die '--repo must use HTTPS.'
fi

TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git python3 python3-venv python3-pip iproute2 iw \
  dnsmasq-base libpcap0.8t64 ethtool rfkill kmod dkms build-essential ffmpeg tzdata
if [[ -n "$REPO" ]]; then
  git init -q "$TEMP/source"
  git -C "$TEMP/source" remote add origin "$REPO"
  git -C "$TEMP/source" fetch --depth=1 origin "$REF"
  git -C "$TEMP/source" checkout -q --detach FETCH_HEAD
  SOURCE=$TEMP/source
fi
[[ -f "$SOURCE/tools/distribution.py" ]] || die 'The source is not a complete CR100 bridge checkout.'

# The source's registry says what this board's radio needs: packages, DKMS module, firmware patch.
radio_field() { python3 "$SOURCE/tools/radio_platform.py" --board "$BOARD" --field "$1"; }
RADIO=$(radio_field radio) || die "This release does not support $MODEL ($BOARD)."
read -ra RADIO_PACKAGES <<< "$(radio_field packages)"
RADIO_DKMS=$(radio_field dkms_module)
RADIO_PATCH=$(radio_field firmware_patch)
echo "Radio: $(radio_field description) [$RADIO]"
# Do not change kernels automatically. DKMS needs headers for the kernel actually booted.
if [[ -n "$RADIO_DKMS" && ! -e /lib/modules/$(uname -r)/build/Makefile ]]; then
  apt-cache show "linux-headers-$(uname -r)" >/dev/null 2>&1 || \
    die "No headers for $(uname -r). Update Kali's kernel/headers, reboot, then rerun the installer."
  apt-get install -y "linux-headers-$(uname -r)"
fi
((${#RADIO_PACKAGES[@]} == 0)) || apt-get install -y "${RADIO_PACKAGES[@]}"
if [[ -n "$RADIO_DKMS" ]]; then
  dkms status | awk -v kernel="$(uname -r)" -v module="$RADIO_DKMS/" \
    'index($0, module) == 1 && index($0, kernel ",") && /: installed/ {found=1} END {exit !found}' || \
    die "The $RADIO_DKMS DKMS driver is not installed for the running kernel. Check dkms status."
fi
check_uplink

install -d -m 755 /opt/cr100-bridge /opt/cr100-bridge/releases
install -d -m 700 /var/lib/cr100-bridge
RELEASE=$(mktemp -d /opt/cr100-bridge/releases/release-XXXXXXXX)
python3 "$SOURCE/tools/distribution.py" --copy "$RELEASE"
chmod -R go-w "$RELEASE"
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install --disable-pip-version-check -r "$RELEASE/requirements.txt"
(cd "$RELEASE/tools" && "$RELEASE/.venv/bin/python" -c \
 'import ccmp, ha_client, sonos_backend, spotify_service, netstart_pair, scapy.all, zoneplayers; zoneplayers.load_peer_module(zoneplayers.DEFAULT_PEER_DIR)')

# Existing configuration, tokens, passwords and pairing state are never overwritten.
if [[ ! -e /var/lib/cr100-bridge/bridge-config.json ]]; then
  install -m 600 "$RELEASE/config/bridge-config.example.json" /var/lib/cr100-bridge/bridge-config.json
fi
PAIR_STATE=$(systemctl show -p ActiveState --value cr100-pair.service 2>/dev/null || true)
if [[ "$PAIR_STATE" == active || "$PAIR_STATE" == activating || "$PAIR_STATE" == deactivating ]]; then
  die 'Pairing is in progress. Wait for it to finish and rerun installation.'
fi
# Patch only the exact supported package image, retaining its alternatives links
# and a private original backup. Running firmware is verified by the radio service.
[[ -z "$RADIO_PATCH" ]] || "$RELEASE/.venv/bin/python" "$RELEASE/$RADIO_PATCH" --apply
systemctl stop cr100-bridge.service cr100-webui.service cr100-radio.service 2>/dev/null || true
OLD_RELEASE=$(readlink -e /opt/cr100-bridge/current 2>/dev/null || true)   # -e: empty on a first install
ln -s "$RELEASE" /opt/cr100-bridge/.current-new
mv -Tf /opt/cr100-bridge/.current-new /opt/cr100-bridge/current
# Retain recent releases and both live/rollback targets; ignore unrelated paths.
python3 - "$RELEASE" "$OLD_RELEASE" <<'PY'
from pathlib import Path
import shutil
import sys
release = Path(sys.argv[1]).resolve()
root = release.parent
protected = {release, (root.parent / 'current').resolve()}
if sys.argv[2]: protected.add(Path(sys.argv[2]).resolve())
candidates = sorted((path for path in root.iterdir()
                     if path.name.startswith('release-') and not path.is_symlink() and path.is_dir()
                     and path.resolve() != release), key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
protected.update(path.resolve() for path in candidates[:2])
for path in candidates:
    if path.resolve() not in protected: shutil.rmtree(path)
PY
install -m 755 "$RELEASE/scripts/cr100-bridge" /usr/local/bin/cr100-bridge
install -m 644 "$RELEASE"/systemd/*.service /etc/systemd/system/
install -d -m 755 /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-cr100-bridge.conf <<'EOF'
[device-cr100-bridge]
match-device=interface-name:wlan0;interface-name:mon0;interface-name:sonos0
managed=0
EOF
if systemctl is-active --quiet NetworkManager; then
  nmcli general reload
  nmcli device set wlan0 managed no
fi
systemctl daemon-reload
systemctl enable cr100-webui.service cr100-bridge.service
if [[ -n "$OLD_RELEASE" ]]; then
  printf 'Previous application release retained at %s\n' "$OLD_RELEASE"
fi
if ((START)); then
  systemctl restart cr100-webui.service
  # Readiness is an HTTP check, not just systemd's process-start result.
  python3 - <<'PY'
import time, urllib.request
for attempt in range(50):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/api/session', timeout=1) as response:
            if response.status == 200: break
    except OSError: time.sleep(0.2)
else: raise SystemExit('Web UI did not start. Run: sudo journalctl -u cr100-webui.service -n 50')
PY
  if systemctl start cr100-radio.service; then
    systemctl start cr100-bridge.service
  else
    echo 'Web UI is ready. Radio setup needs attention; a reboot may be required for the new driver/firmware.'
    echo 'After reboot, services start automatically. Diagnose with: sudo cr100-bridge logs'
  fi
fi
ip -o -4 addr show scope global | awk '$2 != "wlan0" && $2 != "mon0" && $2 != "sonos0" {split($4,a,"/"); print "Web UI: http://" a[1] ":8080"}'
echo 'Set your web password, choose the speaker source, select speakers, then pair from Controllers.'
echo 'CLI pairing: sudo cr100-bridge pair'
