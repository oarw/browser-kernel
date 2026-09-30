"""Explicit, fail-closed migration of the reviewed first checkpoint."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from build_support import digest, now, validate_prepared, write_json
from source_overlays import HERE, targets, validate_sources


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
    # Only the build driver and newly added overlay/migration implementation may
    # differ. Sources, original patch, flags, Python and toolchain must agree.
    for key, value in legacy['inputs'].items():
        if key != 'buildScriptSha256' and identity.get(key) != value:
            raise RuntimeError(f'Migration changed an unsupported input: {key}')
    validate_prepared(prepared, legacy['inputs'], source, list(legacy['preparedFiles']))
    validate_sources(source, overlays, 'postUpstreamSha256')


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
        validate_legacy(prepared, identity, source, overlays, legacy)
        for item in overlays:
            subprocess.run(['git', 'apply', '--check', str(HERE / item['patch'])],
                           cwd=source, check=True, timeout=30)
        before = command_fingerprint(source, root / 'migration-commands-before.txt')
        unchanged = {name: value for name, value in legacy['preparedFiles'].items()
                     if name != 'out/Default/build.ninja'}
        report.update(status='applying', sourceBefore={name: digest(source / name) for name in targets(overlays)})
        write_json(report_path, report)
        for item in overlays:
            subprocess.run(['git', 'apply', str(HERE / item['patch'])], cwd=source, check=True, timeout=30)
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
        query = subprocess.run([str(source / 'third_party/ninja/ninja.exe'), '-C', 'out/Default',
                                '-t', 'query', 'blink_common.dll'], cwd=source, check=True,
                               capture_output=True, text=True, timeout=30).stdout
        if 'ungoogled_switches.dll.lib' not in query:
            raise RuntimeError('Regenerated Blink target is missing its direct import library')
        report.update(status='migrated', finishedAt=now(), commandsPreserved=sum(before.values()),
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
