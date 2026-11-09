"""Construct configured music services without coupling the player to providers.

Factories take a settings dictionary and ServiceContext, and return an unstarted
MusicService. Add a factory to PROVIDERS to support another catalogue; speaker
playback adapters are registered separately in provider_playback.

The explicit music_services list replaces the legacy service selection when
present. Spotify retains precedence over the diagnostic stub. The legacy
Speaker Settings catalogue is opt-in for unmodified controller firmware;
custom firmware uses the native Room Settings menu instead.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Protocol

from music_services import MusicServices


class MusicService(Protocol):
    def descriptor(self) -> dict: ...
    def media_servers(self) -> str: ...
    def owns(self, uri: str, metadata: str = '') -> bool: ...
    def start(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ServiceContext:
    config_path: Path
    listen_ip: str
    log: Callable
    rooms: Callable

    def path(self, value, default):
        path = Path(value or default).expanduser()
        return path if path.is_absolute() else self.config_path.resolve().parent / path


def spotify(settings, context):
    from spotify_client import SpotifyClient
    from spotify_service import SpotifyService
    from spotify_folders import make_folder_provider
    from spotify_autoplay import SpotifyAutoplay
    autoplay = settings.get('autoplay', False)
    if not isinstance(autoplay, bool):
        raise ValueError('Spotify Autoplay must be a boolean')
    client = SpotifyClient(settings.get('client_id', ''), context.path(settings.get('token_file'), 'spotify-token.json'),
                           log=context.log)
    folders = make_folder_provider(context.config_path, settings, client, log=context.log)
    policy = None
    try:
        def enabled():
            # Autoplay-only saves do not restart the virtual players. Read the
            # persisted provider setting so a live change touches no transport.
            try:
                config = json.loads(context.config_path.read_text())
                current = provider_settings(config, 'spotify')
                return current.get('autoplay', False) is True
            except (OSError, ValueError, TypeError, AttributeError):
                return False
        policy = SpotifyAutoplay(client, folders, enabled, log=context.log, rooms=context.rooms)
        return SpotifyService(settings.get('host', context.listen_ip), settings.get('port', 8400), client,
                              service_id=settings.get('id', 240), name=settings.get('name', 'Spotify'),
                              log=context.log, menu_settings=settings, folder_provider=folders,
                              playback_policy=policy)
    except BaseException:
        if policy is not None:
            try:
                policy.close()
            except Exception:
                pass
        if folders is not None:
            folders.close()
        raise


def stub(settings, context):
    from smapi_stub import SmapiStub
    return SmapiStub(settings.get('host', context.listen_ip), settings.get('port', 8400),
                     settings.get('id', 240), settings.get('name', 'Bridge Test'), context.log,
                     str(settings.get('capabilities', '1')))


def speaker_settings(settings, context):
    from speaker_settings import SpeakerSettingsService
    return SpeakerSettingsService(settings.get('host', context.listen_ip), settings.get('port', 8401),
                                  context.rooms, service_id=settings.get('id', 244),
                                  name=settings.get('name', 'Speaker Settings'), log=context.log)


PROVIDERS: dict[str, Callable[[dict, ServiceContext], MusicService]] = {
    'spotify': spotify, 'stub': stub, 'speaker_settings': speaker_settings,
}
LEGACY_KEYS = {'spotify': 'spotify_music_service', 'stub': 'music_service_stub',
               'speaker_settings': 'speaker_settings'}


def provider_settings(config, provider, *, create=False):
    """Read/edit one provider in either supported configuration format.

    Disabled services are included so the setup UI can enable them again. The
    explicit list has one entry per provider; accounts belong to that service.
    """
    if 'music_services' in config:
        _explicit_specs(config['music_services'])
        for settings in config['music_services']:
            if settings['provider'] == provider:
                return settings
        if create:
            settings = {'provider': provider}
            config['music_services'].append(settings)
            return settings
        return {}
    key = LEGACY_KEYS[provider]
    return config.setdefault(key, {}) if create else config.get(key) or {}


def _explicit_specs(specs):
    if not isinstance(specs, list):
        raise ValueError('music_services must be a list of service configuration objects')
    result, seen = [], set()
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get('provider'), str) or not spec['provider']:
            raise ValueError('Each music service needs a provider name')
        settings = dict(spec)
        provider = settings.pop('provider')
        if provider in seen:
            raise ValueError('Duplicate music service provider: %s' % provider)
        seen.add(provider)
        if settings.get('enabled', True):
            result.append((provider, settings))
    return result


def service_specs(config):
    """Normalize explicit/legacy selection before allocating any HTTP listeners."""
    if 'music_services' in config:
        return _explicit_specs(config['music_services'])

    result = []
    for key in ('spotify_music_service', 'music_service_stub', 'speaker_settings'):
        if config.get(key) is not None and not isinstance(config[key], dict):
            raise ValueError('%s must be a configuration object' % key)
    settings = config.get('spotify_music_service')
    if settings and settings.get('enabled', True):
        result.append(('spotify', settings))
    else:
        settings = config.get('music_service_stub')
        if settings and settings.get('enabled', True):
            result.append(('stub', settings))
    settings = config.get('speaker_settings') or {}
    if settings.get('enabled', False):
        result.append(('speaker_settings', settings))
    return result


def load_services(config, config_path, listen_ip, log=print, rooms=None, *, registry=None):
    registry = PROVIDERS if registry is None else registry
    specs = service_specs(config)
    for name, _ in specs:
        if name not in registry:
            raise ValueError('Unknown music service provider: %s' % name)
    context = ServiceContext(Path(config_path), listen_ip, log, rooms or (lambda: ()))
    services = []
    try:
        for name, settings in specs:
            services.append(registry[name](settings, context))
        if len(services) > 1:
            return MusicServices(services)
        return services[0] if services else None
    except BaseException:
        # Preserve the construction error even if one provider's cleanup fails.
        for service in reversed(services):
            try:
                service.close()
            except Exception:
                pass
        raise
