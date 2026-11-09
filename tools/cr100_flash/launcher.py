"""Generate the RAM-only, state-guarded stock-updater launcher."""

from __future__ import annotations

from ipaddress import ip_address


def build_launcher(bank: int, generation: int, bind_ip: str, port: int = 18091) -> bytes:
    """Return a POSIX-sh script pinned to the boot state captured in this run.

    A launcher from an earlier flash cannot be reused: the bank and generation
    change after every successful installation. Only numeric and validated
    values are interpolated into shell code.
    """
    if bank not in (0, 1) or not 1 <= generation <= 0xFFFFFFFE:
        raise ValueError("unsupported boot section or generation")
    address = ip_address(bind_ip)
    if address.version != 4 or not 1 <= port <= 65535:
        raise ValueError("expected an IPv4 Pi address and TCP port")
    script = f'''#!/bin/sh
# Generated for boot section {bank}, generation {generation}. RAM-only staging.
umask 077
LOG=/tmp/upgrade.log
STATE=/tmp/lab-upgrade.phase
STOP=/var/run/stopanacapa
KEY=/tmp/lab.pub
URL=http://{address}:{port}/firmware.upd
INETD_CONF=/tmp/cve-inetd-clean.conf

: >"$LOG" || exit 39
exec </dev/null >>"$LOG" 2>&1
echo "LAB_UPGRADE_LAUNCH"

recover() {{
    echo "LAB_UPGRADE_RECOVERY"
    rm -f "$STOP"
    if ! pidof udhcpc >/dev/null 2>&1; then
        /sbin/udhcpc -s /etc/dhcp.script -i ath0 -w ath0 -h SonosCR \\
            -d access.bestbuy.com -p /tmp/udhcpc.pid </dev/null >/tmp/lab-udhcpc.log 2>&1 &
    fi
    if [ -f "$INETD_CONF" ] && ! pidof inetd >/dev/null 2>&1; then
        /usr/sbin/inetd "$INETD_CONF"
    fi
    echo "recovered" >"$STATE"
    if grep ' /jffs ' /proc/mounts >/dev/null 2>&1; then
        cp "$LOG" /jffs/lab-upgrade-failure.log
    fi
}}

if [ ! -c /dev/mtd/0 ] || [ ! -x /bin/upgrade ] || [ ! -f "$KEY" ] || \\
   [ "$(wc -c <"$KEY")" -ne 260 ] || [ -f /var/run/upgradeflag ] || \\
   [ -f "$STOP" ] || [ -e "$STATE" ] || \\
   [ "$(cat /proc/sys/kernel/bootsection)" != {bank} ] || \\
   [ "$(cat /proc/sys/kernel/bootgeneration)" != {generation} ] || \\
   pidof upgrade >/dev/null 2>&1; then
    echo "LAB_UPGRADE_ABORT: preflight state changed"
    exit 40
fi
echo "preflight" >"$STATE"
touch "$STOP" || exit 41
echo "stopping-app" >"$STATE"
/opt/bin/hh.anacapactl stop &
stop_pid=$!
elapsed=0
while kill -0 "$stop_pid" >/dev/null 2>&1 && [ "$elapsed" -lt 30 ]; do
    sleep 1
    elapsed=$((elapsed + 1))
done
if kill -0 "$stop_pid" >/dev/null 2>&1; then
    kill "$stop_pid" >/dev/null 2>&1
    echo "LAB_UPGRADE_ABORT: app stop timed out"
    recover
    exit 42
fi
wait "$stop_pid"
stop_rc=$?
if [ "$stop_rc" -ne 0 ] || pidof hh.anacapad >/dev/null 2>&1; then
    echo "LAB_UPGRADE_ABORT: app still running or stop failed, rc=$stop_rc"
    recover
    exit 43
fi

echo "installing" >"$STATE"
echo "Boot section $(cat /proc/sys/kernel/bootsection), generation $(cat /proc/sys/kernel/bootgeneration)"
/bin/upgrade -f -K "$KEY" "$URL"
upgrade_rc=$?
echo "LAB_UPGRADE_EXIT=$upgrade_rc"
# A successful install reboots before this line. A returned updater is a
# failure even if its status is zero; preserve logs and recover the UI.
recover
if [ "$upgrade_rc" -eq 0 ]; then exit 44; fi
exit "$upgrade_rc"
'''
    return script.encode("ascii")
