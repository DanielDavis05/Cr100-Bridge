#!/usr/bin/env python3
"""Disposable production-service update fixture for host and native acceptance.

No backend is connected. Default package delivery is refused so a native lab
can intercept the controller's installer handoff without installing firmware.
Run --self-test for the loopback production-handler regression matrix.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import signal
import sys
import threading


def self_test():
    import unittest
    from test_update_service import UpdateServiceIntegration
    from test_online_updates import Lifecycle
    names = [
        (UpdateServiceIntegration, 'test_controller_offer_selected_subset_restarts_current_persona_and_preserves_retry'),
        (UpdateServiceIntegration, 'test_controller_offer_two_shared_ip_participants_finish_without_bridge_install'),
        (UpdateServiceIntegration, 'test_controller_player_begin_rejects_wrong_source_url_flags_options_and_hidden_udn'),
        (UpdateServiceIntegration, 'test_controller_compatibility_cancel_rearm_expiry_and_interruption_invalidate_sessions'),
        (UpdateServiceIntegration, 'test_controller_tcp_rechecks_paired_source_and_phase_order'),
        (Lifecycle, 'test_same_version_reports_preserve_retry_without_install_evidence'),
    ]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(cls(name) for cls, name in names))
    print(json.dumps(dict(scope='host production-handler sockets; native handoff not exercised',
        tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        success=result.wasSuccessful(), backend_connected=False, installer_executed=False)))
    return 0 if result.wasSuccessful() else 1


def serve(args):
    try:
        __import__('zlib')
    except ImportError:
        # The minimal SH4 lab Python omits its zlib extension. Use the guest's
        # real system libz for bounded parsing by unchanged production code.
        import ctypes
        import types
        library = ctypes.CDLL('libz.so.1')
        library.crc32.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_uint]
        library.crc32.restype = ctypes.c_ulong
        library.uncompress2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        library.uncompress2.restype = ctypes.c_int
        shim = types.ModuleType('zlib')

        class ZlibError(ValueError):
            pass

        def crc32(value, checksum=0):
            value = bytes(value)
            return library.crc32(checksum, value, len(value)) & 0xffffffff

        class Decoder:
            eof, unused_data, unconsumed_tail = False, b'', b''

            def decompress(self, value, max_length=0):
                limit = max_length or 4 * 1024 * 1024
                if not 0 < limit <= 4 * 1024 * 1024:
                    raise ZlibError('Invalid lab decompression bound')
                output = ctypes.create_string_buffer(limit)
                size, consumed = ctypes.c_ulong(limit), ctypes.c_ulong(len(value))
                result = library.uncompress2(output, ctypes.byref(size), value, ctypes.byref(consumed))
                if result != 0:
                    raise ZlibError('System libz rejected the compressed block')
                self.eof, self.unused_data = True, value[consumed.value:]
                return output.raw[:size.value]

        shim.crc32, shim.decompressobj, shim.error = crc32, Decoder, ZlibError
        sys.modules['zlib'] = shim
    # The SH4 lab image has the audited peer XML shim but lacks stdlib xml.
    # Production installations continue to use the unmodified stdlib package.
    try:
        import xml.etree.ElementTree
    except ImportError:
        import types
        sys.path[:0] = [str(Path(__file__).parent / 'zp80'), args.xml_compat_dir]
        import zp80_peer as peer
        xml, etree = types.ModuleType('xml'), types.ModuleType('xml.etree')
        xml.__path__, etree.__path__ = [], []
        xml.etree, etree.ElementTree = etree, peer.ET
        sys.modules.update({'xml': xml, 'xml.etree': etree, 'xml.etree.ElementTree': peer.ET})
        if not hasattr(peer.ET.Element, 'set'):
            peer.ET.Element.set = lambda self, key, value: self.attrib.__setitem__(key, value)
    import zoneplayers
    from bridge_config import write_json
    from controller_state import normalize_mac
    from online_updates import UpdateManager
    from update_service import UpdateService, _PACKAGE
    from urllib.parse import unquote, urlsplit

    for address in [args.bind, args.controller_address] + (args.room_address or []):
        value = ipaddress.ip_address(address)
        if value.version != 4 or not (value.is_loopback or value.is_private) or address == '0.0.0.0':
            raise ValueError('Use explicit private lab IPv4 addresses')
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / 'bridge-config.json'
    if not config.exists():
        write_json(config, {})
    mac = normalize_mac(args.controller_mac)
    addresses = {args.controller_address: mac}
    manager = UpdateManager(config)
    if args.package:
        reviewed = manager.stage_controller(mac, args.package, arm=False,
            current_version=args.current_version)
        manager.arm_controller(mac, current_version=args.current_version,
            expected_sha256=reviewed['sha256'], allow_unverified=args.allow_unverified)
    zp = zoneplayers.load_peer_module(Path(__file__).parent / 'zp80')
    zoneplayers.configure_profile(zp)
    room_addresses = args.room_address or [args.bind]
    house = zoneplayers.Household(zp, args.household,
        ['Update Lab %d' % (index + 1) for index in range(len(room_addresses))],
        room_addresses, lambda record: print(json.dumps(record), flush=True), port=0,
        bridge_ip=args.bind, controller_resolver=addresses.get,
        controller_addresses=lambda: dict(addresses), boot_sequence=args.boot_sequence)
    if args.soap_port:
        counts = {}
        for player in house.devices():
            address = player.config.advertise_ip
            desired = args.soap_port + counts.get(address, 0)
            # Peer construction already binds HTTP. Rebind the actual server,
            # preserving its production handler and state, before any thread
            # starts; changing only the advertised Config would be misleading.
            old = player.http
            old.server_close()
            player.http = old.__class__((player.config.bind, desired), old.RequestHandlerClass)
            player.http.peer = player
            player.config.port = player.http.server_port
            if player.state.is_bridge:
                house.bridge_host.port = player.config.port
            counts[address] = counts.get(address, 0) + 1
    service = UpdateService(house, manager, args.bind, http_port=args.http_port,
        tcp_port=args.tcp_port, tls_port=args.tls_port, phase_delay=args.phase_delay)
    house.updates = service
    stale_discovery = {'version': None}
    production_profile = service.profile
    production_complete = service.coordinator.on_complete

    def profile(address=None):
        result = production_profile(address)
        if stale_discovery['version']:
            result['server'] = result['server'].replace('/' + result['sw'], '/' + stale_discovery['version'])
            result['sw'] = stale_discovery['version']
        return result

    def complete(offer):
        # End the controlled stale-description condition at the actual logical
        # restart; all protocol validation/completion remains production code.
        stale_discovery['version'] = None
        return production_complete(offer)

    service.profile, service.coordinator.on_complete = profile, complete
    production_request = service.transport.request
    delivery = {'enabled': args.allow_package_delivery}

    def request(method, path, address, headers, body):
        if not delivery['enabled'] and _PACKAGE.fullmatch(unquote(urlsplit(path).path)):
            return 403, b'Native acceptance stops at installer handoff', 'text/plain', {}
        return production_request(method, path, address, headers, body)

    service.transport.request = request
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        house.start(args.bind, ssdp=args.ssdp)
        ready = dict(scope='disposable production multi-component lab', backend_connected=False,
            package_delivery=args.allow_package_delivery, controller_address=args.controller_address,
            controller_mac=mac, players=[dict(udn=p.config.udn, location=p.state.location)
                for p in house.devices()], update_servers=[list(server.server_address)
                for server in service.transport.servers])
        write_json(directory / 'ready.json', ready)
        print(json.dumps(ready), flush=True)
        while not stop.wait(.25):
            command_path = directory / 'command.json'
            if command_path.exists():
                command = json.loads(command_path.read_text())
                command_path.unlink()
                try:
                    action = command['action']
                    if action == 'stage':
                        reviewed = manager.stage_controller(mac, Path(command['package']), arm=False,
                            current_version=args.current_version)
                        manager.arm_controller(mac, current_version=args.current_version,
                            expected_sha256=reviewed['sha256'],
                            allow_unverified=command.get('allow_unverified') is True)
                    elif action == 'cancel':
                        manager.cancel_controller(mac)
                    elif action == 'delivery':
                        if type(command.get('enabled')) is not bool:
                            raise ValueError('Delivery must be explicitly true or false')
                        delivery['enabled'] = command['enabled']
                    elif action in ('persona', 'cache-persona'):
                        from online_updates import version_tuple
                        from update_transport import next_version
                        version_tuple(command['version'])
                        next_version(command['version'])
                        # Deliberately retain BootSeq/discovery generation so a
                        # native lab can exercise a stale cached player version.
                        if action == 'persona':
                            service.version = command['version']
                        else:
                            stale_discovery['version'] = command['version']
                    else:
                        raise ValueError('Unknown disposable acceptance command')
                    result = dict(ok=True, action=action, controller=manager.status(mac),
                                  persona=service.version, stale_discovery_persona=stale_discovery['version'])
                except (OSError, ValueError, KeyError) as error:
                    result = dict(ok=False, error=str(error))
                write_json(directory / 'command-result.json', result)
            service.tick()
            write_json(directory / 'snapshot.json', dict(controller=manager.status(mac),
                player_session=manager.bridge_status(), persona=service.version,
                package_delivery=delivery['enabled'],
                stale_discovery_persona=stale_discovery['version'],
                message=service.message(mac), boot_sequences={p.config.udn: p.config.boot_sequence
                    for p in house.devices()}))
    finally:
        house.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--xml-compat-dir', default='/opt/cr100-lab')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--controller-address', default='127.0.0.2')
    parser.add_argument('--controller-mac', default='02:00:00:00:01:00')
    parser.add_argument('--room-address', action='append')
    parser.add_argument('--household', default='Sonos_CFW_UPDATE_LAB')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--current-version', default='25.2-50130')
    parser.add_argument('--allow-unverified', action='store_true')
    parser.add_argument('--allow-package-delivery', action='store_true')
    parser.add_argument('--ssdp', action='store_true')
    parser.add_argument('--soap-port', type=int, default=0)
    parser.add_argument('--http-port', type=int, default=0)
    parser.add_argument('--tcp-port', type=int, default=0)
    parser.add_argument('--tls-port', type=int)
    parser.add_argument('--phase-delay', type=float, default=0)
    parser.add_argument('--boot-sequence', type=int, default=61)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.state_dir:
        parser.error('--state-dir is required for a disposable serving fixture')
    return serve(args)


if __name__ == '__main__':
    raise SystemExit(main())
