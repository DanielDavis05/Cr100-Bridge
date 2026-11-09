"""Controller discovery recipients and GENA ownership without network sockets."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spotify_controllers import ControllerNotices, ControllerRegistry

sys.path.insert(0, str(Path(__file__).resolve().parent / 'zp80'))
import zp80_peer as zp

A, B = '00:0e:58:01:02:03', '00:0e:58:04:05:06'


class RegistryRecipients(unittest.TestCase):
    def test_addresses_share_lookup_cache_return_copies_and_refresh_current_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state').write_text(json.dumps({'controllers': [{'mac': A}, {'mac': B}]}))
            (root / 'leases').write_text('0 ' + A + ' 192.168.69.50 controller *\n')
            neighbours = Mock(return_value='')
            registry = ControllerRegistry(root / 'state', root / 'leases', neighbours=neighbours)
            now = [100.0]
            with patch('spotify_controllers.time.monotonic', side_effect=lambda: now[0]):
                addresses = registry.addresses()
                self.assertEqual(addresses, {'192.168.69.50': A})
                addresses.clear()
                self.assertEqual(registry('192.168.69.50'), A)
                neighbours.assert_called_once()
                neighbours.return_value = '192.168.69.50 lladdr ' + B + ' REACHABLE\n'
                now[0] += 1.1
                self.assertEqual(registry.addresses(), {'192.168.69.50': B})
                self.assertEqual(registry('192.168.69.50'), B)
                self.assertEqual(neighbours.call_count, 2)


class SubscriptionOwnership(unittest.TestCase):
    def setUp(self):
        self.peer = object.__new__(zp.ZP80Peer)
        self.peer.config = SimpleNamespace(callback_network='127.0.0.0/8', browser_workers=False)
        self.peer.state = SimpleNamespace(lock=threading.RLock(), event_paths={'/event': 'ZoneGroupTopology'},
                                          subscriptions={})
        self.handler = object.__new__(zp.PeerHandler)
        self.handler.server = SimpleNamespace(peer=self.peer)
        self.handler.path = '/event'
        self.handler.client_address = ('127.0.0.2', 30000)
        self.handler.reply = Mock()

    def subscribe(self):
        self.handler.headers = {'CALLBACK': '<http://127.0.0.1/callback>', 'NT': 'upnp:event'}
        with patch.object(zp.threading, 'Timer'):
            self.handler.do_SUBSCRIBE()
        self.assertEqual(self.handler.reply.call_args.args[0], 200)
        return self.handler.reply.call_args.kwargs['headers']['SID']

    def test_subscription_records_requester_separately_from_allowed_shared_callback(self):
        sid = self.subscribe()
        subscription = self.peer.state.subscriptions[sid]
        self.assertEqual(subscription['source'], '127.0.0.2')
        self.assertEqual(subscription['callback'], 'http://127.0.0.1/callback')

    def test_other_requester_cannot_renew_or_unsubscribe_even_when_callback_is_shared(self):
        sid = self.subscribe()
        self.handler.headers = {'SID': sid}
        for method in (self.handler.do_SUBSCRIBE, self.handler.do_UNSUBSCRIBE):
            self.handler.client_address = ('127.0.0.1', 30000)
            previous = dict(self.peer.state.subscriptions[sid])
            method()
            self.assertEqual(self.handler.reply.call_args.args[0], 412)
            self.assertEqual(self.peer.state.subscriptions[sid], previous)
        self.handler.client_address = ('127.0.0.2', 30000)
        self.handler.do_SUBSCRIBE()
        self.assertEqual(self.handler.reply.call_args.args[0], 200)
        self.handler.do_UNSUBSCRIBE()
        self.assertEqual(self.handler.reply.call_args.args[0], 200)
        self.assertNotIn(sid, self.peer.state.subscriptions)

    def test_legacy_manual_subscription_uses_callback_address_as_owner(self):
        self.peer.state.subscriptions['legacy'] = {'service': 'ZoneGroupTopology',
            'callback': 'http://127.0.0.2/event', 'expires': time.monotonic() + 100}
        self.handler.headers = {'SID': 'legacy'}
        self.handler.do_SUBSCRIBE()
        self.assertEqual(self.handler.reply.call_args.args[0], 200)
        self.handler.do_UNSUBSCRIBE()
        self.assertEqual(self.handler.reply.call_args.args[0], 200)


class NotificationHook(unittest.TestCase):
    def test_addressed_notice_uses_requester_when_callbacks_are_shared(self):
        delivered = queue.Queue()
        subscriptions = {sid: {'service': 'AVTransport', 'source': source,
            'callback': 'http://127.0.0.1/shared', 'expires': time.monotonic() + 100}
            for sid, source in (('controller-a', '127.0.0.2'), ('controller-b', '127.0.0.3'))}
        # Older/manual subscriptions retain callback-host matching.
        subscriptions['legacy'] = {'service': 'AVTransport', 'callback': 'http://127.0.0.2/legacy',
                                   'expires': time.monotonic() + 100}
        player = SimpleNamespace(state=SimpleNamespace(lock=threading.Lock(), subscriptions=subscriptions),
                                 notify=lambda sid, body_override: delivered.put((sid, body_override)))
        sender = ControllerNotices([player], log=lambda _: None, delay=0, clear_delay=0)
        self.addCleanup(sender.close)
        sender('127.0.0.2', 'Only controller A')
        events = [delivered.get(timeout=2) for _ in range(4)]
        self.assertEqual({sid for sid, _ in events}, {'controller-a', 'legacy'})
        self.assertEqual(sum(b'Only controller A' in body for _, body in events), 2)
        self.assertTrue(delivered.empty())

    def test_hook_receives_subscription_under_its_lock_and_override_bypasses_hook(self):
        subscription = {'service': 'ZoneGroupTopology', 'source': '127.0.0.2',
            'callback': 'http://127.0.0.1/event', 'expires': time.monotonic() + 100,
            'sequence': 0, 'notify_lock': threading.Lock()}
        class Peer(zp.ZP80Peer):
            def notification_body(self, sub):
                self.assert_locked = not sub['notify_lock'].acquire(blocking=False)
                self.seen = sub
                return b'controller-specific body'
        peer = object.__new__(Peer)
        peer.state = SimpleNamespace(lock=threading.Lock(), subscriptions={'sid': subscription})
        peer.config = SimpleNamespace(browser_workers=False)
        connection = Mock()
        connection.getresponse.return_value.status = 200
        with patch.object(zp.http.client, 'HTTPConnection', return_value=connection):
            peer.notify('sid')
            self.assertTrue(peer.assert_locked)
            self.assertIs(peer.seen, subscription)
            self.assertEqual(connection.request.call_args.args[2], b'controller-specific body')
            peer.notification_body = Mock(side_effect=AssertionError('Body override called hook'))
            peer.notify('sid', body_override=b'policy notice')
            self.assertEqual(connection.request.call_args.args[2], b'policy notice')
            peer.notification_body.assert_not_called()
        self.assertEqual(subscription['sequence'], 2)

    def test_default_hook_preserves_ordinary_propertysets(self):
        peer = object.__new__(zp.ZP80Peer)
        peer.state = SimpleNamespace(propertyset=Mock(return_value=b'ordinary event'))
        self.assertEqual(peer.notification_body({'service': 'RenderingControl'}), b'ordinary event')
        peer.state.propertyset.assert_called_once_with('RenderingControl')


if __name__ == '__main__': unittest.main()
