import copy
import unittest
from urllib.parse import parse_qs, urlsplit

from radio_catalog import RadioCatalog
from radio_store import RadioStore
from radiotime import RadioTimeClient, RadioTimeError


def station(number, title=None, **fields):
    return dict(element='outline', type='audio', text=title or 'Station %d' % number,
                guide_id='s%d' % number, URL='http://opml.radiotime.com/Tune.ashx?id=s%d' % number,
                item='station', image='https://images.test/s%d.jpg' % number, **fields)


def link(title, **params):
    from urllib.parse import urlencode
    return dict(element='outline', type='link', text=title,
                URL='http://opml.radiotime.com/Browse.ashx?' + urlencode(params))


class CatalogTests(unittest.TestCase):
    def make(self, handler):
        calls = []
        def transport(url, **kwargs):
            parsed = urlsplit(url)
            params = {key: values[0] for key, values in parse_qs(parsed.query).items() if key != 'render'}
            calls.append((parsed.path, params))
            result = handler(parsed.path, params)
            return copy.deepcopy(result if isinstance(result, dict) else dict(head={'status': '200'}, body=result))
        client = RadioTimeClient(transport, min_request_interval=0)
        return RadioCatalog(client, RadioStore()), calls

    def test_original_root_and_podcasts_visible_without_network(self):
        catalog, calls = self.make(lambda *args: self.fail('Root must not fetch the directory'))
        self.assertEqual([item.title for item in catalog.root()],
                         ['Favorites', 'Search', 'Local Radio', 'Music', 'Talk', 'Sports', 'Location', 'Podcasts', 'Radio Settings'])
        catalog.store.set_location({'label': 'Boston', 'latlon': '42,-71'})
        self.assertEqual(catalog.root()[2].title, 'Local Radio (Boston)')
        self.assertEqual(calls, [])

    def test_finite_pages_are_lazy_frozen_deduplicated_and_cursor_loops_end(self):
        next_row = dict(link('More Stations', id='g11', offset=26, filter='s'), key='nextStations')
        def handler(path, query):
            if query.get('c') == 'music':
                return [link('Rock', id='g19'), link('Jazz', id='g11')]
            if query.get('offset') == '26':
                return [station(1), station(2), next_row]
            return [dict(text='Stations', key='stations', children=[station(1), station(1), next_row]),
                    dict(text='Shows', key='shows', children=[dict(type='link', text='A Show', guide_id='p17',
                         URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p17')])]
        catalog, calls = self.make(handler)
        genres = catalog.children('music')
        self.assertEqual([item.title for item in genres], ['Jazz', 'Rock'])
        self.assertEqual(len(calls), 1)
        catalog.store.update_settings({'station_order': 'provider'})
        page = catalog.browse(genres[0].id)
        self.assertEqual([item.id for item in page.items if item.kind == 'station'], ['station:s1'])
        more = next(item for item in page.items if item.metadata.get('cursor'))
        self.assertEqual(len(calls), 2)
        frozen = catalog.browse(genres[0].id)
        self.assertEqual([item.id for item in page.items], [item.id for item in frozen.items])
        self.assertEqual(len(calls), 2)
        shows = next(item for item in page.items if item.title == 'Shows')
        self.assertEqual(catalog.children(shows.id)[0].id, 'show:p17')
        second = catalog.browse(more.id)
        self.assertEqual([item.id for item in second.items], ['station:s2'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(second.total, 1)

    def test_popularity_default_preserves_show_and_related_sections(self):
        def handler(path, query):
            if query.get('c') == 'music':
                return [link('Jazz', id='g11')]
            if query.get('filter') == 's:popular':
                return [station(2), station(1)]
            return [dict(key='stations', text='Stations', children=[station(1), station(2),
                         dict(link('Most Popular', id='g11', filter='s:popular'), key='popular')]),
                    dict(key='shows', text='Shows', children=[dict(type='link', text='Program', guide_id='p17',
                         URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p17')]),
                    dict(key='related', text='Explore Jazz', children=[link('Smooth Jazz', id='g12')])]
        catalog, calls = self.make(handler)
        genre = catalog.children('music')[0]
        items = catalog.children(genre.id)
        self.assertEqual([item.id for item in items if item.kind == 'station'], ['station:s2', 'station:s1'])
        self.assertIn('Shows', [item.title for item in items])
        self.assertIn('Explore Jazz', [item.title for item in items])
        shows = next(item for item in items if item.title == 'Shows')
        self.assertEqual(catalog.children(shows.id)[0].id, 'show:p17')

    def test_host_search_returns_associated_shows_without_relabeling_station_or_artist(self):
        def handler(path, query):
            self.assertEqual(query['filter'], 'p')
            return [station(1), dict(type='link', text='Artist: Terry Gross', guide_id='m1',
                                    URL='http://opml.radiotime.com/Browse.ashx?id=m1'),
                    dict(type='link', text='Fresh Air', guide_id='p17',
                         URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p17')]
        catalog, calls = self.make(handler)
        page = catalog.search('Terry Gross', 'hosts')
        self.assertEqual([item.id for item in page.items], ['show:p17'])
        self.assertEqual(page.items[0].metadata['host_query'], 'Terry Gross')
        self.assertEqual(len(calls), 1)
        self.assertEqual(catalog.store.searches(), ['Terry Gross'])

    def test_show_options_episodes_and_finite_playback_resolution(self):
        def handler(path, query):
            if path.endswith('Describe.ashx'):
                if query['id'] == 'p17':
                    return [dict(element='show', guide_id='p17', title='Fresh Air', logo='show.jpg', has_topics=True)]
                return [dict(element='topic', guide_id='t123', title='An Episode', duration='1800', program_id='p17')]
            if query.get('c') == 'pbrowse':
                return [dict(type='link', text='Listen now', key='liveShow', URL='http://opml.radiotime.com/Tune.ashx?c=sbrowse&id=p17'),
                        dict(text='Recent Episodes', key='topics', children=[dict(type='audio', text='An Episode', guide_id='t123',
                             URL='http://opml.radiotime.com/Tune.ashx?id=t123&sid=p17', topic_duration='1800', item='topic')])]
            return [dict(element='audio', url='https://podcast.test/123.mp3', media_type='mp3')]
        catalog, calls = self.make(handler)
        choices = catalog.children('show:p17')
        episodes = catalog.children(next(item.id for item in choices if item.title == 'Recent Episodes'))
        self.assertEqual(episodes[0].duration, 1800)
        self.assertEqual(episodes[0].kind, 'episode')
        self.assertFalse(episodes[0].container)
        self.assertEqual(catalog.resolve('episode:t123', ['mp3'])[0].url, 'https://podcast.test/123.mp3')
        self.assertEqual(calls[-1][1]['sid'], 'p17')

    def test_metadata_merges_raw_fields_profile_and_art(self):
        def handler(path, query):
            if path.endswith('Describe.ashx'):
                return [dict(element='station', guide_id='s1', name='One', current_song='Song', current_artist='Artist',
                             current_album_art=None, logo='canonical.jpg', location='Boston', language='English')]
            return [station(1, playing='Artist - Song', playing_image='album.jpg', current_track='Program', show_id='p17')]
        catalog, _ = self.make(handler)
        catalog.store.update_settings({'station_order': 'provider'})
        folder = catalog._folder('Browse.ashx?id=g11', 'Jazz')
        catalog.children(folder.id)
        item = catalog.metadata('station:s1')
        self.assertEqual(item.image, 'album.jpg')
        self.assertEqual(item.metadata['show_id'], 'p17')
        self.assertEqual(item.metadata['station_image'], 'canonical.jpg')
        self.assertEqual(item.metadata['current_song'], 'Song')

    def test_zip_saved_location_wins_and_customs_need_no_network(self):
        def handler(path, query):
            self.assertEqual(query['latlon'], '42.0,-71.0')
            return [dict(text='FM', children=[station(2, '99.5 | Second'), station(1, '88.5 | First')])]
        catalog, calls = self.make(handler)
        catalog.zip_resolver = lambda zipcode: dict(label='Boston', zip=zipcode, latlon='42,-71')
        catalog.set_zip('02110')
        fm = catalog.children('local')[0]
        self.assertEqual([item.id for item in catalog.children(fm.id)], ['station:s1', 'station:s2'])
        custom = catalog.store.add_custom('Custom', 'https://radio.test/live.mp3')
        before = len(calls)
        self.assertEqual(catalog.resolve(custom['id'], ['mp3'])[0].url, custom['url'])
        self.assertEqual(len(calls), before)

    def test_settings_browse_does_not_apply_and_refresh_cross_instance_invalidates(self):
        catalog, _ = self.make(lambda *args: [])
        before = catalog.store.snapshot()
        catalog.children('settings')
        choices = catalog.children('settings:quality')
        self.assertEqual(catalog.store.snapshot(), before)
        catalog.apply_action(next(item.id for item in choices if item.metadata['value'] == 'low'))
        self.assertEqual(catalog.store.settings()['quality'], 'low')
        with self.assertRaises(ValueError):
            catalog.apply_setting('made_up', True)

    def test_station_information_reaches_schedule_and_read_only_nowplaying(self):
        def handler(path, query):
            if path.endswith('Describe.ashx') and query.get('c') != 'nowplaying':
                return [dict(element='station', guide_id='s1', name='Station', has_schedule=True)]
            if path.endswith('Options.ashx'):
                return [dict(link('Schedule', c='schedule', id='s1'), key='schedule'),
                        dict(type='link', text='Now playing', key='nowplaying',
                             URL='http://opml.radiotime.com/Describe.ashx?c=nowplaying&id=s1')]
            if query.get('c') == 'nowplaying':
                return [dict(type='text', text='Station', guide_id='s1'), dict(type='text', text='Current Show')]
            if query.get('c') == 'schedule':
                return [dict(type='link', text='Fresh Air', guide_id='p17', start_utc='2026-03-08T06:30:00',
                             URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p17'),
                        dict(type='link', text='Fresh Air', guide_id='p17', start_utc='2026-03-08T07:30:00',
                             URL='http://opml.radiotime.com/Tune.ashx?c=pbrowse&id=p17')]
            return [dict(type='link', text='Listen now', key='liveShow',
                         URL='http://opml.radiotime.com/Tune.ashx?c=sbrowse&id=p17')]
        catalog, _ = self.make(handler)
        catalog.timezone = lambda: 'America/New_York'
        catalog.store.add_favorite(dict(id='station:s1', title='Station', kind='station'))
        self.assertIn('station-info', [item.id for item in catalog.children('settings')])
        station_info = catalog.children('station-info')[0]
        options = catalog.children(station_info.id)
        schedule = catalog.children(next(item.id for item in options if item.title == 'Schedule'))
        self.assertEqual(len(schedule), 2)  # The same show can air twice in a day.
        self.assertNotEqual(schedule[0].id, schedule[1].id)
        self.assertIn('01:30 EST', schedule[0].title)
        self.assertIn('03:30 EDT', schedule[1].title)
        self.assertEqual(catalog.children(schedule[0].id)[0].title, 'Listen now')
        current = catalog.children(next(item.id for item in options if item.title == 'Now playing'))
        self.assertEqual([item.kind for item in current], ['message', 'message'])
        self.assertFalse(any(item.playable for item in current))

    def test_absent_schedules_remain_empty(self):
        catalog, _ = self.make(lambda *args: [])
        self.assertEqual(catalog.schedule('station:s1'), [])

    def test_shared_refresh_invalidates_upstream_ttl_and_native_refresh_is_shared(self):
        current = [1]
        def handler(path, query):
            if path.endswith('Describe.ashx'):
                return [dict(element='station', guide_id='s1', name='Name %d' % current[0])]
            return [station(current[0])]
        catalog, calls = self.make(handler)
        folder = catalog._folder('Browse.ashx?id=g11', 'Genre')
        catalog.store.update_settings({'station_order': 'provider'})
        self.assertEqual(catalog.children(folder.id)[0].id, 'station:s1')
        self.assertEqual(catalog.metadata('station:s1').title, 'Name 1')
        current[0] = 2
        # Simulates the counter the independently running web store changes.
        catalog.store.refresh()
        self.assertEqual(catalog.children(folder.id)[0].id, 'station:s2')
        self.assertEqual(catalog.metadata('station:s1').title, 'Name 2')
        before = catalog.store.snapshot()['radio_directory_update_id']
        catalog.apply_setting('refresh')
        self.assertEqual(catalog.store.snapshot()['radio_directory_update_id'], before + 1)

    def test_language_filter_is_validated_before_provider_can_silently_ignore_it(self):
        def handler(path, query):
            if query.get('c') == 'lang':
                return [dict(link('English', c='lang', filter='l203'), guide_id='l203')]
            self.assertEqual(query['filter'], 'l203')
            return [link('Jazz', id='g11', filter='l203')]
        catalog, _ = self.make(handler)
        catalog.apply_setting('preferred_language', 'l203')
        catalog.apply_setting('language_filter', True)
        self.assertEqual(catalog.children('music')[0].title, 'Jazz')
        before = catalog.store.snapshot()
        with self.assertRaises(ValueError):
            catalog.apply_setting('preferred_language', 'l99999')
        self.assertEqual(catalog.store.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
