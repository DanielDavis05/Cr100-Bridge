#!/usr/bin/env python3
"""Build/copy the distributable tree without local credentials or research captures."""
import argparse
import io
import json
from pathlib import Path
import shutil
import tarfile

import build_info

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = '''bridge_service.py controller_state.py controller_visibility.py sonosnet_link.py nexmon_power.py nexmon_clock.py frames.py ccmp.py ccmp_pn.py zoneplayers.py
backends.py bridge_config.py provider_playback.py room_capabilities.py sonos_settings.py content.py ha_backend.py ha_client.py ha_hub.py ha_content.py ha_linein.py smart_devices.py smart_device_service.py room_icons.py music_service_events.py
smapi_service.py smapi_stub.py speaker_settings.py music_services.py music_service_registry.py sonos_queue_art.py spotify_auth.py spotify_client.py spotify_playback.py
spotify_service.py spotify_ui_actions.py spotify_menu.py spotify_folders.py spotify_controllers.py spotify_connect.py spotify_autoplay.py webui.py netstart_pair.py link_pairing.py synthetic_public_key.bin fake_ha.py
sonos_backend.py sonos_content.py sonos_upnp.py sonos_identity.py fake_sonos.py build_info.py
radio_store.py radiotime.py radio_catalog.py native_radio.py radio_playback.py radio_management.py radio_runtime.py radio_transcode.py alarms.py alarm_runtime.py alarm_timezones.py patch_native_radio_ui.py patch_controller_settings_ui.py
online_updates.py update_transport.py update_service.py bridge_updater.py ui_events.py workspace.py radio_platform.py
stock_bootstrap.py stock_room_mode.py
bridge_queue.py media_sources.py spotify_media.py relay_stream.py bridge_player.py bridge_runtime.py zeroconf_browse.py
distribution.py'''.split()
FIRMWARE_ASSETS = '''README.md .gitignore ownership.c ownership.h tsf_query.c tsf_query.h
shm_query.c shm_query.h beacon_stamp.c beacon_stamp.h build.py manifest.json
status.py apply.py test_build.py test_ownership.c test_beacon_stamp.c test_shm_query.c
test_status.py test_apply.py'''.split()


def files(root=ROOT):
    selected = [root / name for name in ('install.sh', 'README.md', 'LICENSE',
                                        'requirements.txt', '.gitignore', '.gitattributes')
                if name != 'LICENSE' or (root / name).is_file()]
    selected += [root / 'tools' / name for name in RUNTIME]
    selected += sorted(p for p in (root / 'docs').rglob('*')
                       if p.is_file() and p.suffix in ('.md', '.png'))
    selected += [root / 'firmware/nexmon-ownership' / name for name in FIRMWARE_ASSETS]
    selected += sorted((root / 'tools').glob('test_*.py')) + [root / 'tools' / 'run_tests.py']
    for name in ('systemd', 'scripts', 'config', '.github', 'tools/zp80', 'tools/webui',
                 'tools/cr100_flash', 'tools/stock_bootstrap_assets'):
        selected += sorted(p for p in (root / name).rglob('*')
                           if p.is_file() and '__pycache__' not in p.parts and p.name != '.DS_Store')
    for path in selected:
        if not path.is_file(): raise FileNotFoundError(path)
    return selected


def copy(destination, root=ROOT):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination == root: raise ValueError('Choose a destination outside the source tree')
    metadata = _metadata(root)   # before writing: output must not make its own source dirty
    for path in files(root):
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    (destination / build_info.STAMP).write_bytes(metadata)


def _metadata(root):
    return (json.dumps(build_info.read(root), indent=2, sort_keys=True) + '\n').encode()


def archive(destination, root=ROOT):
    root = Path(root).resolve()
    metadata = _metadata(root)
    with tarfile.open(destination, 'w:gz') as bundle:
        for path in files(root):
            bundle.add(path, arcname=Path('cr100-bridge') / path.relative_to(root), recursive=False)
        stamp = tarfile.TarInfo('cr100-bridge/' + build_info.STAMP)
        stamp.size, stamp.mode = len(metadata), 0o644
        bundle.addfile(stamp, io.BytesIO(metadata))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--copy', type=Path)
    target.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.copy:
        copy(args.copy)
    else:
        archive(args.archive)


if __name__ == '__main__': main()
