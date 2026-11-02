#!/usr/bin/env python3
"""Build/copy the distributable tree without local credentials or research captures."""
import argparse
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = '''bridge_service.py sonosnet_link.py frames.py ccmp.py zoneplayers.py
backends.py content.py ha_backend.py ha_client.py ha_content.py room_icons.py music_service_events.py
smapi_stub.py sonos_queue_art.py spotify_auth.py spotify_client.py spotify_playback.py
spotify_service.py webui.py netstart_pair.py synthetic_public_key.bin fake_ha.py
sonos_backend.py sonos_content.py sonos_upnp.py fake_sonos.py
distribution.py'''.split()


def files(root=ROOT):
    selected = [root / name for name in ('install.sh', 'README.md', 'requirements.txt', '.gitignore', '.gitattributes')]
    selected += [root / 'tools' / name for name in RUNTIME]
    selected += sorted((root / 'tools').glob('test_*.py'))
    for name in ('systemd', 'scripts', 'config', '.github', 'tools/zp80', 'tools/webui'):
        selected += sorted(p for p in (root / name).rglob('*')
                           if p.is_file() and '__pycache__' not in p.parts and p.name != '.DS_Store')
    for path in selected:
        if not path.is_file(): raise FileNotFoundError(path)
    return selected


def copy(destination, root=ROOT):
    destination = Path(destination).resolve()
    if destination == root.resolve(): raise ValueError('Choose a destination outside the source tree')
    for path in files(root):
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--copy', type=Path)
    target.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.copy:
        copy(args.copy)
    else:
        with tarfile.open(args.archive, 'w:gz') as archive:
            for path in files():
                archive.add(path, arcname=Path('cr100-bridge') / path.relative_to(ROOT), recursive=False)


if __name__ == '__main__': main()
