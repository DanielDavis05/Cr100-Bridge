"""Player-process Home Assistant owner, independent of the music source.

The web UI deliberately keeps its own client. Backends borrow this client's
connection; only credential replacement or process shutdown closes it. Registry
reads run on this worker, never the WebSocket/event or UPnP request threads.
"""
from __future__ import annotations

import atexit
import copy
from pathlib import Path
import threading
import time

from ha_client import HomeAssistant

REGISTRY_INTERVAL = 60
RETRY_INTERVAL = 5
REGISTRY_TIMEOUT = 2
REGISTRIES = {'entities': 'entity', 'devices': 'device', 'areas': 'area', 'floors': 'floor'}


class HAHub:
    def __init__(self, client, log=print, interval=REGISTRY_INTERVAL):
        self.ha = self.client = client
        self.log, self.interval = log, interval
        self.lock = threading.RLock()
        self.registries = {name: [] for name in REGISTRIES}
        self.revision = 0
        self.ready = threading.Event()
        self.listeners = []       # fn(), after a registry/connection change
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        client.ha_hub = self
        self._connection_listener = lambda: self.wake.set()
        client.connection_listeners.append(self._connection_listener)

    def start(self):
        self.client.start()
        self.thread = threading.Thread(target=self.run, name='ha-registries', daemon=True)
        self.thread.start()

    def snapshot(self):
        """A coherent caller-owned snapshot; consumers never mutate the shared cache."""
        with self.lock: return copy.deepcopy(self.registries)

    def notify(self):
        for listener in list(self.listeners):
            try: listener()
            except Exception as error:
                self.log({'event': 'ha-hub-listener-error', 'error': repr(error)})

    def refresh(self):
        """Read each registry once. Unsupported registries keep their last snapshot."""
        updated, success = {}, True
        for name, command in REGISTRIES.items():
            if self.stop.is_set(): return False
            try:
                entries = self.client.call({'type': 'config/%s_registry/list' % command}, REGISTRY_TIMEOUT)
                if not isinstance(entries, list): raise ValueError('Invalid registry response')
                updated[name] = [entry for entry in entries if isinstance(entry, dict)]
            except Exception:
                # Older HA installations can lack a floor registry. Keeping the
                # other registries usable also preserves legacy line-in discovery.
                success = False
                self.log({'event': 'ha-registry-unavailable', 'registry': name})
        with self.lock:
            changed = any(self.registries[name] != entries for name, entries in updated.items())
            self.registries.update(updated)
            if changed: self.revision += 1
        self.ready.set()
        if changed: self.notify()
        return success

    def run(self):
        previous, generation, next_refresh = None, None, 0
        while not self.stop.is_set():
            connected = self.client.connected.is_set()
            current_generation = self.client.connection_generation
            if connected != previous or current_generation != generation:
                previous, generation, next_refresh = connected, current_generation, 0
                self.notify()
            if connected and time.monotonic() >= next_refresh:
                succeeded = self.refresh()
                next_refresh = time.monotonic() + (self.interval if succeeded else RETRY_INTERVAL)
            self.wake.wait(.25)
            self.wake.clear()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread and threading.current_thread() is not self.thread:
            self.thread.join(REGISTRY_TIMEOUT * len(REGISTRIES) + 1)
        if self._connection_listener in self.client.connection_listeners:
            self.client.connection_listeners.remove(self._connection_listener)
        self.client.close()


_lock = threading.RLock()
_hub = None
_credentials = None


def current():
    """The configured player-process hub, or None when credentials are absent."""
    with _lock: return _hub


def configure(config, config_dir, log=print):
    """Retain one client for unchanged credentials, whatever the music source is.

    HA credentials have always lived at the top level; source switching stashes
    only rooms and library choices, so existing files need no migration.
    """
    global _hub, _credentials
    settings = config.get('home_assistant') or {}
    url, token = str(settings.get('url') or '').rstrip('/'), ''
    if url and settings.get('token_file'):
        path = Path(settings['token_file']).expanduser()
        if not path.is_absolute(): path = Path(config_dir) / path
        try: token = path.read_text().strip()
        except OSError: log({'event': 'ha-token-unavailable'})
    credentials = (url, token) if url and token else None
    with _lock:
        if credentials == _credentials: return _hub
        previous, _hub, _credentials = _hub, None, credentials
        if previous: previous.close()
        if credentials:
            _hub = HAHub(HomeAssistant(*credentials, log), log)
            _hub.start()
        return _hub


def close():
    global _hub, _credentials
    with _lock:
        previous, _hub, _credentials = _hub, None, None
        if previous: previous.close()


atexit.register(close)
