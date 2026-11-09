"""Cached routing identity, separate from the CR100's descriptive About metadata.

Bindings are snapshots. Reading them must not discover devices or contact HA.
Operations describe implemented bridge routes, not everything hardware might do.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RoomBinding:
    platform: str = ''
    device_id: str = ''
    native_id: str = ''
    native_url: str = ''
    group_key: tuple[str, ...] | None = None

    @property
    def is_sonos(self):
        return self.platform == 'sonos'

    @property
    def native_sonos(self):
        return self.is_sonos and bool(self.native_id and self.native_url)
