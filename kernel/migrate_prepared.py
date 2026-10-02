"""Explicit, fail-closed migration of the reviewed checkpoint."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from build_support import digest, now, validate_prepared, write_json
from source_overlays import HERE, targets, validate_sources


def split_overlays(legacy, overlays):
    """Only append overlays to the exact reviewed, unchanged patch prefix."""
    installed = legacy['inputs'].get('overlaySha256', {})
    names = [item['patch'] for item in overlays]
    if len(names) != len(set(names)) or list(installed) != names[:len(installed)]:
        raise RuntimeError('Existing overlays are not the reviewed patch prefix')
    applied, pending = overlays[:len(installed)], overlays[len(installed):]
    for item in applied:
        if digest(HERE / item['patch']) != installed[item['patch']]:
            raise RuntimeError(f'Previously applied overlay changed: {item["patch"]}')
    if set(targets(applied)) & set(targets(pending)):
        raise RuntimeError('Migration overlays must not overwrite previously reviewed sources')
    return applied, pending


def reviewed_overlay_series(overlays):
    legacy = json.loads((HERE / 'migration-lock.json').read_text(encoding='utf8'))
    applied, _ = split_overlays(legacy, overlays)
    return ''.join(item['seriesName'] + '\n' for item in applied)


def command_fingerprint(source, output):
    ninja = source / 'third_party/ninja/ninja.exe'
    with output.open('wb') as stream:
        subprocess.run([str(ninja), '-C', 'out/Default', '-t', 'commands', 'chrome'],
                       cwd=source, stdout=stream, check=True, timeout=120)
    commands = Counter()
    with output.open('rb') as stream:
        for command in stream:
            commands[hashlib.sha256(command.rstrip(b'\r\n')).hexdigest()] += 1
    return commands


def validate_legacy(prepared, identity, source, overlays, legacy):
    if prepared != legacy:
        raise RuntimeError('Prepared tree is not the reviewed legacy checkpoint')
    # Sources, existing patches, flags, Python and toolchain must agree. The
    # reviewed driver/migration metadata can change to append the new overlay.
    mutable = {'buildScriptSha256', 'overlayLockSha256', 'migrationScriptSha256',
               'migrationLockSha256', 'overlaySha256'}
    for key, value in legacy['inputs'].items():
        if key not in mutable and identity.get(key) != value:
            raise RuntimeError(f'Migration changed an unsupported input: {key}')
    applied, pending = split_overlays(legacy, overlays)
    for item in applied:
        name = item['patch']
        if identity.get('overlaySha256', {}).get(name) != legacy['inputs']['overlaySha256'][name]:
            raise RuntimeError(f'Migration changed an existing overlay identity: {name}')
    validate_prepared(prepared, legacy['inputs'], source, list(legacy['preparedFiles']))
    validate_sources(source, applied)
    validate_sources(source, pending, 'preparedBeforeSha256')
    return pending


def migrate(root, identity, overlays, prepared_files, regenerate):
    source = root / 'windows/build/src'
    marker = root / 'prepared.json'
    prepared = json.loads(marker.read_text(encoding='utf8'))
    legacy = json.loads((HERE / 'migration-lock.json').read_text(encoding='utf8'))
    report_path = root / 'migration.json'
    report = {'schemaVersion': 1, 'status': 'validating', 'startedAt': now(),
              'fromInputs': prepared.get('inputs'), 'toInputs': identity,
              'browserCompiled': False, 'browserAcceptancePassed': False}
    try:
        pending = validate_legacy(prepared, identity, source, overlays, legacy)
        for item in pending:
            subprocess.run(['git', 'apply', '--directory=build/src', '--check', str(HERE / item['patch'])],
                           cwd=root / 'windows', check=True, timeout=30)
        before = command_fingerprint(source, root / 'migration-commands-before.txt')
        unchanged = {name: value for name, value in legacy['preparedFiles'].items()
                     if name != 'out/Default/build.ninja'}
        report.update(status='applying', sourceBefore={name: digest(source / name) for name in targets(overlays)})
        write_json(report_path, report)
        for item in pending:
            subprocess.run(['git', 'apply', '--directory=build/src', str(HERE / item['patch'])],
                           cwd=root / 'windows', check=True, timeout=30)
        validate_sources(source, overlays)
        regenerate()
        for name, expected in unchanged.items():
            if digest(source / name) != expected:
                raise RuntimeError(f'GN regeneration changed a protected input: {name}')
        # Ninja response-file paths do not change when a GN dependency is added.
        # Requiring every command to stay identical also protects compilation
        # options of unrelated objects; source mtimes schedule the intended work.
        after = command_fingerprint(source, root / 'migration-commands-after.txt')
        if before != after:
            raise RuntimeError('GN migration changed commands outside the reviewed dependency update')
        for target in ('blink_common.dll', 'blink_core.dll', 'blink_modules.dll'):
            query = subprocess.run([str(source / 'third_party/ninja/ninja.exe'), '-C', 'out/Default',
                                    '-t', 'query', target], cwd=source, check=True,
                                   capture_output=True, text=True, timeout=30).stdout
            # Match an input line, not a substring in an unrelated output name.
            inputs = query.split('  input:', 1)[-1].split('  outputs:', 1)[0]
            if ('  input:' not in query or 'ungoogled_switches.dll.lib' not in
                    {line.strip().removeprefix('| ') for line in inputs.splitlines()}):
                raise RuntimeError(f'Regenerated {target} is missing its direct import library')
        report.update(status='migrated', finishedAt=now(), commandsPreserved=sum(before.values()),
                      appliedOverlays=[item['patch'] for item in pending],
                      sourceAfter={name: digest(source / name) for name in targets(overlays)},
                      oldBuildGraphSha256=legacy['preparedFiles']['out/Default/build.ninja'],
                      newBuildGraphSha256=digest(source / 'out/Default/build.ninja'))
        replacement = {**prepared, 'inputs': identity, 'preparedAt': now(),
                       'preparedFiles': {name: digest(source / name) for name in prepared_files},
                       'migration': report}
        validate_prepared(replacement, identity, source, prepared_files)
        write_json(report_path, report)
        write_json(marker, replacement)
    except BaseException as error:
        report.update(status='failed', finishedAt=now(), error=f'{type(error).__name__}: {error}')
        write_json(report_path, report)
        raise
