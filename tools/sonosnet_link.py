#!/usr/bin/env python3
"""SonosNet link for paired CR100 controllers, presented as one Linux TAP interface.

The radio side is a userspace AP on a nexmon monitor interface: beacons with a
TIM, probe responses while the CR100 searches, association responses, and CCMP
(KeyID 2) data in both directions. Each data frame tunnels a whole Ethernet frame
under SNAP ethertype 0x6969 (STATIC-FINDINGS §5), so the TAP side carries the
CR100's own Ethernet traffic: ARP, DHCP, SSDP, HTTP. Anything later (a DHCP
server, a UPnP player surface, a bridge to the LAN) attaches to the TAP.

Power save follows the ZP80 driver (handle_ps_poll, handle_null_or_data,
infra_check_to_client_locked; STATIC-FINDINGS "CR100 link drops"): every frame goes
to each CR100 as unicast; while it dozes, its frames wait and the beacon sets its
own AID's TIM bit. Each PS-Poll releases one frame from that station's queue, with
MoreData if more wait; a data or Null frame with PM clear releases its whole queue.
Nothing is sent to a dozing CR100 unprompted. Ethernet multicast is replicated to
associated controllers; Ethernet unicast is routed by destination MAC.

Usage: sudo python3 sonosnet_link.py --state bridge-state.json --tap sonos0 --addr 192.168.69.1/24
"""
from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass, field
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

from cryptography.exceptions import InvalidTag

import ccmp
from ccmp_pn import PacketNumbers
import frames
import link_pairing
import netstart_pair as netstart
from controller_state import controller_records, normalize_mac
from nexmon_clock import ClockUnavailable, HardwareClock

TUNSETIFF, IFF_TAP, IFF_NO_PI = 0x400454ca, 0x0002, 0x1000
ETH_P_ALL = 0x0003
# The CR100 can doze without saying so: send directly only this soon after its last frame
# with PM clear (covers replies to its own requests); anything later waits for TIM + PS-Poll.
AWAKE_TIMEOUT = 0.05
# Retain one PS-Poll delivery grant briefly when the shared pacer/socket is busy.
# It authorizes one reply without changing the station's dozing state, and must
# expire before a later queue update could become an unsolicited transmission.
PS_POLL_REPLY_TIMEOUT = 0.05
# A received request permits a short retry window, never a persistent stream of
# replies to a station that has stopped asking. Only one association and one
# probe response can be pending for each station.
MANAGEMENT_REPLY_TTL = 1.0
# Retain the conservative injection pacing used during the firmware crash audit.
# Pacing alone did not prevent stale-packet crashes: the native packet ownership
# fix in firmware/nexmon-ownership is required. This also bounds queue pressure.
PACE_FRAMES, PACE_WINDOW = 1, 0.0015
# The required firmware gives acquisition probes native NoAck delivery and
# hardware-stamps beacons. Keep acquisition limited to controllers that are
# unassociated or no longer answering TIM, as the ZP80 does: answer their probe
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
# Legacy construction value retained for isolated frame/capture harnesses.
# Production firmware overwrites beacon and probe timestamp bodies with the
# hardware TSF; this lead therefore does not predict an on-air timestamp or
# compensate for queue delay. The host still schedules on the hardware grid.
TSF_LEAD_US = 2000
QUEUE_TTL = 3.0         # drop frames the CR100 never polled for
QUEUE_LIMIT = 256
# The ZP80 driver keeps a silent station associated indefinitely; the CR100 is quiet for a
# minute or more while it dozes. This long without a frame is only logged.
LINK_SILENT = 30.0
# In-place reopen cadence for isolated process-clock harnesses. Production
# radio loss invalidates the clock and requires supervised startup validation.
REOPEN_INTERVAL = 0.3
# A stale hardware-clock model on an unchanged radio epoch is recalibrated in
# place by the clock worker. Until then, withhold only timestamped frames
# (beacons, probe responses) and keep every station's association, queues and
# PNs. Restarting the link instead cost ~12 s of beacons and made every dozing
# CR100 reassociate on its next wake (live 2026-11-08..10: eight restarts).
# A clock that stays stale this long, a TSF reset or radio loss still exits
# for supervised firmware/MPC validation and a fresh calibration.
CLOCK_STALE_GRACE = 10.0
RADIO_GONE = {errno.ENXIO, errno.ENETDOWN, errno.ENODEV, errno.EBADF, errno.EIO, errno.ENETRESET}
RADIO_BUSY = {errno.EAGAIN, errno.ENOBUFS, errno.ENOMEM}
FRAME_ERRORS = {errno.EMSGSIZE, errno.EINVAL}
RADIO_COMMAND_TIMEOUT = 2.0
RECONNECT_CONFIG_INTERVAL = 1.0
TAP_BUSY = {errno.EAGAIN, errno.ENOBUFS, errno.ENOMEM}
SENT, BUSY, FAILED = 'sent', 'busy', 'failed'
# mon0's MTU (1500) bounds radiotap + 802.11 header + CCMP + SNAP + the inner Ethernet
# frame: 1500 - 10 - 24 - 16 - 8 - 14 = 1428 bytes of inner IP. Keep the TAP below that.
TAP_MTU = 1400


def open_radio(iface, channel):
    """Return a raw socket on the monitor interface, recreating it if a firmware crash
    removed it. The driver renumbers the phy on reload, so look it up from wlan0."""
    exists = os.path.exists(f'/sys/class/net/{iface}')
    if not exists:
        subprocess.run(['ip', 'link', 'set', 'wlan0', 'up'], check=True, timeout=RADIO_COMMAND_TIMEOUT)

    def metadata(name):
        info = subprocess.run(['iw', 'dev', name, 'info'], capture_output=True, text=True,
                              check=True, timeout=RADIO_COMMAND_TIMEOUT).stdout
        phys = re.findall(r'^\s*wiphy\s+(\d+)\s*$', info, re.MULTILINE)
        if len(phys) != 1:
            raise RuntimeError('Cannot verify the radio PHY for ' + name)
        return int(phys[0]), info

    wlan_phy, _ = metadata('wlan0')
    if not exists:
        phy = 'phy' + str(wlan_phy)
        subprocess.run(['iw', 'phy', phy, 'interface', 'add', iface, 'type', 'monitor'], check=True,
                       timeout=RADIO_COMMAND_TIMEOUT)
    monitor_phy, info = metadata(iface)
    if (monitor_phy != wlan_phy or
            re.findall(r'^\s*type\s+(\S+)\s*$', info, re.MULTILINE) != ['monitor']):
        # Firmware diagnostics and GET433 address global wlan0. A preexisting
        # monitor on another PHY must not use that radio's validated clock.
        raise RuntimeError(iface + ' must be a monitor on the same PHY as wlan0')
    subprocess.run(['ip', 'link', 'set', iface, 'up'], check=True, timeout=RADIO_COMMAND_TIMEOUT)
    subprocess.run(['iw', 'dev', iface, 'set', 'channel', str(channel)], check=True,
                   timeout=RADIO_COMMAND_TIMEOUT)
    radio = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        radio.bind((iface, 0))
        radio.setblocking(False)
    except BaseException:
        radio.close()
        raise
    return radio


def open_verified_radio(iface, channel):
    """Require the running radio's ownership, timestamp and delivery capabilities."""
    from bridge_service import verify_injection_ownership
    radio = open_radio(iface, channel)
    try:
        # MPC off first: a reloaded firmware re-enables it and powers the idle core
        # down, which makes the clock/beacon verification report not ready.
        from nexmon_power import disable_mpc
        disable_mpc()
        verify_injection_ownership()
        return radio
    except BaseException:
        radio.close()
        raise


def open_tap(name):
    fd = os.open('/dev/net/tun', os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack('16sH', name.encode(), IFF_TAP | IFF_NO_PI))
    return fd


def valid_information_elements(body):
    """Accept only a complete sequence of bounded tag/length/value elements."""
    offset = 0
    while offset < len(body):
        if offset + 2 > len(body): return False
        size = body[offset + 1]
        offset += 2
        if offset + size > len(body): return False
        offset += size
    return True


def valid_association_body(body, *, reassociation=False):
    """Validate fixed fields and complete IEs before changing station state.

    Captured CR100 requests contain only capability/listen interval and a Sonos
    vendor IE; SSID and supported-rate IEs are absent. Reassociation adds the
    six-byte current-AP field. Do not impose standard optional IEs on this legacy
    format, or treat these unprotected fields as cryptographic authentication.
    """
    fixed = 10 if reassociation else 4
    return len(body) >= fixed and valid_information_elements(body[fixed:])


@dataclass
class Station:
    mac: bytes
    aid: int
    associated: bool = False
    last_rx: float = 0.0
    dozing: bool = True
    last_pm_rx: float = 0.0
    silent: bool = False
    searching: bool = False
    check_from: float | None = None
    rx_last_pn: int = -1
    next_probe: float = 0.0
    queue: collections.deque = field(default_factory=collections.deque)
    stats: collections.Counter = field(default_factory=collections.Counter)
    auto_reconnect: bool = True
    poll_pending_until: float | None = None
    management_replies: dict = field(default_factory=dict)


class Link:
    def __init__(self, state, radio, tap_fd, log, reopen=None, tsf_lead_us=TSF_LEAD_US,
                 pn_allocator=None, config_path=None, hardware_clock=None, run_dir=None, state_path=None,
                 iface='mon0', set_channel=None):
        self.tsf_lead_us = tsf_lead_us
        # Production main supplies the radio clock. The process-relative clock
        # remains available only to isolated frame/test harnesses.
        self.hardware_clock = hardware_clock
        self.clock_failure_logged = False
        self.clock_stale_since = None
        # Hearing checks count silence only from here: no beacon carried a TIM
        # bit while a stale clock withheld them (see check_hearing).
        self.hearing_from = float('-inf')
        self.clock_resets = None
        self.config_path = Path(config_path) if config_path is not None else None
        self.bssid = frames.mac_bytes(state['bssid'])
        # With an explicit config, fail closed for proactive probes until its
        # first valid policy is loaded. Received traffic remains fully enabled.
        self.stations = {frames.mac_bytes(record['mac']): Station(frames.mac_bytes(record['mac']), record['aid'],
                                                               auto_reconnect=self.config_path is None)
                         for record in controller_records(state)}
        if not self.stations: raise ValueError('No paired controllers')
        self.household = state['household'].encode()
        self.key = bytes.fromhex(state['network_key'])
        self.freq = state['freq']
        self.channel = {2412: 1, 2437: 6, 2462: 11}[state['freq']]
        # The channel the radio is tuned to; it differs from self.channel only while a
        # pairing window visits the setup channel (link_pairing). None: unknown.
        self.tuned = self.channel
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.state_path = Path(state_path) if state_path is not None else None
        self.set_channel = set_channel or (lambda channel: subprocess.run(
            ['iw', 'dev', iface, 'set', 'channel', str(channel)], check=True, capture_output=True,
            timeout=RADIO_COMMAND_TIMEOUT))
        self.pairing = None
        self.pair_check_at = 0.0
        self.pair_finished = None
        self.pair_reported = None
        self.away_until = None
        self.tune_cost = 0.0     # s, a decaying maximum of recent channel changes
        self.last_beacon_at = None
        self.next_alive = 0.0
        self.state_lock = threading.Lock()
        self.radio, self.tap_fd, self.log, self.reopen = radio, tap_fd, log, reopen
        self.radio_down = False
        self.lock = threading.RLock()
        self.start = time.monotonic()
        self.seq = 0
        # AP-wide: CCMP nonce uses transmitter MAC + PN, not destination MAC. With
        # a shared key, separate TX counters would reuse nonces across controllers.
        # Reserve durably before encryption, so clock rollback and power loss
        # cannot reuse a nonce. Production always supplies the lifetime allocator.
        self.pn_allocator = pn_allocator
        self.tx_pn = pn_allocator.initial if pn_allocator else int(time.time() * 1000)
        self.station_cursor = 0
        self.stats = collections.Counter()
        self.tx_lock = threading.Lock()
        self.tx_times = collections.deque(maxlen=PACE_FRAMES)
        self.stop = threading.Event()
        self.workers = {}
        self.worker_failure = None
        self.worker_lock = threading.Lock()
        self.reopen_error = None
        self.reopen_error_at = 0.0
        self.config_check_at = 0.0
        self.config_signature = None
        self.config_error = False
        if tap_fd >= 0:
            os.set_blocking(tap_fd, False)
        self.reload_reconnect_policy(self.start, force=True)

    def reload_reconnect_policy(self, now, force=False):
        """Apply only automatic-search preferences, preserving every radio identity.

        Config writes are atomic. Read outside the station lock and retain the
        last valid map on any I/O, JSON or schema error; a bad edit must not
        unexpectedly resume probes to a paused, powered-off controller.
        """
        if self.config_path is None or (not force and now < self.config_check_at): return
        self.config_check_at = now + RECONNECT_CONFIG_INTERVAL
        try:
            info = self.config_path.stat()
            signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            if not force and signature == self.config_signature: return
            config = json.loads(self.config_path.read_text())
            if not isinstance(config, dict): raise ValueError('Invalid controller settings')
            profiles = config.get('controllers')
            if profiles is None: profiles = {}
            if not isinstance(profiles, dict): raise ValueError('Invalid controller settings')
            policy = {}
            for mac, profile in profiles.items():
                mac = normalize_mac(mac)
                if mac in policy: raise ValueError('Duplicate controller settings')
                if profile is None: profile = {}
                if not isinstance(profile, dict): raise ValueError('Invalid controller settings')
                enabled = profile.get('auto_reconnect', True)
                if type(enabled) is not bool: raise ValueError('Invalid automatic reconnection setting')
                policy[mac] = enabled
        except (OSError, ValueError, TypeError):
            if not self.config_error: self.log({'event': 'reconnect-config-invalid'})
            self.config_error = True
            return
        self.config_signature = signature
        self.config_error = False
        with self.lock:
            for station in self.stations.values():
                enabled = policy.get(frames.mac_text(station.mac), True)
                if station.auto_reconnect != enabled:
                    station.auto_reconnect = enabled
                    if enabled: station.next_probe = 0.0
                    self.station_log(station, event='controller-auto-reconnect', enabled=enabled)

    # --- transmit -------------------------------------------------------------
    def clock_unavailable(self, error):
        """Capture public clock fault metadata before shutdown closes its helper."""
        with self.lock:
            if self.clock_failure_logged: return
            self.clock_failure_logged = True
            self.log({'event': 'clock-unavailable', 'error': str(error),
                      'clock': self.hardware_clock.status()})

    def clock_stale(self, error, now=None):
        """Main loop only: note a stale clock, and fail once it outlasts the grace."""
        now = time.monotonic() if now is None else now
        if self.clock_stale_since is None:
            self.clock_stale_since = now
            self.stats['clock_stale'] += 1
            self.log({'event': 'clock-stale', 'error': str(error),
                      'clock': self.hardware_clock.status()})
        if now - self.clock_stale_since >= CLOCK_STALE_GRACE:
            self.clock_unavailable(error)
            raise error

    def clock_fresh(self, now):
        if self.clock_stale_since is not None:
            self.log({'event': 'clock-recovered', 'seconds': round(now - self.clock_stale_since, 3)})
            self.clock_stale_since = None
            self.hearing_from = now

    def tsf(self):
        """Raises ClockUnavailable while the hardware clock is stale; callers withhold the frame."""
        if self.hardware_clock is not None:
            return self.hardware_clock.tsf_us()
        return int((time.monotonic() - self.start) * 1e6)

    def air_tsf(self):
        """The timestamp to advertise in beacons and probe responses (see TSF_LEAD_US)."""
        return self.tsf() + self.tsf_lead_us

    def next_seq(self):
        self.seq = (self.seq + 1) & 0xfff
        return self.seq

    def send(self, frame, block=True, channel=None):
        """Return SENT, BUSY (transmit queue full: try again later) or FAILED.

        Every socket send is nonblocking: management traffic must also leave the
        main loop free to log stats and reopen a failed radio. ``block`` permits
        only the bounded pacing wait, never a wait for transmit-buffer space.
        A frame for another channel than the tuned one (by default the home
        channel) is BUSY: it waits while a pairing window visits channel 1.
        """
        if self.radio_down:
            self.stats['tx_radio_down'] += 1
            return FAILED
        try:
            with self.tx_lock:
                if self.tuned != (self.channel if channel is None else channel):
                    self.stats['tx_off_channel'] += 1
                    return BUSY
                now = time.monotonic()
                if len(self.tx_times) == PACE_FRAMES and now - self.tx_times[0] < PACE_WINDOW:
                    if not block:
                        self.stats['tx_paced'] += 1
                        return BUSY
                    time.sleep(PACE_WINDOW - (now - self.tx_times[0]))
                self.stats['tx_attempts'] += 1
                self.radio.send(frames.TX_RADIOTAP + frame, socket.MSG_DONTWAIT)
                self.tx_times.append(time.monotonic())
                self.stats['tx_sent'] += 1
            return SENT
        except OSError as error:
            with self.lock:
                self.stats['tx_send_failures'] += 1
                if error.errno in RADIO_BUSY:
                    self.stats['tx_busy'] += 1
                    return BUSY
                if error.errno in RADIO_GONE:
                    self.radio_lost(error)
                else:
                    self.stats['tx_error_' + (errno.errorcode.get(error.errno) or str(error.errno))] += 1
                    if error.errno not in FRAME_ERRORS:
                        raise
            return FAILED

    def radio_lost(self, error):
        if not self.radio_down:
            self.radio_down = True
            if self.hardware_clock is not None:
                # A reload can restart the hardware TSF. Retire the old epoch
                # immediately; only a newly supervised link may inject again.
                self.hardware_clock.invalidate()
            self.stats['radio_lost'] += 1
            self.log({'event': 'radio-lost', 'error': repr(error)})

    def send_mgmt(self, subtype, addr1, body, from_ds=False, block=True):
        with self.lock:
            return self.send(frames.mgmt_header(subtype, addr1, self.bssid, self.next_seq(), from_ds) + body,
                             block=block)

    def queue_mgmt_reply(self, station, subtype, now):
        """Coalesce solicited replies into two bounded slots; caller holds lock."""
        if subtype not in (1, 3, 5): raise ValueError('Unsupported solicited management reply')
        kind = 'probe' if subtype == 5 else 'association'
        station.management_replies[kind] = (now, subtype)
        self.service_mgmt_replies(station, now)

    def service_mgmt_replies(self, station, now):
        for kind in ('association', 'probe'):
            pending = station.management_replies.get(kind)
            if pending is None: continue
            queued_at, subtype = pending
            if now - queued_at >= MANAGEMENT_REPLY_TTL:
                del station.management_replies[kind]
                self.count(station, 'tx_mgmt_expired')
                continue
            # Build a probe's advertised TSF for this attempt, not for the
            # original request; socket or pacing backpressure can delay it.
            # While the clock is stale the probe stays pending within its TTL.
            try:
                body = (frames.probe_response_body(self.air_tsf(), self.household) if subtype == 5 else
                        frames.association_response_body(station.aid))
            except ClockUnavailable:
                continue
            result = self.send_mgmt(subtype, station.mac, body, from_ds=subtype in (1, 3), block=False)
            if result == BUSY or (result == FAILED and self.radio_down): return
            del station.management_replies[kind]
            self.count(station, 'tx_mgmt_sent' if result == SENT else 'tx_mgmt_dropped')

    def count(self, station, name):
        self.stats[name] += 1
        station.stats[name] += 1

    def station_log(self, station, **record):
        self.log({'mac': frames.mac_text(station.mac), **record})

    def send_data(self, station, inner, more_data):
        if self.tuned != self.channel:   # away for pairing: keep the frame, and its nonce unspent
            self.stats['tx_off_channel'] += 1
            return BUSY
        next_pn = self.tx_pn + 1
        if next_pn >= 1 << 48: raise OverflowError('CCMP packet number exhausted; re-pair with a new key')
        if self.pn_allocator: self.pn_allocator.ensure(next_pn)
        self.tx_pn = next_pn
        header = frames.data_header(station.mac, self.bssid, self.next_seq(), more_data)
        plaintext = ccmp.SNAP_PREFIX + b'\x69\x69' + inner
        result = self.send(ccmp.encrypt(header, plaintext, self.key, self.tx_pn), block=False)
        if result == SENT: self.count(station, 'tx_data')
        # Consume the nonce even on a failed send; acceptance can be uncertain.
        return result

    def flush(self, station, limit=None):
        """Send one station's queued frames; caller holds the lock."""
        now, sent = time.monotonic(), 0
        while station.queue and (limit is None or sent < limit):
            queued_at, inner = station.queue.popleft()
            if now - queued_at > QUEUE_TTL:
                self.count(station, 'tx_expired')
                continue
            result = self.send_data(station, inner, more_data=bool(station.queue))
            if result == BUSY or (result == FAILED and self.radio_down):
                station.queue.appendleft((queued_at, inner))  # keep it for the next poll or wake
                break
            if result == FAILED:
                self.count(station, 'tx_dropped')
            sent += 1
        return sent

    def expire_queue(self, station, now):
        """Drop frames never polled for within QUEUE_TTL; caller holds the lock.

        flush() only expires frames it reaches, and a CR100 in standby powers
        its radio off, so nothing reached them: live (2026-11-10) up to 69
        frames sat for 100 minutes, holding its TIM bit in every beacon.
        """
        while station.queue and now - station.queue[0][0] > QUEUE_TTL:
            station.queue.popleft()
            self.count(station, 'tx_expired')

    def awake(self, station, now):
        return not station.dozing and now - station.last_pm_rx < AWAKE_TIMEOUT

    def service_ps_poll(self, station, now):
        """Finish at most one recently polled reply; caller holds the station lock."""
        if station.poll_pending_until is None: return
        if now >= station.poll_pending_until:
            station.poll_pending_until = None
            self.count(station, 'tx_poll_expired')
            return
        if self.flush(station, limit=1) or not station.queue:
            station.poll_pending_until = None

    def from_tap(self, inner):
        if len(inner) < 14: return
        with self.lock:
            if inner[0] & 1:  # broadcast/multicast: outer unicast for each station's power save
                destinations = list(self.stations.values())
            else:
                station = self.stations.get(inner[:6])
                destinations = [station] if station else []
            if not destinations:
                self.stats['tx_unknown_destination'] += 1
                return
            now = time.monotonic()
            for station in destinations:
                if not station.associated:
                    self.count(station, 'tx_not_associated')
                    continue
                if len(station.queue) >= QUEUE_LIMIT:
                    station.queue.popleft(); self.count(station, 'tx_overflow')
                station.queue.append((now, inner))
                if self.awake(station, now): self.flush(station)

    # --- receive --------------------------------------------------------------
    def on_frame(self, raw):
        if len(raw) < 16: return
        if raw[0] & 3: return  # only 802.11 protocol version 0 is defined here
        if raw[0] == 0x40 and len(raw) >= 24 and raw[10:16] != self.bssid:
            # A Netstart probe request (pairing) is never a station's discovery probe.
            payload = netstart.parse_body(raw[24:])
            if payload is not None:
                session = self.pairing
                if session is not None: session.receive(frames.mac_text(raw[10:16]), payload)
                return
        station = self.stations.get(raw[10:16])
        if station is None: return
        now = time.monotonic()
        fc0, fc1 = raw[0], raw[1]
        ftype, subtype = (fc0 >> 2) & 3, fc0 >> 4
        pm = bool(fc1 & 0x10)
        is_ps_poll = ftype == 1 and subtype == 10
        is_probe = ftype == 0 and subtype == 4
        # A known transmitter can also be talking to another AP. Ignore those
        # frames before they change this station's sleep/hearing/association state.
        if is_ps_poll:
            # Stock CR100s send a bare AID (observed 0x0001 on air), while
            # standard PS-Polls set the two high bits. Match the 14-bit AID
            # in either format, retaining per-station queue isolation.
            if raw[4:10] != self.bssid or struct.unpack_from('<H', raw, 2)[0] & 0x3fff != station.aid: return
        elif len(raw) < 24:
            return
        elif is_probe:
            if raw[4:10] not in (self.bssid, frames.BROADCAST) or raw[16:22] not in (self.bssid, frames.BROADCAST): return
            if not valid_information_elements(raw[24:]): return
        elif ftype == 0 and subtype in (0, 2, 10, 12):
            if raw[4:10] != self.bssid or raw[16:22] != self.bssid: return
            if subtype in (0, 2) and not valid_association_body(raw[24:], reassociation=subtype == 2): return
            if subtype in (10, 12) and len(raw) < 26: return
        elif ftype == 2 and subtype in (0, 4, 8, 12):
            # ToDS identifies the AP in addr1; addr3 is a destination address.
            if raw[4:10] != self.bssid or fc1 & 3 != 1: return
            if subtype in (8, 12) and len(raw) < 26: return
            if subtype in (0, 8) and not fc1 & 0x40: return
        else:
            return
        with self.lock:
            if ftype == 2 and subtype in (0, 8) and not self.receive_data(station, raw): return
            station.last_rx = now
            if station.silent or station.searching:
                self.station_log(station, event='link-heard', was_searching=station.searching)
                station.silent = station.searching = False
            station.check_from = None
            if not is_ps_poll:
                # The PM bit of data, Null and management frames says whether the CR100 stays
                # awake after them; a PS-Poll leaves that as it was (ZP80 handle_ps_poll).
                station.dozing, station.last_pm_rx = pm, now
                station.poll_pending_until = None
            if is_probe:
                self.count(station, 'probe_req')
                self.queue_mgmt_reply(station, 5, now)
                return
            if ftype == 0 and subtype in (0, 2):
                self.queue_mgmt_reply(station, subtype + 1, now)
                if not station.associated: self.station_log(station, event='associated', reassoc=subtype == 2)
                # A real CR100 reboot can restart its transmit PN with the same
                # shared key. Keep that legacy recovery behavior; unprotected
                # association requests cannot prove a fresh cryptographic epoch.
                station.associated, station.rx_last_pn = True, -1
                self.count(station, 'assoc_req')
                return
            if ftype == 0 and subtype in (10, 12):
                self.station_log(station, event='disassoc' if subtype == 10 else 'deauth', reason=raw[24:26].hex())
                station.associated = False
                station.queue.clear()
                station.management_replies.clear()
                return
            if not station.associated:
                # Stored AIDs let a station resume with a PS-Poll after a bridge
                # restart, without making other controllers reassociate too.
                station.associated = True
                self.station_log(station, event='resumed')
            if is_ps_poll:  # one frame per poll, whatever its PM bit (ZP80 _send_one_buffered_packet)
                self.count(station, 'ps_poll')
                # Retries coalesce into one outstanding grant; repeatedly hearing
                # the same unanswered poll must not extend its lifetime.
                if station.poll_pending_until is None or now >= station.poll_pending_until:
                    station.poll_pending_until = now + PS_POLL_REPLY_TIMEOUT
                self.service_ps_poll(station, now)
                return
            if not pm and station.queue: self.flush(station)

    def receive_data(self, station, raw):
        try:
            pn, key_id, plaintext = ccmp.decrypt(raw, self.key)
        except (InvalidTag, ValueError, IndexError, struct.error):
            self.count(station, 'rx_decrypt_fail')
            return False
        if key_id != 2:
            self.count(station, 'rx_wrong_key')
            return False
        self.count(station, 'rx_authenticated')
        if pn <= station.rx_last_pn:  # retransmission or replay from this transmitter
            self.count(station, 'rx_replay')
            # Still a valid encrypted frame from this station: it heard the AP
            # and its PM bit remains meaningful, but Ethernet must not repeat.
            return True
        station.rx_last_pn = pn
        tunnel = ccmp.parse_tunnel(plaintext)
        if not tunnel or tunnel[0] not in ccmp.TUNNEL_ETHERTYPES:
            self.count(station, 'rx_not_tunnel')
            return False
        try:
            written = os.write(self.tap_fd, plaintext[8:])
        except OSError as error:
            if error.errno in TAP_BUSY or error.errno == errno.EMSGSIZE:
                self.count(station, 'rx_tap_busy' if error.errno in TAP_BUSY else 'rx_tap_oversize')
                return True
            raise
        if written != len(plaintext) - 8:
            raise OSError(errno.EIO, 'Incomplete Ethernet frame written to TAP')
        self.count(station, 'rx_data')
        if station.stats['rx_data'] <= 20:
            outer, dst, src, inner_type, payload = tunnel
            self.station_log(station, **{'event': 'rx', 'pn': pn, 'outer': hex(outer), 'dst': dst.hex(':'), 'src': src.hex(':'),
                      'type': hex(inner_type), **frames.describe_inner(inner_type, payload)})
        return True

    # --- loops ----------------------------------------------------------------
    def worker_loop(self, name, target):
        try:
            target()
        except BaseException as error:
            if self.stop.is_set(): return
            with self.worker_lock:
                if self.worker_failure is None:
                    self.worker_failure = (name, error)
            self.log({'event': 'worker-failed', 'worker': name, 'error': repr(error)})

    def check_workers(self):
        with self.worker_lock:
            failure = self.worker_failure
        if failure is not None:
            name, error = failure
            raise RuntimeError('SonosNet %s worker failed' % name) from error
        if self.hardware_clock is not None:
            if self.radio_down:
                error = ClockUnavailable('Radio link lost; supervised clock calibration is required')
                self.clock_unavailable(error)
                raise error
            status = self.hardware_clock.status()
            if not status['worker_alive']:
                error = ClockUnavailable('Radio hardware clock worker stopped')
                self.clock_unavailable(error)
                raise error
            # A reset epoch fails closed: the supervisor restarts the link and
            # verifies the firmware/MPC before calibrating a new epoch.
            resets = status.get('resets', 0)
            if self.clock_resets is None:
                self.clock_resets = resets
            elif resets != self.clock_resets:
                error = ClockUnavailable('Hardware TSF reset; supervised clock calibration is required')
                self.clock_unavailable(error)
                raise error
            # A stale model on the same epoch only withholds timestamped frames
            # (see CLOCK_STALE_GRACE); return whether they may be sent now.
            try:
                self.tsf()
            except ClockUnavailable as error:
                self.clock_stale(error)
                return False
        return True

    def radio_loop(self):
        while not self.stop.is_set():
            # Re-read self.radio every pass with a timeout: after a firmware crash the
            # main loop swaps in a new socket, and a recv blocked on the old one would
            # never return (the device is gone, so no error arrives either).
            radio = self.radio
            try:
                if not select.select([radio], [], [], 0.5)[0]: continue
                packet = radio.recv(4096)
            except (OSError, ValueError) as error:  # ValueError: socket closed under us
                if isinstance(error, OSError) and error.errno in RADIO_BUSY:
                    self.stats['rx_busy'] += 1
                    self.stop.wait(0.002)
                    continue
                if radio is self.radio:
                    with self.lock: self.radio_lost(error)
                time.sleep(0.2)
                continue
            try:
                raw, _ = frames.parse_radiotap(packet)
            except frames.BadFrameChecksum:
                self.stats['rx_bad_fcs'] += 1
                continue
            except (ValueError, struct.error):
                self.stats['rx_malformed_radiotap'] += 1
                continue
            self.on_frame(raw)

    def tap_loop(self):
        while not self.stop.is_set():
            if not select.select([self.tap_fd], [], [], 0.5)[0]: continue
            try:
                inner = os.read(self.tap_fd, 4096)
            except BlockingIOError:
                continue
            if not inner: raise OSError(errno.EIO, 'TAP interface closed')
            if len(inner) >= 14: self.from_tap(inner)

    def next_tbtt(self, now):
        """Next beacon on the advertised clock's 100-TU grid.

        Hardware probe responses use the radio TSF, so production beacons must
        share that clock and grid rather than a new process-start epoch.
        """
        if self.hardware_clock is not None:
            return self.hardware_clock.next_tbtt(now, frames.BEACON_INTERVAL_TU * 1024)
        interval = frames.BEACON_INTERVAL_TU * 1024e-6
        deadline = self.start + (math.floor((now - self.start) / interval) + 1) * interval
        # Float subtraction can round an exact boundary just below its index.
        # Keep the harness clock's promised deadline strictly after now too.
        return deadline + interval if deadline <= now else deadline

    def send_beacon(self):
        """Build under the lock, send outside it, so receive/transmit bursts cannot hold
        the beacon back. Returns how far past the grid it was built, in µs (before the lead)."""
        with self.lock:
            now = time.monotonic()
            tim = [station.aid for station in self.stations.values() if station.associated and
                   ((station.queue and not self.awake(station, now)) or station.check_from is not None)]
            seq = self.next_seq()
        tsf = self.tsf()
        self.stats['beacon_attempts'] += 1
        result = self.send(frames.mgmt_header(8, frames.BROADCAST, self.bssid, seq)
                           + frames.beacon_body(tsf + self.tsf_lead_us, self.channel, tim))
        if result == SENT: self.stats['beacon_sent'] += 1
        else: self.stats['beacon_failed'] += 1
        interval = frames.BEACON_INTERVAL_TU * 1024
        offset = tsf % interval
        if self.hardware_clock is not None:
            # Calibration can move the estimate slightly across the grid.
            # A beacon built 50 us early is not 102,350 us late.
            offset = (offset + interval // 2) % interval - interval // 2
        return offset

    def check_hearing(self, station, now):
        """Decide whether a silent associated CR100 still hears the beacons; caller holds the
        lock. Any frame from it clears the check and the searching state (on_frame)."""
        if not station.associated or station.searching: return
        if self.clock_stale_since is not None:
            # No beacons are on air, so an unanswered TIM proves nothing. Taking a dozing
            # CR100 for a searching one sends it probe responses that knock it off.
            station.check_from = None
            return
        # Silence and queue age count only while beacons carried the TIM bit.
        since = self.hearing_from
        if (station.queue and now - max(station.queue[0][0], since) > CHECK_WINDOW
                and station.last_rx < station.queue[0][0]):
            reason = 'tim-unanswered'   # frames waited a whole window with TIM set
        elif station.check_from is None:
            if now - max(station.last_rx, since) > CHECK_SILENCE: station.check_from = now
            return
        elif now - station.check_from > CHECK_WINDOW:
            reason = 'check-unanswered'
        else:
            return
        station.searching, station.check_from = True, None
        self.station_log(station, event='cr100-searching', reason=reason, silent=round(now - station.last_rx, 1))

    def service_stations(self, now):
        """Rotate first use of the shared pacer so a busy station cannot starve others.

        Caller holds the lock. Probes are nonblocking too: a missing station must
        not delay the next beacon or another controller's power-save response.
        """
        stations = list(self.stations.values())
        start = self.station_cursor
        self.station_cursor = (start + 1) % len(stations)
        for station in stations[start:] + stations[:start]:
            self.service_ps_poll(station, now)
            self.service_mgmt_replies(station, now)
            if station.associated and not station.silent and now - station.last_rx > LINK_SILENT:
                station.silent = True
                self.station_log(station, event='link-silent', seconds=LINK_SILENT)
            if station.associated and station.queue and self.awake(station, now): self.flush(station)
            self.check_hearing(station, now)  # before expiry: queue age is its tim-unanswered evidence
            self.expire_queue(station, now)
            if station.auto_reconnect and (not station.associated or station.searching) and now >= station.next_probe:
                try:
                    body = frames.probe_response_body(self.air_tsf(), self.household)
                except ClockUnavailable:
                    continue  # stale clock: withhold timestamped frames (CLOCK_STALE_GRACE)
                result = self.send_mgmt(5, station.mac, body, block=False)
                if result == SENT: station.next_probe = now + SEARCH_PROBE_INTERVAL

    # --- pairing (link_pairing) ------------------------------------------------
    def tune(self, channel):
        """Retune the radio; hold the transmit lock so no frame leaves mid-change."""
        with self.tx_lock:
            if self.tuned == channel: return True
            started = time.monotonic()
            try:
                self.set_channel(channel)
            except (OSError, subprocess.SubprocessError) as error:
                self.tuned = None
                self.stats['channel_change_failures'] += 1
                self.log({'event': 'channel-change-failed', 'channel': channel, 'error': repr(error)})
                if channel == self.channel:
                    raise RuntimeError('Cannot return the radio to channel %d' % channel) from error
                return False
            self.tuned = channel
            self.stats['channel_changes'] += 1
            # Plan visits around the slowest recent change (the board, load and driver all vary).
            self.tune_cost = max(time.monotonic() - started, self.tune_cost * 0.98)
            return True

    def persist_controller(self, record):
        """Radio thread: save a newly paired controller, then accept it as a station."""
        from bridge_service import validate_state
        if self.state_path is None: raise RuntimeError('No pairing state file')
        with self.state_lock:
            current = validate_state(self.state_path)
            for name, value in (('bssid', frames.mac_text(self.bssid)), ('household', self.household.decode()),
                                ('network_key', self.key.hex()), ('freq', self.freq)):
                if current[name] != value:
                    raise ValueError('The saved household changed (%s); restart the bridge' % name)
            saved = netstart.save_pairing(self.state_path, current, [record])
        aid = next(item['aid'] for item in saved['controllers'] if item['mac'] == record['mac'])
        mac = frames.mac_bytes(record['mac'])
        with self.lock:
            if mac not in self.stations:
                # Probe it at once so it finds the household; the next policy load applies
                # any saved automatic-search preference for this MAC.
                self.stations[mac] = Station(mac, aid)
                self.station_log(self.stations[mac], event='controller-added', aid=aid)
        self.config_signature, self.config_check_at = None, 0.0

    def send_netstart(self, payload):
        with self.lock:
            seq = self.next_seq()
        frame = (b'\x40\x00\x00\x00' + frames.BROADCAST + self.bssid + frames.BROADCAST
                 + struct.pack('<H', (seq & 0xfff) << 4) + netstart.frame_body(payload))
        return self.send(frame, block=False, channel=link_pairing.SETUP_CHANNEL)

    def send_pairing_frames(self, session, now):
        """On the setup channel: queued RESPONSEs first, then ALIVE every 100 ms."""
        # The shared pacer spaces frames 1.5 ms apart: send what it allows, the rest next pass.
        while (reply := session.next_reply()) is not None:
            if self.send_netstart(reply) != SENT: return
            session.reply_sent(reply)
            self.stats['pairing_responses'] += 1
        if now >= self.next_alive and self.send_netstart(session.alive) == SENT:
            self.next_alive = now + link_pairing.ALIVE_INTERVAL
            self.stats['pairing_alive'] += 1

    def write_pair_status(self, session):
        if self.run_dir is None: return
        try:
            link_pairing.write_json_atomic(self.run_dir / link_pairing.PAIR_STATUS, session.status(
                channel=self.channel, hopping=self.channel != link_pairing.SETUP_CHANNEL))
        except OSError as error:
            self.log({'event': 'pairing-status-failed', 'error': repr(error)})

    def check_pair_request(self):
        request = link_pairing.read_request(self.run_dir / link_pairing.PAIR_REQUEST)
        session = self.pairing
        if session is not None and (request is None or request['id'] != session.id):
            session.fail('Pairing was cancelled')
            self.end_pairing()
            session = None
        if session is None and request is not None and request['id'] != self.pair_finished:
            state = {'bssid': frames.mac_text(self.bssid), 'household': self.household.decode(),
                     'network_key': self.key.hex(), 'freq': self.freq}
            self.pairing = link_pairing.Session(request, state, self.persist_controller, self.log)
            self.pair_reported, self.next_alive = None, 0.0
            self.log({'event': 'pairing-started', 'pairing': request['id'], 'channel': self.channel,
                      'hopping': self.channel != link_pairing.SETUP_CHANNEL})

    def end_pairing(self):
        session, self.pairing = self.pairing, None
        self.pair_finished = session.id
        if self.tuned != self.channel: self.tune(self.channel)
        self.away_until = None
        self.write_pair_status(session)
        status = session.status()
        self.log({'event': 'pairing-ended', 'pairing': session.id, 'result': status['state'],
                  'controllers': status['controllers'], 'error': status['error']})

    def service_pairing(self, now, next_beacon):
        """Main loop: open/close pairing windows and visit the setup channel between beacons."""
        if self.run_dir is not None and now >= self.pair_check_at:
            self.pair_check_at = now + link_pairing.REQUEST_CHECK_INTERVAL
            self.check_pair_request()
        session = self.pairing
        if session is None: return
        if session.finish_if_due() is not None:
            self.end_pairing()
            return
        reported = len(session.persisted)
        if reported != self.pair_reported:
            self.pair_reported = reported
            self.write_pair_status(session)
        if self.channel == link_pairing.SETUP_CHANNEL:
            self.send_pairing_frames(session, now)
            return
        if self.tuned == self.channel:
            if next_beacon is None or self.last_beacon_at is None: return
            back = next_beacon - max(link_pairing.RETURN_MARGIN, self.tune_cost + link_pairing.RETURN_SLACK)
            if now < self.last_beacon_at + link_pairing.HOME_DWELL or back - now < link_pairing.MIN_AWAY: return
            self.last_beacon_at = None   # at most one visit per beacon interval
            if not self.tune(link_pairing.SETUP_CHANNEL):
                session.fail('The radio could not change to the setup channel')
                self.tune(self.channel)
                return
            self.away_until, self.next_alive = back, 0.0
            self.stats['pairing_visits'] += 1
        if self.tuned == link_pairing.SETUP_CHANNEL:
            now = time.monotonic()
            if now >= self.away_until:
                self.tune(self.channel)
            else:
                self.send_pairing_frames(session, now)

    def station_status(self, station, now):
        return {'mac': frames.mac_text(station.mac), 'aid': station.aid, 'associated': station.associated,
                'auto_reconnect': station.auto_reconnect,
                'pending_poll': station.poll_pending_until is not None,
                'pending_mgmt': len(station.management_replies),
                'searching': station.searching, 'dozing': station.dozing, 'queued': len(station.queue),
                'last_heard_s': round(now - station.last_rx, 1) if station.last_rx else None, **station.stats}

    def run(self, seconds):
        self.stop.clear()
        with self.worker_lock: self.worker_failure = None
        self.workers = {}
        try:
            for name, target in (('radio', self.radio_loop), ('tap', self.tap_loop)):
                worker = threading.Thread(target=self.worker_loop, args=(name, target),
                                          name='sonosnet-' + name, daemon=True)
                self.workers[name] = worker
                worker.start()
            self.run_main(seconds)
        finally:
            self.stop.set()
            for worker in self.workers.values(): worker.join(timeout=0.6)

    def reopen_failed(self, error):
        self.stats['radio_reopen_failures'] += 1
        if isinstance(error, subprocess.TimeoutExpired): self.stats['radio_reopen_timeouts'] += 1
        now, description = time.monotonic(), repr(error)
        if description != self.reopen_error or now - self.reopen_error_at >= 10:
            self.reopen_error, self.reopen_error_at = description, now
            self.log({'event': 'radio-reopen-failed', 'error': description,
                      'timeout': isinstance(error, subprocess.TimeoutExpired)})

    def run_main(self, seconds):
        self.log({'event': 'start', 'bssid': frames.mac_text(self.bssid),
                  'controllers': [frames.mac_text(mac) for mac in self.stations],
                  'channel': self.channel, 'household': self.household.decode(), 'tsf_lead_us': self.tsf_lead_us})
        next_stats = next_reopen = time.monotonic()
        next_beacon = None  # None: no beacon scheduled until the clock gives a grid point
        beacon_late = []
        while seconds is None or time.monotonic() - self.start < seconds:
            clock_ok = self.check_workers()
            now = time.monotonic()
            if not clock_ok:
                next_beacon = None
            elif next_beacon is None:
                next_beacon = self.beacon_deadline(now)
            if next_beacon is not None and now >= next_beacon:
                if self.tuned != self.channel:   # a pairing visit overran: beacons come first
                    self.stats['pairing_visit_overruns'] += 1
                    self.tune(self.channel)
                try:
                    beacon_late.append(self.send_beacon())
                except ClockUnavailable as error:
                    self.clock_stale(error)
                    next_beacon = None
                else:
                    self.last_beacon_at = time.monotonic()
                    next_beacon = self.beacon_deadline(self.last_beacon_at)
                now = time.monotonic()
            self.reload_reconnect_policy(now)
            self.service_pairing(time.monotonic(), next_beacon)
            now = time.monotonic()
            # Harnesses retain in-place reopen. Production must restart through
            # firmware/MPC validation and a fresh clock after any lost radio,
            # including a loss racing with the check at the top of this loop.
            if self.hardware_clock is None and self.radio_down and self.reopen and now >= next_reopen:
                next_reopen = now + REOPEN_INTERVAL
                try:
                    radio = self.reopen()
                except Exception as error:
                    self.reopen_failed(error)
                else:
                    with self.lock:
                        old, self.radio, self.radio_down = self.radio, radio, False
                    old.close()
                    self.reopen_error = None
                    self.log({'event': 'radio-reopened'})
            now = time.monotonic()  # recovery commands may have used their bounded timeout
            with self.lock:
                self.service_stations(now)
                if now >= next_stats:
                    late = sorted(beacon_late) or [0]
                    stations = [self.station_status(station, now) for station in self.stations.values()]
                    self.log({'event': 'stats', 'wall': round(time.time(), 1), 'stations': stations,
                              'associated': any(station['associated'] for station in stations),
                              'searching': any(station['searching'] for station in stations),
                              'queued': sum(station['queued'] for station in stations),
                              'workers': {name: worker.is_alive() for name, worker in self.workers.items()},
                              'clock': self.hardware_clock.status() if self.hardware_clock else None,
                              'pairing': self.pairing.status() if self.pairing else None,
                              'tuned': self.tuned, 'tune_ms': round(self.tune_cost * 1000, 1),
                              'beacons': len(beacon_late), 'beacon_late_us_p50': late[len(late) // 2],
                              'beacon_late_us_min': late[0], 'beacon_late_us_max': late[-1],
                              'beacon_grid_error_us_abs_max': max(abs(value) for value in late), **self.stats})
                    beacon_late = []
                    next_stats = now + 10
            wait = 0.002 if next_beacon is None else next_beacon - time.monotonic()
            time.sleep(max(0.0, min(0.002, wait)))
        self.check_workers()

    def beacon_deadline(self, now):
        """Next beacon time, or None (stale clock) to retry on a later pass."""
        try:
            deadline = self.next_tbtt(now)
        except ClockUnavailable as error:
            self.clock_stale(error, now)
            return None
        self.clock_fresh(now)
        return deadline


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--iface', default='mon0')
    parser.add_argument('--state', default='bridge-state.json')
    parser.add_argument('--config', type=Path, help='live per-controller automatic-reconnection preferences')
    parser.add_argument('--tap', default='sonos0')
    parser.add_argument('--addr', default='192.168.69.1/24', help='address for the Linux end of the TAP')
    parser.add_argument('--log', help='also append JSON lines here')
    parser.add_argument('--run-dir', type=Path, help='pairing requests and status (link_pairing)')
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--tsf-lead-us', type=int, default=TSF_LEAD_US,
                        help='legacy software timestamp lead in µs; required firmware overwrites the timestamp')
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
    pn_path = Path(args.state).with_suffix('.radio-pn.json')
    with PacketNumbers(pn_path, frames.mac_bytes(state['bssid']), bytes.fromhex(state['network_key'])) as numbers:
        tap_fd = open_tap(args.tap)
        try:
            # Locally administered MAC derived from the bridge's, so it is stable across runs.
            tap_mac = bytearray(frames.mac_bytes(state['bssid'])); tap_mac[0] = (tap_mac[0] | 0x02) & 0xfe
            for command in (['ip', 'link', 'set', args.tap, 'address', frames.mac_text(tap_mac)],
                            ['sysctl', '-qw', f'net.ipv6.conf.{args.tap}.disable_ipv6=1'],
                            ['ip', 'link', 'set', args.tap, 'mtu', str(TAP_MTU)],
                            ['ip', 'addr', 'replace', args.addr, 'dev', args.tap],
                            ['ip', 'link', 'set', args.tap, 'up']):
                subprocess.run(command, check=True, timeout=RADIO_COMMAND_TIMEOUT)
            channel = {2412: 1, 2437: 6, 2462: 11}[state['freq']]
            # A later firmware reset can reload a package-replaced disk image.
            # Verify every fresh socket before it is allowed to inject traffic.
            reopen = lambda: open_verified_radio(args.iface, channel)
            radio = reopen()
            clock = HardwareClock()
            try:
                if not clock.bootstrap():
                    raise ClockUnavailable('Radio hardware clock calibration failed; verify the radio firmware')
                clock.start()
                Link(state, radio, tap_fd, log, reopen, tsf_lead_us=args.tsf_lead_us,
                     pn_allocator=numbers, config_path=args.config, hardware_clock=clock,
                     run_dir=args.run_dir, state_path=args.state, iface=args.iface).run(args.seconds)
            finally:
                clock.close()
                radio.close()
        finally:
            os.close(tap_fd)


if __name__ == '__main__':
    main()
