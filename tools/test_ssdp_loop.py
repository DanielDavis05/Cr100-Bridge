"""SSDP discovery survives failed sends and changes to advertised components."""
from contextlib import nullcontext
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import zoneplayers


class DiscoveryLoop(unittest.TestCase):
    def run_loop(self, *, send_error=None, changing=False, unexpected=False):
        real_socket = socket.socket
        ready, failed = threading.Event(), threading.Event()
        stop = threading.Event()
        self.addCleanup(stop.set)
        logs = []
        house = zoneplayers.Household.__new__(zoneplayers.Household)
        house.zp = SimpleNamespace(SSDP_GROUP='239.255.255.250')
        house.ssdp_port = 0
        house.discovery_generation = 0
        house.discovery_sources = set()
        house.log = logs.append
        def player(name):
            return SimpleNamespace(config=SimpleNamespace(udn=name, advertise_ip='127.0.0.2'),
                request_context=lambda address: nullcontext(), ssdp_targets=lambda: ('upnp:rootdevice',),
                ssdp_packet=lambda target, alive: b'NOTIFY ' + name.encode() + b' NTS: ssdp:alive',
                search_responses=lambda packet: [b'HTTP/1.1 200 OK\r\nUSN: ' + name.encode() + b'\r\n\r\n'])
        first, second = player('first'), player('second')
        calls, announcements = [], []
        def devices(address=None):
            calls.append(address)
            if unexpected and len(calls) == 1:
                raise RuntimeError('fixture topology changed')
            return [first, second] if changing and (address or len(calls) > 2) else [first]
        house.devices = devices
        house.discovery_views = lambda: {'127.0.0.1': [first, second] if changing else [first]}
        class Socket:
            def __init__(self): self.socket = real_socket(socket.AF_INET, socket.SOCK_DGRAM)
            def __getattr__(self, name): return getattr(self.socket, name)
            def bind(self, address):
                # macOS does not alias all of 127/8 onto its loopback interface.
                self.socket.bind(('127.0.0.1', address[1]) if address[0] == '127.0.0.2' else address)
                if address == ('', 0):
                    house.ssdp_port = self.socket.getsockname()[1]; ready.set()
            def setsockopt(self, level, option, value):
                if option == socket.IP_MULTICAST_IF: value = socket.inet_aton('127.0.0.1')
                self.socket.setsockopt(level, option, value)
            def sendto(self, packet, address):
                if packet.startswith(b'NOTIFY'):
                    announcements.append(packet); return len(packet)
                if send_error and not failed.is_set():
                    failed.set(); raise send_error('fixture send failed')
                return self.socket.sendto(packet, address)
        worker = threading.Thread(target=house.ssdp_loop, args=('127.0.0.1', stop), daemon=True)
        with patch.object(zoneplayers.socket, 'socket', side_effect=lambda *args: Socket()):
            worker.start()
            try:
                self.assertTrue(ready.wait(2), 'SSDP listener did not bind')
                with real_socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.bind(('127.0.0.1', 0)); client.settimeout(.15)
                    responses = set()
                    for attempt in range(30):
                        client.sendto(('M-SEARCH * HTTP/1.1\r\nX-Test: %s\r\n\r\n' % attempt).encode(),
                                      ('127.0.0.1', house.ssdp_port))
                        try: responses.add(client.recvfrom(8192)[0])
                        except socket.timeout: pass
                        if responses and (not changing or any(b'second' in value for value in responses)): break
                self.assertTrue(worker.is_alive(), 'SSDP thread exited')
                self.assertTrue(any(b'first' in value for value in responses))
                if changing:
                    self.assertTrue(any(b'second' in value for value in responses))
                    self.assertIn(b'NOTIFY second NTS: ssdp:alive', announcements)
                if send_error: self.assertTrue(failed.is_set())
                if unexpected or send_error is RuntimeError:
                    self.assertTrue(any(record['event'] == 'ssdp-loop-error' for record in logs))
            finally:
                stop.set(); worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_response_send_failure_does_not_end_discovery(self): self.run_loop(send_error=OSError)
    def test_new_component_waits_for_its_sender(self): self.run_loop(changing=True)
    def test_unexpected_loop_failure_is_logged_and_recovers(self): self.run_loop(unexpected=True)
    def test_unexpected_response_failure_is_logged_and_recovers(self): self.run_loop(send_error=RuntimeError)


if __name__ == '__main__': unittest.main()
