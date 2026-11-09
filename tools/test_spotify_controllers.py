"""Controller identity and addressed notification integration checks."""
import http.client
import http.server
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from spotify_controllers import ControllerRegistry, ControllerNotices, notice_body
from spotify_menu import LOCK_MESSAGE, default_items, scoped_id
from spotify_service import SpotifyService
from test_backends import MemoryBackend
from test_spotify_service import Client, NS, TRACK, call, item
from test_zoneplayers import HOUSEHOLD, soap, zoneplayers, zp

A = '00:0e:58:01:02:03'
B = '00:0e:58:04:05:06'


class Identity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'state').write_text(json.dumps({'controllers':[{'mac':A},{'mac':B}]}))
        self.leases = self.root/'leases'

    def tearDown(self): self.temp.cleanup()

    def test_lease_expiry_unknown_hosts_and_current_neighbour(self):
        self.leases.write_text('%d %s 192.168.69.50 a *\n%d %s 192.168.69.51 b *\n0 ff:ff:ff:ff:ff:ff 192.168.69.52 x *\n' %
                               (time.time()+100,A,time.time()-1,B))
        reg = ControllerRegistry(self.root/'state',self.leases,neighbours=lambda:'')
        self.assertEqual(reg('192.168.69.50'),A)
        self.assertIsNone(reg('192.168.69.51'))
        self.assertIsNone(reg('192.168.69.52'))
        self.assertIsNone(reg('not-an-ip'))
        reg = ControllerRegistry(self.root/'state',self.leases,
            neighbours=lambda:'192.168.69.50 lladdr '+B+' REACHABLE\n192.168.69.51 lladdr '+A+' FAILED\n')
        self.assertEqual(reg('192.168.69.50'),B)
        self.assertIsNone(reg('192.168.69.51'))

    def test_no_stale_guesses_on_corrupt_state(self):
        self.leases.write_text('0 '+A+' 192.168.69.50 a *\n')
        (self.root/'state').write_text('{broken')
        reg=ControllerRegistry(self.root/'state',self.leases,neighbours=lambda:'')
        self.assertIsNone(reg('192.168.69.50'))


class Notices(unittest.TestCase):
    def test_body_round_trips_attributes_and_newlines(self):
        root=ET.fromstring(notice_body('ERROR_BRIDGE_POLICY','A & B\n"quoted"'))
        inner=ET.fromstring(root.find('.//LastChange').text)
        self.assertEqual(next(x for x in inner.iter() if x.tag.endswith('TransportErrorDescription')).get('val'),
                         'A & B\n"quoted"')

    def test_only_target_subscription_receives_error_then_ok(self):
        received=queue.Queue()
        class Callback(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):
                data=self.rfile.read(int(self.headers['Content-Length']))
                received.put((self.path,self.headers['SID'],self.headers['SEQ'],data))
                self.send_response(200);self.end_headers()
            def log_message(self,*_):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Callback)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        peer=zp.ZP80Peer(zp.Config(bind='127.0.0.1',port=0,household=HOUSEHOLD))
        peer.state.uri='spotify:track:unchanged';peer.state.transport='PLAYING'
        def sub(host,service='AVTransport'):
            return {'service':service,'callback':f'http://{host}:{server.server_port}/target',
                    'sequence':4,'expires':time.monotonic()+100,'notify_lock':threading.Lock()}
        peer.state.subscriptions={'target':sub('127.0.0.1'),'other':sub('127.0.0.2'),
                                  'rendering':sub('127.0.0.1','RenderingControl')}
        sender=ControllerNotices([peer],lambda _:None,delay=0,clear_delay=0)
        try:
            sender('127.0.0.1','This feature has been locked by your administrator')
            first,second=received.get(timeout=3),received.get(timeout=3)
            self.assertEqual((first[1:3],second[1:3]),(('target','4'),('target','5')))
            self.assertIn(b'ERROR_BRIDGE_POLICY',first[3]);self.assertIn(b'OK',second[3])
            self.assertEqual(peer.state.uri,'spotify:track:unchanged')
            self.assertEqual(peer.state.transport,'PLAYING')
            self.assertEqual(peer.state.subscriptions['other']['sequence'],4)
            with self.assertRaises(queue.Empty):received.get(timeout=.1)
        finally:
            sender.close();peer.http.server_close();server.shutdown();server.server_close()

    def test_closed_sender_does_not_deliver(self):
        sender=ControllerNotices([],lambda _:None,delay=0);sender.close()
        sender('127.0.0.1','message')

    def notice_fixture(self, count=1):
        received = queue.Queue()
        players = []
        for number in range(count):
            state = SimpleNamespace(lock=threading.Lock(), subscriptions={str(number): {
                'service': 'AVTransport', 'callback': 'http://127.0.0.1/notify',
                'expires': time.monotonic()+300}})
            players.append(SimpleNamespace(state=state,
                notify=lambda sid, body_override: received.put((sid, body_override))))
        sender = ControllerNotices(players, lambda _: None, delay=0, clear_delay=60)
        self.addCleanup(sender.close)
        return sender, received

    def controlled_clear(self, sender):
        waiting, finished, gates = queue.Queue(), queue.Queue(), []
        original_wait, original_deliver = sender.closed.wait, sender._deliver

        def wait(timeout):
            if timeout != sender.clear_delay:
                return original_wait(timeout)
            gate = threading.Event()
            gates.append(gate)
            waiting.put((threading.current_thread(), gate))
            return not gate.wait(3) or sender.closed.is_set()

        def deliver(*args):
            try:
                return original_deliver(*args)
            finally:
                finished.put(threading.current_thread())

        waits = patch.object(sender.closed, 'wait', side_effect=wait)
        deliveries = patch.object(sender, '_deliver', side_effect=deliver)
        waits.start(); deliveries.start()
        self.addCleanup(waits.stop); self.addCleanup(deliveries.stop)
        def cleanup():
            sender.close()
            for gate in gates: gate.set()
        self.addCleanup(cleanup)
        return waiting, finished

    def test_all_error_sends_precede_wait_and_clear_is_withheld(self):
        sender, received = self.notice_fixture(count=2)
        waiting, finished = self.controlled_clear(sender)
        sender('127.0.0.1', 'Held message')
        worker, gate = waiting.get(timeout=2)
        errors = [received.get_nowait(), received.get_nowait()]
        self.assertEqual({sid for sid, _ in errors}, {'0', '1'})
        self.assertTrue(all(b'ERROR_BRIDGE_POLICY' in body for _, body in errors))
        self.assertTrue(received.empty())
        gate.set()
        self.assertIs(finished.get(timeout=2), worker)
        clears = [received.get_nowait(), received.get_nowait()]
        self.assertTrue(all(b'TransportStatus val="OK"' in body for _, body in clears))
        self.assertTrue(received.empty())

    def test_close_interrupts_pending_clear(self):
        sender, received = self.notice_fixture()
        finished = threading.Event()
        original = sender._deliver
        def deliver(*args):
            try:
                return original(*args)
            finally:
                finished.set()
        with patch.object(sender, '_deliver', side_effect=deliver):
            sender('127.0.0.1', 'Cancel pending clear')
            self.assertIn(b'ERROR_BRIDGE_POLICY', received.get(timeout=2)[1])
            sender.close()
            self.assertTrue(finished.wait(2), 'Closing must interrupt the 60-second clear wait')
        self.assertTrue(received.empty())
        self.assertEqual(sender.pending, {})

    def test_older_clear_cannot_erase_newer_notice(self):
        sender, received = self.notice_fixture()
        waiting, finished = self.controlled_clear(sender)
        now = [time.monotonic()]
        with patch('spotify_controllers.time.monotonic', side_effect=lambda: now[0]):
            sender('127.0.0.1', 'First message')
            old_worker, old_gate = waiting.get(timeout=2)
            self.assertIn(b'First message', received.get_nowait()[1])
            sender('127.0.0.1', 'Debounced message')
            self.assertTrue(received.empty())
            now[0] += .8
            sender('127.0.0.1', 'New message')
            new_worker, new_gate = waiting.get(timeout=2)
            self.assertIn(b'New message', received.get_nowait()[1])
            old_gate.set()
            self.assertIs(finished.get(timeout=2), old_worker)
            self.assertTrue(received.empty(), 'The obsolete generation must not send OK')
            new_gate.set()
            self.assertIs(finished.get(timeout=2), new_worker)
            self.assertIn(b'TransportStatus val="OK"', received.get_nowait()[1])
            self.assertTrue(received.empty())
        self.assertEqual(sender.pending, {})


class PlaybackHTTP(unittest.TestCase):
    """Exercise policy before the real player handler mutates transport state."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root/'state').write_text(json.dumps({'controllers': [{'mac': A}]}))
        (root/'leases').write_text('0 '+A+' 127.0.0.1 controller *\n')
        self.resolver = Mock(wraps=ControllerRegistry(root/'state', root/'leases', neighbours=lambda: ''))
        self.items = default_items()
        self.client, self.notices = Client(), []
        self.client.responses['/me/tracks'] = {'items': [{'track': item()}], 'total': 1}
        self.service = SpotifyService('127.0.0.1', 0, self.client,
            menu_settings={'controller_menus': {A: {'items': self.items}}},
            controller_resolver=self.resolver,
            on_locked=lambda ip, message: self.notices.append((ip, message)), log=lambda _: None)
        self.service.start()
        self.addCleanup(self.service.close)
        self.backend = MemoryBackend('memory')
        self.house = zoneplayers.Household(zp, HOUSEHOLD, ['Den'], ['127.0.0.1'],
            lambda _: None, port=0, backends=[self.backend], refs=[('memory', 'den')],
            music_service=self.service)
        self.addCleanup(self.house.close)
        self.house.start('127.0.0.1', ssdp=False)
        self.player = self.house.players[0]
        self.room = self.player.state.backend
        self.enqueue = self.room.enqueue_provider = Mock(return_value={
            'FirstTrackNumberEnqueued': '1', 'NumTracksAdded': '1', 'NewQueueLength': '1'})
        self.play = self.room.play_provider = Mock()

        # This is a legitimately issued cached child, not a forged alias. Both
        # SMAPI and the virtual player receive actual loopback HTTP requests.
        status, doc = call(self.service, 'getMetadata', {'id': scoped_id('tracks', 'library:tracks')})
        self.assertEqual(status, 200)
        alias = doc.find('.//m:id', NS).text
        self.uri = 'x-sonos-http:'+quote(alias, safe='')+'.mp3?sid=240&flags=0'
        self.items[-1]['locked'] = True
        self.client.calls.clear()
        self.resolver.reset_mock()
        self.player.state.uri = 'x-rincon-queue:preserved#0'
        self.player.state.metadata = '<preserved/>'
        self.player.state.transport = self.room.transport = 'PLAYING'

    def check_locked_action(self, action):
        name = 'Enqueued' if action == 'AddURIToQueue' else 'Current'
        args = ('<InstanceID>0</InstanceID><%sURI>%s</%sURI>'
                '<%sURIMetaData></%sURIMetaData>' % (name, escape(self.uri), name, name, name))
        if action == 'AddURIToQueue':
            args += '<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued><EnqueueAsNext>0</EnqueueAsNext>'
        state = self.player.state
        before = (state.uri, state.metadata, state.transport, self.room.transport,
                  self.room.playing, self.room.queue_version(), list(self.backend.calls))
        with patch.object(self.service, 'resolve_playback', wraps=self.service.resolve_playback) as resolve:
            status, body = soap(self.player, '/MediaRenderer/AVTransport/Control', 'AVTransport', action, args)
            resolve.assert_called_once_with(self.uri, '', controller_ip='127.0.0.1')
        self.assertEqual(status, 500)
        fault = ET.fromstring(body)
        self.assertEqual(next(n.text for n in fault.iter() if n.tag.endswith('errorCode')), '501')
        self.assertIn(LOCK_MESSAGE.encode(), body)
        self.assertEqual(self.notices, [('127.0.0.1', LOCK_MESSAGE)])
        self.resolver.assert_called_with('127.0.0.1')
        self.assertEqual((state.uri, state.metadata, state.transport, self.room.transport,
                          self.room.playing, self.room.queue_version(), self.backend.calls), before)
        self.enqueue.assert_not_called()
        self.play.assert_not_called()
        self.assertEqual(self.client.calls, [])

        # The same issued alias and HTTP body reach the backend when unlocked;
        # a malformed request or broken routing cannot make this test pass.
        self.items[-1]['locked'] = False
        status, body = soap(self.player, '/MediaRenderer/AVTransport/Control', 'AVTransport', action, args)
        self.assertEqual(status, 200, body)
        if action == 'AddURIToQueue':
            self.enqueue.assert_called_once_with('spotify:track:'+TRACK, 'music', False)
            self.play.assert_not_called()
        else:
            self.play.assert_called_once_with('spotify:track:'+TRACK, 'music')
            self.enqueue.assert_not_called()

    def test_locked_cached_add_uri_to_queue_denied_before_backend(self):
        self.check_locked_action('AddURIToQueue')

    def test_locked_cached_set_transport_uri_denied_before_state_mutation(self):
        self.check_locked_action('SetAVTransportURI')


if __name__=='__main__': unittest.main()
