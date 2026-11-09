"""Provider extension, legacy configuration and construction failure boundaries."""
from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from music_service_registry import load_services, provider_settings, service_specs
from music_services import MusicServices


class Service:
    def __init__(self, ident, name):
        self.ident, self.name, self.closed = ident, name, False

    def descriptor(self):
        uri = 'http://127.0.0.1:%d/smapi' % (8000 + self.ident)
        return {'AvailableServiceDescriptorList':
                '<Services><Service Id="%d" Name="%s" Uri="%s" SecureUri="%s"/></Services>'
                % (self.ident, self.name, uri, uri),
                'AvailableServiceTypeList': str(self.ident * 256 + 7), 'AvailableServiceListVersion': '1'}

    def media_servers(self):
        return '<MediaServers><Service UDN="service-%d"/></MediaServers>' % self.ident

    def owns(self, uri, metadata=''):
        return uri.startswith(self.name + ':')

    def resolve_playback(self, uri, metadata=''):
        return uri, 'music'

    def start(self):
        pass

    def close(self):
        self.closed = True


class Registry(unittest.TestCase):
    def test_defaults_omit_redundant_settings_and_keep_spotify_stub_precedence(self):
        spotify, stub = {'name': 'Spotify'}, {'name': 'Stub'}
        self.assertEqual(service_specs({}), [])
        self.assertEqual(service_specs({'spotify_music_service': spotify, 'music_service_stub': stub}),
                         [('spotify', spotify)])
        spotify['enabled'] = False
        self.assertEqual(service_specs({'spotify_music_service': spotify, 'music_service_stub': stub,
                                       'speaker_settings': {'enabled': False}}), [('stub', stub)])

    def test_stock_settings_service_requires_explicit_opt_in(self):
        for settings in ({}, {'host': '127.0.0.1', 'port': 0}, {'enabled': False}):
            with self.subTest(settings=settings):
                self.assertEqual(service_specs({'speaker_settings': settings}), [])
        settings = {'enabled': True, 'port': 8401}
        self.assertEqual(service_specs({'speaker_settings': settings}), [('speaker_settings', settings)])
        self.assertEqual(service_specs({'music_services': [{'provider': 'speaker_settings'}]}),
                         [('speaker_settings', {})])

    def test_defaults_do_not_construct_or_open_legacy_settings_service(self):
        legacy = Mock()
        self.assertIsNone(load_services({}, '/tmp/config.json', '127.0.0.1',
                                       registry={'speaker_settings': legacy}))
        legacy.assert_not_called()
        spotify = Mock(return_value=Service(240, 'spotify'))
        loaded = load_services({'spotify_music_service': {'enabled': True}},
                               '/tmp/config.json', '127.0.0.1',
                               registry={'speaker_settings': legacy, 'spotify': spotify})
        self.addCleanup(loaded.close)
        self.assertEqual(loaded.name, 'spotify')
        self.assertNotIn('Speaker Settings', loaded.descriptor()['AvailableServiceDescriptorList'])
        self.assertNotIn('62471', loaded.media_servers())
        legacy.assert_not_called()

    def test_another_provider_registers_without_changes_to_player_loader(self):
        first, second = Service(240, 'spotify'), Service(245, 'example')
        spotify, example = Mock(return_value=first), Mock(return_value=second)
        rooms = lambda: ['room']
        config = {'music_services': [{'provider': 'spotify', 'name': 'Spotify'},
                                     {'provider': 'example', 'token_file': 'tokens/example.json'}]}
        services = load_services(config, '/tmp/bridge/config.json', '127.0.0.1', rooms=rooms,
                                 registry={'spotify': spotify, 'example': example})
        self.addCleanup(services.close)
        self.assertIsInstance(services, MusicServices)
        self.assertEqual(services.services, [first, second])
        self.assertEqual(services.resolve_playback('example:track:123'), ('example:track:123', 'music'))
        settings, context = example.call_args.args
        self.assertEqual(settings, {'token_file': 'tokens/example.json'})
        self.assertEqual(context.path(settings['token_file'], ''), Path('/tmp/bridge').resolve() / 'tokens/example.json')
        self.assertIs(context.rooms, rooms)
        self.assertEqual(context.listen_ip, '127.0.0.1')
        self.assertNotIn('provider', spotify.call_args.args[0])

    def test_explicit_empty_or_disabled_list_does_not_activate_legacy_services(self):
        for entries in ([], [{'provider': 'example', 'enabled': False}]):
            factory = Mock()
            config = {'music_services': entries, 'spotify_music_service': {'enabled': True}}
            self.assertIsNone(load_services(config, '/tmp/config.json', '127.0.0.1', registry={'example': factory}))
            factory.assert_not_called()

    def test_unknown_provider_and_malformed_entries_fail_before_any_factory_runs(self):
        malformed = [None, {}, [None], [{}], [{'provider': ''}],
                     [{'provider': 'example'}, {'provider': 'example'}],
                     [{'provider': 'example'}, {'provider': 'missing'}]]
        for entries in malformed:
            with self.subTest(entries=entries):
                factory = Mock()
                with self.assertRaises(ValueError):
                    load_services({'music_services': entries}, '/tmp/config.json', '127.0.0.1',
                                  registry={'example': factory})
                factory.assert_not_called()

    def test_factory_failure_closes_previously_constructed_services_and_preserves_error(self):
        first = Service(240, 'first')
        close = Mock(side_effect=RuntimeError('cleanup failed'))
        first.close = close
        failure = ValueError('provider construction failed')
        config = {'music_services': [{'provider': 'first'}, {'provider': 'second'}]}
        with self.assertRaises(ValueError) as raised:
            load_services(config, '/tmp/config.json', '127.0.0.1',
                          registry={'first': Mock(return_value=first), 'second': Mock(side_effect=failure)})
        self.assertIs(raised.exception, failure)
        close.assert_called_once_with()

    def test_setup_settings_edit_disabled_explicit_service_in_place(self):
        settings = {'provider': 'spotify', 'enabled': False}
        config = {'music_services': [settings]}
        self.assertIs(provider_settings(config, 'spotify'), settings)
        provider_settings(config, 'spotify', create=True)['enabled'] = True
        self.assertEqual(len(config['music_services']), 1)
        self.assertNotIn('spotify_music_service', config)
        self.assertEqual(provider_settings(config, 'speaker_settings'), {})
        added = provider_settings(config, 'speaker_settings', create=True)
        self.assertIs(added, config['music_services'][1])

    def test_setup_settings_preserve_legacy_format(self):
        config = {}
        self.assertEqual(provider_settings(config, 'spotify'), {})
        self.assertEqual(config, {})
        settings = provider_settings(config, 'spotify', create=True)
        settings['enabled'] = True
        self.assertEqual(config, {'spotify_music_service': {'enabled': True}})

    def test_spotify_policy_reads_live_flag_in_both_configuration_formats(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'bridge.json'
                settings = {'client_id': '', 'autoplay': False}
                config = {'music_services': [{'provider': 'spotify', **settings}]} if explicit else {
                    'spotify_music_service': settings, 'speaker_settings': {'enabled': False}}
                path.write_text(json.dumps(config))
                policy, folders = Mock(), Mock()
                constructor = Mock(return_value=policy)
                module = types.ModuleType('spotify_autoplay')
                module.SpotifyAutoplay = constructor
                service = Service(240, 'spotify')
                rooms = lambda: ['room']
                with patch.dict(sys.modules, {'spotify_autoplay': module}), \
                        patch('spotify_client.SpotifyClient') as client, \
                        patch('spotify_folders.make_folder_provider', return_value=folders), \
                        patch('spotify_service.SpotifyService', return_value=service) as factory:
                    self.assertIs(load_services(config, path, '127.0.0.1', rooms=rooms), service)
                enabled = constructor.call_args.args[2]
                self.assertFalse(enabled())
                self.assertIs(constructor.call_args.args[0], client.return_value)
                self.assertIs(constructor.call_args.args[1], folders)
                self.assertIs(constructor.call_args.kwargs['rooms'], rooms)
                self.assertIs(factory.call_args.kwargs['playback_policy'], policy)
                provider_settings(config, 'spotify')['autoplay'] = True
                path.write_text(json.dumps(config))
                self.assertTrue(enabled())
                provider_settings(config, 'spotify')['autoplay'] = 'true'
                path.write_text(json.dumps(config))
                self.assertFalse(enabled())
                path.write_text('incomplete concurrent configuration')
                self.assertFalse(enabled())

    def test_invalid_initial_autoplay_flag_fails_before_allocating_provider_resources(self):
        module = types.ModuleType('spotify_autoplay')
        module.SpotifyAutoplay = Mock()
        with patch.dict(sys.modules, {'spotify_autoplay': module}), \
                patch('spotify_client.SpotifyClient') as client:
            with self.assertRaisesRegex(ValueError, 'Autoplay must be a boolean'):
                load_services({'spotify_music_service': {'autoplay': 'false'},
                               'speaker_settings': {'enabled': False}}, '/tmp/config.json', '127.0.0.1')
        client.assert_not_called()
        module.SpotifyAutoplay.assert_not_called()


if __name__ == '__main__':
    unittest.main()
