"""Controller identity and addressed CR100 policy notices.

Policies are keyed by stable MAC addresses. Requests are identified by the actual
socket peer, DHCP leases and current neighbours, never caller-supplied headers.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

MAC = re.compile(r'[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z')


class ControllerRegistry:
    def __init__(self, state_path, leases_file=None, iface='sonos0', neighbours=None):
        self.state_path = Path(state_path)
        self.leases_file = Path(leases_file) if leases_file else None
        self.iface = iface
        self.neighbours = neighbours or self._neighbours
        self.lock = threading.Lock()
        self.cached = (0.0, {})

    def _neighbours(self):
        try:
            return subprocess.run(['ip', 'neigh', 'show', 'dev', self.iface],
                                  capture_output=True, text=True, timeout=1).stdout
        except (OSError, subprocess.SubprocessError):
            return ''

    def _mapping(self):
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        known = {str(x.get('mac', '')).lower() for x in state.get('controllers', []) if isinstance(x, dict)}
        known = {x for x in known if MAC.fullmatch(x)}
        mapping = {}
        if self.leases_file:
            try:
                for line in self.leases_file.read_text().splitlines():
                    fields = line.split()
                    if len(fields) < 3: continue
                    expiry, mac, address = fields[:3]
                    if mac.lower() not in known: continue
                    try:
                        if int(expiry) != 0 and int(expiry) <= time.time(): continue
                        address = str(ipaddress.IPv4Address(address))
                    except ValueError: continue
                    mapping[address] = mac.lower()
            except OSError:
                pass
        # A current neighbour overrides a lease whose IP has changed owners.
        for line in self.neighbours().splitlines():
            fields = line.split()
            if len(fields) < 4 or 'lladdr' not in fields or fields[-1] in ('FAILED', 'INCOMPLETE'): continue
            try:
                address = str(ipaddress.IPv4Address(fields[0]))
                mac = fields[fields.index('lladdr') + 1].lower()
            except (ValueError, IndexError): continue
            mapping.pop(address, None)
            if mac in known: mapping[address] = mac
        return mapping

    def addresses(self):
        """Current known controller addresses, shared with discovery recipients."""
        with self.lock:
            when, mapping = self.cached
            if time.monotonic() - when >= 1:
                mapping = self._mapping()
                self.cached = (time.monotonic(), mapping)
            return dict(mapping)

    def __call__(self, address):
        try:
            address = str(ipaddress.IPv4Address(address))
        except (ValueError, TypeError):
            return None
        return self.addresses().get(address)


def notice_body(status, message):
    event = ET.Element('Event', xmlns='urn:schemas-upnp-org:metadata-1-0/AVT/')
    instance = ET.SubElement(event, 'InstanceID', val='0')
    ET.SubElement(instance, 'TransportStatus', val=status)
    ET.SubElement(instance, 'TransportErrorDescription', val=message)
    root = ET.Element('e:propertyset', {'xmlns:e': 'urn:schemas-upnp-org:event-1-0'})
    ET.SubElement(ET.SubElement(root, 'e:property'), 'LastChange').text = ET.tostring(event, encoding='unicode')
    return ET.tostring(root, encoding='utf-8')


class ControllerNotices:
    """Send an error/clear pulse only to the requester's AVTransport subscribers.

    This uses normal per-subscription SID/sequence locks, while preserving actual
    player state. An OK update clears the synthetic status; the stock popup keeps
    its normal key dismissal and timeout. No audio transport command is sent.
    """
    def __init__(self, players, log=print, delay=.2, clear_delay=2.0):
        self.players, self.log, self.delay, self.clear_delay = players, log, delay, clear_delay
        self.lock = threading.Lock()
        self.last = {}
        self.pending = {}
        self.closed = threading.Event()

    def close(self):
        self.closed.set()

    def __call__(self, address, message):
        try:
            address = str(ipaddress.IPv4Address(address))
        except (TypeError, ValueError):
            return
        with self.lock:
            if self.closed.is_set(): return
            now = time.monotonic()
            if now - self.last.get(address, -1) < .75: return
            self.last = {ip: stamp for ip, stamp in self.last.items() if now - stamp < 30}
            self.last[address] = now
            previous = self.pending.get(address)
            pulse = (object(), previous[1] if previous else threading.Lock())
            self.pending[address] = pulse
        worker = threading.Thread(target=self._deliver, args=(address, str(message), pulse), daemon=True,
                                  name='spotify-policy-notice')
        worker.start()

    def _current(self, address, pulse):
        with self.lock:
            return not self.closed.is_set() and self.pending.get(address) is pulse

    def _deliver(self, address, message, pulse):
        try:
            if self.closed.wait(self.delay): return
            error = notice_body('ERROR_BRIDGE_POLICY', message)
            recipients = []
            # Serialize only this controller's sends, not incoming policy checks.
            # A newer pulse may replace this generation while a send is in flight;
            # its ERROR then follows that send, and this generation cannot clear it.
            with pulse[1]:
                if not self._current(address, pulse): return
                for player in self.players:
                    with player.state.lock:
                        ids = [sid for sid, sub in player.state.subscriptions.items()
                               if sub.get('service') == 'AVTransport'
                               and sub.get('expires', 0) > time.monotonic()
                               and (sub.get('source') or urlsplit(sub.get('callback', '')).hostname) == address]
                    for sid in ids:
                        if not self._current(address, pulse): return
                        player.notify(sid, body_override=error)
                        recipients.append((player, sid))
            self.log({'event': 'spotify-policy-notice', 'controller_ip': address,
                      'subscriptions': len(recipients)})
            # Back-to-back ERROR/OK updates can be coalesced before the stock
            # Flash UI sees the error. Hold it long enough to render the popup.
            if not recipients or self.closed.wait(self.clear_delay): return
            clear = notice_body('OK', '')
            with pulse[1]:
                for player, sid in recipients:
                    if not self._current(address, pulse): return
                    player.notify(sid, body_override=clear)
        finally:
            with self.lock:
                if self.pending.get(address) is pulse:
                    self.pending.pop(address)
