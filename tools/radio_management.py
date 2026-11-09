"""Radio management shared by authenticated bridge routes and native actions."""
from __future__ import annotations

import json
from pathlib import Path

from radio_store import RadioStore
from radiotime import RadioTimeClient


def state_paths(config_path):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    def resolve(value, default):
        path = Path(value or default).expanduser()
        return path if path.is_absolute() else config_path.parent / path
    return (resolve((config.get('radio') or {}).get('state_file'), 'radio-state.json'),
            resolve((config.get('alarms') or {}).get('state_file'), 'alarms.sqlite3'))


class RadioManagement:
    def __init__(self, config_path, *, store=None, client=None, catalog=None, alarms=None):
        self.config_path = Path(config_path)
        self.store = store or RadioStore(state_paths(config_path)[0])
        self.client = client or RadioTimeClient()
        self._catalog, self.alarms = catalog, alarms

    @property
    def catalog(self):
        if self._catalog is None:
            from radio_catalog import RadioCatalog
            self._catalog = RadioCatalog(self.client, self.store)
            self._catalog.timezone = lambda: getattr(self.alarms, 'timezone', 'UTC')
        return self._catalog

    def snapshot(self):
        data = self.store.snapshot()
        data['artwork_enabled'] = True
        data['message'] = 'Radio preferences apply without restarting the controller connection.'
        return data

    def command(self, command, body):
        if not isinstance(body, dict): raise ValueError('Expected a JSON object')
        if command == 'settings':
            patch = body.get('settings', body)
            if not isinstance(patch, dict): raise ValueError('Expected a Radio settings object')
            current = self.store.settings()
            preferred = patch.get('preferred_language', current['preferred_language'])
            if (preferred != current['preferred_language'] or
                    patch.get('language_filter') is True and not current['language_filter']):
                self.catalog.validate_language(preferred)
            if patch.get('language_filter', current['language_filter']) and not preferred:
                raise ValueError('Choose a preferred language before enabling its filter')
            self.store.update_settings(patch)
        elif command == 'language-options':
            return {'languages': self.catalog.language_choices()}
        elif command == 'location-browse':
            return self.catalog.location_page(body.get('id', 'r0'), body.get('ancestors'))
        elif command == 'location-select':
            region = body.get('id') or body.get('region_id')
            page = self.catalog.location_page(region, body.get('ancestors'))
            if not page['selectable']: raise ValueError('Choose a city or local area from the directory')
            label = body.get('label') or page['title']
            if not isinstance(label, str) or not label.strip() or len(label) > 250:
                raise ValueError('A location name of up to 250 characters is required')
            self.catalog.set_location({'region_id': region, 'label': label.strip()})
        elif command == 'location':
            if body.get('zip') is not None: self.catalog.set_zip(body['zip'])
            else: self.catalog.set_location(body.get('location', body))
        elif command == 'favorite-add':
            identity = body.get('id')
            if not identity: raise ValueError('Choose a station or show')
            self.catalog.favorite(identity)
        elif command == 'favorite-remove': self.store.remove_favorite(body.get('id'))
        elif command == 'favorite-edit':
            self.store.update_favorite(body.get('id'), title=body.get('title'), index=body.get('index'))
        elif command == 'favorite-reorder': self.store.reorder_favorites(body.get('ids'))
        elif command == 'custom':
            operation = body.get('operation', 'add')
            if operation == 'add':
                self.store.add_custom(body.get('name', ''), body.get('url', ''), body.get('logo', ''))
            elif operation == 'edit':
                self.store.update_custom(body.get('id'), {k: body[k] for k in ('name', 'title', 'url', 'logo') if k in body})
            elif operation == 'delete':
                self._check_custom_alarms(body.get('id'), body.get('disable_alarms') is True)
                self.store.delete_custom(body.get('id'))
            else: raise ValueError('Unknown custom station operation')
        elif command == 'test-url':
            streams = self.client.test_url(body.get('url', ''), formats=('mp3', 'aac', 'hls', 'wma', 'ogg'))
            return {'streams': [stream.as_dict() for stream in streams], 'playback_started': False}
        elif command == 'history-clear': self.store.clear_history(); self.store.clear_searches()
        elif command == 'refresh':
            self.client.clear_cache()
            self.store.refresh()
        elif command == 'reset': self.store.reset_settings()
        elif command == 'import':
            # Validate before disabling any alarms, then guard references that a
            # replacement import would remove just like explicit deletion.
            validated = self.store._validated(body.get('data'))
            if body.get('merge') is not True:
                retained = {item['id'] for item in validated['custom_stations']}
                for item in self.store.custom_stations():
                    if item['id'] not in retained:
                        self._check_custom_alarms(item['id'], body.get('disable_alarms') is True)
            self.store.import_data(body.get('data'), merge=body.get('merge') is True)
        elif command == 'export': return self.store.export_data()
        elif command == 'browse':
            page = self.catalog.browse(body.get('id', '0'), start=int(body.get('start', 0)), count=100)
            return page.as_dict() if hasattr(page, 'as_dict') else page
        elif command == 'search':
            page = self.catalog.search(body.get('query', ''), body.get('kind', 'stations'))
            return page.as_dict() if hasattr(page, 'as_dict') else page
        else: raise ValueError('Unknown Radio operation')
        return self.snapshot()

    def _check_custom_alarms(self, identity, disable):
        if self.alarms is None: return
        for alarm in self.alarms.list():
            reference = alarm.get('media_ref') or alarm.get('source') or {}
            content = reference.get('content_id') or reference.get('id') or ''
            if reference.get('provider') != 'radio' or content != identity: continue
            if not disable: raise ValueError('This station is used by an alarm. Disable its alarms before deleting it.')
            self.alarms.save({**alarm, 'enabled': False})

