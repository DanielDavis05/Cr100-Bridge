"""Advertise and route independent SMAPI services through one virtual household.

Each child retains its own HTTP endpoint, account and playback policy. The
household still encodes a single combined MediaServers document through the
existing ThirdPartyMediaServersX codec.
"""
from __future__ import annotations

import hashlib
import json
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit


class MusicServices:
    """The single-service interface backed by an ordered set of services."""

    preflight_playback = True

    def __init__(self, services):
        self.services = list(services)
        self.controller_services = [service for service in self.services
                                    if hasattr(service, 'controller_resolver')]
        self._lock = threading.RLock()
        self._started = self._closed = False
        try:
            self.descriptor()  # Reject conflicting identities before any server starts.
            self.media_servers()
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def descriptor(self):
        """Merge service descriptors and derive a repeatable list change token."""
        merged = ET.Element('Services')
        descriptors, types = [], []
        seen_ids, seen_types, seen_uris, seen_endpoints = set(), set(), set(), set()
        for service in self.services:
            values = service.descriptor()
            root = ET.fromstring(values['AvailableServiceDescriptorList'])
            if root.tag != 'Services' or any(child.tag != 'Service' for child in root):
                raise ValueError('Invalid music service descriptor list')
            child_types = values['AvailableServiceTypeList'].split(',')
            if len(root) != len(child_types) or not len(root):
                raise ValueError('Music service descriptors and types must correspond')
            for child, child_type in zip(root, child_types):
                service_id = self._number(child.get('Id'), 'id')
                service_type = self._number(child_type, 'type')
                if service_id in seen_ids:
                    raise ValueError('Duplicate music service id: %s' % service_id)
                if service_type in seen_types:
                    raise ValueError('Duplicate music service type: %s' % service_type)
                seen_ids.add(service_id)
                seen_types.add(service_type)
                # Uri and SecureUri commonly name the same endpoint within one
                # service, but separate services must have separate listeners.
                uris, endpoints = set(), set()
                for attribute in ('Uri', 'SecureUri'):
                    uri = child.get(attribute)
                    try:
                        parsed = urlsplit(uri or '')
                        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                    except ValueError:
                        raise ValueError('Invalid music service URI') from None
                    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username \
                            or parsed.password or parsed.fragment or not 1 <= port <= 65535:
                        raise ValueError('Invalid music service URI')
                    uris.add(uri)
                    endpoints.add((parsed.hostname.lower(), port))
                if seen_uris.intersection(uris):
                    raise ValueError('Duplicate music service URI')
                if seen_endpoints.intersection(endpoints):
                    raise ValueError('Music services must use distinct HTTP ports')
                seen_uris.update(uris)
                seen_endpoints.update(endpoints)
                types.append(str(service_type))
                merged.append(child)
            descriptors.append(values)

        # Include every child out-argument, including each child's own version.
        # Keep the decimal token within the CR100's signed 32-bit integer range.
        digest = hashlib.sha256(json.dumps(descriptors, sort_keys=True, separators=(',', ':')).encode()).digest()
        version = str(int.from_bytes(digest[:4], 'big') % 2147483647 + 1)
        return {'AvailableServiceDescriptorList': ET.tostring(merged, encoding='unicode'),
                'AvailableServiceTypeList': ','.join(types), 'AvailableServiceListVersion': version}

    @staticmethod
    def _number(value, kind):
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ValueError('Invalid music service %s' % kind)
        return int(value)

    def media_servers(self):
        """Return plain account XML for the household's existing X encoder."""
        merged, accounts = ET.Element('MediaServers'), set()
        for service in self.services:
            root = ET.fromstring(service.media_servers())
            if root.tag != 'MediaServers':
                raise ValueError('Invalid music service account list')
            for account in root:
                udn = account.get('UDN')
                if not udn or udn in accounts:
                    raise ValueError('Missing or duplicate music service account')
                accounts.add(udn)
                merged.append(account)
        return ET.tostring(merged, encoding='unicode')

    def owns(self, uri, metadata=''):
        return any(service.owns(uri, metadata) for service in self.services)

    def playback_policy_for(self, uri, metadata=''):
        """The single owning provider's optional runtime playback policy.

        Policy selection follows the same identity boundary as resolution; it
        must not select a policy from only an unvalidated URI scheme.
        """
        owners = [service for service in self.services if service.owns(uri, metadata)]
        if len(owners) != 1:
            raise ValueError('Playback must identify one music service')
        owner = owners[0]
        selector = getattr(owner, 'playback_policy_for', None)
        return selector(uri, metadata) if callable(selector) else getattr(owner, 'playback_policy', None)

    def resolve_playback(self, uri, metadata='', controller_ip=None):
        owners = [service for service in self.services if service.owns(uri, metadata)]
        if len(owners) != 1:
            raise ValueError('Playback must identify one music service')
        owner = owners[0]
        resolver = getattr(owner, 'resolve_playback', None)
        if not callable(resolver):
            raise ValueError('This music service item cannot be played')
        if controller_ip is not None and hasattr(owner, 'controller_resolver'):
            return resolver(uri, metadata, controller_ip=controller_ip)
        return resolver(uri, metadata)

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Music services are closed')
            if self._started:
                return
            try:
                for service in self.services:
                    service.start()
            except Exception:
                try:
                    self.close()
                except Exception:
                    pass
                raise
            self._started = True

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            first_error = None
            closed_children = set()
            for service in reversed(self.services):
                if id(service) in closed_children:
                    continue
                closed_children.add(id(service))
                try:
                    service.close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
                try:
                    # A provider cleanup failure can precede HTTP shutdown in
                    # the child's close(); still stop its thread and listener.
                    server = getattr(service, 'server', None)
                    if server is not None:
                        thread = getattr(service, '_http_thread', None)
                        if thread is not None and thread.is_alive():
                            server.shutdown()
                            thread.join()
                        server.server_close()
                except Exception as error:
                    if first_error is None:
                        first_error = error
            if first_error is not None:
                raise first_error
