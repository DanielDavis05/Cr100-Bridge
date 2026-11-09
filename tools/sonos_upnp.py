"""Local UPnP for real Sonos players (S1 and S2, port 1400): SOAP actions, DIDL-Lite,
GENA events, zone group topology and discovery, as the Sonos backend needs them.

Seen on a ZP80 with S1 11.16.1 (2026-11): AVTransport, RenderingControl and Queue events
carry a LastChange document; ContentDirectory events carry ContainerUpdateIDs ("Q:0,2")
and *UpdateID properties; ZoneGroupTopology events carry ZoneGroupState. Events arrive
2-8 s after a change and coalesce several changes, so the backend also reads state
itself after acting. A group member answers AVTransport for its coordinator only
partly (GetTransportInfo always PLAYING), so transport goes to the coordinator.
"""
from __future__ import annotations

import html
import http.client
import http.server
import itertools
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

PORT = 1400
SOAP_TIMEOUT = 5
SUBSCRIBE_SECONDS = 1800
RENEW_MARGIN = 120          # s before expiry a subscription is renewed
RETRY_DELAY = 10            # s between attempts to subscribe again after a failure
SEARCH_TARGET = 'urn:schemas-upnp-org:device:ZonePlayer:1'
BRIDGE_PLAYERS = 'RINCON_0269'   # the bridge's own virtual players (zoneplayers.player_mac 02:69:...)

SERVICES = {   # name -> (path, service type); /Control and /Event below the path
    'AVTransport': ('/MediaRenderer/AVTransport', 'urn:schemas-upnp-org:service:AVTransport:1'),
    'RenderingControl': ('/MediaRenderer/RenderingControl', 'urn:schemas-upnp-org:service:RenderingControl:1'),
    'Queue': ('/MediaRenderer/Queue', 'urn:schemas-sonos-com:service:Queue:1'),
    'ContentDirectory': ('/MediaServer/ContentDirectory', 'urn:schemas-upnp-org:service:ContentDirectory:1'),
    'ZoneGroupTopology': ('/ZoneGroupTopology', 'urn:schemas-upnp-org:service:ZoneGroupTopology:1'),
    'DeviceProperties': ('/DeviceProperties', 'urn:schemas-upnp-org:service:DeviceProperties:1'),
    'AudioIn': ('/AudioIn', 'urn:schemas-upnp-org:service:AudioIn:1'),
}
NS = {'s': 'http://schemas.xmlsoap.org/soap/envelope/', 'u': 'urn:schemas-upnp-org:control-1-0',
      'd': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/', 'dc': 'http://purl.org/dc/elements/1.1/',
      'upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/', 'r': 'urn:schemas-rinconnetworks-com:metadata-1-0/',
      'e': 'urn:schemas-upnp-org:event-1-0'}
DIDL_OPEN = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
             'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">')


class UPnPFault(Exception):
    """A failed action; code is the UPnP error code (701, 800, ...) or None."""

    def __init__(self, action, code=None, detail=''):
        super().__init__('%s failed%s%s' % (action, ' (UPnP error %s)' % code if code else '',
                                            ': ' + detail if detail else ''))
        self.code = code


def base_url(location):
    """http://ip:1400 from a device description URL."""
    parts = urlsplit(location)
    return '%s://%s' % (parts.scheme or 'http', parts.netloc)


def local(tag):
    return tag.rpartition('}')[2]


def soap(base, service, action, args=(), timeout=SOAP_TIMEOUT):
    """Run one action; returns its output arguments by name ('' for empty ones)."""
    path, service_type = SERVICES[service]
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s>'
            '</s:Body></s:Envelope>') % (action, service_type, ''.join(
                '<%s>%s</%s>' % (name, html.escape(str(value), quote=False), name) for name, value in args), action)
    request = urllib.request.Request(base + path + '/Control', body.encode(), {
        'Content-Type': 'text/xml; charset="utf-8"', 'SOAPACTION': '"%s#%s"' % (service_type, action)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response: data = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        code = re.search(rb'<errorCode>(\d+)</errorCode>', error.read(65536) or b'')
        raise UPnPFault(action, int(code.group(1)) if code else None, 'HTTP %d' % error.code) from None
    except (OSError, http.client.HTTPException) as error:
        raise UPnPFault(action, None, str(error)) from error
    try:
        answer = ET.fromstring(data).find('.//{%s}%sResponse' % (service_type, action))
    except ET.ParseError as error:
        raise UPnPFault(action, None, 'unreadable answer') from error
    return {local(child.tag): child.text or '' for child in answer} if answer is not None else {}


# --- DIDL-Lite ---------------------------------------------------------------------
def parse_didl(text):
    """The items and containers in a DIDL-Lite document, as plain dicts."""
    if not text or not text.strip(): return []
    try: root = ET.fromstring(text)
    except ET.ParseError: return []
    entries = []
    for element in root:
        tag = local(element.tag)
        if tag not in ('item', 'container'): continue
        def child(name, ns='dc'):
            found = element.find('%s:%s' % (ns, name), NS)
            return (found.text or '').strip() if found is not None else ''
        res = element.find('d:res', NS)
        count = element.get('childCount')
        entries.append({
            'container': tag == 'container', 'id': element.get('id') or '', 'parent': element.get('parentID') or '',
            'title': child('title'), 'class': child('class', 'upnp'),
            'artist': child('creator') or child('artist', 'upnp') or child('albumArtist', 'r'),
            'album': child('album', 'upnp'), 'art': child('albumArtURI', 'upnp'),
            'res': (res.text or '').strip() if res is not None else '',
            'duration': res.get('duration', '') if res is not None else '',
            'res_md': child('resMD', 'r'), 'stream_content': child('streamContent', 'r'),
            'radio_show': child('radioShowMd', 'r'), 'description': child('description', 'r'),
            'child_count': int(count) if count and count.isdigit() else None,
        })
    return entries


def didl_entry(object_id, parent_id, title, upnp_class, container=False, res=''):
    """A minimal DIDL-Lite document for one entry, as Sonos controllers send with AddURIToQueue."""
    tag = 'container' if container else 'item'
    return (DIDL_OPEN + '<%s id="%s" parentID="%s" restricted="true">%s<dc:title>%s</dc:title>'
            '<upnp:class>%s</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
            'RINCON_AssociatedZPUDN</desc></%s></DIDL-Lite>') % (
        tag, html.escape(object_id), html.escape(parent_id),
        '<res protocolInfo="%s:*:*:*">%s</res>' % (html.escape(res.split(':', 1)[0]), html.escape(res)) if res else '',
        html.escape(title, quote=False), html.escape(upnp_class, quote=False), tag)


# --- events ------------------------------------------------------------------------
def properties(body):
    """name -> text of each <e:property> in a NOTIFY body."""
    root = ET.fromstring(body)
    return {local(value.tag): value.text or '' for prop in root.findall('e:property', NS) for value in prop}


def last_change(text):
    """name -> value for instance/queue 0 of a LastChange document; channel values only
    for the Master channel."""
    if not text: return {}
    root = ET.fromstring(text)
    values = {}
    for instance in root:
        if instance.get('val') != '0': continue
        for variable in instance:
            channel = variable.get('channel')
            if channel and channel != 'Master': continue
            values[local(variable.tag)] = variable.get('val', '')
    return values


def container_updates(text):
    """{'Q:0': 2, ...} from a ContainerUpdateIDs value ("Q:0,2" or "Q:0,2,FV:2,7")."""
    parts = (text or '').split(',')
    return {parts[i]: int(parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i + 1].isdigit()}


def read_line_in(base, timeout=SOAP_TIMEOUT):
    """The input's name/icon, or UPnPFault if this player has no AudioIn service.

    Connection status is only reported by AudioIn events (LineInConnected). Neither
    this getter nor the presence of an AI: ContentDirectory item proves a cable is
    attached, so callers keep that status separately from the input attributes.
    """
    values = soap(base, 'AudioIn', 'GetAudioInputAttributes', timeout=timeout)
    if not {'CurrentName', 'CurrentIcon'} & values.keys():
        raise UPnPFault('GetAudioInputAttributes', None, 'missing AudioIn attributes')
    return {'name': values.get('CurrentName') or 'Line-In', 'icon': values.get('CurrentIcon') or ''}


class Subscription:
    def __init__(self, listener, base, service, handler):
        self.listener, self.base, self.service, self.handler = listener, base, service, handler
        self.token = 'e%d' % next(listener.tokens)
        self.sid, self.expires, self.retry_at = None, 0.0, 0.0
        self.heard = False        # an event arrived since the last (re)subscription

    @property
    def healthy(self):
        return bool(self.sid) and self.heard and time.monotonic() < self.expires


class EventListener:
    """One HTTP server for the speakers' NOTIFYs, routed by path (an initial NOTIFY may beat
    the SUBSCRIBE answer, so not by SID); subscriptions are renewed before they lapse."""

    def __init__(self, log=print, bind='', port=0):
        self.log = log
        self.tokens = itertools.count(1)
        self.subscriptions = {}            # token -> Subscription
        self.lock = threading.Lock()
        self.stop = threading.Event()
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(min(length, 4 * 1024 * 1024))
                self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
                listener.deliver(self.path.strip('/'), self.headers.get('SID'), body)

            def log_message(self, *args): pass

        self.server = http.server.ThreadingHTTPServer((bind, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def start(self):
        threading.Thread(target=self.server.serve_forever, name='sonos-events', daemon=True).start()
        threading.Thread(target=self.renew_loop, name='sonos-renew', daemon=True).start()

    def close(self):
        self.stop.set()
        with self.lock: subscriptions = list(self.subscriptions.values())
        for subscription in subscriptions: self.unsubscribe(subscription)
        self.server.shutdown(); self.server.server_close()

    def subscribe(self, base, service, handler):
        """handler(properties) runs on a server thread for every event of this service."""
        subscription = Subscription(self, base, service, handler)
        with self.lock: self.subscriptions[subscription.token] = subscription
        self.send(subscription)
        return subscription

    def deliver(self, token, sid, body):
        with self.lock: subscription = self.subscriptions.get(token)
        if subscription is None or (subscription.sid and sid and sid != subscription.sid): return
        try:
            values = properties(body)
        except ET.ParseError as error:
            self.log({'event': 'sonos-event-unreadable', 'service': subscription.service, 'error': str(error)})
            return
        subscription.heard = True
        try: subscription.handler(values)
        except Exception as error:
            self.log({'event': 'sonos-event-error', 'service': subscription.service, 'error': repr(error)})

    def callback_host(self, base):
        """This machine's address on the route to the speaker."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((urlsplit(base).hostname, PORT))
            return probe.getsockname()[0]
        finally:
            probe.close()

    def request(self, subscription, method, headers):
        parts = urlsplit(subscription.base)
        connection = http.client.HTTPConnection(parts.hostname, parts.port or PORT, timeout=SOAP_TIMEOUT)
        try:
            connection.request(method, SERVICES[subscription.service][0] + '/Event', headers=headers)
            response = connection.getresponse(); response.read()
            return response
        finally:
            connection.close()

    def send(self, subscription):
        """Renew, or subscribe afresh when there is nothing to renew or the renewal fails."""
        renewing = subscription.sid is not None
        try:
            if renewing:
                response = self.request(subscription, 'SUBSCRIBE', {'SID': subscription.sid,
                                                                    'TIMEOUT': 'Second-%d' % SUBSCRIBE_SECONDS})
                if response.status != 200: subscription.sid = None; renewing = False
            if not renewing:
                subscription.heard = False
                callback = '<http://%s:%d/%s>' % (self.callback_host(subscription.base), self.port, subscription.token)
                response = self.request(subscription, 'SUBSCRIBE', {
                    'CALLBACK': callback, 'NT': 'upnp:event', 'TIMEOUT': 'Second-%d' % SUBSCRIBE_SECONDS})
                if response.status != 200: raise OSError('HTTP %d' % response.status)
                subscription.sid = response.getheader('SID')
            match = re.search(r'Second-(\d+)', response.getheader('TIMEOUT') or '')
            subscription.expires = time.monotonic() + (int(match.group(1)) if match else SUBSCRIBE_SECONDS)
            return True
        except (OSError, http.client.HTTPException) as error:
            subscription.sid, subscription.heard = None, False
            subscription.retry_at = time.monotonic() + RETRY_DELAY
            self.log({'event': 'sonos-subscribe-failed', 'speaker': subscription.base,
                      'service': subscription.service, 'error': str(error)})
            return False

    def unsubscribe(self, subscription):
        with self.lock: self.subscriptions.pop(subscription.token, None)
        if subscription.sid:
            try: self.request(subscription, 'UNSUBSCRIBE', {'SID': subscription.sid})
            except (OSError, http.client.HTTPException): pass

    def renew_loop(self):
        while not self.stop.wait(1):
            now = time.monotonic()
            with self.lock: due = [s for s in self.subscriptions.values()
                                   if (s.sid and now > s.expires - RENEW_MARGIN) or (not s.sid and now > s.retry_at)]
            for subscription in due:
                if self.stop.is_set(): return
                self.send(subscription)


# --- topology and discovery ------------------------------------------------------------
def parse_topology(text):
    """(groups, zones): groups as [(coordinator, [members, coordinator first])], zones by UUID as
    {'name', 'base', 'icon'}. Bonded and hidden players (Invisible, satellites, bridges) are left out."""
    root = ET.fromstring(text)
    groups, zones = [], {}
    for group in root.iter('ZoneGroup'):
        coordinator, members = group.get('Coordinator'), []
        for member in group.findall('ZoneGroupMember'):
            if member.get('Invisible') == '1' or member.get('IsZoneBridge') == '1': continue
            uuid = member.get('UUID')
            zones[uuid] = {'name': member.get('ZoneName') or uuid, 'base': base_url(member.get('Location') or ''),
                           'icon': member.get('Icon') or ''}
            members.append(uuid)
        if not members: continue
        members.sort(key=lambda uuid: uuid != coordinator)
        groups.append((coordinator if coordinator in members else members[0], members))
    return groups, zones


def topology(base, timeout=SOAP_TIMEOUT):
    """parse_topology of what one speaker reports for its household."""
    return parse_topology(soap(base, 'ZoneGroupTopology', 'GetZoneGroupState', timeout=timeout)['ZoneGroupState'])


def discover(timeout=2.0):
    """Base URLs of the Sonos players answering an SSDP search, never the bridge's own."""
    search = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\n'
              'ST: %s\r\n\r\n' % SEARCH_TARGET).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.3)
    found = []
    try:
        for _ in range(2): sock.sendto(search, ('239.255.255.250', 1900))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try: data, _ = sock.recvfrom(4096)
            except socket.timeout: continue
            base = search_response(data.decode('latin-1'))
            if base and base not in found: found.append(base)
    finally:
        sock.close()
    return found


def search_response(text):
    """The base URL an M-SEARCH answer points to, or None (not a player, or the bridge's own)."""
    location = re.search(r'^LOCATION:\s*(\S+)', text, re.I | re.M)
    usn = re.search(r'^USN:\s*uuid:(\S+?)(?:::|\s)', text, re.I | re.M)
    if not location or not usn or usn.group(1).startswith(BRIDGE_PLAYERS): return None
    return base_url(location.group(1))


def household(hosts, timeout=SOAP_TIMEOUT, discover_fallback=False):
    """(base that answered, groups, zones) from the first of hosts (addresses or base URLs) that
    reports a topology; hosts empty: discovery. Raises UPnPFault when none answers."""
    bases = [host if '://' in host else 'http://' + host if ':' in host else 'http://%s:%d' % (host, PORT)
             for host in hosts] or discover()
    if not bases: raise UPnPFault('Discovery', None, 'no Sonos players answered')
    error = None
    for base in bases:
        try:
            groups, zones = topology(base, timeout)
            return base, groups, zones
        except UPnPFault as fault:
            error = fault
    if hosts and discover_fallback:
        for base in discover():
            if base in bases: continue
            try:
                groups, zones = topology(base, timeout)
                return base, groups, zones
            except UPnPFault as fault:
                error = fault
    raise error
