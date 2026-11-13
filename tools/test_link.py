#!/usr/bin/env python3
"""Offline tests for sonosnet_link.py: fake radio and TAP, real CCMP, real captures."""
import errno
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccmp
import frames
import workspace
import sonosnet_link as sl
from sonosnet_link import Link

AID = 1  # legacy state receives the first association ID

HERE = Path(__file__).resolve().parent.parent
STATE = {'bssid': '02:00:00:00:00:01', 'cr100_mac': '02:00:00:00:00:02', 'household': 'Sonos_test',
         'network_key': '0123456789abcdef0123456789abcdef', 'freq': 2437}
KEY = bytes.fromhex(STATE['network_key'])
AP, CR = frames.mac_bytes(STATE['bssid']), frames.mac_bytes(STATE['cr100_mac'])


class FakeRadio:
    def __init__(self):
        self.sent = []
        self._idle = socket.socketpair()  # selectable, never readable: an idle radio
    def fileno(self): return self._idle[0].fileno()
    def recv(self, size): return self._idle[0].recv(size)
    def close(self): pass
    def send(self, packet, flags=0):
        assert packet.startswith(frames.TX_RADIOTAP)
        self.sent.append(packet[len(frames.TX_RADIOTAP):])


def pcap_packets(path):
    data = path.read_bytes(); offset = 24
    while offset + 16 <= len(data):
        _, _, length, _ = struct.unpack_from('<IIII', data, offset); offset += 16
        yield data[offset:offset + length]; offset += length


def test_radiotap_on_pi_capture():
    import json
    # Private capture credentials are supplied locally, never embedded in the distribution.
    private = json.loads((workspace.CAPTURES / 'bridge-state.json').read_text())
    capture_cr = frames.mac_bytes(private['cr100_mac'])
    capture_key = bytes.fromhex(private['network_key'])
    # Pi captures carry TSFT, flags with FCS, rate, channel, antenna signal.
    capture = workspace.CAPTURES / 'Pi T4 CR100 associated, encrypted data (channel 6).pcap'
    checked = 0
    for packet in pcap_packets(capture):
        raw, rssi = frames.parse_radiotap(packet)
        if raw[10:16] == capture_cr and raw[0] == 0x08 and raw[1] & 0x40:
            ccmp.decrypt(raw, capture_key)  # FCS correctly stripped, or the MIC would fail
            assert -90 < rssi < 0
            checked += 1
    assert checked > 100, checked
    return checked


def make_link():
    # Datagram pair: one read per frame, like a TAP.
    tap_side, test_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    logs = []
    link = Link(STATE, FakeRadio(), tap_side.fileno(), logs.append)
    link._keep = (tap_side, test_side)  # closing either end would end the TAP reader
    return link, test_side, logs


def cr100_data(inner, pn, pm=False, retry=False):
    header = bytes((0x08, 0x41 | (0x10 if pm else 0) | (0x08 if retry else 0))) + b'\0\0' + AP + CR + AP + b'\x10\x00'
    return ccmp.encrypt(header, ccmp.SNAP_PREFIX + b'\x69\x69' + inner, KEY, pn)


def decode_tx(frame):
    assert frame[0] == 0x08 and frame[1] & 0x42 == 0x42 and frame[4:10] == CR and frame[10:16] == AP
    pn, key_id, plain = ccmp.decrypt(frame, KEY)
    assert key_id == 2 and plain[:8] == bytes.fromhex('aaaa030000006969')
    return pn, bool(frame[1] & 0x20), plain[8:]


def pspoll(pm):
    return bytes((0xa4, 0x11 if pm else 0x01)) + struct.pack('<H', 0xc000 | AID) + AP + CR


def null_frame(pm):
    return bytes((0x48, 0x11 if pm else 0x01)) + b'\0\0' + AP + CR + AP + b'\x20\x00'


def drain(link, seconds=1):
    """The main loop's job, inline: send what the pacer held back while the CR100 stays awake."""
    deadline = time.monotonic() + seconds
    while link.stations[CR].queue and time.monotonic() < deadline:
        link.stations[CR].last_rx = link.stations[CR].last_pm_rx = time.monotonic()  # CR100 still awake and talking
        with link.lock: link.flush(link.stations[CR])
        time.sleep(0.001)


def test_link():
    link, tap_read, logs = make_link()
    radio = link.radio
    inner = b'\xff' * 6 + bytes.fromhex('dea632e73b0e') + b'\x08\x00' + b'x' * 50

    link.from_tap(inner)  # not associated yet: dropped
    assert not radio.sent and link.stats['tx_not_associated'] == 1

    assoc = b'\x00\x01\x00\x00' + AP + CR + AP + b'\x10\x00' + bytes.fromhex('1003f401dd06000e58020101')
    link.on_frame(assoc)
    resp = radio.sent.pop()
    assert resp[:2] == b'\x10\x02' and resp[4:10] == CR and resp[24:] == bytes.fromhex('1003000001c0')
    assert link.stations[CR].associated

    # Just associated: awake, so frames go straight out (the ZP80 did, 13 ms after association).
    time.sleep(0.003)  # past the pacer's window after the association response
    link.from_tap(inner)
    pn1, more, got = decode_tx(radio.sent.pop())
    assert got == inner and not more

    # Doze: queue, TIM bit 1 in the next beacon. A PS-Poll releases one frame whatever its
    # PM bit (ZP80 handle_ps_poll); a Null frame with PM clear wakes it and releases the rest.
    link.stations[CR].dozing = True
    for i in range(3): link.from_tap(inner[:-1] + bytes((i,)))
    assert not radio.sent and len(link.stations[CR].queue) == 3
    body = frames.beacon_body(0, 6, AID)
    assert body[body.index(bytes((5, 11))) + 5] == 0x02
    time.sleep(0.007)  # outside the pacer's window, as a real poll would be
    link.on_frame(pspoll(pm=False))
    assert len(radio.sent) == 1 and len(link.stations[CR].queue) == 2 and link.stations[CR].dozing
    time.sleep(0.007)
    link.on_frame(null_frame(pm=False))
    drain(link)
    out = [decode_tx(f) for f in radio.sent]; radio.sent.clear()
    assert [o[1] for o in out] == [True, True, False]
    assert [o[2][-1] for o in out] == [0, 1, 2]
    # Paced/busy attempts also consume a nonce; accepted packets must increase.
    numbers = [pn1] + [o[0] for o in out]
    assert all(left < right for left, right in zip(numbers, numbers[1:]))

    # PS-Poll with PM set also releases one frame at a time.
    link.stations[CR].dozing = True
    for i in range(2): link.from_tap(inner)
    time.sleep(0.007)
    link.on_frame(pspoll(pm=True))
    assert len(radio.sent) == 1 and decode_tx(radio.sent.pop())[1] is True
    time.sleep(0.007)
    link.on_frame(pspoll(pm=True))
    assert decode_tx(radio.sent.pop())[1] is False and not link.stations[CR].queue

    # Receive: decrypt, untunnel, write the inner frame to the TAP; drop retransmissions.
    rx_inner = b'\xff' * 6 + CR + b'\x08\x06' + b'a' * 28
    link.on_frame(cr100_data(rx_inner, 0))
    link.on_frame(cr100_data(rx_inner, 0, retry=True))
    link.on_frame(cr100_data(rx_inner, 1, pm=True))
    assert tap_read.recv(4096) == rx_inner and tap_read.recv(4096) == rx_inner
    assert link.stats['rx_data'] == 2 and link.stats['rx_replay'] == 1

    # A frame with a bad MIC is counted, not delivered.
    bad = bytearray(cr100_data(rx_inner, 2)); bad[-1] ^= 1
    link.on_frame(bytes(bad))
    assert link.stats['rx_decrypt_fail'] == 1

    # Frames from anyone else are ignored.
    link.on_frame(cr100_data(rx_inner, 3)[:10] + b'\x02' * 6 + cr100_data(rx_inner, 3)[16:])
    assert link.stats['rx_data'] == 2
    return dict(link.stats)


def test_resume_after_bridge_restart():
    link, tap_read, logs = make_link()
    rx_inner = b'\xff' * 6 + CR + b'\x08\x06' + b'a' * 28
    link.on_frame(cr100_data(rx_inner, 500))  # no association request seen by this process
    assert link.stations[CR].associated and tap_read.recv(4096) == rx_inner
    assert [r['event'] for r in logs] == ['rx', 'resumed']
    link2, _, _ = make_link()
    link2.on_frame(bytes((0x80, 0)) + b'\0\0' + frames.BROADCAST + CR + CR + b'\0\0' + bytes(12))
    assert not link2.stations[CR].associated  # management frames do not resume


def test_oversize_frame_is_not_radio_loss():
    class SmallRadio(FakeRadio):
        def send(self, packet, flags=0):
            if len(packet) > 1500: raise OSError(errno.EMSGSIZE, 'Message too long')
            super().send(packet)
    link, _, _ = make_link()
    link.radio = SmallRadio()
    link.stations[CR].associated, link.stations[CR].dozing, link.stations[CR].last_rx = True, False, time.monotonic()
    link.stations[CR].last_pm_rx = link.stations[CR].last_rx
    link.from_tap(CR + bytes(8 + 1500))   # too big for mon0: dropped and counted
    link.from_tap(CR + bytes(8 + 1400))   # TAP_MTU-sized: fits
    assert not link.radio_down and link.stats['tx_error_EMSGSIZE'] == 1 and len(link.radio.sent) == 1
    assert len(link.radio.sent[0]) + 8 <= 1500


def test_receive_follows_reopened_radio():
    # The receive thread must pick up the replacement socket, not stay on the dead one.
    old_a, old_b = socket.socketpair()
    new_a, new_b = socket.socketpair()
    link, tap_read, _ = make_link()
    link.radio = old_a
    threading.Thread(target=link.radio_loop, daemon=True).start()
    time.sleep(0.1)
    link.radio = new_a; old_a.close()          # what the reopen path does
    rx_inner = b'\xff' * 6 + CR + b'\x08\x06' + b'a' * 28
    new_b.send(frames.MIN_RADIOTAP + cr100_data(rx_inner, 7))
    tap_read.settimeout(3)
    assert tap_read.recv(4096) == rx_inner


def test_beacons_on_tsf_grid():
    # Verify the scheduling origin and recovery from a late wakeup. A desktop
    # scheduler cannot promise a two-millisecond deadline, and injected packet
    # timestamps would not establish actual on-air timing even if it could.
    interval = frames.BEACON_INTERVAL_TU * 1024
    worst_grid_error = 0
    for oversleep_us in (0, 23000):
        origin_ns = 1_000_000_000_000
        now_ns = origin_ns
        delayed = False

        def monotonic():
            return now_ns / 1e9

        def sleep(seconds):
            nonlocal now_ns, delayed
            target = now_ns + max(1, round(seconds * 1e9))
            if not delayed and target >= origin_ns + interval * 1000:
                target += oversleep_us * 1000
                delayed = True
            now_ns = target

        clock = SimpleNamespace(monotonic=monotonic, sleep=sleep, time=lambda: 1_700_000_000 + monotonic())
        with patch.object(sl, 'time', clock):
            link, _, _ = make_link()
            try:
                link.stations[CR].associated = True
                link.stations[CR].last_rx = monotonic()
                sleep(.03)  # Construction/run gap must not become the beacon origin.
                link.run_main(seconds=1.1)
                stamps = [struct.unpack_from('<Q', f, 24)[0] for f in link.radio.sent if f[0] == 0x80]
            finally:
                for resource in (*link._keep, *link.radio._idle): resource.close()
        assert len(stamps) == 10, len(stamps)
        offsets = [(stamp - sl.TSF_LEAD_US + interval // 2) % interval - interval // 2
                   for stamp in stamps]
        assert abs(offsets[0] - oversleep_us) <= 2, offsets
        # A late beacon must not shift all later beacons off the original grid.
        assert all(abs(offset) <= 2 for offset in offsets[1:]), offsets
        if not oversleep_us:
            assert all(abs((b - a) - interval) <= 2 for a, b in zip(stamps, stamps[1:]))
            worst_grid_error = max(abs(offset) for offset in offsets)
    return worst_grid_error


def test_busy_radio_keeps_frames_and_tracks_doze():
    class BusyRadio(FakeRadio):
        busy = True
        def send(self, packet, flags=0):
            if self.busy and flags & socket.MSG_DONTWAIT: raise OSError(errno.EAGAIN, 'busy')
            super().send(packet)
    link, _, _ = make_link()
    link.radio = BusyRadio()
    link.stations[CR].associated, link.stations[CR].dozing, link.stations[CR].last_rx = True, False, time.monotonic()
    link.stations[CR].last_pm_rx = link.stations[CR].last_rx
    inner = b'\xff' * 6 + bytes(6) + b'\x08\x00' + b'x' * 40
    link.from_tap(inner); link.from_tap(inner)
    assert len(link.stations[CR].queue) == 2 and link.stats['tx_busy'] == 2   # both tries kept, nothing lost
    link.radio.busy = False
    link.on_frame(null_frame(pm=True))     # dozing now
    link.on_frame(pspoll(pm=False))        # releases one; a PS-Poll does not wake it
    assert link.stations[CR].dozing and len(link.stations[CR].queue) == 1 and len(link.radio.sent) == 1
    link.from_tap(inner)                   # dozing: queued, not sent
    assert len(link.stations[CR].queue) == 2 and len(link.radio.sent) == 1
    link.on_frame(null_frame(pm=False))    # awake: all
    drain(link)
    assert not link.stations[CR].dozing and not link.stations[CR].queue and len(link.radio.sent) == 3
    link.stations[CR].last_pm_rx -= 2                   # silent for 2 s: treat as dozing again
    link.from_tap(inner)
    assert len(link.stations[CR].queue) == 1


def test_pacing_spaces_frames_for_firmware():
    class TimedRadio(FakeRadio):
        def __init__(self):
            super().__init__(); self.times = []
        def send(self, packet, flags=0):
            self.times.append(time.monotonic()); super().send(packet)
    link, _, _ = make_link()
    link.radio = TimedRadio()
    link.stations[CR].associated, link.stations[CR].dozing, link.stations[CR].last_rx = True, True, time.monotonic()
    inner = b'\xff' * 6 + bytes(6) + b'\x08\x00' + b'x' * 400
    for _ in range(12): link.from_tap(inner)             # dozing: all queued
    link.on_frame(null_frame(pm=False))                  # awake: release the queue
    assert 0 < len(link.radio.sent) <= sl.PACE_FRAMES and link.stats['tx_paced'] >= 1
    drain(link)
    assert not link.stations[CR].queue and len(link.radio.sent) == 12
    t = link.radio.times
    n, window = sl.PACE_FRAMES, sl.PACE_WINDOW
    assert all(t[i + n] - t[i] >= window - 1e-4 for i in range(len(t) - n)), [round(b - a, 4) for a, b in zip(t, t[n:])]
    more = [bool(f[1] & 0x20) for f in link.radio.sent]
    assert more == [True] * 11 + [False]


def test_no_unprompted_frames_to_associated_cr100():
    # Associated and silent past LINK_SILENT: still associated, queue kept, no probe
    # responses (they would be retried to a dozing CR100 and delay beacons); only logged.
    link, _, logs = make_link()
    inner = b'\xff' * 6 + bytes(6) + b'\x08\x00' + b'x' * 40
    link.stations[CR].associated, link.stations[CR].last_rx = True, time.monotonic() - sl.LINK_SILENT - 1
    link.from_tap(inner)
    link.run(seconds=0.3)
    assert link.stations[CR].associated and len(link.stations[CR].queue) == 1
    assert not [f for f in link.radio.sent if f[0] != 0x80], 'only beacons'
    assert [r['event'] for r in logs if r['event'].startswith('link')] == ['link-silent']
    link.on_frame(pspoll(pm=True))
    assert [r['event'] for r in logs if r['event'].startswith('link')] == ['link-silent', 'link-heard']
    # A probe request is answered while associated too. Pacer/backpressure
    # deferral has separate tests; the preceding PS-Poll just used the pacer.
    link.radio.sent.clear()
    with patch.object(sl, 'PACE_WINDOW', 0):
        link.on_frame(bytes((0x40, 0)) + b'\0\0' + frames.BROADCAST + CR + frames.BROADCAST + b'\0\0' + bytes((0, 0)))
    assert len(link.radio.sent) == 1 and link.radio.sent[0][:2] == b'\x50\x00' and link.radio.sent[0][4:10] == CR


def test_silent_cr100_checked_with_tim_then_probed():
    link, _, logs = make_link()
    link.stations[CR].associated = True
    tim_bit = lambda f: f[0] == 0x80 and f[f.index(bytes((5, 11))) + 5] == 0x02
    # Silent past CHECK_SILENCE: beacons advertise TIM (no unicast frames) for CHECK_WINDOW.
    link.stations[CR].last_rx = time.monotonic() - sl.CHECK_SILENCE - 1
    link.run(seconds=0.35)
    assert link.stations[CR].check_from is not None and not link.stations[CR].searching
    assert any(tim_bit(f) for f in link.radio.sent) and all(f[0] == 0x80 for f in link.radio.sent)
    # A dozing CR100 answers with a PS-Poll: check over, nothing else happens.
    link.on_frame(pspoll(pm=True))
    assert link.stations[CR].check_from is None and not link.stations[CR].searching
    # No answer within the window: searching, so unicast probe responses until it is heard.
    with link.lock:
        link.stations[CR].last_rx = time.monotonic() - sl.CHECK_SILENCE - 3
        link.stations[CR].check_from = time.monotonic() - sl.CHECK_WINDOW - 0.1
        link.check_hearing(link.stations[CR], time.monotonic())
    assert link.stations[CR].searching and logs[-1]['reason'] == 'check-unanswered'
    link.radio.sent.clear()
    link.start = time.monotonic()  # run() counts its seconds from here
    link.run(seconds=0.35)
    probes =[f for f in link.radio.sent if f[0] == 0x50]
    assert len(probes) >= 3 and all(f[4:10] == CR for f in probes)
    link.on_frame(cr100_data(b'\xff' * 6 + CR + b'\x08\x06' + b'a' * 28, 1))
    assert not link.stations[CR].searching and logs[-1]['event'] == 'link-heard' and logs[-1]['was_searching']
    # Frames queued with TIM set and never polled for mean the same thing.
    link.stations[CR].dozing = True
    link.from_tap(b'\xff' * 6 + bytes(6) + b'\x08\x00' + b'x' * 40)
    link.stations[CR].queue[0] = (time.monotonic() - sl.CHECK_WINDOW - 0.1, link.stations[CR].queue[0][1])
    link.stations[CR].last_rx = link.stations[CR].queue[0][0] - 1
    with link.lock: link.check_hearing(link.stations[CR], time.monotonic())
    assert link.stations[CR].searching and logs[-1]['reason'] == 'tim-unanswered'


def test_unpolled_frames_expire_and_clear_tim_while_cr100_sleeps():
    link, _, logs = make_link()
    station = link.stations[CR]
    tim_bit = lambda f: f[0] == 0x80 and f[f.index(bytes((5, 11))) + 5] == 0x02
    station.associated, station.dozing = True, True
    station.last_rx = time.monotonic() - 10
    link.from_tap(b'\xff' * 6 + bytes(6) + b'\x08\x00' + b'x' * 40)
    queued_at = time.monotonic() - sl.QUEUE_TTL - 0.1
    station.queue[0] = (queued_at, station.queue[0][1])
    with link.lock: link.service_stations(time.monotonic())
    # The same pass still reports the unanswered TIM, then drops the frame.
    assert station.searching and logs[-1]['reason'] == 'tim-unanswered'
    assert not station.queue and station.stats['tx_expired'] == 1
    link.radio.sent.clear()
    link.start = time.monotonic()
    link.run(seconds=0.25)
    beacons = [f for f in link.radio.sent if f[0] == 0x80]
    assert beacons and not any(tim_bit(f) for f in beacons)


def test_unassociated_probes_often():
    # A searching CR100 only takes its AP from probe responses, caught while it dwells on
    # our channel; once associated they stop (see the test above).
    link, _, _ = make_link()
    link.run(seconds=0.55)
    probes = [f for f in link.radio.sent if f[0] == 0x50]
    assert 5 <= len(probes) <= 7, len(probes)


def test_radio_loss_and_reopen():
    class DeadRadio(FakeRadio):
        def send(self, packet, flags=0): raise OSError(errno.ENXIO, 'No such device or address')
    fresh = FakeRadio()
    link, _, logs = make_link()
    link.radio, link.reopen = DeadRadio(), lambda: fresh
    link.send_mgmt(8, frames.BROADCAST, frames.beacon_body(0, 6))
    assert link.radio_down and link.stats['radio_lost'] == 1
    link.send_mgmt(8, frames.BROADCAST, frames.beacon_body(0, 6))  # no second error while down
    assert link.stats['radio_lost'] == 1
    link.run(seconds=0.25)  # the loop reopens the radio and resumes beacons on it
    assert not link.radio_down and link.radio is fresh and any(f[0] == 0x80 for f in fresh.sent)
    assert [r['event'] for r in logs if r['event'].startswith('radio')] == ['radio-lost', 'radio-reopened']


if __name__ == '__main__':
    if ((workspace.CAPTURES / 'Pi T4 CR100 associated, encrypted data (channel 6).pcap').exists()
            and (workspace.CAPTURES / 'bridge-state.json').exists()):
        print('radiotap: decrypted', test_radiotap_on_pi_capture(), 'CR100 frames from the Pi capture')
    else:
        print('Private on-air capture is absent; running synthetic link tests.')
    print('link:', test_link())
    test_resume_after_bridge_restart()
    test_oversize_frame_is_not_radio_loss()
    test_receive_follows_reopened_radio()
    test_busy_radio_keeps_frames_and_tracks_doze()
    test_pacing_spaces_frames_for_firmware()
    print('beacons: deterministic scheduling-grid error', test_beacons_on_tsf_grid(), 'µs')
    test_no_unprompted_frames_to_associated_cr100()
    test_silent_cr100_checked_with_tim_then_probed()
    test_unpolled_frames_expire_and_clear_tim_while_cr100_sleeps()
    test_unassociated_probes_often()
    test_radio_loss_and_reopen()
    print('ok')
