"""Extract a verified experimental runtime from a completed build checkpoint.

Uses the built source's FILES.cfg plus component DLLs. Never ships the source
tree, compiler, object files or PDBs, and never labels packaging as acceptance.
"""
import argparse
import ast
import fnmatch
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

from build_support import digest, write_json
from checkpoint import inspect_archive, validate_links, safe_relative, MAX_ARCHIVE

PREFIX = 'windows/build/src/out/Default/'
CFG = 'windows/build/src/chrome/tools/build/win/FILES.cfg'
EXCLUDE = {'mini_installer.exe', 'setup.exe', 'chrome.packed.7z'}


def patterns_from_cfg(content):
    tree = ast.parse(content)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'FILES' for target in node.targets)]
    if len(assignments) != 1:
        raise RuntimeError('Expected a literal FILES configuration')
    specs = ast.literal_eval(assignments[0].value)
    return [spec['filename'].replace('\\', '/') for spec in specs
            if 'official' in spec['buildtype'] and 'archive' not in spec and ('arch' not in spec or '64bit' in spec['arch'])]


def runtime_members(names, patterns):
    selected = []
    for name in names:
        if not name.startswith(PREFIX) or name.endswith('/'):
            continue
        relative = name[len(PREFIX):]
        path = safe_relative(relative)
        if path.suffix.lower() in ('.pdb', '.obj', '.lib', '.exp') or relative in EXCLUDE:
            continue
        component = len(path.parts) == 1 and path.suffix.lower() == '.dll'
        matched = any((len(path.parts) == len(PurePosixPath(pattern).parts)
                       and all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(path.parts, PurePosixPath(pattern).parts)))
                      or relative.startswith(pattern.rstrip('/') + '/') for pattern in patterns)
        if component or matched:
            selected.append((name, relative))
    required = {'chrome.exe', 'chrome.dll', 'icudtl.dat', 'resources.pak', 'chrome_100_percent.pak', 'locales/en-US.pak'}
    missing = required - {relative for _, relative in selected}
    if missing:
        raise RuntimeError(f'Missing runtime files: {sorted(missing)}')
    return sorted(selected)


def package(source, output, manifest_sha, expected_context):
    source, output = source.resolve(), output.resolve()
    if output.exists():
        raise RuntimeError('Candidate output must be absent')
    if digest(source / 'manifest.json') != manifest_sha:
        raise RuntimeError('Manifest digest mismatch')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf8'))
    if (manifest.get('context') != expected_context or manifest.get('buildStatus') != 'built'
            or manifest.get('browserCompiled') is not True):
        raise RuntimeError('Expected a completed build from the selected source')
    archive = source / 'checkpoint.zip'
    if (archive.stat().st_size != manifest['archiveBytes'] or archive.stat().st_size > MAX_ARCHIVE
            or digest(archive) != manifest['archiveSha256']):
        raise RuntimeError('Checkpoint digest or size mismatch')
    if inspect_archive(archive, validate_links(manifest.get('links', []))) != (manifest['expandedBytes'], manifest['entries']):
        raise RuntimeError('Checkpoint entries differ from manifest')
    with zipfile.ZipFile(archive) as reader:
        result = json.loads(reader.read('build-result.json'))
        if (result.get('status') != 'built' or result.get('browserCompiled') is not True
                or result.get('inputs') != manifest['inputs']):
            raise RuntimeError('Build result disagrees with manifest')
        selected = runtime_members(reader.namelist(), patterns_from_cfg(reader.read(CFG).decode('utf8')))
        runtime = output / 'runtime'
        runtime.mkdir(parents=True)
        for name, relative in selected:
            destination = runtime / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with reader.open(name) as src, destination.open('wb') as dst:
                shutil.copyfileobj(src, dst)
        if digest(runtime / 'chrome.exe') != result['executableSha256']:
            raise RuntimeError('Executable differs from completed build')
        licenses = runtime / 'licenses'
        licenses.mkdir()
        for path in (Path(__file__).parent / 'third-party-licenses').glob('*.txt'):
            shutil.copyfile(path, licenses / path.name)
        (licenses / 'chromium-source-LICENSE.txt').write_bytes(reader.read('windows/build/src/LICENSE'))
        # Component experiments can contain a placeholder about:credits page.
        # Preserve the actual source license texts and dependency metadata too.
        license_count = 0
        for name in reader.namelist():
            if not name.startswith('windows/build/src/') or name.endswith('/'):
                continue
            relative = name.removeprefix('windows/build/src/')
            if relative.startswith(('out/', '.git/')):
                continue
            base = PurePosixPath(relative).name.upper()
            if base == 'README.CHROMIUM' or base.startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'COPYRIGHT')):
                target = licenses / 'source' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with reader.open(name) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                license_count += 1
        files = [{'path': path.relative_to(runtime).as_posix(), 'bytes': path.stat().st_size, 'sha256': digest(path)}
                 for path in sorted(runtime.rglob('*')) if path.is_file()]
        report = {'schemaVersion': 1, 'kind': 'experimental-component-build', 'source': expected_context,
                  'checkpointManifestSha256': manifest_sha, 'checkpointArchiveSha256': manifest['archiveSha256'],
                  'executableSha256': result['executableSha256'], 'inputs': result['inputs'],
                  'browserCompiled': True, 'browserAcceptancePassed': False,
                  'runtimeBytes': sum(item['bytes'] for item in files), 'files': files,
                  'licenseInventoryFiles': license_count, 'distributionReviewPassed': False}
        write_json(runtime / 'build-provenance.json', report)
        destination = output / 'browser-kernel-windows-x64.zip'
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as writer:
            for path in sorted(runtime.rglob('*')):
                if path.is_file():
                    info = zipfile.ZipInfo(path.relative_to(runtime).as_posix(), (2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with path.open('rb') as src, writer.open(info, 'w') as dst:
                        shutil.copyfileobj(src, dst)
        report.update(archiveBytes=destination.stat().st_size, archiveSha256=digest(destination))
        write_json(output / 'package-report.json', report)
        (output / 'SHA256SUMS.txt').write_text(f"{report['archiveSha256']}  {destination.name}\n", encoding='utf8')
        return {key: value for key, value in report.items() if key not in ('files', 'inputs')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--repository', default='oarw/browser-kernel')
    parser.add_argument('--commit', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--attempt', default='1')
    args = parser.parse_args()
    context = dict(GITHUB_REPOSITORY=args.repository, GITHUB_SHA=args.commit,
                   GITHUB_RUN_ID=args.run_id, GITHUB_RUN_ATTEMPT=args.attempt)
    print(json.dumps(package(args.source, args.output, args.manifest_sha256, context), indent=2))
