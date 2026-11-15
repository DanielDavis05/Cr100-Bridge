"""Shared anonymous SMAPI 1.1 transport for CR100 catalogue services.

Providers implement catalogue methods and, when needed, error_fault(). HTTP
framing, SOAP validation, request context, discovery and listener lifetime stay
here so adding a service does not copy the diagnostic server implementation.
"""
from __future__ import annotations

from contextlib import contextmanager
import html
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

SMAPI_NS = 'http://www.sonos.com/Services/1.1'
SOAP_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
SERVICE_TYPE_SUFFIX = 7
LIST_VERSION = '2'
ACCOUNT_NAME = 'bridge'
MAX_REQUEST_BYTES = 1 << 20
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]*\Z')
# The web UI's controller preview browses as a named controller. Only the bridge itself
# may ask for that, and only for read-only browse calls.
PREVIEW_HEADER = 'X-Bridge-Preview-Controller'
PREVIEW_METHODS = frozenset(('getMetadata', 'getMediaMetadata', 'getExtendedMetadata', 'search'))
_MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z')


def text(value):
    return html.escape(str(value), quote=False)


def collection(item_id, title, item_type='container', children=(), can_play=False, artist=''):
    return {'id': item_id, 'itemType': item_type, 'title': title, 'canPlay': can_play,
            'canEnumerate': True, 'artist': artist, 'children': list(children)}


class SmapiFault(Exception):
    """A provider-supplied fault whose message is safe for the controller."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SmapiService:
    startup_event = 'smapi-service'
    response_event = 'smapi'

    def __init__(self, host, port, service_id, name, log=print, capabilities='1'):
        self.host, self.service_id, self.name, self.log = host, service_id, name, log
        self.capabilities = capabilities
        self.request = threading.local()
        self.requests = []  # Only diagnostic subclasses retain request data.
        self._lifecycle_lock = threading.RLock()
        self._http_thread = None
        self._closed = False
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self): service.handle_post(self)
            def do_GET(self): service.handle_get(self)
            def log_message(self, fmt, *args): pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    @property
    def uri(self):
        return 'http://%s:%d/smapi' % (self.host, self.port)

    @property
    def service_type(self):
        return self.service_id * 256 + SERVICE_TYPE_SUFFIX

    def descriptor(self):
        services = ('<Services><Service Id="%d" Name="%s" Version="1.1" Uri="%s" SecureUri="%s" '
                    'ContainerType="MService" Capabilities="%s"><Policy Auth="Anonymous" PollInterval="3600"/>'
                    '<Presentation/></Service></Services>'
                    % (self.service_id, html.escape(self.name), self.uri, self.uri, self.capabilities))
        return {'AvailableServiceDescriptorList': services, 'AvailableServiceTypeList': str(self.service_type),
                'AvailableServiceListVersion': LIST_VERSION}

    def media_servers(self):
        # A synthetic account makes the anonymous service appear in the native
        # Music menu. The household encodes this XML once for the X event path.
        user = ACCOUNT_NAME
        return ('<MediaServers><Service UDN="SA_RINCON%d_%s" Md="" Password="" NumAccounts="1" '
                'Username0="%s" Md0="" Password0=""/></MediaServers>' % (self.service_type, user, user))

    def owns(self, uri, metadata=''):
        return False

    def _start_http_server(self):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError('Music service is closed')
            if self._http_thread is not None:
                return False
            self._http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self._http_thread.start()
            return True

    def start(self):
        if self._start_http_server():
            self.log({'event': self.startup_event, 'uri': self.uri, 'id': self.service_id, 'type': self.service_type})

    def close(self):
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if self._http_thread is not None and self._http_thread.is_alive():
                self.server.shutdown()
                self._http_thread.join()
            self.server.server_close()

    @contextmanager
    def request_context(self, address, preview=None):
        previous = getattr(self.request, 'address', None), getattr(self.request, 'preview', None)
        self.request.address, self.request.preview = address, preview
        try:
            yield
        finally:
            self.request.address, self.request.preview = previous

    def preview_controller(self, request, method):
        """The controller MAC a bridge-local preview request names, or None for a real caller."""
        values = request.headers.get_all(PREVIEW_HEADER) or []
        if not values:
            return None
        address = request.client_address[0]
        try:
            local = ipaddress.ip_address(address).is_loopback or address == self.host
        except ValueError:
            local = False
        mac = values[0].strip().lower()
        if len(values) != 1 or not local or not _MAC.fullmatch(mac) or method not in PREVIEW_METHODS:
            raise SmapiFault('Client.InvalidRequest', 'Invalid request.')
        return mac

    def handle_get(self, request):
        self.reply(request, 404, b'Not found', 'text/plain')

    def reply(self, request, status, body, content_type='text/xml; charset="utf-8"'):
        request.send_response(status)
        request.send_header('Content-Type', content_type)
        request.send_header('Content-Length', str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def parse_request(self, request):
        """Validate framing and one flat, namespaced SMAPI call before dispatch."""
        try:
            if urlsplit(request.path).path != '/smapi':
                raise ValueError
            lengths = request.headers.get_all('Content-Length', [])
            if request.headers.get('Transfer-Encoding') is not None or len(lengths) != 1:
                raise ValueError
            length_text = lengths[0]
            if not re.fullmatch(r'[0-9]{1,10}', length_text):
                raise ValueError
            length = int(length_text)
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError
            raw = request.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            # Removing NULs also catches declarations in UTF-16/32 XML.
            declarations = raw.replace(b'\x00', b'').upper()
            if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
                raise ValueError
            envelope = ET.fromstring(raw)
            if envelope.tag != '{%s}Envelope' % SOAP_NS:
                raise ValueError
            bodies = envelope.findall('{%s}Body' % SOAP_NS)
            headers = envelope.findall('{%s}Header' % SOAP_NS)
            if len(bodies) != 1 or len(headers) > 1 or len(envelope) != len(bodies) + len(headers):
                raise ValueError
            body = bodies[0]
            if len(body) != 1:
                raise ValueError
            call = body[0]
            prefix = '{%s}' % SMAPI_NS
            if not call.tag.startswith(prefix):
                raise ValueError
            method = call.tag[len(prefix):]
            if not _NAME.fullmatch(method):
                raise ValueError
            args = {}
            for child in call:
                if not child.tag.startswith(prefix) or len(child):
                    raise ValueError
                key = child.tag[len(prefix):]
                if not _NAME.fullmatch(key) or key in args:
                    raise ValueError
                args[key] = child.text or ''
            return method, args, headers[0] if headers else None
        except (ET.ParseError, TypeError, ValueError, OverflowError):
            # Invalid framing/endpoint can leave bytes unread on a keep-alive
            # connection. Never interpret those bytes as another HTTP request.
            request.close_connection = True
            raise SmapiFault('Client.InvalidRequest', 'Invalid request.') from None

    def on_request(self, request, method, args, header):
        """Optional diagnostic hook; production services do not retain inputs."""

    def log_response(self, method, status):
        self.log({'event': self.response_event, 'method': method[:80], 'status': status})

    def error_fault(self, error):
        if isinstance(error, SmapiFault):
            return error.code, str(error)
        if isinstance(error, (ET.ParseError, IndexError, AttributeError, TypeError, ValueError, OverflowError)):
            return 'Client.InvalidRequest', 'Invalid request.'
        if isinstance(error, LookupError):
            return 'Client.ItemNotFound', 'This item is unavailable.'
        if isinstance(error, NotImplementedError):
            return 'Server.NotImplemented', 'This action is unavailable.'
        return 'Server.ServiceUnavailable', 'This service is unavailable. Try again shortly.'

    def handle_post(self, request):
        method = ''
        try:
            with self.request_context(request.client_address[0]):
                method, args, header = self.parse_request(request)
                self.request.preview = self.preview_controller(request, method)
                self.on_request(request, method, args, header)
                result = self.dispatch(method, args)
            status = 200
            xml = self.envelope('<%sResponse xmlns="%s">%s</%sResponse>' % (method, SMAPI_NS, result, method))
        except Exception as error:
            code, message = self.error_fault(error)
            status, xml = 500, self.fault(code, message)
        self.log_response(method, status)
        self.reply(request, status, xml.encode('utf-8'))

    def envelope(self, body):
        return '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="%s"><s:Body>%s</s:Body></s:Envelope>' % (SOAP_NS, body)

    def fault(self, code, message):
        return self.envelope('<s:Fault><faultcode>s:%s</faultcode><faultstring>%s</faultstring></s:Fault>'
                             % (code, text(message)))

    def dispatch(self, method, args):
        if method == 'getMetadata': return self.get_metadata(args)
        if method == 'getMediaMetadata':
            return '<getMediaMetadataResult>%s</getMediaMetadataResult>' % self.entry(self.node(args.get('id')), wrap=False)
        if method == 'getExtendedMetadata':
            return '<getExtendedMetadataResult>%s</getExtendedMetadataResult>' % self.entry(self.node(args.get('id')))
        if method == 'search': return self.search(args)
        if method == 'getScrollIndices': return '<getScrollIndicesResult></getScrollIndicesResult>'
        raise NotImplementedError(method)
