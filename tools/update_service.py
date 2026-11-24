"""Controller-scoped Online Updates over the original Sonos wire protocol.

Firmware is delivered unchanged to the controller's own authenticated installer.
The bridge provider deliberately simulates installation while exercising the
complete native player-update protocol. Real speaker backends are never targets.
"""
from __future__ import annotations

import copy
import hashlib
import html
import re
import secrets
import subprocess
import tarfile
import threading
from urllib.parse import unquote, urlsplit

import update_transport
from online_updates import (CFW_RELEASE_PROPERTY, CONTROLLER_FLOOR, UpdateError, UpdateManager,
                            candidate_firmware, version_tuple)
from update_transport import BridgeUpdateCoordinator, UpdateTransport

# Compatibility alias used by update callers.
UpdateTransportError = update_transport.UpdateTransportError

ORIGIN = 'http://update.sonos.com'
BASE_FLOOR = CONTROLLER_FLOOR
# Firmware 4.3, the oldest controller known to accept the players' 7.1 persona.
# Older firmware (for example 16.5-48010a) shows its "older software than the
# rest of your Sonos system" screen whenever players report a newer software
# version, whatever the floors say, so those controllers see their own version.
PERSONA_CONTROLLER = '24.1-00000'
# Some stock builds append a letter to the version (Sonos/16.5-48010a (CR100)).
_CONTROLLER_AGENT = re.compile(r'Sonos/(\d+\.\d+-\d+)[A-Za-z]?\s+\(CR100\)')
_TOKEN = r'[A-Za-z0-9_-]{24,128}'
_MANIFEST = re.compile(r'/updates/(controller|bridge)/(' + _TOKEN + r')/manifest\.upm\Z')
_PACKAGE = re.compile(r'/updates/controller/(' + _TOKEN + r')/([^/]+)/([^/]+)\Z')
_SOFTWARE_PROPERTIES = frozenset(('R_CustomerID', 'R_UpdatePreference', 'R_LastUpdateVersion',
                                 'R_PromoVersion', 'R_ForceReIndex', 'R_UpdateAvailable'))


class UpdateService:
    def __init__(self, house, manager, listen_ip, *, origin=ORIGIN, transport_factory=UpdateTransport,
                 tls_port=443, http_port=80, tcp_port=4444, phase_delay=2, recover=True,
                 release_updater=None):
        self.house, self.manager, self.listen_ip = house, manager, listen_ip
        self.release_updater = release_updater
        self.origin = origin.rstrip('/')
        self.lock = threading.RLock()
        self.offers, self.versions, self.generations, self.floors, self.availability = {}, {}, {}, {}, {}
        self.base_version = house.zp.PROFILE['sw']
        self.coordinator = BridgeUpdateCoordinator(self.bridge_completed,
            state_path=manager.storage / 'bridge.json', compatibility_valid=self.compatibility_valid)
        self.version = self.coordinator.installed_version or self.base_version
        if recover:
            # A service restart must never revive a controller lockout. Keep
            # valid staged images available through a later manual check.
            release = getattr(manager, 'release_prompts', None)
            if callable(release):
                release('The bridge restarted; use Online Updates to retry.')
        transport_kwargs = dict(request=self.request, log=house.log,
            tls_port=tls_port, http_port=http_port, tcp_port=tcp_port, phase_delay=phase_delay,
            delivery_failed=self.delivery_failed)
        if release_updater is not None:
            transport_kwargs['phase_runner'] = self.run_bridge_phase
        self.transport = transport_factory(listen_ip, house.controller_resolver, self.message,
            self.coordinator, manager.storage / 'tls', **transport_kwargs)
        self.started = False

    def _mac(self, address):
        return self.house.controller_resolver(address) if address else None

    def observe(self, address, user_agent=''):
        mac = self._mac(address)
        match = _CONTROLLER_AGENT.search(user_agent or '')
        if mac and match:
            try:
                version_tuple(match[1])
            except UpdateError:
                return
            with self.lock:
                self.versions[mac] = match[1]
            self.manager.report_controller(mac, match[1])

    def profile(self, address=None):
        profile = dict(self.house.zp.PROFILE)
        mac = self._mac(address)
        software = (self._mirrored_version(mac) if mac else None) or self.version
        profile['server'] = profile['server'].replace('/' + profile['sw'], '/' + software)
        profile['sw'] = software
        floor = self._floor(mac) if mac else BASE_FLOOR
        profile.update(mincompat=floor, legacycompat=floor)
        return profile

    def _floor(self, mac):
        """One source of truth for request profiles and discovery invalidation."""
        try:
            return self.manager.compatibility_floor(mac, BASE_FLOOR)
        except (OSError, ValueError):
            # Controller admission must not depend on writable update storage.
            return BASE_FLOOR

    def _mirrored_version(self, mac):
        """The controller's own version when it predates the players' persona."""
        try:
            current = self._current_controller(mac)
            if current and version_tuple(current) < version_tuple(PERSONA_CONTROLLER):
                return current
        except (OSError, ValueError):
            pass
        return None

    def _advertised(self, mac):
        return self._floor(mac), self._mirrored_version(mac)

    def generation(self, address):
        mac = self._mac(address)
        with self.lock:
            return self.generations.get(mac, 0)

    def tick(self):
        """Refresh cross-process staging changes without restarting the radio."""
        try:
            self.coordinator.tick()
        except OSError as error:
            self.house.log({'event': 'bridge-update-status-unavailable', 'error_type': type(error).__name__})
        try:
            states = self.manager.status()
        except (OSError, ValueError):
            states = {}
        with self.lock:
            controllers = states.keys() | self.versions.keys() | self.floors.keys() | self.availability.keys()
        floors = {mac: self._advertised(mac) for mac in controllers}
        availability = {mac: self._available_for_mac(mac) for mac in controllers}
        changed = []
        update_changed = []
        with self.lock:
            for mac in floors.keys() | self.floors.keys():
                if floors.get(mac, (BASE_FLOOR, None)) != self.floors.get(mac, (BASE_FLOOR, None)):
                    self.generations[mac] = self.generations.get(mac, 0) + 1
                    changed.append(mac)
            self.floors = floors
            update_changed = [mac for mac in availability.keys() | self.availability.keys()
                              if availability.get(mac, '') != self.availability.get(mac, '')]
            self.availability = availability
        if changed or update_changed:
            self.house.updates_changed()
            self.house.log({'event': 'controller-update-prompt-changed',
                            'controllers': sorted(set(changed + update_changed))})

    def start(self):
        mapping = {}
        for player in self.house.devices():
            mapping.setdefault(player.config.advertise_ip, []).append(player.config.udn)
        self.transport.start(mapping)
        self.started = True
        return self

    def close(self):
        self.transport.close()
        self.coordinator.close()
        if self.release_updater is not None:
            self.release_updater.close()

    def run_bridge_phase(self, mac, token, player, command, report_progress):
        offer = self.coordinator.status(mac)
        if (not self.release_updater or not offer or offer.get('token') != token
                or offer.get('stub', True)):
            raise UpdateTransportError('Bridge update package is no longer available')
        try:
            if command == 'DOWNLOAD':
                self.release_updater.download(token, offer, report_progress)
            elif command == 'FLASHWRITE':
                self.release_updater.install(token, offer, report_progress)
            else:
                raise UpdateTransportError('Unknown update command')
        except UpdateTransportError:
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as error:
            raise UpdateTransportError(str(error) or 'Bridge update failed') from error

    def bridge_completed(self, offer):
        compatibility = offer['kind'] == 'controller-compatibility'
        with self.lock:
            if not compatibility:
                self.version = offer['target_version']
        # All virtual components share one bridge software installation. Advance
        # every discovery generation together; no real speaker operation runs.
        with self.house.lock:
            for player in self.house.devices():
                if not compatibility or player.config.udn in offer['restart_players']:
                    player.config.boot_sequence = player.config.boot_sequence % 0xffffffff + 1
        self.house.updates_changed()
        event = ('controller-player-compatibility-completed' if compatibility
                 else 'bridge-update-stub-completed' if offer.get('stub', True)
                 else 'bridge-update-completed')
        record = {'event': event, 'version': self.version}
        if offer.get('target_commit'):
            record['commit'] = offer['target_commit']
        self.house.log(record)
        if not compatibility and not offer.get('stub', True) and self.release_updater is not None:
            self.release_updater.schedule_restart()

    def compatibility_valid(self, mac, token, player=None):
        """Revalidate the live selection and visibility at every transport phase."""
        addresses = [address for address, paired in self.house.controller_addresses().items()
                     if paired == mac and self._mac(address) == mac]
        record = self.manager.compatibility_selection(mac, token)
        if not addresses or not record or record['token'] != token:
            return False
        visible = {p.config.udn for address in addresses for p in self.house.devices(address)}
        return player is None or player in visible

    def _available_for_mac(self, mac):
        """Supply the native post-controller recovery's AvailableSWInfo input.

        Merely staging/arming firmware does not advertise an automatic prompt.
        Only an actual download or its proven completion has a recovery lease.
        """
        try:
            addresses = [address for address, paired in self.house.controller_addresses().items()
                         if paired == mac and self._mac(address) == mac]
            if not addresses:
                return ''
            record = self.manager.compatibility_selection(mac)
            if not record or (record['status'] != 'downloading' and record['kind'] != 'controller-recovery'):
                return ''
            offer = dict(record, controller_version=self._current_controller(mac) or record.get('current_version'))
            return self.update_item(offer)
        except (OSError, ValueError):
            return ''

    def available_update(self, address):
        mac = self._mac(address)
        return self._available_for_mac(mac) if mac else ''

    def _current_controller(self, mac):
        with self.lock:
            known = self.versions.get(mac)
        return known or self.manager.status(mac).get('current_version')

    def check(self, address, args, *, demonstration=False):
        mac = self._mac(address)
        if not mac:
            raise UpdateError('Unknown controller.', 403)
        if args.get('UpdateType') != 'Software' or args.get('CachedOnly') not in ('0', '1', 'true', 'false') \
                or 'Version' not in args:
            raise UpdateError('Invalid software update check.')
        current = self._current_controller(mac)
        selected = self.manager.selection(mac, current)
        if selected:
            self.manager.acknowledge_prompt(mac, selected['token'])
            self.coordinator.retry_compatibility(mac)
        elif demonstration:
            players = [p.config.udn for p in self.house.devices(address)]
            selected = self.coordinator.offer(mac, players, self.version)
            selected['controller_version'] = current
        else:
            release = self.release_updater.check_for_update() if self.release_updater is not None else None
            if release and release.get('error'):
                self.house.log({'event': 'bridge-update-check-failed', 'error': release['error']})
            if release and release.get('available'):
                players = [p.config.udn for p in self.house.devices(address)]
                selected = self.coordinator.offer(mac, players, self.version, release=release)
                selected['controller_version'] = current
            else:
                # The native client treats an empty UpdateItem as a connection
                # error. An inert manifest expresses no update while preserving
                # the guarded model-specific lookup and current player persona.
                selected = dict(kind='none', token=secrets.token_urlsafe(24),
                    controller_version=current, version=current or self.version,
                    expires_at=self.manager.clock() + min(900, self.manager.ttl))
        with self.lock:
            self.offers[mac] = dict(selected)
        self.tick()
        return {'UpdateItem': self.update_item(selected)}

    def _offer(self, mac, kind=None, token=None, *, compatibility=False):
        with self.lock:
            offer = copy.deepcopy(self.offers.get(mac))
        selected_kind = 'controller' if offer and offer.get('kind') in ('none', 'controller-recovery') else (offer or {}).get('kind')
        if not offer or (kind and selected_kind != kind) or (token and offer.get('token') != token):
            # Controller downloads can resume across a player service restart.
            record = (self.manager.compatibility_selection(mac, token) if compatibility and token
                      else self.manager.selection(mac))
            if record and (not token or record['token'] == token) and kind in (None, 'controller'):
                offer = record
            else:
                raise UpdateError('This update selection is no longer available.', 404)
        if offer['kind'] in ('controller', 'controller-recovery'):
            record = (self.manager.compatibility_selection(mac, offer['token']) if compatibility
                      else self.manager.selection(mac))
            if not record or record['token'] != offer['token']:
                raise UpdateError('The staged controller update was cancelled or expired.', 410)
            offer = record
        elif offer['kind'] == 'bridge':
            record = self.coordinator.status(mac)
            now = self.coordinator.clock()
            if (not record or record['token'] != offer['token']
                    or record['status'] in ('failed', 'cancelled')
                    or (record['status'] == 'completed' and
                        now - record.get('completed_at', 0) >= self.coordinator.cooldown)
                    or (record['status'] != 'completed' and record['expires_at'] <= now)):
                raise UpdateError('The bridge update was cancelled or expired. Check again.', 410)
            offer = dict(record, controller_version=offer.get('controller_version'))
        elif offer['kind'] == 'none':
            if self.manager.clock() >= offer['expires_at'] or self.manager.selection(mac):
                raise UpdateError('This no-update check expired or its selection changed.', 410)
        return offer

    def update_item(self, offer):
        kind = 'controller' if offer['kind'] in ('none', 'controller-recovery') else offer['kind']
        token = offer['token']
        version = self.version if kind == 'controller' else offer['target_version']
        # Stock clients can use this generic version before loading the per-
        # model manifest. Suppress fallback handheld installs on bridge jobs;
        # their explicit model-1 row still carries the actual player target.
        if offer['kind'] != 'controller':
            version = offer.get('controller_version') or version
        version_tuple(version)
        root = '%s/updates/%s/%s' % (self.origin, kind, token)
        return ('<UpdateItem xmlns="urn:schemas-rinconnetworks-com:update-1-0" Type="Software" '
                'Version="%s" UpdateURL="%s/begin" DownloadSize="0" ManifestURL="%s/manifest.upm"/>'
                % (html.escape(version), root, root))

    def manifest(self, mac, offer):
        # Model 2 is an explicit, armed controller package selection only.
        # Clients that support same-version reinstall must not interpret a
        # current-version placeholder in a bridge demonstration as an install.
        controller = offer.get('version') if offer['kind'] == 'controller' else None
        target = offer['target_version'] if offer['kind'] == 'bridge' else self.version
        # Stock clients use system_version as the controller fallback when no
        # model-2 image matches. Keep that fallback equal to the requesting
        # controller, while the explicit model-1 image retains its real target.
        default_version = target
        if not controller:
            default_version = self._current_controller(mac) or offer.get('controller_version') or target
        version_tuple(default_version)
        token = offer['token']
        image = ''
        if controller:
            version_tuple(controller)
            url = '%s/updates/controller/%s/%s/^%s' % (self.origin, token, controller, controller)
            image = '<image model="2" version="%s">%s</image>' % (controller, html.escape(url))
        revision_key = token + (':controller-recovery' if offer['kind'] == 'controller-recovery' else '')
        revision = int(hashlib.sha256(revision_key.encode()).hexdigest()[:7], 16)
        return ('<?xml version="1.0"?>\n<update_manifest version="1.1" revision="%d" '
                'system_version="%s" sys_flags="0x1"><update_list>'
                '<image model="1" version="%s">%s/updates/bridge/%s/begin</image>%s'
                '</update_list></update_manifest>\n'
                % (revision, default_version, target, self.origin, token, image)).encode()

    def message(self, mac):
        recovery = self.manager.compatibility_selection(mac)
        if recovery and recovery['kind'] == 'controller-recovery':
            # GetOnlineUpdateMessage carries no offer token. The paired MAC's
            # current, independently completed record still authorizes its
            # bounded player recovery, including after an ordinary inert check.
            return dict(statusCode='1', MessageHeader='Finishing system update',
                MessageBody='Complete the virtual player compatibility checks for the selected system update.',
                ContinuePrompt='Continue update', CancelPrompt='Back to settings', AllowUpdate='true')
        try:
            offer = self._offer(mac)
        except UpdateError:
            return dict(statusCode='1', MessageHeader='Update cancelled',
                MessageBody='The staged update is no longer available. Return to settings and check again.',
                CancelPrompt='Back to settings', AllowUpdate='false')
        if offer['kind'] == 'controller':
            current = self._current_controller(mac)
            older = current and version_tuple(offer['version']) < version_tuple(current)
            identity = candidate_firmware(offer)
            firmware_kind = identity['firmware_kind']
            if firmware_kind == 'official':
                title = 'Official firmware downgrade' if older else 'Official controller firmware'
                target = 'Official Sonos firmware %s' % offer['version']
                body = 'Install %s on this CR100. Keep it docked until it restarts.' % target
                intro = '%s is staged for this CR100. Press OK to review the installation.' % target
                action = 'Install older firmware' if older else 'Install official firmware'
            elif firmware_kind == 'custom':
                release = identity['cfw_release']
                title = 'Custom firmware downgrade' if older else 'Custom controller firmware'
                target = 'Custom firmware %s (%s)' % (release, offer['version']) if release else (
                    'Custom firmware (%s; release unknown)' % offer['version'])
                body = ('Install %s on this CR100. If it still runs stock firmware, use the '
                        "bridge's one-time Install custom firmware setup instead of installing here.") % target
                intro = ('%s is staged for this CR100. First installation from stock needs '
                         'one-time setup in the bridge. Press OK to review.') % target
                action = 'Install custom firmware'
            else:
                title = 'Unverified controller firmware'
                target = 'Unverified firmware %s' % offer['version']
                body = ('Install %s on this CR100. The bridge could not identify it as '
                        'official Sonos firmware or a custom release. Keep it docked until it restarts.') % target
                intro = '%s is staged for this CR100. Press OK to review the installation.' % target
                action = 'Install unverified firmware'
            return dict(statusCode='1', MessageHeader=title,
                MessageBody=body, ContinuePrompt=action,
                CancelPrompt='Keep current firmware', AllowUpdate='true',
                IntroMessageHeader=title, IntroMessageBody=intro)
        if offer['kind'] == 'none':
            return dict(statusCode='1', MessageHeader='No update available',
                MessageBody='No controller firmware is armed for installation.',
                CancelPrompt='Back to settings', AllowUpdate='false')
        status = self.coordinator.status()
        if status.get('active') and status['active'] != offer['token']:
            return dict(statusCode='1', MessageHeader='Bridge update in progress',
                MessageBody='Another controller is updating the bridge. Please check again when it finishes.',
                CancelPrompt='Back to settings', AllowUpdate='false')
        body = 'Update the CR100 Bridge. Your controller firmware will stay unchanged.'
        if offer.get('target_commit'):
            short = offer['target_commit'][:8]
            summary = (offer.get('summary') or '').strip()
            body = 'Update the CR100 Bridge to %s. Your controller firmware will stay unchanged.' % short
            if summary:
                candidate = 'Update the CR100 Bridge to %s (%s). Your controller firmware will stay unchanged.' % (
                    short, summary)
                if len(candidate.encode('utf-8')) <= 511:
                    body = candidate
        return dict(statusCode='1', MessageHeader='Bridge software update',
            MessageBody=body,
            ContinuePrompt='Update bridge', CancelPrompt='Later', AllowUpdate='true',
            IntroMessageHeader='Bridge software update',
            IntroMessageBody='This updates the CR100 Bridge. Your controller firmware stays unchanged. Press OK to review.')

    def soap(self, state, service, action, args, address):
        """Return a complete SOAP result before backend/informational guards."""
        mac = self._mac(address)
        variable = args.get('VariableName')
        if service == 'SystemProperties' and action in ('SetString', 'SetStringX') \
                and variable in ('CR100_CFWBuild', CFW_RELEASE_PROPERTY):
            if not mac:
                raise UpdateError('Unknown controller.', 403)
            report = self.manager.report_release if variable == CFW_RELEASE_PROPERTY else self.manager.report_build
            report(mac, args.get('StringValue'), self._current_controller(mac))
            self.tick()
            return {}, []
        if service == 'ZoneGroupTopology' and action == 'CheckForUpdate':
            return self.check(address, args), []
        if service == 'ZoneGroupTopology' and action == 'BeginSoftwareUpdate':
            if not mac or 'UpdateURL' not in args or 'Flags' not in args:
                raise UpdateError('Invalid software update request.')
            match = re.fullmatch(re.escape(self.origin) + r'/updates/bridge/(' + _TOKEN + r')/begin',
                                 args['UpdateURL'])
            if not match:
                raise UpdateError('The software update selection changed. Check again.')
            offer = self._offer(mac, token=match[1], compatibility=True)
            if offer['kind'] not in ('bridge', 'controller', 'controller-recovery'):
                raise UpdateError('No active software update selection.')
            expected = '%s/updates/bridge/%s/begin' % (self.origin, offer['token'])
            if args['UpdateURL'] != expected or args['Flags'] not in ('0', '2') or args.get('ExtraOptions', ''):
                raise UpdateError('The software update selection changed. Check again.')
            players = [p.config.udn for p in self.house.devices(address)]
            if state.config.udn not in players:
                raise UpdateError('This virtual player is no longer visible.', 403)
            if offer['kind'] in ('controller', 'controller-recovery'):
                session = self.coordinator.compatibility_offer(mac, offer['token'], players, self.version)
                token = session['token']
            else:
                token = offer['token']
            self.coordinator.begin(mac, state.config.udn, token)
            return {}, []
        if service == 'SystemProperties' and action in ('GetString', 'GetStringX') \
                and args.get('VariableName') == 'R_CustomerID':
            return {'StringValue': '1'}, []
        if service == 'SystemProperties' and action in ('SetString', 'SetStringX', 'Remove', 'RemoveX') \
                and args.get('VariableName') in _SOFTWARE_PROPERTIES:
            name = args['VariableName']
            with state.lock:
                if action.startswith('Remove'):
                    state.properties.pop(name, None)
                else:
                    state.properties[name] = args.get('StringValue', '')
            return {}, ['SystemProperties']
        if service == 'SystemProperties' and action == 'DoPostUpdateTasks':
            if self.release_updater is not None:
                self.release_updater.expedite_restart()
            return {}, []
        return None

    def request(self, method, path, address, headers, body):
        mac = self._mac(address)
        if not mac:
            return 403, b'Unknown controller', 'text/plain', {}
        self.observe(address, next((v for k, v in headers.items() if k.lower() == 'user-agent'), ''))
        path = unquote(urlsplit(path).path)
        try:
            if method not in ('GET', 'HEAD'):
                return 503, b'No online registration service', 'text/plain', {}
            match = _MANIFEST.fullmatch(path)
            if match:
                offer = self._offer(mac, match[1], match[2], compatibility=True)
                if offer['kind'] == 'controller-recovery':
                    self.coordinator.retry_compatibility(mac)
                return 200, self.manifest(mac, offer), 'text/xml; charset=utf-8', {}
            match = _PACKAGE.fullmatch(path)
            if match:
                token, version, filename = match.groups()
                offer = self._offer(mac, 'controller', token)
                if version != offer['version'] or filename not in ('^' + version, version + '-1-2.upd'):
                    raise UpdateError('Wrong controller firmware selection.', 404)
                package = self.manager.authorize_download(token, mac)
                if method == 'GET':
                    self.manager.controller_started(token, mac)
                    self.tick()
                    self.house.log({'event': 'controller-firmware-delivery', 'controller': mac,
                                    'version': version, 'sha256': offer['sha256']})
                return 200, package, 'application/octet-stream', {}
            return 404, b'Not found', 'text/plain', {}
        except UpdateError as error:
            return error.status, str(error).encode(), 'text/plain', {}

    def delivery_failed(self, address, path, error):
        """Release only the matching controller's interrupted package transfer."""
        mac = self._mac(address)
        match = _PACKAGE.fullmatch(unquote(urlsplit(path).path))
        if not mac or not match:
            return
        token, version, _ = match.groups()
        try:
            record = self.manager.selection(mac)
            if (not record or record['token'] != token or record['version'] != version
                    or record['status'] != 'downloading'):
                return
            self.manager.fail_controller(token, mac,
                'The firmware download was interrupted. Upload the firmware again to retry.')
        except UpdateError:
            # The administrator may have cancelled or replaced this token while
            # the old transfer was still unwinding. Its replacement stays armed.
            return
        self.tick()
        self.house.log({'event': 'controller-firmware-delivery-failed', 'controller': mac,
                        'error_type': type(error).__name__})


def configure_updates(house, config_path, listen_ip, **options):
    if 'release_updater' not in options:
        from bridge_updater import BridgeReleaseUpdater
        options['release_updater'] = BridgeReleaseUpdater(config_path)
    house.updates = UpdateService(house, UpdateManager(config_path), listen_ip, **options)
    return house.updates
