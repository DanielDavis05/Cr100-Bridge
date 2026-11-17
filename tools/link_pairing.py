"""Pair CR100s inside the running SonosNet link, so paired controllers stay connected.

A CR100 in add-to-household setup mode sends its Netstart QUERY only on the setup
channel, 1 (STATIC-FINDINGS-4.3.2 "T2 attempt 1"), and resends it every 250 ms for
20 s. A household on channel 1 answers it with no channel change. On channel 6 or 11
the link visits channel 1 between beacons while a pairing window is open: it stays
home after each beacon for the dozing controllers' PS-Polls, then spends the rest of
the interval on channel 1 sending ALIVE and answering QUERYs.

The pairing command (``bridge_service.py pair``) opens a window by writing
``pair-request.json`` in the run directory, and reads progress from
``pair-status.json``. A new controller is saved to the pairing state and added as a
station before its RESPONSE is released, so its first association is recognised.
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import netstart_pair as netstart
from controller_state import normalize_mac

PAIR_REQUEST, PAIR_STATUS = 'pair-request.json', 'pair-status.json'
SETUP_CHANNEL, SETUP_FREQ = 1, 2412
CHANNEL_FREQ = {1: 2412, 6: 2437, 11: 2462}
PAIR_SECONDS = 180.0        # the window the pairing command asks for
LINGER = 5.0                # s after the last QUERY: the CR100 stops once it has a RESPONSE
ALIVE_INTERVAL = 0.1        # as the standalone pairing tool and the ZP80
REQUEST_CHECK_INTERVAL = 0.5
# Hop timing within one 102.4 ms beacon interval. Changing channel took 9-11 ms on the
# Pi 3 B+ bridge (iw, 2026-11-17). A 20 s window there made 185 visits with every beacon
# sent and no clock or radio faults. Stay home long enough after a beacon for a dozing
# controller's PS-Poll and its frame, and be back before the next.
HOME_DWELL = 0.040
RETURN_MARGIN = 0.015
RETURN_SLACK = 0.005      # beyond the slowest recent channel change (the link measures it)
MIN_AWAY = 0.020
PENDING_LIMIT = 32
RESPONSE_COPIES = 2


def read_request(path, now=None):
    """Return a valid, unexpired request {'id', 'until'} or None."""
    now = time.time() if now is None else now
    try:
        request = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(request, dict): return None
    request_id, until = request.get('id'), request.get('until')
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64: return None
    if type(until) not in (int, float) or until <= now: return None
    return {'id': request_id, 'until': float(until)}


def write_json_atomic(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(json.dumps(value) + '\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Session:
    """One pairing window: Netstart replies, persistence gate and progress.

    ``persist(record)`` must save the controller and make the link accept it; until it
    succeeds no RESPONSE is released (the CR100 resends its QUERY every 250 ms).
    """
    def __init__(self, request, state, persist, log, *, clock=time.monotonic, wall=time.time):
        self.id, self.until = request['id'], request['until']
        self.persist, self.log, self.clock, self.wall = persist, log, clock, wall
        bssid = normalize_mac(state['bssid'])
        self.bssid = bssid
        self.pairer = netstart.Pairer(netstart.player_uuid(bssid), state['household'].encode(),
                                      bytes.fromhex(state['network_key']), state['freq'],
                                      log=lambda record: log({**record, 'pairing': self.id}))
        self.alive = self.pairer.alive()
        self.persisted = {}       # mac -> record
        self.errors = collections.Counter()
        self.last_query = None
        self.result = None        # 'done', 'expired' or 'failed' once finished
        self.error = None
        self.pending = collections.OrderedDict()   # payload -> copies still to send on the setup channel
        self.lock = threading.Lock()
        self.persist_lock = threading.Lock()

    def receive(self, src, payload):
        """Radio thread: one Netstart message from ``src`` (aa:bb:..)."""
        try:
            src = normalize_mac(src)
            replies = self.pairer.handle(src, payload)
        except ValueError:
            self.errors['bad'] += 1
            return
        if not replies: return
        with self.lock:
            if self.result is not None: return
            self.last_query = self.clock()
            record = self.pairer.paired_controllers.get(src)
        # persist takes the link's locks: never call it holding self.lock, which the
        # link's stats line takes inside its own lock.
        if record is not None and src not in self.persisted:
            with self.persist_lock:
                if src not in self.persisted:
                    try:
                        self.persist({'mac': src, 'txid': record['txid'], 'time': round(record['time'], 3)})
                    except Exception as error:     # retried on the next QUERY
                        self.errors['persist'] += 1
                        self.log({'event': 'pairing-save-failed', 'mac': src, 'error': str(error)})
                        return
                    with self.lock: self.persisted[src] = record
                    self.log({'event': 'controller-paired', 'mac': src, 'pairing': self.id})
        with self.lock:
            if self.result is not None: return
            for reply in replies:
                if reply not in self.pending and len(self.pending) >= PENDING_LIMIT:
                    self.pending.popitem(last=False)
                self.pending[reply] = RESPONSE_COPIES

    def next_reply(self):
        with self.lock:
            return next(iter(self.pending), None)

    def reply_sent(self, reply):
        with self.lock:
            left = self.pending.get(reply)
            if left is None: return
            if left <= 1: del self.pending[reply]
            else: self.pending[reply] = left - 1

    def finish_if_due(self):
        """Return the result once the window is over (main loop)."""
        with self.lock:
            if self.result is None:
                if self.persisted and self.clock() - self.last_query >= LINGER: self.result = 'done'
                elif self.wall() >= self.until: self.result = 'done' if self.persisted else 'expired'
            return self.result

    def fail(self, error):
        with self.lock:
            if self.result is None: self.result, self.error = 'failed', str(error)

    def status(self, **extra):
        with self.lock:
            return {'id': self.id, 'state': self.result or ('paired' if self.persisted else 'listening'),
                    'controllers': sorted(self.persisted), 'error': self.error, 'until': self.until,
                    'time': round(self.wall(), 3), **extra}
