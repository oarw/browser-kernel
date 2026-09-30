import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import time
import zipfile

from build_support import GIB, digest, now, validate_work_dir, write_json
from checkpoint_source import receipt_context

MAX_ARCHIVE = 20 * GIB
MAX_TREE = 120 * GIB
MEMBERS = {'windows', 'prepared.json', 'build-state.json', 'build-result.json'}


def safe_relative(value):
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in value
            or any(c in value for c in ':<>"|?*\r\n')
            or any(part.endswith((' ', '.')) for part in path.parts)):
        raise RuntimeError(f'Invalid relative path: {value}')
    return path


def collect_links(root):
    base = root / 'windows'
    if base.is_symlink() or base.is_junction():
        raise RuntimeError('Windows source root must not be a link')
    links = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ('download_cache', '__pycache__')]
        for name in list(dirs) + files:
            path = Path(directory) / name
            if not path.is_symlink() and not path.is_junction():
                continue
            target = path.resolve()
            if not target.is_relative_to(base):
                raise RuntimeError(f'Link target escapes source tree: {path}')
            info = path.lstat()
            links.append({'path': path.relative_to(root).as_posix(), 'target': target.relative_to(root).as_posix(),
                          'directory': name in dirs or bool(getattr(info, 'st_file_attributes', 0) & 0x10),
                          'mtimeNs': info.st_mtime_ns})
            if name in dirs:
                dirs.remove(name)
    return links


def validate_links(links):
    if not isinstance(links, list) or len(links) > 10000:
        raise RuntimeError('Invalid link manifest')
    sources = set()
    for link in links:
        path, target = safe_relative(link['path']), safe_relative(link['target'])
        if (path.parts[0] != 'windows' or len(path.parts) < 2 or target.parts[0] != 'windows'
                or not isinstance(link['directory'], bool) or not isinstance(link['mtimeNs'], int)
                or link['path'].casefold() in sources):
            raise RuntimeError('Invalid internal link')
        sources.add(link['path'].casefold())
    for link in links:
        if any(parent.as_posix().casefold() in sources for parent in PurePosixPath(link['path']).parents):
            raise RuntimeError('Nested link destinations are not allowed')
    return sources


def set_link_mtime(path, timestamp):
    if os.name != 'nt':
        os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.FILETIME)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x100, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ticks = timestamp // 100 + 116444736000000000
        modified = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
        if not kernel32.SetFileTime(handle, None, None, ctypes.byref(modified)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def restore_links(root, links):
    validate_links(links)
    for link in links:
        path, target = root / link['path'], root / link['target']
        if (os.path.lexists(path) or not path.parent.resolve().is_relative_to(root / 'windows')
                or not target.resolve().is_relative_to(root / 'windows')):
            raise RuntimeError('Invalid link restoration destination')
        os.symlink(os.path.relpath(target, path.parent), path, target_is_directory=link['directory'])
        set_link_mtime(path, link['mtimeNs'])


def context():
    return {key: os.environ.get(key, 'local') for key in
            ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')}


def completed_outputs(root):
    root = root.resolve()
    output = root / 'windows/build/src/out/Default'
    log = output / '.ninja_log'
    completed = {}
    for line in log.read_text(encoding='utf8').splitlines():
        fields = line.split('\t')
        if len(fields) == 5:
            completed[fields[3]] = fields
    samples = []
    for name in sorted(completed):
        if not name.endswith('.obj'):
            continue
        path = (output / name).resolve()
        if output.resolve() not in path.parents or not path.is_file():
            raise RuntimeError('Invalid completed object path')
        samples.append({'path': path.relative_to(root).as_posix(), 'sha256': digest(path),
                        'mtimeNs': path.stat().st_mtime_ns})
        if len(samples) == 8:
            break
    if not samples:
        raise RuntimeError('No completed C++ objects to validate')
    return {'completedOutputs': len(completed), 'samples': samples}


def verify_samples(root, samples):
    for sample in samples:
        relative = PurePosixPath(sample['path'])
        if relative.is_absolute() or '..' in relative.parts or '\\' in sample['path'] or ':' in sample['path'] or relative.parts[:5] != ('windows', 'build', 'src', 'out', 'Default'):
            raise RuntimeError('Invalid object sample path')
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink() or digest(path) != sample['sha256'] or path.stat().st_mtime_ns != sample['mtimeNs']:
            raise RuntimeError(f'Object sample changed: {relative}')


def inspect_archive(archive, link_sources=()):
    size = 0
    count = 0
    seen = set()
    with zipfile.ZipFile(archive) as reader:
        for info in reader.infolist():
            path = PurePosixPath(info.filename)
            key = info.filename.rstrip('/').casefold()
            if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in info.filename
                    or any(c in info.filename for c in ':<>"|?*')
                    or any(part.endswith((' ', '.')) for part in path.parts)
                    or path.parts[0] not in MEMBERS or key in seen
                    or stat.S_ISLNK(info.external_attr >> 16) or info.external_attr & 0x400):
                raise RuntimeError(f'Invalid checkpoint entry: {info.filename}')
            if any(key == link or key.startswith(link + '/') for link in link_sources):
                raise RuntimeError('Link destination must not be embedded in archive')
            seen.add(key)
            size += info.file_size
            count += 1
            if size > MAX_TREE or count > 2_000_000:
                raise RuntimeError('Checkpoint expansion exceeds limit')
    required = {name for name in MEMBERS if name != 'windows'}
    if not required.issubset(seen) or not any(name.startswith('windows/') for name in seen):
        raise RuntimeError('Incomplete checkpoint archive')
    return size, count


def create(root, output):
    root = root.resolve()
    if (root / 'build.lock').exists():
        raise RuntimeError('Builder still owns the workspace')
    state = json.loads((root / 'build-state.json').read_text(encoding='utf8'))
    prepared = json.loads((root / 'prepared.json').read_text(encoding='utf8'))
    if state.get('status') not in ('checkpoint-ready', 'built') or state.get('inputs') != prepared.get('inputs'):
        raise RuntimeError('Workspace is not ready for checkpointing')
    if state['status'] == 'built':
        result = json.loads((root / 'build-result.json').read_text(encoding='utf8'))
        executable = root / 'windows/build/src/out/Default/chrome.exe'
        if (state.get('browserCompiled') is not True or result.get('status') != 'built'
                or result.get('browserCompiled') is not True or result.get('inputs') != prepared['inputs']
                or not executable.is_file() or digest(executable) != result.get('executableSha256')):
            raise RuntimeError('Completed build evidence does not match the executable')
    objects = completed_outputs(root)
    links = collect_links(root)
    link_sources = validate_links(links)
    output.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(output).free < MAX_ARCHIVE + 5 * GIB:
        raise RuntimeError('Insufficient checkpoint staging space')
    archive = output / 'checkpoint.zip'
    command = ['7z', 'a', '-tzip', '-mx=1', '-mmt=2', '-mtc=on', '-bsp0', str(archive),
               'windows', 'prepared.json', 'build-state.json', 'build-result.json',
               '-xr!download_cache', '-xr!__pycache__']
    if links:
        excluded = output / 'excluded-links.txt'
        excluded.write_text(''.join(link['path'] + '\n' for link in links), encoding='utf8', newline='\n')
        command.extend(['-scsUTF-8', '-x@' + str(excluded)])
    with subprocess.Popen(command, cwd=root) as process:
        deadline = time.monotonic() + 45 * 60
        while process.poll() is None:
            if time.monotonic() > deadline or (archive.exists() and archive.stat().st_size > MAX_ARCHIVE):
                subprocess.run(['taskkill', '/pid', str(process.pid), '/t', '/f'], capture_output=True)
                process.wait()
                raise RuntimeError('Checkpoint exceeded time or size budget')
            time.sleep(1)
        if process.returncode:
            raise RuntimeError(f'Checkpoint compression failed: {process.returncode}')
    if archive.stat().st_size > MAX_ARCHIVE:
        raise RuntimeError('Checkpoint exceeds size limit')
    expanded, count = inspect_archive(archive, link_sources)
    manifest = {'schemaVersion': 1, 'createdAt': now(), 'context': context(), 'workspace': str(root),
                'archiveSha256': digest(archive), 'archiveBytes': archive.stat().st_size,
                'expandedBytes': expanded, 'entries': count, 'inputs': prepared['inputs'], 'links': links,
                'buildStatus': state['status'], 'browserCompiled': state['status'] == 'built',
                'browserAcceptancePassed': False, **objects}
    write_json(output / 'manifest.json', manifest)
    return digest(output / 'manifest.json')


def restore(root, source, manifest_sha, trusted_source=None):
    root = root.resolve()
    if root.exists():
        raise RuntimeError('Restore requires an absent workspace')
    if digest(source / 'manifest.json') != manifest_sha:
        raise RuntimeError('Checkpoint manifest digest mismatch')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf8'))
    expected_context = context() if trusted_source is None else receipt_context(trusted_source, context(), manifest_sha)
    if manifest.get('schemaVersion') != 1 or manifest.get('context') != expected_context or manifest.get('workspace') != str(root):
        raise RuntimeError('Checkpoint identity mismatch')
    if trusted_source is not None and manifest.get('buildStatus') != 'checkpoint-ready':
        raise RuntimeError('Cross-run continuation requires an incomplete checkpoint')
    archive = source / 'checkpoint.zip'
    if archive.stat().st_size != manifest['archiveBytes'] or archive.stat().st_size > MAX_ARCHIVE or digest(archive) != manifest['archiveSha256']:
        raise RuntimeError('Checkpoint archive digest or size mismatch')
    link_sources = validate_links(manifest.get('links', []))
    expanded, count = inspect_archive(archive, link_sources)
    if (expanded, count) != (manifest['expandedBytes'], manifest['entries']):
        raise RuntimeError('Checkpoint entry manifest mismatch')
    if shutil.disk_usage(root.parent).free < expanded + 40 * GIB:
        raise RuntimeError('Insufficient workspace capacity to restore and continue')
    root.mkdir()
    subprocess.run(['7z', 'x', str(archive), '-o' + str(root), '-y', '-bsp0'], check=True, timeout=45 * 60)
    restore_links(root, manifest.get('links', []))
    prepared = json.loads((root / 'prepared.json').read_text(encoding='utf8'))
    if prepared.get('inputs') != manifest['inputs']:
        raise RuntimeError('Prepared inputs differ from manifest')
    verify_samples(root, manifest['samples'])
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['create', 'restore', 'verify-progress'])
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--trusted-source', type=Path)
    args = parser.parse_args()
    root = validate_work_dir(args.work_dir, Path(__file__).resolve().parent.parent)
    if args.mode == 'create':
        if not args.output:
            parser.error('--output required')
        checksum = create(root, args.output.resolve())
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
                stream.write(f'manifest_sha256={checksum}\n')
        print(checksum)
    else:
        if not args.source or not args.manifest_sha256:
            parser.error('--source and --manifest-sha256 required')
        if args.mode == 'restore':
            receipt = json.loads(args.trusted_source.read_text(encoding='utf8')) if args.trusted_source else None
            restored = restore(root, args.source.resolve(), args.manifest_sha256, receipt)
            print(json.dumps({'restored': True, 'samples': len(restored['samples'])}))
        else:
            if digest(args.source / 'manifest.json') != args.manifest_sha256:
                raise RuntimeError('Manifest digest mismatch')
            before = json.loads((args.source / 'manifest.json').read_text(encoding='utf8'))
            verify_samples(root, before['samples'])
            after = completed_outputs(root)
            if after['completedOutputs'] <= before['completedOutputs']:
                raise RuntimeError('No new completed outputs after resume')
            if not args.output:
                parser.error('--output required')
            write_json(args.output, {'resumed': True, 'preservedSamples': len(before['samples']),
                                    'before': before['completedOutputs'], 'after': after['completedOutputs'],
                                    'browserCompiled': json.loads((root / 'build-state.json').read_text(encoding='utf8')).get('browserCompiled') is True,
                                    'browserAcceptancePassed': False})
