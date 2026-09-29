"""Build a local experimental kernel using pinned upstream Windows packaging.

This produces a component build for runtime verification, not a production release.
No user profiles or installed product kernels are modified.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from build_support import digest, now, preflight, run_windows_process, validate_prepared, validate_work_dir, workspace_lock, write_json

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / 'source-lock.json').read_text(encoding='utf8'))
PREPARED_FILES = [LOCK['targetFile'], 'out/Default/args.gn', 'out/Default/build.ninja',
                  'out/Default/gn.exe', 'third_party/rust-toolchain/bin/bindgen.exe']


def experimental_flags(original):
    overrides = {'is_component_build': 'true', 'is_official_build': 'false', 'is_debug': 'false',
                 'chrome_pgo_phase': '0', 'use_thin_lto': 'false', 'symbol_level': '0',
                 'blink_symbol_level': '0', 'v8_symbol_level': '0'}
    lines = []
    seen = set()
    for line in original.splitlines():
        match = re.match(r'^\s*(\w+)\s*=', line)
        key = match.group(1) if match else None
        if key in overrides:
            if key in seen:
                raise RuntimeError(f'Duplicate GN setting: {key}')
            seen.add(key)
            line = f'{key}={overrides[key]}'
        lines.append(line)
    lines.extend(f'{key}={value}' for key, value in overrides.items() if key not in seen)
    return '\n'.join(lines).rstrip() + '\n'


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def git(directory, *args):
    return run(['git', '-C', directory, *args], capture_output=True, text=True, encoding='utf8').stdout.strip()


def checkout(path, spec, allowed_changes):
    # A fresh parent checkout creates an empty submodule directory. Git commands
    # run there otherwise walk up to the parent repository and inspect its HEAD.
    if not path.exists() or (path.is_dir() and not any(path.iterdir())):
        run(['git', 'clone', '--depth', '1', '--branch', spec['ref'], '--single-branch', spec['repository'], path])
    if not (path / '.git').exists() or Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path.resolve():
        raise RuntimeError(f'Expected a dedicated Git checkout; refusing to reuse unrelated files: {path}')
    if git(path, 'remote', 'get-url', 'origin') != spec['repository'] or git(path, 'rev-parse', 'HEAD') != spec['commit']:
        raise RuntimeError(f'Checkout does not match locked source: {path}')
    changed = set(git(path, 'diff', 'HEAD', '--name-only').splitlines())
    if changed - set(allowed_changes):
        raise RuntimeError(f'Unexpected upstream source modifications: {sorted(changed - set(allowed_changes))}')


class StageComplete(Exception):
    pass


def ensure_ready(root, phase, jobs, prepared):
    report = preflight(root, phase, jobs, prepared)
    write_json(root / f'preflight-{phase}.json', report)
    if not report['ready']:
        raise RuntimeError('Build preflight failed: ' + '; '.join(report['issues']))
    return report


def build(args, root):
    state_path = root / 'build-state.json'
    state = {'schemaVersion': 1, 'startedAt': now(), 'status': 'running', 'phase': 'preflight',
             'browserCompiled': False, 'browserAcceptancePassed': False}
    write_json(state_path, state)

    def phase(name):
        state.update(phase=name, updatedAt=now())
        write_json(state_path, state)

    try:
        marker = root / 'prepared.json'
        inspection = ensure_ready(root, 'prepare' if args.prepare_only or not marker.exists() else 'build', args.jobs, marker.exists())
        toolchain = inspection['toolchain']
        phase('sources')
        windows = root / 'windows'
        core = windows / 'ungoogled-chromium'
        overlay_name = 'extra/fingerprint/999-fingerbrowser-text-metrics.patch'
        # The fingerprint fork deliberately differs from the parent's gitlink;
        # validate that nested checkout against its own lock immediately below.
        checkout(windows, LOCK['windows'], ['flags.windows.gn', 'ungoogled-chromium'])
        checkout(core, LOCK['fingerprint'], ['patches/series'])
        source = windows / 'build/src'
        archive = root / 'downloads' / f"chromium-{LOCK['chromiumVersion']}-lite.tar.xz"
        if not archive.is_file() or archive.stat().st_size != LOCK['sourceArchive']['size'] or digest(archive) != LOCK['sourceArchive']['sha256']:
            raise RuntimeError(f'Download and verify the locked source archive at {archive} before building')
        cache = windows / 'build/download_cache'
        cache.mkdir(parents=True, exist_ok=True)
        cached_archive = cache / archive.name
        if not cached_archive.exists():
            try:
                os.link(archive, cached_archive)
            except OSError:
                shutil.copy2(archive, cached_archive)
        elif digest(cached_archive) != LOCK['sourceArchive']['sha256']:
            raise RuntimeError('Cached Chromium source archive differs from the lock')

        patch = HERE / LOCK['patch']
        overlay = core / 'patches' / overlay_name
        if overlay.exists() and overlay.read_bytes() != patch.read_bytes():
            raise RuntimeError('Existing overlay differs; use a new work directory')
        original_series = git(core, 'show', 'HEAD:patches/series') + '\n'
        series_path = core / 'patches/series'
        expected_series = original_series.rstrip() + '\n' + overlay_name + '\n'
        if series_path.read_text(encoding='utf8') not in [original_series, expected_series]:
            raise RuntimeError('Refusing to overwrite unrelated upstream patch series changes')
        original_flags = git(windows, 'show', 'HEAD:flags.windows.gn') + '\n'
        flags = experimental_flags(original_flags)
        flag_path = windows / 'flags.windows.gn'
        if flag_path.read_text(encoding='utf8') not in [original_flags, flags]:
            raise RuntimeError('Refusing to overwrite unrelated Windows build flag changes')

        identity = {'lockSha256': digest(HERE / 'source-lock.json'), 'patchSha256': digest(patch),
                    'flagsSha256': hashlib.sha256(flags.encode()).hexdigest(),
                    'buildScriptSha256': digest(HERE / 'build_windows.py'),
                    'supportScriptSha256': digest(HERE / 'build_support.py'),
                    'requirementsSha256': digest(HERE / 'requirements.txt'),
                    'pythonVersion': inspection['python']['version'], 'toolchain': toolchain}
        state['inputs'] = identity
        if marker.exists():
            prepared = json.loads(marker.read_text(encoding='utf8'))
            validate_prepared(prepared, identity, source, PREPARED_FILES)
        elif source.exists() and any(source.iterdir()):
            raise RuntimeError('Incomplete source preparation: inspect build-state.json/logs and use a new work directory; no source tree was deleted')
        overlay.write_bytes(patch.read_bytes())
        series_path.write_text(expected_series, encoding='utf8', newline='\n')
        flag_path.write_text(flags, encoding='utf8', newline='\n')
        temp = root / 'tmp'
        temp.mkdir(exist_ok=True)
        os.environ.update(TEMP=str(temp), TMP=str(temp), PYTHONUTF8='1', DEPOT_TOOLS_WIN_TOOLCHAIN='0')

        def build_process(*command, timeout=None, **kwargs):
            script = root / 'build-step.cmd'
            lines = ['@echo off', f'call "{toolchain["vcvars"]}" {toolchain["sdk"]} -vcvars_ver=14.44 >nul',
                     'if errorlevel 1 exit /b %errorlevel%', 'set DEPOT_TOOLS_WIN_TOOLCHAIN=0',
                     'set GYP_MSVS_VERSION=2022', f'set "vs2022_install={toolchain["visualStudio"]}"',
                     subprocess.list2cmdline([str(value) for value in command]), 'exit /b %errorlevel%']
            script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf8')
            print('Running:', subprocess.list2cmdline([str(value) for value in command]), flush=True)
            run_windows_process(['cmd.exe', '/d', '/v:off', '/c', str(script)], timeout=timeout, **kwargs)

        if not marker.exists():
            phase('preparing')
            spec = importlib.util.spec_from_file_location('windows_recipe', windows / 'build.py')
            recipe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(recipe)

            def finish_preparation(*command, timeout):
                actual = hashlib.sha256((source / LOCK['targetFile']).read_text(encoding='utf8').encode()).hexdigest()
                if actual != LOCK['patchedSha256']:
                    raise RuntimeError('Final native source does not match the reviewed patched file')
                prepared = {'schemaVersion': 1, 'inputs': identity, 'preparedAt': now(), 'sourceSha256': actual,
                            'preparedFiles': {name: digest(source / name) for name in PREPARED_FILES}}
                write_json(marker, prepared)
                raise StageComplete()

            recipe._run_build_process = build_process
            recipe._run_build_process_timeout = finish_preparation
            os.chdir(windows)
            sys.argv = [str(windows / 'build.py'), '--tarball', '--ci', '--7z-path', inspection['tools']['7z'], '-j', str(args.jobs)]
            try:
                recipe.main()
            except StageComplete:
                pass
            except SystemExit as error:
                raise RuntimeError(f'Upstream recipe exited before preparation completed: {error.code}') from error
            if not marker.exists():
                raise RuntimeError('Upstream recipe returned without complete preparation')
        prepared = json.loads(marker.read_text(encoding='utf8'))
        validate_prepared(prepared, identity, source, PREPARED_FILES)
        if args.prepare_only:
            state.update(status='prepared', phase='prepared', finishedAt=now())
            write_json(state_path, state)
            print('Native source, GN and bindgen preparation completed', flush=True)
            return

        phase('compile-preflight')
        ensure_ready(root, 'build', args.jobs, True)
        phase('compiling')
        write_json(root / 'build-result.json', {**state, 'status': 'running'})
        try:
            build_process('third_party\\ninja\\ninja.exe', '-j', str(args.jobs), '-C', 'out\\Default', 'chrome',
                          cwd=source, timeout=args.build_timeout_minutes * 60)
            executable = source / 'out/Default/chrome.exe'
            result = {**prepared, 'status': 'built', 'browserCompiled': True, 'browserAcceptancePassed': False,
                      'builtAt': now(), 'executable': str(executable), 'executableSha256': digest(executable),
                      'kind': 'experimental-component-build'}
            write_json(root / 'build-result.json', result)
        except BaseException as error:
            write_json(root / 'build-result.json', {**state, 'status': 'failed', 'error': str(error), 'finishedAt': now()})
            raise
        state.update(status='built', phase='built', browserCompiled=True, finishedAt=now())
        write_json(state_path, state)
    except BaseException as error:
        if (isinstance(error, subprocess.TimeoutExpired) and state['phase'] == 'compiling'
                and getattr(args, 'checkpoint_on_timeout', False)):
            state.update(status='checkpoint-ready', finishedAt=now())
            write_json(root / 'build-result.json', state)
            write_json(state_path, state)
            return 124
        state.update(status='failed', error=f'{type(error).__name__}: {error}', finishedAt=now())
        write_json(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--jobs', type=int, choices=range(1, 9), default=2)
    parser.add_argument('--build-timeout-minutes', type=int, default=210)
    parser.add_argument('--checkpoint-on-timeout', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Windows is required')
    if not 1 <= args.build_timeout_minutes <= 1440:
        parser.error('--build-timeout-minutes must be between 1 and 1440')
    try:
        root = validate_work_dir(args.work_dir, HERE.parent)
    except ValueError as error:
        parser.error(str(error))
    root.mkdir(parents=True, exist_ok=True)
    with workspace_lock(root):
        return build(args, root)


if __name__ == '__main__':
    raise SystemExit(main())
