"""Home Assistant backend for virtual ZonePlayers: one media_player entity per room.

A backends.Room answers the CR100 (UPnP variables, action translation, volume); this
module reads transport, volume and now-playing from the entity's state and turns the
CR100's actions into Home Assistant service calls. The Music menu comes from Home
Assistant's media browser (`ha_content.py`), and album art from the entity's picture,
fetched from Home Assistant.

Config: {"home_assistant": {"url": "http://homeassistant.local:8123", "token_file": "ha-token"}},
rooms {"entity": "media_player.kitchen"} (or {"backend": "ha", "id": ...}).
"""
from __future__ import annotations

import datetime
from backends import ArtStore, Backend, BackendError, NativeSettingsRoom, track_revision
from ha_content import Content
from ha_linein import SonosLineIn, source_uuid
from room_capabilities import RoomBinding
import sonos_settings

STARTUP_WAIT = 15      # s to wait for Home Assistant before naming rooms

# media_player supported_features bits
PAUSE, SEEK, VOLUME_SET, VOLUME_MUTE, PREVIOUS, NEXT, PLAY_MEDIA = 1, 2, 4, 8, 16, 32, 512
STOP, CLEAR_PLAYLIST, PLAY, SHUFFLE_SET, REPEAT_SET, GROUPING = 4096, 8192, 16384, 32768, 262144, 524288
BROWSE_MEDIA, MEDIA_ENQUEUE, TURN_ON = 131072, 2097152, 128
# Queue and music-service routes a bridge-owned queue provides for non-Sonos players.
BRIDGE_QUEUE_OPERATIONS = ('queue_browse', 'queue_remove', 'queue_clear', 'queue_play', 'queue_use',
                           'provider_play', 'provider_enqueue')
BRIDGE_TRANSPORT = ('play', 'pause', 'stop', 'seek', 'next', 'previous', 'shuffle', 'repeat')

TRANSPORT = {'playing': 'PLAYING', 'paused': 'PAUSED_PLAYBACK', 'buffering': 'TRANSITIONING'}


class HABackend(Backend):
    """One Home Assistant connection (an ha_client.HomeAssistant); rooms are entity_ids."""
    name = 'ha'

    def __init__(self, ha, log=print, line_in=None, settings=None):
        super().__init__(log)
        self.ha = ha
        self.art = ArtStore(self, log)
        self.content = Content(ha, self.art, log, binding=self.binding)
        self.line_in = line_in or SonosLineIn(ha, self.line_in_changed, log)
        self.line_in.start()
        self.settings = settings if settings is not None else sonos_settings.SettingsProvider(
            self.settings_binding, self.settings_changed, coordinator_reader=self.settings_coordinator, log=log)
        self.settings.start()
        self._ha_listener = lambda entity_id, state: self.changed(entity_id)
        ha.listeners.append(self._ha_listener)
        self.bridge = None
        self.content.queue_provider = self.bridge_queue

    def attach_bridge_playback(self, service):
        """Give non-Sonos rooms a bridge-owned queue (bridge_player.BridgePlayback)."""
        self.bridge = service

    def bridge_player(self, entity_id, room=None):
        if self.bridge is None or self.binding(entity_id).is_sonos: return None
        speaker = HASpeaker(room or HARoom(self, entity_id, self.log))
        return self.bridge.player(entity_id, speaker, on_change=lambda: self.changed(entity_id))

    def bridge_queue(self, entity_id):
        """The bridge queue's entries for Q:0, or None when the room uses its own queue."""
        player = self.bridge_player(entity_id)
        return player.queue.snapshot() if player is not None else None

    def room(self, entity_id):
        self.settings.register(entity_id)
        return HARoom(self, entity_id, self.log)

    def settings_binding(self, entity_id):
        """Native extras require a live HA connection and a validated cached endpoint."""
        connected = getattr(self.ha, 'connected', None)
        if connected is None or not connected.is_set() \
                or self.ha.state(entity_id).get('state') in (None, 'unavailable', 'unknown'):
            return RoomBinding()
        return self.binding(entity_id)

    def settings_coordinator(self, entity_id):
        if not self.settings_binding(entity_id).native_sonos: return None
        members = (self.ha.state(entity_id).get('attributes') or {}).get('group_members')
        if not isinstance(members, list) or not members or entity_id not in members \
                or not all(isinstance(member, str) for member in members):
            return None
        coordinator = members[0]
        return coordinator if self.settings_binding(coordinator).native_sonos else None

    def settings_changed(self, entity_id):
        values = self.settings.values(entity_id)
        update = {key: values[key] for key in ('AudioInputName', 'Icon') if key in values}
        binding = self.binding(entity_id)
        on_audio = getattr(self.line_in, 'on_audio', None)
        if update and binding.native_sonos and on_audio: on_audio(binding.native_id, update)
        self.changed(entity_id)

    def display_name(self, entity_id):
        return self.ha.state(entity_id).get('attributes', {}).get('friendly_name') \
            or entity_id.split('.', 1)[-1].replace('_', ' ').title()

    def fetch(self, picture):
        return self.ha.fetch(picture)

    def line_in_sources(self):
        return self.line_in.sources()

    def identity(self, entity_id):
        return self.line_in.identity(entity_id)

    def binding(self, entity_id):
        reader = getattr(self.line_in, 'binding', None)
        return reader(entity_id) if reader else RoomBinding()

    def bonded_devices(self, entity_id):
        return self.line_in.bonded_devices(entity_id)

    def line_in_changed(self):
        # Input names and cable changes affect the menu and every receiving room.
        for entity in list(self.ha.states):
            if entity.startswith('media_player.'): self.changed(entity)

    def close(self):
        self.settings.close()
        self.line_in.close()
        if self._ha_listener in self.ha.listeners: self.ha.listeners.remove(self._ha_listener)
        if getattr(self.ha, 'ha_hub', None) is None: self.ha.close()


def load(settings, config_dir, log):
    import ha_hub
    if not settings.get('url') or not settings.get('token_file'):
        raise ValueError('Home Assistant rooms need home_assistant.url and token_file in the config')
    hub = ha_hub.configure({'home_assistant': settings}, config_dir, log)
    if hub is None: raise ValueError('Home Assistant token_file must contain a token')
    ha = hub.client
    if not ha.connected.wait(STARTUP_WAIT):
        log({'event': 'ha-not-connected', 'detail': 'starting anyway; rooms update when it connects'})
    return HABackend(ha, log)


class HASpeaker:
    """bridge_player.Speaker for a Home Assistant media_player."""

    def __init__(self, room):
        self.room = room

    @property
    def name(self):
        return self.room.attributes.get('friendly_name') or self.room.backend.display_name(self.room.entity_id)

    def snapshot(self):
        room = self.room
        attributes = room.attributes
        return {'available': room.reachable, 'state': room.state.get('state'),
                'source': attributes.get('source') or attributes.get('app_name') or '',
                'content_id': str(attributes.get('media_content_id') or ''),
                'title': attributes.get('media_title') or '', 'artist': attributes.get('media_artist') or '',
                'album': attributes.get('media_album_name') or '', 'picture': attributes.get('entity_picture') or '',
                'duration': attributes.get('media_duration'), 'position': room.ha_position()}

    def can_play_url(self):
        return bool(self.room.features() & PLAY_MEDIA)

    def play_url(self, url):
        self.room.ha_call('play_media', {'media_content_id': url, 'media_content_type': 'music'}, feature=PLAY_MEDIA)

    def pause(self):
        self.room.ha_call('media_pause', feature=PAUSE)

    def resume(self):
        self.room.ha_call('media_play', feature=PLAY)

    def stop(self):
        if self.room.features() & STOP: self.room.ha_call('media_stop', feature=STOP)
        else: self.pause()

    def wake(self):
        # Best effort: a Connect receiver usually wakes itself when playback starts.
        if self.room.state.get('state') in ('off', 'standby') and self.room.features() & TURN_ON:
            try: self.room.ha_call('turn_on', feature=TURN_ON)
            except BackendError: pass


class HARoom(NativeSettingsRoom):
    """One virtual room backed by one Home Assistant media_player entity.

    A non-Sonos player has no queue of its own; when the bridge provides one
    (bridge_player.py), queue and music-service actions use it, and while it is
    playing, transport and now-playing come from it.
    """

    def __init__(self, backend, entity_id, log=print):
        super().__init__(backend, entity_id, log)
        self.ha, self.entity_id = backend.ha, entity_id

    @property
    def bridge(self):
        """The bridge-owned queue for a non-Sonos player, or None."""
        getter = getattr(self.backend, 'bridge_player', None)   # composable/test backends may lack it
        return getter(self.entity_id, self) if callable(getter) else None

    def playing_bridge(self):
        bridge = self.bridge
        return bridge if bridge is not None and bridge.active else None

    def spotify_autoplay_status(self):
        """The bridge-owned queue's Autoplay state; Autoplay is only on its Connect route."""
        bridge = self.playing_bridge()
        return bridge.autoplay_status() if bridge is not None else None

    # --- state ------------------------------------------------------------------
    @property
    def state(self):
        return self.ha.state(self.entity_id)

    @property
    def attributes(self):
        return self.state.get('attributes') or {}

    @property
    def available(self):
        connected = getattr(self.ha, 'connected', None)
        return (connected is None or connected.is_set()) and self.state.get('state') not in (None, 'unavailable', 'unknown')

    @property
    def reachable(self):
        """Connected and the entity exists, even if HA cannot describe its state."""
        connected = getattr(self.ha, 'connected', None)
        return (connected is None or connected.is_set()) and self.state.get('state') not in (None, 'unavailable')

    def features(self):
        try: return max(0, int(self.attributes.get('supported_features') or 0))
        except (TypeError, ValueError): return 0

    @property
    def reported_volume(self):
        level = self.attributes.get('volume_level')
        return int(round(level * 100)) if level is not None else 0

    @property
    def reported_mute(self):
        return bool(self.attributes.get('is_volume_muted'))

    @property
    def transport(self):
        bridge = self.playing_bridge()
        if bridge is not None: return bridge.transport()
        # HA folds Sonos STOPPED into "paused"; a live input only plays or stops.
        if self.state.get('state') == 'paused' and self.line_in_source() is not None: return 'STOPPED'
        return TRANSPORT.get(self.state.get('state'), 'STOPPED')

    def shuffle_repeat(self):
        bridge = self.playing_bridge()
        if bridge is not None: return bridge.queue.shuffle, bridge.queue.repeat
        if self.line_in_source() is not None: return False, 'off'
        attributes = self.attributes
        return bool(attributes.get('shuffle')), attributes.get('repeat') or 'off'

    def group_members(self):
        members = self.attributes.get('group_members')
        return list(members) if isinstance(members, list) and all(isinstance(x, str) for x in members) else [self.entity_id]

    def line_in_source(self):
        if not self.available or not self.backend.binding(self.entity_id).is_sonos: return None
        attributes = self.attributes
        uuid = source_uuid(attributes.get('media_content_id'))
        if uuid: return self.backend.line_in.entity(uuid) or uuid
        # The source label alone cannot identify which player's input is used.
        return None

    def position(self):
        bridge = self.playing_bridge()
        if bridge is not None: return bridge.position()
        return self.ha_position()

    def ha_position(self):
        if self.line_in_source() is not None: return 0
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
        bridge = self.playing_bridge()
        if bridge is not None:
            found = bridge.track()
            if found is None: return None
            title, artist, album, picture, duration = found
            revision = track_revision(self.track_reference(), title, artist, album, duration)
            return title, artist, album, self.backend.art.path_for(picture, revision=revision), duration
        attributes = self.attributes
        if not self.available or self.state.get('state') in ('off', 'idle', 'standby'): return None
        channel = attributes.get('media_channel') or ''
        source = attributes.get('source') or attributes.get('app_name') or ''
        title = attributes.get('media_title') or channel or source
        if not title: return None
        artist = attributes.get('media_artist') or attributes.get('media_album_artist') or \
            (channel if channel != title else '')
        album, duration = attributes.get('media_album_name') or '', attributes.get('media_duration')
        revision = track_revision(self.track_reference(), title, artist, album, duration)
        return (title, artist, album,
                self.backend.art.path_for(attributes.get('entity_picture'), revision=revision), duration)

    def track_reference(self):
        bridge = self.playing_bridge()
        if bridge is not None:
            entry = bridge.queue.current
            return 'bridge:%s:%s:%s' % (self.entity_id, bridge.queue.numbers()[0], entry.ref if entry else '')
        return '%s:%s' % (self.entity_id, self.attributes.get('media_content_id') or '')

    def queue_numbers(self):
        bridge = self.playing_bridge()
        if bridge is not None: return bridge.queue.numbers()
        if self.line_in_source() is not None: return 0, 0
        attributes = self.attributes
        if self.track() is None: return 0, 0
        position, size = attributes.get('queue_position'), attributes.get('queue_size')
        if position and size: return int(position), int(size)
        return 1, 1

    def queue_token(self):
        bridge = self.bridge
        if bridge is not None: return 'bridge', bridge.queue.version, bridge.queue.numbers()
        return self.attributes.get('queue_size')

    def capabilities(self):
        return self.operation_capabilities() & {'seek', 'next', 'previous'}

    def operation_capabilities(self):
        """Implemented routes, with source restrictions and cached registry identity."""
        if not self.available:
            # A speaker HA cannot describe ("unknown": a SoundTouch with no source) can
            # still take bridge-queue playback, which does not go through HA.
            bridge = self.bridge if self.reachable else None
            return frozenset(self.bridge_operations(bridge, set())) if bridge is not None else frozenset()
        features, binding = self.features(), self.backend.binding(self.entity_id)
        operations = {name for name, bit in (
            ('play', PLAY), ('pause', PAUSE), ('stop', STOP), ('next', NEXT),
            ('previous', PREVIOUS), ('seek', SEEK), ('volume', VOLUME_SET),
            ('mute', VOLUME_MUTE), ('queue_clear', CLEAR_PLAYLIST),
            ('shuffle', SHUFFLE_SET), ('repeat', REPEAT_SET), ('browse', BROWSE_MEDIA)) if features & bit}
        if features & PAUSE: operations.add('stop')  # deliberate stop -> pause fallback
        if features & PLAY_MEDIA: operations.update(('play_item', 'radio_play'))
        if features & GROUPING and binding.group_key: operations.add('group')
        if binding.is_sonos:
            operations.update(('queue_browse', 'queue_remove'))
            if features & PLAY: operations.update(('queue_play', 'queue_use'))
            if features & PLAY_MEDIA:
                operations.add('line_in')
                if features & MEDIA_ENQUEUE:
                    operations.update(('queue_add', 'provider_play', 'provider_enqueue'))
        if self.line_in_source() is not None:
            operations.difference_update(('seek', 'next', 'previous', 'pause', 'shuffle', 'repeat'))
            if features & STOP: operations.add('pause')  # CR100 pause key -> native live-input stop
        bridge = self.bridge
        if bridge is not None: self.bridge_operations(bridge, operations)
        return self.with_settings_capabilities(operations)

    @staticmethod
    def bridge_operations(bridge, operations):
        if len(bridge.queue) or bridge.can_play(): operations.update(BRIDGE_QUEUE_OPERATIONS)
        if bridge.active:
            operations.difference_update(BRIDGE_TRANSPORT)
            operations.update(bridge.capabilities())
        elif len(bridge.queue):
            operations.add('play')
        return operations

    # --- Home Assistant calls -----------------------------------------------------
    def ha_call(self, service, data=None, feature=None):
        if not self.available: raise BackendError('Home Assistant player is unavailable')
        if feature and not self.features() & feature: raise BackendError('%s not supported' % service)
        try:
            self.ha.call_service('media_player', service, self.entity_id, data)
        except Exception as error:
            self.log({'event': 'ha-call-failed', 'entity': self.entity_id, 'service': service, 'error': str(error)})
            raise BackendError(str(error)) from error

    def sonos_call(self, service, data=None, response=False):
        if not self.available: raise BackendError('Home Assistant player is unavailable')
        if not self.backend.binding(self.entity_id).is_sonos:
            raise BackendError('Sonos queue operations are unavailable for this Home Assistant player')
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
        if enqueue:
            if not self.features() & MEDIA_ENQUEUE: raise BackendError('Enqueue is not supported by this Home Assistant player')
            data['enqueue'] = enqueue
        self.ha_call('play_media', data, feature=PLAY_MEDIA)

    # --- actions ----------------------------------------------------------------
    def play(self):
        bridge = self.bridge
        if bridge is not None and len(bridge.queue) and (bridge.active or bridge.queue_selected):
            bridge.play(); return
        self.ha_call('media_play', feature=PLAY)

    def release_bridge(self):
        """Another selection replaces bridge queue playback; the queue itself is kept."""
        bridge = self.bridge
        if bridge is not None: bridge.release()

    def play_radio(self, uri, metadata=''):
        # A broadcast is selected directly. Never enqueue or clear the saved queue.
        self.release_bridge()
        self.play_uri(uri, 'music')

    def radio_formats(self):
        return ('mp3', 'aac', 'wma') if self.binding.is_sonos else ('mp3', 'aac')

    def pause(self):
        bridge = self.playing_bridge()
        if bridge is not None: bridge.pause(); return
        if self.line_in_source() is not None and self.features() & STOP: self.ha_call('media_stop', feature=STOP)
        else:
            self.require('pause')
            self.ha_call('media_pause', feature=PAUSE)
    def stop(self):
        self.require('stop')
        bridge = self.playing_bridge()
        if bridge is not None: bridge.stop(); return
        if self.features() & STOP: self.ha_call('media_stop', feature=STOP)
        else: self.ha_call('media_pause', feature=PAUSE)
    def next(self):
        bridge = self.playing_bridge()
        if bridge is not None: bridge.next(); return
        self.ha_call('media_next_track', feature=NEXT)

    def previous(self):
        bridge = self.playing_bridge()
        if bridge is not None: bridge.previous(); return
        self.ha_call('media_previous_track', feature=PREVIOUS)

    def seek(self, seconds):
        bridge = self.playing_bridge()
        if bridge is not None: bridge.seek(seconds); return
        self.ha_call('media_seek', {'seek_position': seconds}, feature=SEEK)

    def play_track(self, number):
        self.require('queue_play')
        if number < 1: raise BackendError('Queue track number must be positive')
        bridge = self.bridge
        if bridge is not None:
            try: bridge.select(number)
            except ValueError as error: raise BackendError(str(error)) from error
            return
        self.sonos_call('play_queue', {'queue_position': max(0, number - 1)})

    def use_queue(self):
        self.require('queue_use')
        bridge = self.bridge
        if bridge is not None:
            bridge.use_queue(); return
        if self.line_in_source() is not None:
            self.play_track(int(self.attributes.get('queue_position') or 1))

    def remove_track(self, number):
        self.require('queue_remove')
        if number < 1: raise BackendError('Queue track number must be positive')
        bridge = self.bridge
        if bridge is not None:
            try: bridge.remove(number)
            except ValueError as error: raise BackendError(str(error)) from error
            return
        self.sonos_call('remove_from_queue', {'queue_position': number - 1})

    def clear_queue(self):
        bridge = self.bridge
        if bridge is not None: bridge.clear(); return
        self.ha_call('clear_playlist', feature=CLEAR_PLAYLIST)

    def set_play_mode(self, shuffle, repeat):
        bridge = self.playing_bridge()
        if bridge is not None:
            try: bridge.set_mode(shuffle, repeat)
            except ValueError as error: raise BackendError(str(error)) from error
            return
        if self.line_in_source() is not None:
            if shuffle or repeat != 'off': raise BackendError('Play modes are unavailable for Line-In')
            return
        current_shuffle, current_repeat = self.shuffle_repeat()
        if repeat not in ('off', 'all', 'one'): raise BackendError('Invalid repeat mode')
        if shuffle != current_shuffle and not self.features() & SHUFFLE_SET:
            raise BackendError('Shuffle is not supported by this Home Assistant player')
        if repeat != current_repeat and not self.features() & REPEAT_SET:
            raise BackendError('Repeat is not supported by this Home Assistant player')
        if self.features() & SHUFFLE_SET: self.ha_call('shuffle_set', {'shuffle': shuffle})
        if self.features() & REPEAT_SET: self.ha_call('repeat_set', {'repeat': repeat})

    def play_item(self, node):
        self.require('play_item')
        if not self.backend.binding(self.entity_id).is_sonos and (
                str(node.ref).startswith(('x-rincon-', 'x-rincon:', 'x-sonos-', 'x-sonosapi-',
                                          'x-sonosprog:', 'x-file-cifs:', 'spotify:', 'SQ:', 'FV:', 'A:', 'S:'))
                or node.kind == 'favorite_item_id'):
            raise BackendError('This Sonos media item cannot be played by a generic Home Assistant player')
        self.release_bridge()
        self.play_uri(node.ref, node.kind)

    def play_line_in(self, source_id):
        self.require('line_in')
        source = next((entry for entry in self.backend.line_in_sources() if entry['id'] == source_id), None)
        if source is None: raise BackendError('Line-In is not connected: %s' % source_id)
        # HA's Sonos integration sends this URI to the destination coordinator.
        # Omitting enqueue preserves the Sonos queue and starts the live input.
        self.release_bridge()
        self.play_uri(source['uri'], 'music')

    def enqueue_item(self, node, as_next=False):
        """Home Assistant adds it (after the current track for 'as next')."""
        self.require('queue_add')
        if source_uuid(node.ref): raise BackendError('Line-In cannot be added to the queue')
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
        self.require('provider_play')
        bridge = self.bridge
        if bridge is not None:
            bridge.replace(uri, kind)
            self.queue_edited()
            return
        self.play_uri(uri, kind, 'replace')
        self.queue_edited()

    def enqueue_provider(self, uri, kind, as_next=False):
        self.require('provider_enqueue')
        bridge = self.bridge
        if bridge is not None:
            try: result = bridge.enqueue(uri, kind, as_next)
            except ValueError as error: raise BackendError(str(error)) from error
            self.queue_edited()
            return result
        before = self.queue_length()
        current = int(self.attributes.get('queue_position') or 0)
        self.play_uri(uri, kind, 'next' if as_next else 'add')
        self.queue_edited()
        after = self.queue_length()
        # Sonos expands albums/playlists, so use the actual resulting queue length.
        return {'FirstTrackNumberEnqueued': str(current + 1 if as_next and current else before + 1),
                'NumTracksAdded': str(max(0, after - before)), 'NewQueueLength': str(after)}

    def send_volume(self, value):
        self.require('volume')
        self.ha_call('volume_set', {'volume_level': value / 100}, feature=VOLUME_SET)

    def send_mute(self, value):
        self.require('mute')
        self.ha_call('volume_mute', {'is_volume_muted': value}, feature=VOLUME_MUTE)

    def join(self, member):
        if not self.can_group_with(member): raise BackendError('These Home Assistant players do not share a grouping provider')
        self.ha_call('join', {'group_members': [member.entity_id]}, feature=GROUPING)

    def unjoin(self):
        self.require('group')
        self.ha_call('unjoin', feature=GROUPING)
