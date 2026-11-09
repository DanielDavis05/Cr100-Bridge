"""Controller menus, scoped locks and native playback round-trip contracts."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import quote

from spotify_menu import (FeatureLocked, LOCK_MESSAGE, default_items, menu_for_controller,
                          normalize_spotify_uri, scoped_id, unscoped_id, validate_items)
from spotify_service import SpotifyService
from test_spotify_service import Client, TRACK, ALBUM, PLAYLIST, item, result, call, NS
from test_spotify_playback import didl

MAC_A = '02:00:00:00:00:01'
MAC_B = '02:00:00:00:00:02'
IP_A, IP_B = '192.168.69.50', '192.168.69.51'
TRACK_URI = 'spotify:track:' + TRACK
PLAYLIST_URI = 'spotify:playlist:' + PLAYLIST


class MenuValidation(unittest.TestCase):
    def test_defaults_are_independent_and_unknown_controllers_keep_default(self):
        one, two = default_items(), default_items()
        one[0]['locked'] = True
        self.assertFalse(two[0]['locked'])
        self.assertEqual(menu_for_controller({}, None), two)
        self.assertEqual(menu_for_controller({'controller_menus': {MAC_A: {'items': one}}}, MAC_B), two)
        self.assertEqual(menu_for_controller({'controller_menus': {MAC_A: {'items': one}}}, MAC_A.upper()), one)

    def test_uri_links_normalize_without_fetching_arbitrary_hosts(self):
        for source in (PLAYLIST_URI, 'https://open.spotify.com/playlist/' + PLAYLIST + '?si=shared',
                       'https://open.spotify.com/intl-en/playlist/' + PLAYLIST):
            self.assertEqual(normalize_spotify_uri(source), PLAYLIST_URI)
        for source in ('https://evil.test/playlist/' + PLAYLIST,
                       'https://open.spotify.com.evil.test/playlist/' + PLAYLIST,
                       'http://open.spotify.com/playlist/' + PLAYLIST,
                       'https://user@open.spotify.com/playlist/' + PLAYLIST,
                       'https://open.spotify.com:444/playlist/' + PLAYLIST,
                       PLAYLIST_URI + '/suffix', 'spotify:artist:' + TRACK):
            with self.subTest(source=source), self.assertRaises(ValueError):
                normalize_spotify_uri(source)

    def test_validation_preserves_order_and_rejects_broken_profiles(self):
        entries = default_items()[::-1]
        entries.append({'id':'entry_pin','kind':'spotify','target':PLAYLIST_URI,'label':' Dinner ','locked':True})
        self.assertEqual(validate_items(entries)[-1]['label'], 'Dinner')
        self.assertEqual(validate_items(entries)[0]['id'], 'tracks')
        for change in (lambda x:x.pop(), lambda x:x.append(x[0]),
                       lambda x:x[0].update(locked='false'), lambda x:x[0].update(label=''),
                       lambda x:x[0].update(target='tracks'), lambda x:x[0].update(label='bad\x00name')):
            bad = default_items();change(bad)
            with self.assertRaises(ValueError):validate_items(bad)

    def test_alias_is_bounded_canonical_and_roundtrips(self):
        ident = scoped_id('entry_pin', TRACK_URI)
        self.assertEqual(unscoped_id(ident), ('entry_pin', TRACK_URI))
        for bad in (ident+'=', ident+'!', 'bm:missing_id:YQ', 'bm:search:', 'bm:search:____', 'x'*1025):
            with self.subTest(bad=bad[:70]), self.assertRaises(ValueError):unscoped_id(bad)


class Folders:
    revision = '1'
    def __init__(self):
        self.calls=[];self.started=False;self.closed=False
        self.tree={'kind':'folder','id':'outer','name':'Actual outer','children':[
            {'kind':'folder','id':'inner','name':'Nested','children':[
                {'kind':'playlist','uri':PLAYLIST_URI,'name':'A playlist'}]}]}
    def start(self):self.started=True
    def close(self):self.closed=True
    def find(self,ident):
        self.calls.append(ident)
        return self.tree if ident=='outer' else self.tree['children'][0] if ident=='inner' else None


class Menus(unittest.TestCase):
    def setUp(self):
        self.client=Client();self.notices=[];self.logs=[];self.folders=Folders()
        a,b=default_items(),default_items()
        a[-1]['locked']=True
        self.settings={'controller_menus':{MAC_A:{'items':a},MAC_B:{'items':b}}}
        self.service=SpotifyService('127.0.0.1',0,self.client,menu_settings=self.settings,
            controller_resolver=lambda ip:{IP_A:MAC_A,IP_B:MAC_B,'127.0.0.1':MAC_A}.get(ip),
            folder_provider=self.folders,on_locked=lambda ip,msg:self.notices.append((ip,msg)),log=self.logs.append)
        self.service.start()
        self.client.responses['/me/tracks']={'items':[{'track':item()}],'total':1}
        self.client.responses['/search']={'tracks':{'items':[item()],'total':1}}
        self.client.responses['/tracks/'+TRACK]=item()
        self.client.responses['/playlists/'+PLAYLIST]=item('playlist',PLAYLIST,'A playlist')
        self.client.responses['/playlists/'+PLAYLIST+'/items']={'items':[{'item':item()}],'total':1}

    def tearDown(self):self.service.close()

    def dispatch(self, ip, method, args):
        with self.service.request_context(ip):return result(self.service,method,args)

    def first_id(self, doc):return doc.find('.//m:id',NS).text

    def add_pin(self,mac,kind='folder',target='outer',locked=False):
        self.settings['controller_menus'][mac]['items'].append(
            {'id':'entry_pin','kind':kind,'target':target,'label':'Local shortcut','locked':locked})

    def uri(self,alias):return 'x-sonos-http:'+quote(alias,safe='')+'.mp3?sid=240&flags=0'

    def test_visible_locked_entry_and_two_controller_policy_before_api_cache(self):
        doc=self.dispatch(IP_A,'getMetadata',{'id':'root'})
        self.assertEqual([x.text for x in doc.findall('.//m:title',NS)],['My Playlists','Saved Albums','Liked Songs'])
        self.assertEqual(self.client.calls,[])
        ident=scoped_id('tracks','library:tracks')
        with self.assertRaises(FeatureLocked):self.dispatch(IP_A,'getMetadata',{'id':ident})
        self.assertEqual(self.notices,[(IP_A,LOCK_MESSAGE)])
        self.assertEqual(self.client.calls,[])
        track=self.first_id(self.dispatch(IP_B,'getMetadata',{'id':ident}))
        self.assertEqual(unscoped_id(track),('tracks',TRACK_URI))
        before=list(self.client.calls)
        with self.assertRaises(FeatureLocked):self.dispatch(IP_A,'getMediaMetadata',{'id':track})
        self.assertEqual(self.client.calls,before)
        self.assertEqual(self.service.resolve_playback(self.uri(track),didl('00030000'+quote(track,safe='')),controller_ip=IP_B),(TRACK_URI,'music'))
        with self.assertRaises(FeatureLocked):self.service.resolve_playback(self.uri(track),controller_ip=IP_A)

    def test_same_song_remains_playable_through_unlocked_search(self):
        ident=self.first_id(self.dispatch(IP_A,'search',{'id':scoped_id('search','category:tracks'),'term':'song'}))
        self.assertEqual(unscoped_id(ident),('search',TRACK_URI))
        self.assertEqual(self.service.resolve_playback(self.uri(ident),controller_ip=IP_A),(TRACK_URI,'music'))
        album=self.dispatch(IP_A,'getMediaMetadata',{'id':ident}).find('.//m:albumId',NS).text
        self.assertEqual(unscoped_id(album),('search','spotify:album:'+ALBUM))
        with self.assertRaises(ValueError):self.service.resolve_playback(self.uri(TRACK_URI),controller_ip=IP_A)

    def test_native_search_wire_path_and_cached_categories_obey_controller_lock(self):
        descriptor=self.service.descriptor()
        self.assertEqual(descriptor['AvailableServiceListVersion'],'5')
        self.assertEqual(ET.fromstring(descriptor['AvailableServiceDescriptorList']).find('./Service').get('Capabilities'),'1')
        # Accepted stock wire trace: native Search occupies one initial root
        # slot, then obtains provider searchTypes from literal ID "search".
        status,root=call(self.service,'getMetadata',{'id':'root','index':0,'count':499})
        self.assertEqual(status,200)
        self.assertEqual(root.find('.//m:total',NS).text,'3')
        self.assertEqual([x.text for x in root.findall('.//m:title',NS)],
                         ['My Playlists','Saved Albums','Liked Songs'])
        status,categories=call(self.service,'getMetadata',{'id':'search','index':0,'count':500})
        self.assertEqual(status,200)
        self.assertEqual([unscoped_id(n.text) for n in categories.findall('.//m:id',NS)],
                         [('search','category:'+k) for k in ('tracks','albums','artists','playlists')])
        self.assertEqual([n.text for n in categories.findall('.//m:itemType',NS)],['search']*4)
        category=self.first_id(categories)
        status,searched=call(self.service,'search',{'id':category,'term':'song','index':0,'count':500})
        self.assertEqual(status,200)
        cached_track=self.first_id(searched)
        self.assertEqual(unscoped_id(cached_track),('search',TRACK_URI))
        before=list(self.client.calls)
        self.settings['controller_menus'][MAC_A]['items'][0]['locked']=True
        for method,args in [('getMetadata',{'id':'search'}),
                            ('search',{'id':category,'term':'song'}),
                            ('search',{'id':'tracks','term':'song'}),
                            ('getMediaMetadata',{'id':cached_track}),
                            ('getMediaURI',{'id':cached_track})]:
            with self.subTest(method=method),self.assertRaises(FeatureLocked):self.dispatch(IP_A,method,args)
        self.assertEqual(self.client.calls,before)
        # The other controller retains independent access to native Search.
        self.assertEqual(self.dispatch(IP_B,'search',{'id':'tracks','term':'song'}).find('.//m:total',NS).text,'1')

    def test_search_rejects_forged_categories_and_raw_content_ids_before_api(self):
        for method,args in [('search',{'id':scoped_id('playlists','category:tracks'),'term':'song'}),
                            ('search',{'id':scoped_id('search','category:unknown'),'term':'song'}),
                            ('search',{'id':TRACK_URI,'term':'song'}),
                            ('getMetadata',{'id':TRACK_URI}),
                            ('getMediaMetadata',{'id':TRACK_URI}),
                            ('getMetadata',{'id':'library:tracks'})]:
            with self.subTest(method=method,args=args),self.assertRaises((ValueError,LookupError)):
                self.dispatch(IP_A,method,args)
        self.assertEqual(self.client.calls,[])

    def test_http_fault_uses_request_ip_and_does_not_return_content(self):
        status,doc=call(self.service,'getMetadata',{'id':scoped_id('tracks','library:tracks')})
        self.assertEqual(status,500)
        self.assertEqual(doc.find('.//faultstring').text,LOCK_MESSAGE)
        self.assertEqual(self.notices,[('127.0.0.1',LOCK_MESSAGE)])
        self.assertEqual(self.client.calls,[])
        self.assertNotIn('must-not-appear',repr(self.logs))

    def test_custom_folder_nesting_order_and_inherited_lock(self):
        self.add_pin(MAC_A)
        outer=scoped_id('entry_pin','folder:outer')
        nested=self.first_id(self.dispatch(IP_A,'getMetadata',{'id':outer}))
        self.assertEqual(unscoped_id(nested),('entry_pin','folder:inner'))
        playlist=self.first_id(self.dispatch(IP_A,'getMetadata',{'id':nested}))
        track=self.first_id(self.dispatch(IP_A,'getMetadata',{'id':playlist}))
        self.assertEqual(unscoped_id(track),('entry_pin',TRACK_URI))
        self.assertEqual(self.service.resolve_playback(self.uri(track),controller_ip=IP_A),(TRACK_URI,'music'))
        self.settings['controller_menus'][MAC_A]['items'][-1]['locked']=True
        before=(list(self.client.calls),list(self.folders.calls))
        for ident in (outer,nested,playlist,track):
            with self.subTest(ident=ident),self.assertRaises(FeatureLocked):
                self.dispatch(IP_A,'getMediaMetadata',{'id':ident})
        with self.assertRaises(FeatureLocked):self.service.resolve_playback(self.uri(track),controller_ip=IP_A)
        self.assertEqual(before,(self.client.calls,self.folders.calls))

    def test_locked_pin_root_never_looks_up_metadata_or_folder(self):
        self.add_pin(MAC_A,'spotify',TRACK_URI,True)
        doc=self.dispatch(IP_A,'getMetadata',{'id':'root'})
        last=doc.findall('.//m:mediaCollection',NS)[-1]
        self.assertEqual(last.find('m:title',NS).text,'Local shortcut')
        self.assertEqual(last.find('m:canPlay',NS).text,'false')
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.folders.calls,[])

    def test_removed_shortcut_and_mixed_playback_context_are_rejected(self):
        self.add_pin(MAC_B,'spotify',TRACK_URI)
        self.dispatch(IP_B,'getMetadata',{'id':'root'})
        pin=scoped_id('entry_pin',TRACK_URI)
        search=self.first_id(self.dispatch(IP_B,'search',{'id':'tracks','term':'song'}))
        with self.assertRaises(ValueError):
            self.service.resolve_playback(self.uri(pin),didl('00030000'+quote(search,safe='')),controller_ip=IP_B)
        self.settings['controller_menus'][MAC_B]['items'].pop()
        with self.assertRaises(LookupError):self.dispatch(IP_B,'getMediaMetadata',{'id':pin})

    def test_catalog_versions_stable_and_specific_to_profile_and_folder_revision(self):
        version=lambda ip:self.dispatch(ip,'getLastUpdate',{}).find('.//m:catalog',NS).text
        a,b=version(IP_A),version(IP_B)
        self.assertNotEqual(a,b);self.assertEqual(a,version(IP_A))
        self.settings['controller_menus'][MAC_A]['items'][0]['label']='Find music'
        self.assertNotEqual(a,version(IP_A));self.assertEqual(b,version(IP_B))
        self.folders.revision='2';self.assertNotEqual(b,version(IP_B))

    def test_request_context_is_thread_local(self):
        barrier=threading.Barrier(2)
        def fetch(ip):
            with self.service.request_context(ip):
                barrier.wait(2)
                try:self.service.get_metadata({'id':scoped_id('tracks','library:tracks')});return 'allowed'
                except FeatureLocked:return 'locked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(fetch,ip) for ip in (IP_A,IP_B)]
            self.assertEqual([f.result(3) for f in futures],['locked','allowed'])

    def test_lock_while_fetching_denies_returned_page(self):
        def lock_during_fetch(params):
            self.settings['controller_menus'][MAC_B]['items'][-1]['locked']=True
            return {'items':[{'track':item()}],'total':1}
        self.client.responses['/me/tracks']=lock_during_fetch
        with self.assertRaises(FeatureLocked):self.dispatch(IP_B,'getMetadata',{'id':scoped_id('tracks','library:tracks')})
        self.assertEqual(self.notices[-1],(IP_B,LOCK_MESSAGE))

    def test_alias_cannot_change_builtin_context(self):
        with self.assertRaises(ValueError):
            self.dispatch(IP_A,'getMetadata',{'id':scoped_id('search','library:tracks')})
        self.assertEqual(self.client.calls,[])

    def test_forged_alias_cannot_browse_unrelated_music_under_a_track_pin(self):
        self.add_pin(MAC_A,'spotify',TRACK_URI)
        self.dispatch(IP_A,'getMetadata',{'id':'root'})
        with self.assertRaises(LookupError):
            self.dispatch(IP_A,'getMetadata',{'id':scoped_id('entry_pin',PLAYLIST_URI)})
        with self.assertRaises(ValueError):
            self.service.resolve_playback('x-rincon-cpcontainer:'+scoped_id('entry_pin',PLAYLIST_URI),
                didl(scoped_id('entry_pin',PLAYLIST_URI)),controller_ip=IP_A)
        self.assertEqual(self.client.calls,[])

    def test_folder_alias_must_remain_below_configured_folder(self):
        self.add_pin(MAC_A)
        with self.assertRaises(ValueError):
            self.dispatch(IP_A,'getMetadata',{'id':scoped_id('entry_pin','folder:outside')})
        self.assertNotIn('outside',self.folders.calls)
        self.assertEqual(self.client.calls,[])

    def test_issued_descendants_cannot_be_replayed_by_another_controller(self):
        self.add_pin(MAC_A);self.add_pin(MAC_B)
        nested=self.first_id(self.dispatch(IP_B,'getMetadata',{'id':scoped_id('entry_pin','folder:outer')}))
        playlist=self.first_id(self.dispatch(IP_B,'getMetadata',{'id':nested}))
        before=list(self.client.calls)
        with self.assertRaises(LookupError):self.dispatch(IP_A,'getMetadata',{'id':playlist})
        self.assertEqual(self.client.calls,before)
        # Once A actually traverses its own shortcut, the same source is valid
        # under A's independent policy, without inheriting B's issued registry.
        nested_a=self.first_id(self.dispatch(IP_A,'getMetadata',{'id':scoped_id('entry_pin','folder:outer')}))
        playlist_a=self.first_id(self.dispatch(IP_A,'getMetadata',{'id':nested_a}))
        self.assertEqual(playlist,playlist_a)
        self.dispatch(IP_A,'getMetadata',{'id':playlist_a})

    def test_changing_a_shortcut_target_invalidates_issued_old_descendants(self):
        self.add_pin(MAC_B,'spotify',PLAYLIST_URI)
        track=self.first_id(self.dispatch(IP_B,'getMetadata',{'id':scoped_id('entry_pin',PLAYLIST_URI)}))
        self.settings['controller_menus'][MAC_B]['items'][-1]['target']='spotify:playlist:'+('9'*22)
        with self.assertRaises(LookupError):self.dispatch(IP_B,'getMediaMetadata',{'id':track})

    def test_stale_playback_context_uses_safe_value_error(self):
        track=self.first_id(self.dispatch(IP_B,'search',{'id':'tracks','term':'song'}))
        self.service.issued_contexts.clear()
        before=list(self.client.calls)
        with self.assertRaisesRegex(ValueError,'Refresh Spotify'):
            self.service.resolve_playback(self.uri(track),controller_ip=IP_B)
        self.assertEqual(self.client.calls,before)
        self.add_pin(MAC_B,'spotify',TRACK_URI)
        alias=scoped_id('entry_pin',TRACK_URI)
        self.settings['controller_menus'][MAC_B]['items'].pop()
        with self.assertRaisesRegex(ValueError,'no longer available'):
            self.service.resolve_playback(self.uri(alias),controller_ip=IP_B)

    def test_missing_folder_playback_uses_safe_value_error(self):
        self.add_pin(MAC_B,'folder','gone')
        alias=scoped_id('entry_pin','folder:gone')
        with self.assertRaisesRegex(ValueError,'folder is unavailable'):
            self.service.resolve_playback('x-rincon-cpcontainer:'+alias,didl(alias),controller_ip=IP_B)
        self.assertEqual(self.client.calls,[])
        self.settings['controller_menus'][MAC_B]['items'][-1]['locked']=True
        with self.assertRaises(FeatureLocked) as denied:
            self.service.resolve_playback('x-rincon-cpcontainer:'+alias,didl(alias),controller_ip=IP_B)
        self.assertEqual(str(denied.exception),LOCK_MESSAGE)
        self.assertEqual(self.notices[-1],(IP_B,LOCK_MESSAGE))


if __name__=='__main__':unittest.main()
