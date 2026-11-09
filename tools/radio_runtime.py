"""Compose native Radio, playback/art and global alarms for a virtual household."""
from __future__ import annotations

import json
from pathlib import Path

from music_services import MusicServices
from native_radio import NativeRadioService
from radio_catalog import RadioCatalog
from radio_management import state_paths
from radio_playback import RadioPlayback
from radio_store import RadioStore
from radiotime import RadioTimeClient


def renderer_stream_host(house, settings, listen_ip):
    """Resolve the current LAN address when preparing a renderer's proxy URL.

    Household construction initially has only the controller/TAP address; the
    first upstream identity refresh (or a later DHCP change) supplies the LAN IP.
    A captured startup fallback must never become a permanent renderer URL.
    """
    configured = settings.get('stream_host')
    def current():
        if configured: return configured
        host = getattr(house, 'bridge_ip', '')
        if not host or host.startswith('192.168.69.'):
            refresh = getattr(house, 'refresh_identities', None)
            if callable(refresh): refresh(refresh_ip=True)
            host = getattr(house, 'bridge_ip', '')
        if not host or host in ('0.0.0.0', '::') or host.startswith('192.168.69.'):
            from radio_transcode import RadioTranscodeError
            raise RadioTranscodeError('Radio conversion needs a speaker-reachable LAN address; configure radio.stream_host')
        return host
    return current


def configure_sources(house, config_path, listen_ip, *, client=None, native_port=None):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    settings = config.get('radio') or {}
    def publish(services=('ContentDirectory', 'SystemProperties')):
        for player in house.players:
            if getattr(player, 'http_thread', None):
                for service in services: player.publish_changed(service)
    store = RadioStore(state_paths(config_path)[0], on_change=lambda event: publish())
    catalog = RadioCatalog(client or RadioTimeClient(), store, log=house.log)
    catalog.timezone = lambda: house.alarms.timezone if house.alarms else (config.get('alarms') or {}).get('timezone', 'UTC')
    house.radio_store, house.radio_catalog = store, catalog
    from radio_transcode import RadioTranscoder
    transcoder = RadioTranscoder(renderer_stream_host(house, settings, listen_ip),
                                 host=settings.get('stream_bind', '0.0.0.0'),
                                 port=int(settings.get('stream_port', 0 if native_port == 0 else 8403)))
    house.radio_transcoder = transcoder
    house.radio_playback = RadioPlayback(house, catalog, store, log=house.log, transcoder=transcoder)
    # No selected/discovered rooms is normal during first setup and temporary
    # outages. The web catalog/settings still work; a player reload adds Radio
    # discovery once rooms are available, without indexing players[0] here.
    if settings.get('enabled', True) and house.players:
        service = NativeRadioService(settings.get('host') or listen_ip,
                                     int(native_port if native_port is not None else settings.get('port', 8402)),
                                     catalog, art=house.radio_playback.art, log=house.log, on_change=publish)
        house.radio_service = service
        previous = house.music_service
        services = list(getattr(previous, 'services', [previous])) if previous else []
        house.music_service = MusicServices(services + [service])
        house.playback_policies = __import__('zoneplayers').music_service_playback_policies(house.music_service)
        from music_service_events import encode_media_servers
        house.music_service_accounts = encode_media_servers(house.music_service.media_servers(),
                                                           house.players[0].config.household)
    if (config.get('alarms') or {}).get('enabled', True):
        from alarm_runtime import attach_alarms
        attach_alarms(house, config_path, catalog, store)
    house.radio_revision = store.snapshot()['revision']
    return catalog

