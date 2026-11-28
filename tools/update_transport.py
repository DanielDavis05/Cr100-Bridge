"""Private CR100 online-update HTTP/TLS and native player-update transport.

The coordinator runs the native progress/reconnection flow with a stub provider.
It never installs software. Firmware selection and package authorization belong
to the caller; every network request is identified by its real socket source.
"""
from __future__ import annotations

import copy
import datetime
import fcntl
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socketserver
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote
import xml.etree.ElementTree as ET

from bridge_config import write_json
from controller_state import normalize_mac
import legacy_tls_rc4

MAX_BODY = 65536
MAX_BRIDGE_STATE_BYTES = 1024 * 1024
MESSAGE_FIELDS = ('statusCode', 'MessageHeader', 'MessageBody', 'ContinuePrompt',
                  'CancelPrompt', 'AllowUpdate', 'IntroMessageHeader', 'IntroMessageBody')


class UpdateTransportError(ValueError):
    pass


def next_version(current):
    match = re.fullmatch(r'(\d+)\.(\d+)-(\d+)', str(current))
    if not match or int(match[1]) > 34:
        raise UpdateTransportError('Invalid CR100-compatible player update version')
    return '%d.%d-%05d' % (int(match[1]), int(match[2]), int(match[3]) + 1)


class BridgeUpdateCoordinator:
    """Serialize one household operation while retaining controller-scoped offers.

    ``players`` contains logical UDNs, including those sharing an address. Each
    accepted UDN claims its own TCP session. Completion runs once after every
    required UDN has sent REBOOT, then persists the resulting common version.
    """
    def __init__(self, on_complete, state_path=None, offer_ttl=900, cooldown=30, clock=time.time,
                 compatibility_valid=lambda mac, token, player=None: False):
        self.on_complete, self.state_path = on_complete, Path(state_path) if state_path else None
        self.offer_ttl, self.cooldown, self.clock = offer_ttl, cooldown, clock
        self.compatibility_valid = compatibility_valid
        self.condition = threading.Condition(threading.RLock())
        self.offers, self.active, self.installed_version = {}, None, None
        self.timer, self.closed = None, False
        if self.state_path:
            self._restore()

    def _restore(self):
        """A damaged stub receipt must never prevent normal player discovery."""
        try:
            with self.state_path.open('rb') as source:
                raw = source.read(MAX_BRIDGE_STATE_BYTES + 1)
            if len(raw) > MAX_BRIDGE_STATE_BYTES:
                raise ValueError('Bridge update receipt is too large')

            def invalid_number(_):
                raise ValueError('Invalid JSON number')

            value = json.loads(raw, parse_constant=invalid_number)
            if not isinstance(value, dict) or not isinstance(value.get('controllers'), dict):
                raise ValueError('Invalid bridge update receipt')
            installed = value.get('installed_version')
            if installed is not None:
                next_version(installed)
            statuses = {'offered', 'accepted', 'downloading', 'installing', 'restarting',
                        'completing', 'completed', 'cancelled', 'failed'}
            states = {'accepted', 'connected', 'downloading', 'downloaded', 'installing', 'written', 'rebooted'}
            for mac, offer in value['controllers'].items():
                if (normalize_mac(mac) != mac or not isinstance(offer, dict) or offer.get('mac') != mac
                        or offer.get('status') not in statuses
                        or offer.get('kind') not in ('bridge', 'controller-compatibility')
                        or not isinstance(offer.get('token'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', offer['token'])):
                    raise ValueError('Invalid bridge update offer')
                next_version(offer.get('current_version'))
                next_version(offer.get('target_version'))
                if (offer['kind'] == 'controller-compatibility'
                        and (not isinstance(offer.get('controller_token'), str)
                             or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', offer['controller_token']))):
                    raise ValueError('Invalid controller compatibility token')
                players, accepted, progress = offer.get('players'), offer.get('accepted'), offer.get('states')
                if (not isinstance(players, list) or not 0 < len(players) <= 128
                        or any(not isinstance(p, str) or not p for p in players)
                        or len(set(players)) != len(players) or not isinstance(accepted, list)
                        or any(not isinstance(p, str) or p not in players for p in accepted)
                        or not isinstance(progress, dict)
                        or any(p not in players or s not in states for p, s in progress.items())
                        or not isinstance(offer.get('restarted', []), list)
                        or any(p not in accepted for p in offer.get('restarted', []))):
                    raise ValueError('Invalid bridge update players')
                for name in ('created_at', 'expires_at', 'completed_at', 'restart_at'):
                    number = offer.get(name)
                    if name not in offer and name not in ('created_at', 'expires_at'):
                        continue
                    if type(number) not in (int, float) or not math.isfinite(number):
                        raise ValueError('Invalid bridge update timestamp')
                for key in ('target_commit', 'current_commit', 'summary', 'repo', 'branch'):
                    if key in offer and not isinstance(offer[key], str):
                        raise ValueError('Invalid bridge update metadata')
                if 'stub' in offer and type(offer['stub']) is not bool:
                    raise ValueError('Invalid bridge update stub flag')
            self.installed_version = installed
            self.offers = value['controllers']
            for offer in self.offers.values():
                if offer['status'] in ('accepted', 'downloading', 'installing', 'restarting', 'completing'):
                    offer.update(status='failed', error='The update was interrupted by a bridge restart.')
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self.installed_version, self.offers = None, {}
            try:
                if self.state_path.is_file() or self.state_path.is_symlink():
                    self.state_path.replace(self.state_path.with_name(
                        self.state_path.name + '.corrupt-' + secrets.token_hex(6)))
            except OSError:
                pass
        try:
            self._save()
        except OSError:
            # Receipt persistence is unavailable, but the normal compatibility
            # persona and player service must still be able to start.
            pass

    def _save(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.state_path, self.status(), mode=0o600)

    def status(self, mac=None):
        with self.condition:
            if mac is not None:
                return copy.deepcopy(self.offers.get(normalize_mac(mac)))
            return dict(updated_at=self.clock(), active=self.active,
                        installed_version=self.installed_version, controllers=copy.deepcopy(self.offers))

    def _expire(self):
        if self.active:
            offer = next((x for x in self.offers.values() if x['token'] == self.active), None)
            if offer and (offer['expires_at'] <= self.clock() or not self._valid_compatibility(offer)
                          or any(not self._valid_compatibility(offer, p) for p in offer['accepted'])):
                self._fail(offer, 'The update session expired or its selection changed.')
                return True
        return False

    def tick(self):
        """Expire abandoned work without needing another controller request."""
        with self.condition:
            return self._expire()

    expire = tick

    def _valid_compatibility(self, offer, player=None):
        if offer['kind'] != 'controller-compatibility':
            return True
        try:
            return bool(self.compatibility_valid(offer['mac'], offer.get('controller_token'), player))
        except (OSError, ValueError):
            return False

    def compatibility_offer(self, mac, token, player_ids, current_version):
        """No-op player work under a live controller selection, separate receipt.

        Only accepted participants must finish; the native planner may select
        any subset, and may accept another player after an earlier one restarts.
        """
        mac = normalize_mac(mac)
        players = list(dict.fromkeys(player_ids))
        if (not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', token) or not players
                or len(players) > 128 or any(not isinstance(p, str) or not p for p in players)):
            raise UpdateTransportError('Invalid controller compatibility selection')
        next_version(current_version)
        with self.condition:
            self._expire()
            old = self.offers.get(mac)
            if (old and old.get('controller_token') == token
                    and old['status'] not in ('failed', 'cancelled')
                    and old['expires_at'] > self.clock()):
                old['players'] = list(dict.fromkeys(old['players'] + players))
                return copy.deepcopy(old)
            if self.active:
                raise UpdateTransportError('Another update session is active')
            offer = dict(kind='controller-compatibility', stub=True,
                         token=secrets.token_urlsafe(24), controller_token=token, mac=mac,
                         current_version=current_version, version=current_version,
                         target_version=current_version, players=players, status='offered',
                         accepted=[], states={}, restarted=[], created_at=self.clock(),
                         expires_at=self.clock() + self.offer_ttl)
            if self.closed or not self._valid_compatibility(offer):
                raise UpdateTransportError('The controller selection is no longer active')
            self.offers[mac] = offer
            self._save()
            return copy.deepcopy(offer)

    def retry_compatibility(self, mac):
        """A new native check may plan the same players again on a retry."""
        with self.condition:
            offer = self.offers.get(normalize_mac(mac))
            if (offer and offer['kind'] == 'controller-compatibility'
                    and offer['status'] == 'completed'):
                offer['status'] = 'cancelled'
                self._save()

    def offer(self, mac, player_ids, current_version, *, release=None):
        mac = normalize_mac(mac)
        players = list(dict.fromkeys(player_ids))
        if not players or len(players) > 128 or any(not isinstance(x, str) or not x for x in players):
            raise UpdateTransportError('Choose valid virtual players for the update')
        release_commit = release.get('target_commit') if isinstance(release, dict) else None
        with self.condition:
            if self.closed:
                raise UpdateTransportError('The update service is stopped')
            self._expire()
            old = self.offers.get(mac)
            if old and old['kind'] != 'bridge':
                if old['token'] == self.active:
                    raise UpdateTransportError('A controller compatibility session is active')
                old = None
            same_mode = old is not None and (
                (release is None and old.get('stub', True))
                or (release is not None and not old.get('stub', True) and old.get('target_commit') == release_commit)
            )
            if old and same_mode and ((old.get('status') == 'completed' and
                         self.clock() - old.get('completed_at', 0) < self.cooldown) or
                        (old['expires_at'] > self.clock() and old['players'] == players and
                         old['status'] in ('offered', 'accepted', 'downloading', 'installing', 'restarting'))):
                return copy.deepcopy(old)
            # Replacing a controller's candidate cannot abandon its active job.
            if old and old['token'] == self.active:
                return copy.deepcopy(old)
            target = next_version(self.installed_version or current_version)
            offer = dict(kind='bridge', stub=release is None, token=secrets.token_urlsafe(24), mac=mac,
                         current_version=self.installed_version or current_version,
                         version=target, target_version=target, players=players,
                         status='offered', accepted=[], states={}, created_at=self.clock(),
                         expires_at=self.clock() + self.offer_ttl)
            if isinstance(release, dict):
                for key in ('target_commit', 'current_commit', 'summary', 'repo', 'branch'):
                    value = release.get(key)
                    if isinstance(value, str) and value:
                        offer[key] = value
            self.offers[mac] = offer
            self._save()
            return copy.deepcopy(offer)

    def _get(self, mac, token, player=None, allow_completed=False):
        self._expire()
        offer = self.offers.get(normalize_mac(mac))
        if self.closed or not offer or offer['token'] != token or offer['expires_at'] <= self.clock():
            raise UpdateTransportError('The update offer is no longer available')
        if player is not None and player not in offer['players']:
            raise UpdateTransportError('This player is not part of the selected update')
        if not self._valid_compatibility(offer, player):
            self._fail(offer, 'The controller selection or player visibility changed.')
            raise UpdateTransportError('The controller selection is no longer active')
        if offer['status'] in ('failed', 'cancelled') or (offer['status'] == 'completed' and not allow_completed):
            raise UpdateTransportError('The update session has ended')
        return offer

    def begin(self, mac, player_id, token):
        with self.condition:
            while (self.offers.get(normalize_mac(mac), {}).get('status') == 'completing'
                   and not self.closed):
                self.condition.wait(.1)
            offer = self._get(mac, token, player_id, allow_completed=True)
            if offer['status'] == 'completed' and offer['kind'] != 'controller-compatibility':
                raise UpdateTransportError('The update session has ended')
            if offer['states'].get(player_id) == 'rebooted':
                return copy.deepcopy(offer)
            if self.active and self.active != token:
                raise UpdateTransportError('Another controller is already updating this bridge')
            self.active = token
            if player_id not in offer['accepted']:
                offer['accepted'].append(player_id)
                offer['states'][player_id] = 'accepted'
            offer['status'] = 'accepted'
            self._save()
            self.condition.notify_all()
            return copy.deepcopy(offer)

    def claim(self, mac, player_ids, timeout=10):
        """Claim the next accepted UDN on an IP; never infer it from client text."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.closed:
                self._expire()
                offer = self.offers.get(normalize_mac(mac))
                if offer and offer['token'] == self.active:
                    self._get(mac, offer['token'])
                    for player in player_ids:
                        if offer['states'].get(player) == 'accepted':
                            self._get(mac, offer['token'], player)
                            offer['states'][player] = 'connected'
                            self._save()
                            return offer['token'], player
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(remaining, .2))
        raise UpdateTransportError('No accepted update for this player')

    def progress(self, mac, token, player, phase, percent):
        with self.condition:
            offer = self._get(mac, token, player)
            expected = ('connected', 'downloading') if phase == 'DOWNLOAD' else ('downloaded', 'installing')
            if phase not in ('DOWNLOAD', 'FLASHWRITE') or offer['states'].get(player) not in expected:
                raise UpdateTransportError('Unexpected update phase')
            offer['states'][player] = 'downloading' if phase == 'DOWNLOAD' else 'installing'
            offer.update(status=offer['states'][player], phase=phase, percent=int(percent))
            self._save()

    def phase_complete(self, mac, token, player, phase):
        with self.condition:
            offer = self._get(mac, token, player)
            expected = 'downloading' if phase == 'DOWNLOAD' else 'installing'
            if offer['states'].get(player) != expected:
                raise UpdateTransportError('Unexpected update completion')
            offer['states'][player] = 'downloaded' if phase == 'DOWNLOAD' else 'written'
            self._save()

    def reboot(self, mac, token, player, delay=0):
        mac = normalize_mac(mac)
        with self.condition:
            offer = self._get(mac, token, player)
            if offer['states'].get(player) != 'written':
                raise UpdateTransportError('The player update is not ready to restart')
            offer['states'][player] = 'rebooted'
            offer['restart_at'] = max(offer.get('restart_at', 0), self.clock() + min(120, max(0, delay)))
            required = offer['accepted'] if offer['kind'] == 'controller-compatibility' else offer['players']
            if all(offer['states'].get(p) == 'rebooted' for p in required):
                offer['status'] = 'restarting'
                self.timer = threading.Timer(max(0, offer['restart_at'] - self.clock()),
                                             self._complete, args=(mac, token))
                self.timer.daemon = True
                self.timer.start()
            self._save()

    def _complete(self, mac, token):
        mac = normalize_mac(mac)
        with self.condition:
            try:
                offer = self._get(mac, token)
                if offer['status'] != 'restarting':
                    return
                if not all(self._valid_compatibility(offer, p) for p in offer['accepted']):
                    self._fail(offer, 'The controller selection or player visibility changed.')
                    return
                offer['status'] = 'completing'
                value = copy.deepcopy(offer)
                value['restart_players'] = [p for p in offer['accepted'] if p not in offer.get('restarted', [])]
            except UpdateTransportError:
                return
        # No coordinator lock while the household acquires its own locks.
        try:
            self.on_complete(value)
            with self.condition:
                offer = self._get(mac, token)
                if offer['status'] != 'completing':
                    return
                if offer['kind'] == 'bridge':
                    self.installed_version = offer['target_version']
                else:
                    offer['restarted'] = list(offer['accepted'])
                offer.update(status='completed', completed_at=self.clock(), percent=100)
                self.active = None
                self._save()
                self.condition.notify_all()
        except Exception as error:
            with self.condition:
                offer = self.offers.get(mac)
                if offer and offer.get('token') == token and offer.get('status') not in ('cancelled', 'failed'):
                    self._fail(offer, str(error))

    def _fail(self, offer, message):
        offer.update(status='failed', error=str(message)[:256])
        if self.active == offer['token']:
            self.active = None
        self._save()
        self.condition.notify_all()

    def fail(self, mac, token, message):
        with self.condition:
            offer = self.offers.get(normalize_mac(mac))
            if offer and offer['token'] == token and offer['status'] not in ('completed', 'cancelled', 'failed'):
                self._fail(offer, message)

    def cancel(self, mac, token=None):
        with self.condition:
            offer = self.offers.get(normalize_mac(mac))
            if not offer or (token is not None and offer['token'] != token):
                return
            if offer['status'] in ('completed', 'failed', 'cancelled', 'completing'):
                return
            offer['status'] = 'cancelled'
            if self.active == offer['token']:
                self.active = None
            self._save()
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            if self.timer:
                self.timer.cancel()
            for offer in self.offers.values():
                if offer['token'] == self.active:
                    self._fail(offer, 'The update service stopped before completion.')
            self.condition.notify_all()


def ensure_certificate(directory):
    """Generate a local TLS key once, never ship or log private key material."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = directory / 'update-cert.pem', directory / 'update-key.pem'
    descriptor = os.open(directory / '.certificate.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cert.is_file() and key.is_file():
            key.chmod(0o600)
            return cert, key
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'system-api.sonos.com')])
        now = datetime.datetime.now(datetime.timezone.utc)
        # Pre-4.3 controllers boot with an unsynced year-2000 clock; span 1999..+10y.
        not_before = datetime.datetime(1999, 1, 1, tzinfo=datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                       .public_key(private.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(not_before)
                       .not_valid_after(now + datetime.timedelta(days=3650))
                       .add_extension(x509.SubjectAlternativeName([
                           x509.DNSName('system-api.sonos.com')]), critical=False)
                       .sign(private, hashes.SHA256()))
        values = ((key, private.private_bytes(serialization.Encoding.PEM,
                   serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()), 0o600),
                  (cert, certificate.public_bytes(serialization.Encoding.PEM), 0o644))
        for target, data, mode in values:
            fd, temporary = tempfile.mkstemp(prefix='.tls-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as output:
                    os.fchmod(output.fileno(), mode)
                    output.write(data)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return cert, key


def message_xml(fields):
    if isinstance(fields, bytes):
        return fields
    if not isinstance(fields, dict) or set(fields) - set(MESSAGE_FIELDS):
        raise UpdateTransportError('Invalid online update message')
    root = ET.Element('s:Envelope', {'xmlns:s': 'http://schemas.xmlsoap.org/soap/envelope/'})
    response = ET.SubElement(ET.SubElement(root, 's:Body'), 'u:GetOnlineUpdateMessageResponse',
                             {'xmlns:u': 'http://cc.ws'})
    result = ET.SubElement(response, 'GetOnlineUpdateMessageReturn')
    values = dict(statusCode='1', AllowUpdate='true')
    values.update(fields)
    for name, value in values.items():
        if not isinstance(value, str):
            raise UpdateTransportError('Online update message fields must be text')
        ET.SubElement(result, name).text = value
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class _HTTP(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('HEAD')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        transport = self.server.transport
        source = self.client_address[0]
        try:
            mac = transport.controller_resolver(source)
            if not mac:
                return self.reply(403, b'Unknown controller', 'text/plain')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                return self.reply(413, b'Request too large', 'text/plain')
            body = self.rfile.read(length) if length else b''
            if method == 'POST' and '#GetOnlineUpdateMessage' in self.headers.get('SOAPAction', ''):
                fields = transport.message_for_mac(mac)
                if fields is None:
                    return self.reply(503, b'No selected update', 'text/plain')
                transport.log({'event': 'online-update-message', 'controller': mac})
                return self.reply(200, message_xml(fields), 'text/xml; charset=utf-8')
            if transport.request is None:
                return self.reply(503, b'Unavailable', 'text/plain')
            status, content, content_type, headers = transport.request(method, self.path, source,
                                                                        dict(self.headers), body)
            return self.reply(status, content, content_type, headers)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, OSError) as error:
            transport.log({'event': 'online-update-http-failed', 'error': str(error)[:256]})
            self.reply(400, b'Invalid update request', 'text/plain')

    def reply(self, status, content, content_type, headers=None):
        stream = None
        if isinstance(content, Path):
            try:
                stream = content.open('rb')
                length = os.fstat(stream.fileno()).st_size
            except OSError as error:
                if stream:
                    stream.close()
                self.delivery_failed(status, content, error)
                return self.reply(404, b'Not found', 'text/plain')
        else:
            if isinstance(content, str):
                content = content.encode()
            length = len(content)
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            if self.command != 'HEAD':
                if stream:
                    shutil.copyfileobj(stream, self.wfile, 65536)
                else:
                    self.wfile.write(content)
        except OSError as error:
            if self.command == 'GET' and isinstance(content, Path):
                self.delivery_failed(status, content, error)
                self.close_connection = True
                return
            raise
        finally:
            if stream:
                stream.close()

    def delivery_failed(self, status, content, error):
        transport = self.server.transport
        if (self.command != 'GET' or status != 200 or not isinstance(content, Path)
                or not callable(transport.delivery_failed)):
            return
        try:
            transport.delivery_failed(self.client_address[0], self.path, error)
        except Exception as callback_error:
            # A recovery-receipt failure must not attempt another HTTP reply
            # after the response's headers/body have already been sent.
            transport.log({'event': 'controller-download-recovery-failed',
                           'error_type': type(callback_error).__name__})


class _TLS(socketserver.BaseRequestHandler):
    def handle(self):
        mac = self.server.transport.controller_resolver(self.client_address[0])
        if not mac:
            return
        self.request.settimeout(15)
        try:
            legacy = getattr(self.server, 'legacy_tls', None)
            if legacy is not None:
                openssl_ciphers, legacy_key, legacy_cert_der = legacy
                offered = legacy_tls_rc4.peek_client_hello_ciphers(self.request)
                if legacy_tls_rc4.needs_legacy_rc4(offered, openssl_ciphers):
                    with legacy_tls_rc4.LegacyRC4Socket(self.request, legacy_key, legacy_cert_der) as connection:
                        self.server.transport.log({'event': 'online-update-legacy-tls',
                                                   'controller': mac,
                                                   'cipher': connection.cipher_name})
                        _HTTP(connection, self.client_address, self.server)
                    return
            with self.server.context.wrap_socket(self.request, server_side=True) as connection:
                _HTTP(connection, self.client_address, self.server)
        except (EOFError, OSError, ValueError):
            return


class _Upgrade(socketserver.StreamRequestHandler):
    def event(self, name, *fields):
        line = name + '?' + ';'.join('%s=%s' % (key, quote(str(value), safe='')) for key, value in fields) + '\n'
        self.wfile.write(line.encode('ascii'))
        self.wfile.flush()

    def handle(self):
        transport = self.server.transport
        coordinator = transport.coordinator
        mac = transport.controller_resolver(self.client_address[0])
        if not mac:
            return
        self.connection.settimeout(180)
        token = player = None
        hello, written = False, False
        phase = 'HELLO'
        try:
            while True:
                raw = self.rfile.readline(4097)
                if not raw:
                    if token and not written:
                        coordinator.fail(mac, token, 'The controller disconnected during the update.')
                    return
                if len(raw) > 4096 or not raw.endswith(b'\n') or b'\r' in raw:
                    raise UpdateTransportError('Invalid update command framing')
                line = raw[:-1].decode('ascii')
                command, _, query = line.partition('?')
                if transport.controller_resolver(self.client_address[0]) != mac:
                    raise UpdateTransportError('The controller source is no longer paired')
                transport.log({'event': 'bridge-update-command', 'controller': mac,
                               'players': self.server.players, 'command': command})
                if command == 'HELLO':
                    if hello:
                        raise UpdateTransportError('Unexpected HELLO')
                    hello = True
                    self.event('HELLO', ('version', '1'))
                elif command in ('DOWNLOAD', 'FLASHWRITE'):
                    phase = command
                    if not hello:
                        raise UpdateTransportError('HELLO is required')
                    if command == 'DOWNLOAD':
                        if token is not None:
                            raise UpdateTransportError('DOWNLOAD already completed')
                        token, player = coordinator.claim(mac, self.server.players, transport.accept_timeout)
                    elif token is None:
                        raise UpdateTransportError('DOWNLOAD is required')
                    offer = coordinator.status(mac)
                    if transport.phase_runner is not None and offer and offer.get('stub') is False:
                        def report_progress(percent):
                            if transport.stop.is_set():
                                raise UpdateTransportError('The update transport stopped')
                            coordinator.progress(mac, token, player, command, percent)
                            self.event('STATUS', ('phase', command), ('percent-complete', percent))
                        transport.phase_runner(mac, token, player, command, report_progress)
                    else:
                        for percent in (0, 25, 75, 100):
                            coordinator.progress(mac, token, player, command, percent)
                            self.event('STATUS', ('phase', command), ('percent-complete', percent))
                            if transport.stop.wait(transport.phase_delay / 4):
                                return
                    coordinator.phase_complete(mac, token, player, command)
                    self.event('COMPLETE', ('phase', command))
                elif command == 'REBOOT':
                    phase = command
                    if token is None:
                        raise UpdateTransportError('No accepted update')
                    delay = int(parse_qs(query).get('Delay', ['0'])[0])
                    coordinator.reboot(mac, token, player, delay)
                    written = True
                    return
                elif command in ('CANCEL', 'QUIT'):
                    coordinator.cancel(mac, token)
                    return
                elif command == 'PING':
                    continue
                else:
                    raise UpdateTransportError('Unknown update command')
        except (OSError, ValueError) as error:
            if token:
                coordinator.fail(mac, token, str(error))
            else:
                offer = coordinator.status(mac)
                if offer and offer.get('token') == coordinator.active:
                    coordinator.fail(mac, offer['token'], str(error))
            try:
                self.event('ERROR', ('phase', phase), ('error-code', '13'), ('internal-data', '0'))
            except OSError:
                pass


class UpdateTransport:
    def __init__(self, bind, controller_resolver, message_for_mac, coordinator, cert_dir,
                 request=None, log=lambda _: None, tls_port=443, http_port=80, tcp_port=4444,
                 phase_delay=2, accept_timeout=10, delivery_failed=None, phase_runner=None):
        self.bind, self.controller_resolver, self.message_for_mac = bind, controller_resolver, message_for_mac
        self.coordinator, self.cert_dir, self.request, self.log = coordinator, cert_dir, request, log
        self.delivery_failed = delivery_failed
        self.phase_runner = phase_runner
        self.tls_port, self.http_port, self.tcp_port = tls_port, http_port, tcp_port
        self.phase_delay, self.accept_timeout = phase_delay, accept_timeout
        self.servers, self.threads, self.stop = [], [], threading.Event()
        self.player_servers = {}
        self.server_lock = threading.RLock()

    def _start(self, bind, port, handler, players=None, context=None, legacy_tls=None):
        server = _Server((bind, port), handler)
        server.transport, server.players, server.context = self, players, context
        server.legacy_tls = legacy_tls
        self.servers.append(server)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.threads.append(thread)
        thread.start()
        return server

    def start(self, players):
        """Bind only caller-supplied private addresses; tests use loopback/port 0."""
        if self.servers or self.stop.is_set():
            raise UpdateTransportError('Update transport already started or stopped')
        try:
            if self.http_port is not None:
                self._start(self.bind, self.http_port, _HTTP)
            if self.tls_port is not None:
                import ssl
                cert, key = ensure_certificate(self.cert_dir)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1
                context.set_ciphers('ALL:@SECLEVEL=0')
                context.load_cert_chain(str(cert), str(key))
                openssl_ciphers = frozenset(item['id'] & 0xffff for item in context.get_ciphers())
                legacy_key, legacy_cert_der = legacy_tls_rc4.load_certificate_and_key(cert, key)
                self._start(self.bind, self.tls_port, _TLS, context=context,
                            legacy_tls=(openssl_ciphers, legacy_key, legacy_cert_der))
            self.set_players(players)
            return self
        except Exception:
            self.close()
            raise

    def set_players(self, players):
        """Refresh About/room UDNs without restarting HTTP or TLS listeners."""
        with self.server_lock:
            if self.stop.is_set():
                raise UpdateTransportError('The update transport is stopped')
            for address, player_ids in players.items():
                group = (player_ids,) if isinstance(player_ids, str) else tuple(player_ids)
                server = self.player_servers.get(address)
                if server is None:
                    server = self._start(address, self.tcp_port, _Upgrade, group)
                    self.player_servers[address] = server
                else:
                    server.players = group
            for address in set(self.player_servers) - set(players):
                server = self.player_servers.pop(address)
                server.shutdown()
                server.server_close()
                self.servers.remove(server)

    def close(self):
        with self.server_lock:
            self.stop.set()
            for server in self.servers:
                server.shutdown()
                server.server_close()
            for thread in self.threads:
                thread.join(timeout=2)
            self.servers.clear()
            self.player_servers.clear()
