"""Regressions for asynchronous selections, episode takeover and decoder ownership."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from backends import BackendError
from radio_catalog import RadioCatalog, RadioItem
from radio_playback import RadioPlayback, radio_uri
from radio_store import RadioStore
from radiotime import RadioStream, RadioTimeClient, RadioTimeError
from test_radio_playback import Room


class Catalog:
    def __init__(self):
        self.entered,self.release = threading.Event(),threading.Event()
        self.blocked = None
        self.episode = False
        self.empty = False
        self.wma = False
        self.api_error = None
    def metadata(self, identity):
        if identity == self.blocked:
            self.entered.set()
            if not self.release.wait(3): raise AssertionError('Test did not release slow metadata')
        return RadioItem(identity,identity,'episode' if self.episode else 'station',
                         image='https://example.org/image.jpg',artist='Episode host',album='Show',duration=200)
    def resolve(self, identity, formats=()):
        if self.api_error: raise self.api_error
        if self.empty:return []
        if self.wma:
            if formats != ('wma','hls'): raise RadioTimeError('No matching target format','unavailable',404)
            return [RadioStream('http://example.org/first.wma','wma'),RadioStream('http://example.org/second.wma','wma')]
        return [SimpleNamespace(url='https://example.org/'+identity+'.mp3')]


class Transcoder:
    available = True
    def __init__(self): self.sources,self.prepared,self.stopped = {},[],[]
    def prepare(self,url,identity):
        self.stop(identity)
        result = 'http://bridge.local/converted/%d.mp3' % len(self.prepared)
        self.prepared.append((url,identity,result)); self.sources[result] = identity
        return result
    def stop(self,identity):
        self.stopped.append(identity)
        self.sources = {url:owner for url,owner in self.sources.items() if owner != identity}


class PlaybackReview(unittest.TestCase):
    def setUp(self):
        self.room = Room(); self.catalog = Catalog(); self.store = RadioStore()
        self.room.position = lambda: getattr(self.room,'elapsed',0)
        self.room.elapsed = 60
        self.player = SimpleNamespace(config=SimpleNamespace(udn='room',advertise_ip='192.0.2.1',port=1400),
            state=SimpleNamespace(backend=self.room),http_thread=None)
        self.player.state.config = self.player.config
        self.house = SimpleNamespace(by_udn={'room':self.player},coordinator={'room':'room'},
                                     radio_service=SimpleNamespace(art_base_url='http://bridge.local:8402'))
        self.now = 100.0
        self.playback = RadioPlayback(self.house,self.catalog,self.store,clock=lambda:self.now,log=lambda _:None)
        self.playback.art.path_for = lambda *args,**kwargs:'/art/cover.jpg'
        profile = patch.object(self.playback,'refresh_profile');profile.start();self.addCleanup(profile.stop)
        self.addCleanup(self.playback.close)

    def slow_play(self):
        errors = []
        def play():
            try:self.playback.play('room','station:s1')
            except BackendError as error:errors.append(error)
        self.catalog.blocked = 'station:s1'
        worker = threading.Thread(target=play);worker.start()
        self.addCleanup(self.catalog.release.set)
        self.assertTrue(self.catalog.entered.wait(1))
        return worker,errors

    def test_slow_old_selection_cannot_replace_new_selection(self):
        policies=[]
        self.house.before_playback_action=lambda room,service,action,args:policies.append(('before',args['CurrentURI'])) or (None,None)
        self.house.after_playback_action=lambda room,service,action,args,**kwargs:policies.append(('after',args['CurrentURI']))
        worker,errors = self.slow_play()
        self.playback.play('room','station:s2')
        self.catalog.release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(len(errors),1)
        self.assertEqual(self.playback.session('room').item_id,'station:s2')
        self.assertEqual([call[1] for call in self.room.calls if call[0]=='radio'],['https://example.org/station:s2.mp3'])
        self.assertEqual(policies,[('before',radio_uri('station:s2')),('after',radio_uri('station:s2'))])

    def test_stop_cancels_a_selection_still_resolving(self):
        worker,errors = self.slow_play()
        self.playback.transport_action(self.player.state,'AVTransport','Stop',{})
        self.catalog.release.set();worker.join(2)
        self.assertEqual(len(errors),1);self.assertIsNone(self.playback.session('room'))
        self.assertEqual(self.room.calls,[])

    def test_unavailable_target_format_reaches_lazy_wma_fallback(self):
        self.catalog.wma = True; proxy = Transcoder();self.playback.transcoder = proxy
        self.room.radio_formats = lambda:('mp3','aac')
        original = self.room.play_radio
        def play(uri,metadata):
            self.assertIn(uri,proxy.sources)
            original(uri,metadata)
        self.room.play_radio = play
        self.playback.play('room','station:s1')
        self.assertEqual(len(proxy.prepared),1)
        self.assertEqual(proxy.prepared[0][0],'http://example.org/first.wma')

    def test_upstream_api_errors_do_not_trigger_conversion(self):
        self.catalog.api_error = RadioTimeError('Server unavailable','http',503)
        proxy = Transcoder();self.playback.transcoder = proxy
        with self.assertRaises(RadioTimeError):self.playback.play('room','station:s1')
        self.assertEqual(proxy.prepared,[])

    def test_wtmc_mislabeled_mp3_hls_uses_proxy_and_mp3_transport_metadata(self):
        source='https://video.deldot.gov/live/WTMC1380.stream/playlist.m3u8'
        def transport(url,**kwargs):
            if urlsplit(url).path.endswith('Describe.ashx'):
                body=[{'element':'station','guide_id':'s342087','name':'WTMC'}]
            else:
                body=[{'element':'audio','url':source,'media_type':'mp3','bitrate':128}]
            return {'head':{'status':'200'},'body':body}
        self.playback.catalog=RadioCatalog(RadioTimeClient(transport,min_request_interval=0),self.store)
        self.room.radio_formats=lambda:('mp3','aac','wma')
        proxy=Transcoder();self.playback.transcoder=proxy
        before=list(self.room.queue)
        self.playback.play('room','station:s342087')
        self.assertEqual(len(proxy.prepared),1)
        self.assertEqual(proxy.prepared[0][0],source)
        played=self.room.calls[-1]
        self.assertEqual(played[1],proxy.prepared[0][2])
        self.assertIn('protocolInfo="http-get:*:audio/mpeg:*"',played[2])
        self.assertNotIn('protocolInfo="https:',played[2])
        self.assertEqual(self.room.queue,before)
        self.assertEqual(self.playback.session('room').item.title,'WTMC')

    def test_native_supported_wma_is_not_converted(self):
        self.catalog.resolve=lambda identity,formats: [RadioStream('https://example.org/native.wma','wma')]
        self.room.radio_formats=lambda:('mp3','aac','wma')
        proxy=Transcoder();self.playback.transcoder=proxy
        self.playback.play('room','station:s1')
        self.assertEqual(proxy.prepared,[])
        self.assertEqual(self.room.calls[-1][1],'https://example.org/native.wma')
        self.assertIn('protocolInfo="http-get:*:audio/x-ms-wma:*"',self.room.calls[-1][2])

    def test_direct_aac_transport_metadata_uses_http_get_and_actual_mime(self):
        self.catalog.resolve=lambda identity,formats: [RadioStream('https://example.org/live','aac')]
        self.room.radio_formats=lambda:('mp3','aac','wma')
        proxy=Transcoder();self.playback.transcoder=proxy
        self.playback.play('room','station:s1')
        self.assertEqual(proxy.prepared,[])
        self.assertIn('protocolInfo="http-get:*:audio/aac:*"',self.room.calls[-1][2])
        values=self.playback.values('room',{})
        self.assertTrue(values['AVTransportURI'].startswith('x-rincon-mp3radio://bridge-radio/'))

    def test_rejected_converted_alternative_revokes_its_decoder_before_retry(self):
        self.catalog.wma=True;proxy=Transcoder();self.playback.transcoder=proxy
        self.room.radio_formats=lambda:('mp3','aac')
        original=self.room.play_radio
        def play(uri,metadata):
            if uri.endswith('/0.mp3'): raise BackendError('Renderer rejected first alternative')
            self.assertEqual(list(proxy.sources),[uri])
            return original(uri,metadata)
        self.room.play_radio=play
        generation=self.playback.play('room','station:s1')
        self.assertEqual(len(proxy.prepared),2)
        self.assertEqual(list(proxy.sources.values()),[generation])

    def test_stop_converted_room_does_not_revoke_another_room(self):
        self.catalog.wma = True;proxy = Transcoder();self.playback.transcoder = proxy
        second = SimpleNamespace(config=SimpleNamespace(udn='second'),state=SimpleNamespace(backend=Room()),http_thread=None)
        second.state.backend.radio_formats = self.room.radio_formats = lambda:('mp3',)
        self.house.by_udn['second']=second;self.house.coordinator['second']='second'
        first = self.playback.play('room','station:s1');other = self.playback.play('second','station:s2')
        self.playback.transport_action(self.player.state,'AVTransport','Stop',{})
        self.assertNotIn(first,proxy.sources.values());self.assertIn(other,proxy.sources.values())

    def test_episode_takeover_does_not_apply_next_track_to_episode_bookmark(self):
        self.catalog.episode = True;self.playback.play('room','episode:t1')
        session = self.playback.session('room')
        self.room.avt['CurrentTrackURI']=session.stream
        self.playback.refresh(session)
        self.assertEqual(self.store.bookmark('episode:t1')['position'],60)
        self.now += 3;self.room.elapsed=150
        self.room.avt['CurrentTrackURI']='https://example.org/next-song.mp3'
        self.playback.refresh(session)
        self.assertIsNone(self.playback.session('room'))
        self.assertEqual(self.store.bookmark('episode:t1')['position'],60)

    def test_manual_episode_next_releases_overlay_only_after_success(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        self.assertIsNone(self.playback.transport_action(self.player.state,'AVTransport','Next',{}))
        self.assertIsNotNone(self.playback.session('room'))
        self.playback.after_transport_action('room','AVTransport','Next',{})
        self.assertIsNone(self.playback.session('room'))

    def test_failed_or_denied_source_selection_preserves_radio_and_decoder(self):
        self.catalog.wma=True;proxy=Transcoder();self.playback.transcoder=proxy
        self.room.radio_formats=lambda:('mp3',)
        generation=self.playback.play('room','station:s1')
        self.assertIsNone(self.playback.transport_action(self.player.state,'AVTransport','SetAVTransportURI',
            {'CurrentURI':'spotify:locked-selection'}))
        self.assertIsNotNone(self.playback.session('room'));self.assertIn(generation,proxy.sources.values())
        self.playback.after_transport_action('room','AVTransport','SetAVTransportURI',{'CurrentURI':'spotify:permitted-selection'})
        self.assertIsNone(self.playback.session('room'));self.assertNotIn(generation,proxy.sources.values())

    def test_queued_episode_keeps_artist_album_and_absolute_proxy_art(self):
        self.catalog.episode=True;captured=[]
        self.room.enqueue_item=lambda node,as_next:captured.append(node) or {'NumTracksAdded':'1'}
        self.playback.transport_action(self.player.state,'AVTransport','AddURIToQueue',
            {'EnqueuedURI':radio_uri('episode:t1'),'EnqueueAsNext':'1'})
        self.assertIn('Episode host',captured[0].res_md)
        self.assertIn('Show',captured[0].res_md)
        self.assertIn('http://bridge.local:8402/art/cover.jpg',captured[0].res_md)
        self.assertIsNone(self.playback.session('room'))

    def test_empty_episode_resolution_is_a_reportable_backend_error(self):
        self.catalog.episode=True;self.catalog.empty=True
        with self.assertRaises(BackendError):self.playback.transport_action(self.player.state,'AVTransport','AddURIToQueue',
            {'EnqueuedURI':radio_uri('episode:t1')})

    def test_old_refresh_cannot_cancel_new_session(self):
        self.playback.play('room','station:s1');old=self.playback.session('room')
        self.playback.play('room','station:s2');new=self.playback.session('room')
        self.now += 30;self.playback.refresh(old)
        self.assertIs(self.playback.session('room'),new)

    def test_episode_metadata_is_a_finite_music_track(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        values = self.playback.values('room',{})
        self.assertIn('object.item.audioItem.musicTrack',values['AVTransportURIMetaData'])
        self.assertNotIn('audioBroadcast',values['AVTransportURIMetaData'])

    def test_unknown_episode_duration_writes_valid_resume_bookmark(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        session=self.playback.session('room');session.duration=0;session.last_position=30
        self.playback.save_bookmark(session)
        self.assertEqual(self.store.bookmark('episode:t1')['position'],30)
        self.assertIsNone(self.store.bookmark('episode:t1')['duration'])

    def test_completed_episode_clears_its_resume_bookmark(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        session=self.playback.session('room');session.last_position=30
        self.playback.save_bookmark(session);self.assertIsNotNone(self.store.bookmark('episode:t1'))
        session.last_position=200;self.playback.save_bookmark(session)
        self.assertIsNone(self.store.bookmark('episode:t1'))

    def test_resume_waits_for_transition_to_expose_seek(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        session=self.playback.session('room');checks=[];seeks=[]
        self.room.supports=lambda operation:checks.append(operation) or len(checks)>1
        self.room.seek=seeks.append
        with patch.object(self.playback.stop_event,'wait',return_value=False):
            self.playback.resume_episode(session,40)
        self.assertEqual(seeks,[40]);self.assertEqual(len(checks),2)

    def test_live_controls_do_not_advertise_unsupported_stop(self):
        self.room.supports=lambda operation:operation=='radio_play'
        self.playback.play('room','station:s1')
        self.assertEqual(self.playback.values('room',{})['CurrentTransportActions'],'Play')

    def test_finite_overlay_keeps_real_queue_count_and_play_mode(self):
        self.catalog.episode=True;self.playback.play('room','episode:t1')
        underlying={'NumberOfTracks':'3','NrTracks':'3','CurrentTrack':'2','Track':'2',
                    'CurrentPlayMode':'REPEAT_ALL','PlayMode':'REPEAT_ALL'}
        values=self.playback.values('room',underlying)
        for key,value in underlying.items():self.assertEqual(values[key],value)


if __name__ == '__main__':unittest.main()
