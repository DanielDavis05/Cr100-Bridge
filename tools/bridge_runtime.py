"""Compose bridge-owned playback (bridge_player.py) for a virtual household.

Config (all optional; shown with defaults):
  "bridge_playback": {
    "enabled": true,
    "relay": true,                 # stream to speakers that only play URLs
    "stream_port": 8404,           # relay HTTP port on the LAN
    "stream_host": null,           # LAN address speakers use (default: discovered)
    "connect": {"media_player.kitchen": "http://10.0.0.20:8200/zc"}   # or false to never use Connect
  }
Spotify Connect receivers are otherwise discovered on the LAN by mDNS and matched
to rooms by the speaker's name.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from bridge_player import BridgePlayback, spotify_connect_discovery
from media_sources import MediaSources

DEVICE_ID = hashlib.sha1(b'cr100-bridge-queue-control').hexdigest()


def _spotify(music_service):
    for service in getattr(music_service, 'services', [music_service]) if music_service else ():
        if getattr(service, 'folder_provider', None) is not None and getattr(service, 'client', None) is not None:
            return service
    return None


def configure(house, backends, config_path, listen_ip, *, streamer=None, browse=None):
    """Attach a BridgePlayback to every backend that accepts one; returns it or None."""
    targets = [backend for backend in backends if hasattr(backend, 'attach_bridge_playback')]
    if not targets: return None
    config_path = Path(config_path)
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    settings = config.get('bridge_playback') or {}
    if settings.get('enabled', True) is False: return None
    sources, connect, autoplay = MediaSources(), None, None
    spotify = _spotify(house.music_service)
    if spotify is not None:
        from spotify_connect import SpotifyConnect
        from spotify_media import SpotifySource
        connect = SpotifyConnect(spotify.client, spotify.folder_provider, log=house.log, device_id=DEVICE_ID)
        sources.register(SpotifySource(spotify.client, connect, log=house.log))
        # The provider's Autoplay setting also lets Connect playback continue past the queue.
        autoplay = getattr(getattr(spotify, 'playback_policy', None), 'is_enabled', None)
    if not sources: return None
    if streamer is None and settings.get('relay', True):
        from radio_runtime import renderer_stream_host
        from relay_stream import RelayStreamer
        streamer = RelayStreamer(renderer_stream_host(house, settings, listen_ip),
                                 host=settings.get('stream_bind', '0.0.0.0'),
                                 port=int(settings.get('stream_port', 8404)), log=house.log)
    if browse is None:
        from zeroconf_browse import browse
    connect_urls = settings.get('connect') or {}
    if not isinstance(connect_urls, dict): raise ValueError('bridge_playback.connect must map rooms to URLs')
    service = BridgePlayback(sources, connect=connect, streamer=streamer, connect_urls=connect_urls,
                             discover=spotify_connect_discovery(browse, connect) if connect else None,
                             relay=settings.get('relay', True) is not False, autoplay=autoplay,
                             state_file=config_path.parent / settings.get('state_file', 'bridge-queues.json'),
                             log=house.log)
    for backend in targets: backend.attach_bridge_playback(service)
    house.bridge_playback = service
    if connect is not None:
        # Sign in to Spotify playback now: done on the first Play it delays it by ~9 s on a Pi.
        def warm():
            try:
                if connect.available(): connect.prepare()
            except Exception as error:
                house.log({'event': 'bridge-playback-warmup-failed', 'error': str(error)})
        threading.Thread(target=warm, name='bridge-playback-warmup', daemon=True).start()
    return service
