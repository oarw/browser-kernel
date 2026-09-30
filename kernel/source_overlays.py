"""Reviewed overlays shared by source checks, fresh builds and migrations."""
import hashlib
import json
from pathlib import Path

from build_support import digest

HERE = Path(__file__).resolve().parent


def load_overlays():
    return json.loads((HERE / 'overlay-lock.json').read_text(encoding='utf8'))['overlays']


def targets(overlays):
    return {item['path']: item for overlay in overlays for item in overlay['files']}


def source_digest(path):
    return hashlib.sha256(path.read_text(encoding='utf8').encode()).hexdigest()


def validate_sources(source, overlays, field='preparedSha256'):
    for name, item in targets(overlays).items():
        path = source / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise RuntimeError(f'Missing or unsafe overlay source: {name}')
        if source_digest(path) != item[field]:
            raise RuntimeError(f'Overlay source differs from reviewed {field}: {name}')


def overlay_identity(overlays):
    return {'overlayLockSha256': digest(HERE / 'overlay-lock.json'),
            'overlayScriptSha256': digest(HERE / 'source_overlays.py'),
            'migrationScriptSha256': digest(HERE / 'migrate_prepared.py'),
            'migrationLockSha256': digest(HERE / 'migration-lock.json'),
            'overlaySha256': {item['patch']: digest(HERE / item['patch']) for item in overlays}}


def install_overlays(core, overlays):
    # Validate every existing destination before changing the series or patches.
    for item in overlays:
        path = core / 'patches' / item['seriesName']
        if path.exists() and path.read_bytes() != (HERE / item['patch']).read_bytes():
            raise RuntimeError(f'Existing overlay differs: {path}')
    for item in overlays:
        path = core / 'patches' / item['seriesName']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((HERE / item['patch']).read_bytes())
