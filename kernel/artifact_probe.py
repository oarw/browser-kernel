import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import uuid
import zipfile

from build_support import digest, write_json

NAMES = ('sample.bin', 'out/.ninja_log')
STAMP = 1767225600


def identity():
    return {k: os.environ.get(k, 'local') for k in
            ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')}


def create(output):
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'fixture.zip'
    if archive.exists() or (output / 'manifest.json').exists():
        raise RuntimeError('Output already contains a fixture')
    contents = {NAMES[0]: os.urandom(1024 * 1024), NAMES[1]: b'# ninja log v5\n0\t1\t1\tfixture.obj\t1234\n'}
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as writer:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            writer.writestr(info, data)
    marker = 'fb-probe-' + uuid.uuid4().hex
    marker_root = Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir()))
    marker_root.mkdir(parents=True, exist_ok=True)
    (marker_root / marker).write_text('fixture', encoding='utf8')
    manifest = {'schemaVersion': 1, 'identity': identity(), 'host': socket.gethostname(),
                'job': os.environ.get('GITHUB_JOB', 'local'), 'marker': marker,
                'archiveSha256': digest(archive), 'files': {
                    name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'mtime': STAMP}
                    for name, data in contents.items()}}
    write_json(output / 'manifest.json', manifest)
    return digest(output / 'manifest.json')


def verify(source, output, manifest_sha256, require_clean_job=False):
    if digest(source / 'manifest.json') != manifest_sha256:
        raise RuntimeError('Manifest digest mismatch')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf8'))
    if manifest.get('schemaVersion') != 1 or manifest.get('identity') != identity():
        raise RuntimeError('Fixture identity mismatch')
    marker = manifest.get('marker', '')
    if len(marker) != 41 or not marker.startswith('fb-probe-') or any(c not in '0123456789abcdef' for c in marker[9:]):
        raise RuntimeError('Invalid job marker')
    marker_root = Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir()))
    clean_storage = not (marker_root / marker).exists()
    different_job = manifest.get('job') != os.environ.get('GITHUB_JOB', 'local')
    if require_clean_job and (not different_job or not clean_storage):
        raise RuntimeError('Expected another job with clean temporary storage')
    archive = source / 'fixture.zip'
    if archive.stat().st_size > 2 * 1024 * 1024 or digest(archive) != manifest['archiveSha256']:
        raise RuntimeError('Archive size or digest mismatch')
    if set(manifest.get('files', {})) != set(NAMES):
        raise RuntimeError('Unexpected fixture manifest')
    with zipfile.ZipFile(archive) as reader:
        entries = reader.infolist()
        if len(entries) != len(NAMES) or {e.filename for e in entries} != set(NAMES):
            raise RuntimeError('Unexpected archive entries')
        contents = {}
        for entry in entries:
            expected = manifest['files'][entry.filename]
            if (entry.file_size != expected['size'] or entry.file_size > 1024 * 1024
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.date_time != (2026, 1, 1, 0, 0, 0)):
                raise RuntimeError('Invalid archive metadata')
            data = reader.read(entry)
            if hashlib.sha256(data).hexdigest() != expected['sha256'] or expected['mtime'] != STAMP:
                raise RuntimeError('Fixture content mismatch')
            contents[entry.filename] = data
    output.mkdir(parents=True, exist_ok=False)
    for name, data in contents.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.utime(target, (STAMP, STAMP))
        if digest(target) != manifest['files'][name]['sha256'] or target.stat().st_mtime_ns != STAMP * 10**9:
            raise RuntimeError('Restored fixture mismatch')
    result = {'artifactRestored': True, 'hostnameChanged': manifest['host'] != socket.gethostname(),
              'differentJob': different_job, 'freshJobStorage': clean_storage,
              'filesVerified': len(contents), 'timestampsVerified': True,
              'browserCompiled': False, 'browserAcceptancePassed': False}
    write_json(output / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['create', 'verify'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--require-clean-job', action='store_true')
    args = parser.parse_args()
    if args.mode == 'create':
        value = create(args.output)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
                stream.write(f'manifest_sha256={value}\n')
        print(value)
    else:
        if not args.source or not args.manifest_sha256:
            parser.error('verify requires --source and --manifest-sha256')
        print(json.dumps(verify(args.source, args.output, args.manifest_sha256, args.require_clean_job)))
