"""Home Assistant backend for virtual ZonePlayers: one media_player entity per room.

A backends.Room answers the CR100 (UPnP variables, action translation, volume); this
module reads transport, volume and now-playing from the entity's state and turns the
CR100's actions into Home Assistant service calls. The Music menu comes from Home
Assistant's media browser (`ha_content.py`), and album art from the entity's picture,
fetched from Home Assistant.

Config: {"home_assistant": {"url": "http://192.168.1.148:8123", "token_file": "ha-token"}},
rooms {"entity": "media_player.kitchen"} (or {"backend": "ha", "id": ...}).
"""
from __future__ import annotations

import datetime
from pathlib import Path

from backends import ArtStore, Backend, BackendError, Room
from ha_content import Content

STARTUP_WAIT = 15      # s to wait for Home Assistant before naming rooms

# media_player supported_features bits
PAUSE, SEEK, VOLUME_SET, VOLUME_MUTE, PREVIOUS, NEXT, PLAY_MEDIA = 1, 2, 4, 8, 16, 32, 512
STOP, CLEAR_PLAYLIST, PLAY, SHUFFLE_SET, REPEAT_SET, GROUPING = 4096, 8192, 16384, 32768, 262144, 524288

TRANSPORT = {'playing': 'PLAYING', 'paused': 'PAUSED_PLAYBACK', 'buffering': 'TRANSITIONING'}


class HABackend(Backend):
    """One Home Assistant connection (an ha_client.HomeAssistant); rooms are entity_ids."""
    name = 'ha'

    def __init__(self, ha, log=print):
        super().__init__(log)
        self.ha = ha
        self.art = ArtStore(self, log)
        self.content = Content(ha, self.art, log)
        ha.listeners.append(lambda entity_id, state: self.changed(entity_id))

    def room(self, entity_id):
        return HARoom(self, entity_id, self.log)

    def display_name(self, entity_id):
        return self.ha.state(entity_id).get('attributes', {}).get('friendly_name') \
            or entity_id.split('.', 1)[-1].replace('_', ' ').title()

    def fetch(self, picture):
        return self.ha.fetch(picture)

    def close(self):
        self.ha.close()


def load(settings, config_dir, log):
    from ha_client import HomeAssistant
    token_file = Path(settings['token_file']).expanduser()
    if not token_file.is_absolute(): token_file = Path(config_dir) / token_file
    ha = HomeAssistant(settings['url'], token_file.read_text().strip(), log)
    ha.start()
    if not ha.connected.wait(STARTUP_WAIT):
        log({'event': 'ha-not-connected', 'detail': 'starting anyway; rooms update when it connects'})
    return HABackend(ha, log)


class HARoom(Room):
    """One virtual room backed by one Home Assistant media_player entity."""

    def __init__(self, backend, entity_id, log=print):
        super().__init__(backend, entity_id, log)
        self.ha, self.entity_id = backend.ha, entity_id

    # --- state ------------------------------------------------------------------
    @property
    def state(self):
        return self.ha.state(self.entity_id)

    @property
    def attributes(self):
        return self.state.get('attributes') or {}

    @property
    def available(self):
        return self.state.get('state') not in (None, 'unavailable', 'unknown')

    def features(self):
        return int(self.attributes.get('supported_features') or 0)

    @property
    def reported_volume(self):
        level = self.attributes.get('volume_level')
        return int(round(level * 100)) if level is not None else 0

    @property
    def reported_mute(self):
        return bool(self.attributes.get('is_volume_muted'))

    @property
    def transport(self):
        return TRANSPORT.get(self.state.get('state'), 'STOPPED')

    def shuffle_repeat(self):
        attributes = self.attributes
        return bool(attributes.get('shuffle')), attributes.get('repeat') or 'off'

    def group_members(self):
        return list(self.attributes.get('group_members') or [self.entity_id])

    def position(self):
        attributes = self.attributes
        position = attributes.get('media_position')
        if position is None: return 0
        updated = attributes.get('media_position_updated_at')
        if self.state.get('state') == 'playing' and updated:
            try:
                then = datetime.datetime.fromisoformat(str(updated).replace('Z', '+00:00'))
                position += (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
            except ValueError:
                pass
        duration = attributes.get('media_duration')
        return min(position, duration) if duration else position

    def track(self):
        attributes = self.attributes
        if not self.available or self.state.get('state') in ('off', 'idle', 'standby'): return None
        channel = attributes.get('media_channel') or ''
        source = attributes.get('source') or attributes.get('app_name') or ''
        title = attributes.get('media_title') or channel or source
        if not title: return None
        artist = attributes.get('media_artist') or attributes.get('media_album_artist') or \
            (channel if channel != title else '')
        return (title, artist, attributes.get('media_album_name') or '',
                self.backend.art.path_for(attributes.get('entity_picture')), attributes.get('media_duration'))

    def queue_numbers(self):
        attributes = self.attributes
        if self.track() is None: return 0, 0
        position, size = attributes.get('queue_position'), attributes.get('queue_size')
        if position and size: return int(position), int(size)
        return 1, 1

    def queue_token(self):
        return self.attributes.get('queue_size')

    def capabilities(self):
        features = self.features()
        return frozenset(name for name, bit in (('seek', SEEK), ('next', NEXT), ('previous', PREVIOUS))
                         if features & bit)

    # --- Home Assistant calls -----------------------------------------------------
    def ha_call(self, service, data=None, feature=None):
        if feature and not self.features() & feature: raise BackendError('%s not supported' % service)
        try:
            self.ha.call_service('media_player', service, self.entity_id, data)
        except Exception as error:
            self.log({'event': 'ha-call-failed', 'entity': self.entity_id, 'service': service, 'error': str(error)})
            raise BackendError(str(error)) from error

    def sonos_call(self, service, data=None, response=False):
        try:
            return self.ha.call_service('sonos', service, self.entity_id, data, response=response)
        except Exception as error:
            self.log({'event': 'ha-call-failed', 'entity': self.entity_id, 'service': 'sonos.' + service,
                      'error': str(error)})
            raise BackendError(str(error)) from error

    def queue_length(self):
        response = self.sonos_call('get_queue', response=True) or {}
        return len(response.get(self.entity_id) or next(iter(response.values()), None) or [])

    def play_uri(self, uri, media_type, enqueue=None):
        """Hand a resolved provider URI to Sonos; Sonos owns the audio and queue."""
        data = {'media_content_id': uri, 'media_content_type': media_type}
        if enqueue: data['enqueue'] = enqueue
        self.ha_call('play_media', data, feature=PLAY_MEDIA)

    # --- actions ----------------------------------------------------------------
    def play(self): self.ha_call('media_play')
    def pause(self): self.ha_call('media_pause')
    def stop(self): self.ha_call('media_stop' if self.features() & STOP else 'media_pause')
    def next(self): self.ha_call('media_next_track', feature=NEXT)
    def previous(self): self.ha_call('media_previous_track', feature=PREVIOUS)

    def seek(self, seconds):
        self.ha_call('media_seek', {'seek_position': seconds}, feature=SEEK)

    def play_track(self, number):
        self.sonos_call('play_queue', {'queue_position': max(0, number - 1)})

    def remove_track(self, number):
        self.sonos_call('remove_from_queue', {'queue_position': number - 1})

    def clear_queue(self):
        self.ha_call('clear_playlist', feature=CLEAR_PLAYLIST)

    def set_play_mode(self, shuffle, repeat):
        if self.features() & SHUFFLE_SET: self.ha_call('shuffle_set', {'shuffle': shuffle})
        if self.features() & REPEAT_SET: self.ha_call('repeat_set', {'repeat': repeat})

    def play_item(self, node):
        self.play_uri(node.ref, node.kind)

    def enqueue_item(self, node, as_next=False):
        """Home Assistant adds it (after the current track for 'as next')."""
        before, current = self.queue_length(), int(self.attributes.get('queue_position') or 0)
        if node.kind == 'favorite_item_id':   # Home Assistant always plays these at once
            self.play_item(node); first = 1
        else:
            self.play_uri(node.ref, node.kind, 'next' if as_next else 'add')
            first = current + 1 if as_next and current else before + 1
        self.queue_edited()
        after = self.queue_length()
        return {'FirstTrackNumberEnqueued': str(first), 'NumTracksAdded': str(max(0, after - before) or 1),
                'NewQueueLength': str(after)}

    def play_provider(self, uri, kind):
        self.play_uri(uri, kind, 'replace')
        self.queue_edited()

    def enqueue_provider(self, uri, kind, as_next=False):
        before = self.queue_length()
        current = int(self.attributes.get('queue_position') or 0)
        self.play_uri(uri, kind, 'next' if as_next else 'add')
        self.queue_edited()
        after = self.queue_length()
        # Sonos expands albums/playlists, so use the actual resulting queue length.
        return {'FirstTrackNumberEnqueued': str(current + 1 if as_next and current else before + 1),
                'NumTracksAdded': str(max(0, after - before)), 'NewQueueLength': str(after)}

    def send_volume(self, value):
        self.ha_call('volume_set', {'volume_level': value / 100})

    def send_mute(self, value):
        self.ha_call('volume_mute', {'is_volume_muted': value})

    def join(self, member):
        self.ha_call('join', {'group_members': [member.entity_id]})

    def unjoin(self):
        self.ha_call('unjoin')
