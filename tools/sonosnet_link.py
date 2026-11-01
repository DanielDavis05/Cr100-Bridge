#!/usr/bin/env python3
"""SonosNet link for a paired CR100, presented to Linux as a TAP interface.

The radio side is a userspace AP on a nexmon monitor interface: beacons with a
TIM, probe responses while the CR100 searches, association responses, and CCMP
(KeyID 2) data in both directions. Each data frame tunnels a whole Ethernet frame
under SNAP ethertype 0x6969 (STATIC-FINDINGS §5), so the TAP side carries the
CR100's own Ethernet traffic: ARP, DHCP, SSDP, HTTP. Anything later (a DHCP
server, a UPnP player surface, a bridge to the LAN) attaches to the TAP.

Power save follows the ZP80 driver (handle_ps_poll, handle_null_or_data,
infra_check_to_client_locked; STATIC-FINDINGS "CR100 link drops"): every frame goes
to the CR100 as unicast; while it dozes, frames wait and the beacon sets TIM bit
AID 1; each PS-Poll releases exactly one frame, with MoreData if more wait; a data
or Null frame with PM clear wakes it and releases the whole queue. Nothing is sent
to a dozing CR100 unprompted: probe responses go out only while unassociated.

Usage: sudo python3 sonosnet_link.py --state bridge-state.json --tap sonos0 --addr 192.168.69.1/24
"""
from __future__ import annotations

import argparse
import collections
import errno
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import ccmp
import frames

AID = 1
TUNSETIFF, IFF_TAP, IFF_NO_PI = 0x400454ca, 0x0002, 0x1000
ETH_P_ALL = 0x0003
# The CR100 can doze without saying so: send directly only this soon after its last frame
# with PM clear (covers replies to its own requests); anything later waits for TIM + PS-Poll.
AWAKE_TIMEOUT = 0.05
# Nexmon firmware crashes when 4+ frames reach it back-to-back while they go unacknowledged
# (reproduced 2026-10-31: bursts of 4, 5 and 9 to an absent station crashed it; bursts of 3
# every 5 ms ran clean even with the queue full). Never hand it more than this many frames
# inside the window.
# Live it still crashed about once a minute, at the same spot every time (coredumps,
# 2026-10-31): wlc_send_q dequeues a stale packet whose buffer was freed and refilled by
# the next host frame, while the firmware handles a frame from the CR100. Only frames
# waiting in the firmware's software queue can go stale, so hand it one frame at a time,
# spaced past a full-size frame's airtime at TX_RATE_MBPS (~0.5 ms), so the queue stays empty.
PACE_FRAMES, PACE_WINDOW = 1, 0.0015
# Unicast frames to a dozing CR100 go unacknowledged and are retried, and nexmon queues
# beacons behind them. A late beacon carries a stale timestamp, the CR100 sets its wake
# timers from it, sleeps through the next beacons and drops the link after 2 s
# (2026-11-01). So, like the ZP80, never probe a CR100 that still hears us: answer its probe
# requests, and send unsolicited probe responses this often only while it is unassociated
# or searching. A searching CR100 sends no probe requests and picks its AP only from
# unicast probe responses (mobile_handle_probe_resp, state 1; broadcast ones are ignored),
# hopping channels: at 1 s it never found the bridge.
SEARCH_PROBE_INTERVAL = 0.1
# Telling a searching CR100 from a dozing one: a dozing CR100 still wakes for beacons and
# answers its TIM bit with a PS-Poll (mobile_handle_beacon). After this much silence the
# beacons set the TIM bit for CHECK_WINDOW; no frame back in that time means it no longer
# hears the beacons, so it is searching. Queued frames waiting that long mean the same.
# Live (2026-11-01) every alarm was a real loss (an association request followed), and the
# CR100 sits on "searching" until the check ends, so keep the silence short. The window must
# be well past its slowest TIM answer (13 beacons seen): with 1.5 s a dozing CR100 was taken
# for a searching one, and the probe responses that followed knocked it off within ~5 s.
CHECK_SILENCE = 4.0
CHECK_WINDOW = 3.0
# The CR100 sets its doze wake-ups from the timestamp inside each beacon it hears
# (_extract_bs_from_beacon). A ZP80's hardware writes that timestamp as the beacon goes on
# air; ours is written in software before the firmware queues it, so any air-time delay
# (contention on channel 6, a retry ahead of it) makes it stale and the CR100 then wakes
# late and sleeps through later beacons: the idle drops of 2026-11-01. Stamping this far
# ahead turns a delay into the CR100 waking slightly early instead. Experiment: compare the
# rate of cr100-searching alarms with --tsf-lead-us 0.
TSF_LEAD_US = 2000
QUEUE_TTL = 3.0         # drop frames the CR100 never polled for
QUEUE_LIMIT = 256
# The ZP80 driver keeps a silent station associated indefinitely; the CR100 is quiet for a
# minute or more while it dozes. This long without a frame is only logged.
LINK_SILENT = 30.0
# Retry period while the radio is gone (firmware crash and reload, ~1 s). The CR100 drops
# the link after ~3 s without beacons, so come back well before that.
REOPEN_INTERVAL = 0.3
RADIO_GONE = {errno.ENXIO, errno.ENETDOWN, errno.ENODEV, errno.EBADF}
RADIO_BUSY = {errno.EAGAIN, errno.ENOBUFS}
SENT, BUSY, FAILED = 'sent', 'busy', 'failed'
# mon0's MTU (1500) bounds radiotap + 802.11 header + CCMP + SNAP + the inner Ethernet
# frame: 1500 - 10 - 24 - 16 - 8 - 14 = 1428 bytes of inner IP. Keep the TAP below that.
TAP_MTU = 1400


def open_radio(iface, channel):
    """Return a raw socket on the monitor interface, recreating it if a firmware crash
    removed it. The driver renumbers the phy on reload, so look it up from wlan0."""
    if not os.path.exists(f'/sys/class/net/{iface}'):
        subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], check=True)
        info = subprocess.run(['iw', 'dev', 'wlan0', 'info'], capture_output=True, text=True, check=True).stdout
        phy = 'phy' + re.search(r'wiphy (\d+)', info).group(1)
        subprocess.run(['iw', 'phy', phy, 'interface', 'add', iface, 'type', 'monitor'], check=True)
    subprocess.run(['ip', 'link', 'set', iface, 'up'], check=True)
    subprocess.run(['iw', 'dev', iface, 'set', 'channel', str(channel)], check=True)
    radio = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    radio.bind((iface, 0))
    return radio


def open_tap(name):
    fd = os.open('/dev/net/tun', os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack('16sH', name.encode(), IFF_TAP | IFF_NO_PI))
    return fd


class Link:
    def __init__(self, state, radio, tap_fd, log, reopen=None, tsf_lead_us=TSF_LEAD_US):
        self.tsf_lead_us = tsf_lead_us
        self.bssid = frames.mac_bytes(state['bssid'])
        self.cr100 = frames.mac_bytes(state['cr100_mac'])
        self.household = state['household'].encode()
        self.key = bytes.fromhex(state['network_key'])
        self.channel = {2412: 1, 2437: 6, 2462: 11}[state['freq']]
        self.radio, self.tap_fd, self.log, self.reopen = radio, tap_fd, log, reopen
        self.radio_down = False
        self.lock = threading.RLock()
        self.start = time.monotonic()
        self.seq = 0
        # PN must only grow for the CR100's replay check, including across restarts.
        self.tx_pn = int(time.time() * 1000) & 0xffffffffffff
        self.associated = False
        self.last_rx = 0.0
        self.dozing = True      # PM bit of the CR100's last non-PS-Poll frame, as the ZP80 tracks it
        self.last_pm_rx = 0.0   # when that frame arrived
        self.silent = False
        self.searching = False  # associated, but no longer hearing our beacons
        self.check_from = None  # start of the current TIM check, if one is running
        self.rx_last_pn = -1
        self.queue = collections.deque()
        self.stats = collections.Counter()
        self.tx_lock = threading.Lock()
        self.tx_times = collections.deque(maxlen=PACE_FRAMES)

    # --- transmit -------------------------------------------------------------
    def tsf(self):
        return int((time.monotonic() - self.start) * 1e6)

    def air_tsf(self):
        """The timestamp to advertise in beacons and probe responses (see TSF_LEAD_US)."""
        return self.tsf() + self.tsf_lead_us

    def next_seq(self):
        self.seq = (self.seq + 1) & 0xfff
        return self.seq

    def send(self, frame, block=True):
        """Return SENT, BUSY (transmit queue full: try again later) or FAILED. Data is sent
        without blocking: a full queue (frames retrying to a CR100 that stopped answering)
        must not stall the link's threads, and beacons queue behind it anyway."""
        if self.radio_down: return FAILED
        try:
            with self.tx_lock:
                now = time.monotonic()
                if len(self.tx_times) == PACE_FRAMES and now - self.tx_times[0] < PACE_WINDOW:
                    if not block:
                        self.stats['tx_paced'] += 1
                        return BUSY
                    time.sleep(PACE_WINDOW - (now - self.tx_times[0]))
                self.radio.send(frames.TX_RADIOTAP + frame, 0 if block else socket.MSG_DONTWAIT)
                self.tx_times.append(time.monotonic())
            return SENT
        except OSError as error:
            with self.lock:
                if error.errno in RADIO_BUSY:
                    self.stats['tx_busy'] += 1
                    return BUSY
                if error.errno in RADIO_GONE: self.radio_lost(error)
                else: self.stats['tx_error_' + (errno.errorcode.get(error.errno) or str(error.errno))] += 1
            return FAILED

    def radio_lost(self, error):
        if not self.radio_down:
            self.radio_down = True
            self.stats['radio_lost'] += 1
            self.log({'event': 'radio-lost', 'error': repr(error)})

    def send_mgmt(self, subtype, addr1, body, from_ds=False):
        with self.lock:
            self.send(frames.mgmt_header(subtype, addr1, self.bssid, self.next_seq(), from_ds) + body)

    def send_data(self, inner, more_data):
        self.tx_pn += 1
        header = frames.data_header(self.cr100, self.bssid, self.next_seq(), more_data)
        plaintext = ccmp.SNAP_PREFIX + b'\x69\x69' + inner
        result = self.send(ccmp.encrypt(header, plaintext, self.key, self.tx_pn), block=False)
        if result == SENT: self.stats['tx_data'] += 1
        else: self.tx_pn -= 1  # never reached the radio; the retry reuses this PN
        return result

    def flush(self, limit=None):
        """Send queued frames to the CR100; caller holds the lock."""
        now, sent = time.monotonic(), 0
        while self.queue and (limit is None or sent < limit):
            queued_at, inner = self.queue.popleft()
            if now - queued_at > QUEUE_TTL:
                self.stats['tx_expired'] += 1
                continue
            result = self.send_data(inner, more_data=bool(self.queue))
            if result == BUSY:
                self.queue.appendleft((queued_at, inner))  # keep it for the next poll or wake
                break
            sent += 1
        return sent

    def awake(self, now):
        return not self.dozing and now - self.last_pm_rx < AWAKE_TIMEOUT

    def from_tap(self, inner):
        with self.lock:
            if not self.associated:
                self.stats['tx_not_associated'] += 1
                return
            if len(self.queue) >= QUEUE_LIMIT:
                self.queue.popleft(); self.stats['tx_overflow'] += 1
            self.queue.append((time.monotonic(), inner))
            if self.awake(time.monotonic()): self.flush()

    # --- receive --------------------------------------------------------------
    def on_frame(self, raw):
        if len(raw) < 16 or raw[10:16] != self.cr100: return
        now = time.monotonic()
        fc0, fc1 = raw[0], raw[1]
        ftype, subtype = (fc0 >> 2) & 3, fc0 >> 4
        pm = bool(fc1 & 0x10)
        with self.lock:
            self.last_rx = now
            if self.silent or self.searching:
                self.log({'event': 'link-heard', 'was_searching': self.searching})
                self.silent = self.searching = False
            self.check_from = None
            is_ps_poll = ftype == 1 and subtype == 10
            if not is_ps_poll:
                # The PM bit of data, Null and management frames says whether the CR100 stays
                # awake after them; a PS-Poll leaves that as it was (ZP80 handle_ps_poll).
                self.dozing, self.last_pm_rx = pm, now
            if ftype == 0 and subtype == 4 and raw[16:22] in (self.bssid, frames.BROADCAST):
                self.stats['probe_req'] += 1
                self.send_mgmt(5, self.cr100, frames.probe_response_body(self.air_tsf(), self.household))
                return
            if ftype == 0 and subtype in (0, 2) and raw[4:10] == self.bssid:
                self.send_mgmt(subtype + 1, self.cr100, frames.association_response_body(AID), from_ds=True)
                if not self.associated: self.log({'event': 'associated', 'reassoc': subtype == 2})
                self.associated, self.rx_last_pn = True, -1
                self.stats['assoc_req'] += 1
                return
            if ftype == 0 and subtype in (10, 12):
                self.log({'event': 'disassoc' if subtype == 10 else 'deauth', 'reason': raw[24:26].hex()})
                self.associated = False
                return
            if not self.associated:
                # A CR100 still associated from before a bridge restart: the key is shared and
                # nothing else is per-association, so carry on rather than make it search again.
                if not (ftype == 2 or is_ps_poll) or raw[4:10] != self.bssid: return
                self.associated, self.rx_last_pn = True, -1
                self.log({'event': 'resumed'})
            if is_ps_poll:  # one frame per poll, whatever its PM bit (ZP80 _send_one_buffered_packet)
                self.stats['ps_poll'] += 1
                self.flush(limit=1)
                return
            if ftype != 2: return
            if fc1 & 0x40 and subtype in (0, 8): self.receive_data(raw)
            if not pm and self.queue: self.flush()

    def receive_data(self, raw):
        try:
            pn, key_id, plaintext = ccmp.decrypt(raw, self.key)
        except Exception:
            self.stats['rx_decrypt_fail'] += 1
            return
        if pn <= self.rx_last_pn:  # retransmission or replay
            self.stats['rx_replay'] += 1
            return
        self.rx_last_pn = pn
        tunnel = ccmp.parse_tunnel(plaintext)
        if not tunnel or tunnel[0] not in ccmp.TUNNEL_ETHERTYPES:
            self.stats['rx_not_tunnel'] += 1
            return
        os.write(self.tap_fd, plaintext[8:])
        self.stats['rx_data'] += 1
        if self.stats['rx_data'] <= 20:
            outer, dst, src, inner_type, payload = tunnel
            self.log({'event': 'rx', 'pn': pn, 'outer': hex(outer), 'dst': dst.hex(':'), 'src': src.hex(':'),
                      'type': hex(inner_type), **frames.describe_inner(inner_type, payload)})

    # --- loops ----------------------------------------------------------------
    def radio_loop(self):
        while True:
            # Re-read self.radio every pass with a timeout: after a firmware crash the
            # main loop swaps in a new socket, and a recv blocked on the old one would
            # never return (the device is gone, so no error arrives either).
            radio = self.radio
            try:
                if not select.select([radio], [], [], 0.5)[0]: continue
                packet = radio.recv(4096)
            except (OSError, ValueError) as error:  # ValueError: socket closed under us
                if radio is self.radio:
                    with self.lock: self.radio_lost(error)
                time.sleep(0.2)
                continue
            try:
                raw, _ = frames.parse_radiotap(packet)
            except (ValueError, struct.error):
                continue
            self.on_frame(raw)

    def tap_loop(self):
        while True:
            inner = os.read(self.tap_fd, 4096)
            if len(inner) >= 14: self.from_tap(inner)

    def next_tbtt(self, now):
        """Next target beacon transmission time: where our TSF (time since self.start) is a
        whole number of beacon intervals. A dozing CR100 wakes only then; beacons sent 22 ms
        off this grid were missed and it dropped the link after ~3 s (its RSSI history)."""
        interval = frames.BEACON_INTERVAL_TU * 1024e-6
        return self.start + (math.floor((now - self.start) / interval) + 1) * interval

    def send_beacon(self):
        """Build under the lock, send outside it, so receive/transmit bursts cannot hold
        the beacon back. Returns how far past the grid it was built, in µs (before the lead)."""
        with self.lock:
            waiting = (self.queue and not self.awake(time.monotonic())) or self.check_from is not None
            tim = AID if self.associated and waiting else None
            seq = self.next_seq()
        tsf = self.tsf()
        self.send(frames.mgmt_header(8, frames.BROADCAST, self.bssid, seq)
                  + frames.beacon_body(tsf + self.tsf_lead_us, self.channel, tim))
        return tsf % (frames.BEACON_INTERVAL_TU * 1024)

    def check_hearing(self, now):
        """Decide whether a silent associated CR100 still hears the beacons; caller holds the
        lock. Any frame from it clears the check and the searching state (on_frame)."""
        if not self.associated or self.searching: return
        if self.queue and now - self.queue[0][0] > CHECK_WINDOW and self.last_rx < self.queue[0][0]:
            reason = 'tim-unanswered'   # frames waited a whole window with TIM set
        elif self.check_from is None:
            if now - self.last_rx > CHECK_SILENCE: self.check_from = now
            return
        elif now - self.check_from > CHECK_WINDOW:
            reason = 'check-unanswered'
        else:
            return
        self.searching, self.check_from = True, None
        self.log({'event': 'cr100-searching', 'reason': reason, 'silent': round(now - self.last_rx, 1)})

    def run(self, seconds):
        for target in (self.radio_loop, self.tap_loop):
            threading.Thread(target=target, daemon=True).start()
        self.log({'event': 'start', 'bssid': frames.mac_text(self.bssid), 'cr100': frames.mac_text(self.cr100),
                  'channel': self.channel, 'household': self.household.decode(), 'tsf_lead_us': self.tsf_lead_us})
        next_probe = next_stats = next_reopen = time.monotonic()
        next_beacon = self.next_tbtt(next_probe)
        beacon_late = []
        while seconds is None or time.monotonic() - self.start < seconds:
            now = time.monotonic()
            if now >= next_beacon:
                beacon_late.append(self.send_beacon())
                next_beacon = self.next_tbtt(time.monotonic())
                now = time.monotonic()
            if self.radio_down and self.reopen and now >= next_reopen:
                next_reopen = now + REOPEN_INTERVAL
                try:
                    radio = self.reopen()
                except Exception as error:
                    self.log({'event': 'radio-reopen-failed', 'error': repr(error)})
                else:
                    with self.lock:
                        old, self.radio, self.radio_down = self.radio, radio, False
                    old.close()
                    self.log({'event': 'radio-reopened'})
            with self.lock:
                if self.associated and not self.silent and now - self.last_rx > LINK_SILENT:
                    self.silent = True
                    self.log({'event': 'link-silent', 'seconds': LINK_SILENT})
                # Finish bursts the pacer held back while the CR100 is still awake.
                if self.associated and self.queue and self.awake(now): self.flush()
                self.check_hearing(now)
                if (not self.associated or self.searching) and now >= next_probe:
                    self.send_mgmt(5, self.cr100, frames.probe_response_body(self.air_tsf(), self.household))
                    next_probe = now + SEARCH_PROBE_INTERVAL
                if now >= next_stats:
                    late = sorted(beacon_late) or [0]
                    # 'stations' is per controller (one today) for the web interface; 'wall'
                    # dates the line, since 't' counts from this process's start.
                    station = {'mac': frames.mac_text(self.cr100), 'associated': self.associated,
                               'searching': self.searching, 'dozing': self.dozing,
                               'last_heard_s': round(now - self.last_rx, 1) if self.last_rx else None}
                    self.log({'event': 'stats', 'wall': round(time.time(), 1), 'stations': [station],
                              'associated': self.associated, 'searching': self.searching,
                              'queued': len(self.queue),
                              'beacons': len(beacon_late), 'beacon_late_us_p50': late[len(late) // 2],
                              'beacon_late_us_max': late[-1], **self.stats})
                    beacon_late = []
                    next_stats = now + 10
            time.sleep(max(0.0, min(0.002, next_beacon - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--iface', default='mon0')
    parser.add_argument('--state', default='bridge-state.json')
    parser.add_argument('--tap', default='sonos0')
    parser.add_argument('--addr', default='192.168.69.1/24', help='address for the Linux end of the TAP')
    parser.add_argument('--log', help='also append JSON lines here')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--tsf-lead-us', type=int, default=TSF_LEAD_US,
                        help='advertise the TSF this far ahead (µs; see TSF_LEAD_US)')
    args = parser.parse_args()

    log_file = logging.getLogger('link-file') if args.log else None
    if log_file:
        log_file.setLevel(logging.INFO)
        log_file.addHandler(RotatingFileHandler(args.log, maxBytes=2 * 1024 * 1024, backupCount=1))
    start = time.monotonic()

    def log(record):
        line = json.dumps({'t': round(time.monotonic() - start, 3), **record})
        print(line, flush=True)
        if log_file: log_file.info(line)

    state = json.loads(Path(args.state).read_text())
    tap_fd = open_tap(args.tap)
    # Locally administered MAC derived from the bridge's, so it is stable across runs.
    tap_mac = bytearray(frames.mac_bytes(state['bssid'])); tap_mac[0] = (tap_mac[0] | 0x02) & 0xfe
    for command in (['ip', 'link', 'set', args.tap, 'address', frames.mac_text(tap_mac)],
                    ['sysctl', '-qw', f'net.ipv6.conf.{args.tap}.disable_ipv6=1'],
                    ['ip', 'link', 'set', args.tap, 'mtu', str(TAP_MTU)],
                    ['ip', 'addr', 'replace', args.addr, 'dev', args.tap],
                    ['ip', 'link', 'set', args.tap, 'up']):
        subprocess.run(command, check=True)
    channel = {2412: 1, 2437: 6, 2462: 11}[state['freq']]
    reopen = lambda: open_radio(args.iface, channel)
    Link(state, reopen(), tap_fd, log, reopen, tsf_lead_us=args.tsf_lead_us).run(args.seconds)


if __name__ == '__main__':
    main()
