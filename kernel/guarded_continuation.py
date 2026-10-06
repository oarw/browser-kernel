"""Explicit four-job continuation policy with checkpoint-compatible inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from benchmark_resources import FastSampler, guard_reason, stop_tree
from build_support import GIB, digest, now, preflight, resource_issues, validate_prepared, validate_work_dir, workspace_lock, write_json
from build_windows import HERE, LOCK, OVERLAYS, PREPARED_FILES, experimental_flags, git
from source_overlays import overlay_identity, validate_sources


POLICY = {
    'name': 'guarded-four-job-continuation-v1', 'jobs': 4,
    'availableMemoryBytes': 10 * GIB, 'commitHeadroomBytes': 10 * GIB,
    'totalMemoryBytes': 15 * GIB, 'freeDiskBytes': 40 * GIB,
    'guardIntervalSeconds': 1, 'stopImmediatelyBelowBytes': GIB,
    'stopAfterThreeSamplesBelowBytes': 2 * GIB, 'stopDiskBelowBytes': 20 * GIB,
    'ordinaryFourJobMinimumBytes': 12 * GIB,
}


def starting_issues(sample):
    measured = {**sample, 'commitHeadroomBytes': sample['commitLimitBytes'] - sample['committedBytes']}
    return resource_issues(measured, {key: POLICY[key] for key in
        ('availableMemoryBytes', 'commitHeadroomBytes', 'totalMemoryBytes', 'freeDiskBytes')})


def prepared_inputs(root, platform_report):
    """Recompute the existing identity; never rewrite it to accept new inputs."""
    windows = root / 'windows'
    core = windows / 'ungoogled-chromium'
    for path, spec, allowed in (
        (windows, LOCK['windows'], {'flags.windows.gn', 'ungoogled-chromium'}),
        (core, LOCK['fingerprint'], {'patches/series'}),
    ):
        if (not (path / '.git').exists()
                or Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path.resolve()
                or git(path, 'remote', 'get-url', 'origin') != spec['repository']
                or git(path, 'rev-parse', 'HEAD') != spec['commit']
                or set(git(path, 'diff', 'HEAD', '--name-only').splitlines()) - allowed):
            raise RuntimeError('Prepared upstream checkout no longer matches the source lock')
    flags = experimental_flags(git(windows, 'show', 'HEAD:flags.windows.gn') + '\n')
    if (windows / 'flags.windows.gn').read_text(encoding='utf8') != flags:
        raise RuntimeError('Prepared GN flags changed')
    return {
        'lockSha256': digest(HERE / 'source-lock.json'), 'patchSha256': digest(HERE / LOCK['patch']),
        'flagsSha256': hashlib.sha256(flags.encode()).hexdigest(),
        'buildScriptSha256': digest(HERE / 'build_windows.py'),
        'supportScriptSha256': digest(HERE / 'build_support.py'),
        'requirementsSha256': digest(HERE / 'requirements.txt'),
        'pythonVersion': platform_report['python']['version'], 'toolchain': platform_report['toolchain'],
        **overlay_identity(OVERLAYS),
    }


def compile_command(root, toolchain):
    # Same vcvars and Ninja command as build_windows.py; only -j is four.
    script = root / 'build-four-step.cmd'
    lines = ['@echo off', f'call "{toolchain["vcvars"]}" {toolchain["sdk"]} -vcvars_ver=14.44 >nul',
             'if errorlevel 1 exit /b %errorlevel%', 'set DEPOT_TOOLS_WIN_TOOLCHAIN=0',
             'set GYP_MSVS_VERSION=2022', f'set "vs2022_install={toolchain["visualStudio"]}"',
             r'third_party\ninja\ninja.exe -j 4 -C out\Default chrome', 'exit /b %errorlevel%']
    script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf8')
    return ['cmd.exe', '/d', '/v:off', '/c', str(script)]


def run_compiler(command, cwd, output, timeout, sampler, interval=1):
    """Stop only this compiler tree, then permit the normal checkpoint protocol."""
    output.mkdir(parents=True, exist_ok=True)
    report = {'startedAt': now(), 'status': 'running', 'policy': POLICY,
              'sampleCount': 0, 'sampleErrors': 0, 'minAvailableMemoryBytes': None,
              'minCommitHeadroomBytes': None, 'minFreeDiskBytes': None}
    process = None
    started = time.monotonic()
    low_count = 0
    try:
        try:
            initial = sampler()
        except Exception:
            report['sampleErrors'] += 1
            raise
        issues = starting_issues(initial)
        if issues:
            raise RuntimeError('Four-job starting resources: ' + '; '.join(issues))
        with (output / 'guards.jsonl').open('w', encoding='utf8') as stream:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=sys.stdout, stderr=sys.stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while True:
                code = process.poll()
                if code is not None:
                    report['compilerExitCode'] = code
                    if code:
                        raise subprocess.CalledProcessError(code, command)
                    report['status'] = 'completed'
                    break
                try:
                    sample = sampler()
                except Exception:
                    report['sampleErrors'] += 1
                    raise
                report['sampleCount'] += 1
                for key, value in (
                    ('minAvailableMemoryBytes', sample['availableMemoryBytes']),
                    ('minCommitHeadroomBytes', sample['commitLimitBytes'] - sample['committedBytes']),
                    ('minFreeDiskBytes', sample['freeDiskBytes']),
                ):
                    report[key] = value if report[key] is None else min(report[key], value)
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                reason, low_count = guard_reason(sample, low_count)
                if not reason and time.monotonic() - started >= timeout:
                    reason = 'compile-budget-ended'
                if reason:
                    # Do not hide an independently completed compiler failure.
                    code = process.poll()
                    if code is not None:
                        report['compilerExitCode'] = code
                        if code:
                            raise subprocess.CalledProcessError(code, command)
                        report['status'] = 'completed'
                    else:
                        stopped = stop_tree(process)
                        if not stopped:
                            report['compilerExitCode'] = process.returncode
                            if process.returncode:
                                raise subprocess.CalledProcessError(process.returncode, command)
                            report['status'] = 'completed'
                        else:
                            report.update(status='checkpoint-ready', stopReason=reason,
                                          terminatedCompilerExitCode=process.returncode)
                    break
                try:
                    process.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
        return report
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        try:
            if process:
                stop_tree(process)
        finally:
            report.update(finishedAt=now(), elapsedSeconds=time.monotonic() - started)
            write_json(output / 'guard-report.json', report)


def continue_build(root, output, minutes):
    state = {'schemaVersion': 1, 'status': 'running', 'phase': 'preflight', 'startedAt': now(),
             'browserCompiled': False, 'browserAcceptancePassed': False,
             'execution': {'jobs': 4, 'policy': POLICY, 'launcherSha256': digest(Path(__file__)),
                           'guardModuleSha256': digest(HERE / 'benchmark_resources.py')}}
    try:
        # Full platform/toolchain/dependency checks, followed by this explicit
        # build policy. The ordinary unguarded 12 GiB gate is not relabelled passed.
        platform_report = preflight(root, 'prepare', 4, True)
        sampler = FastSampler(root)
        machine = sampler()
        issues = platform_report['issues'] + starting_issues(machine)
        if platform_report['machine'].get('cpus', 0) < 4:
            issues.append('Four-job continuation requires at least four logical CPUs')
        inspection = {'phase': 'build', 'jobs': 4, 'policy': POLICY,
                      'platformPreflight': platform_report, 'machine': machine,
                      'issues': issues, 'ready': not issues}
        write_json(root / 'preflight-four-jobs.json', inspection)
        if issues:
            raise RuntimeError('Four-job preflight failed: ' + '; '.join(issues))
        prepared = json.loads((root / 'prepared.json').read_text(encoding='utf8'))
        identity = prepared_inputs(root, platform_report)
        source = root / 'windows/build/src'
        validate_prepared(prepared, identity, source, PREPARED_FILES)
        validate_sources(source, OVERLAYS)
        previous = json.loads((root / 'build-state.json').read_text(encoding='utf8'))
        if previous.get('status') != 'prepared' or previous.get('inputs') != identity:
            raise RuntimeError('Run the normal prepare-only validation before four-job continuation')
        state.update(inputs=identity, phase='compiling')
        write_json(root / 'build-state.json', state)
        write_json(root / 'build-result.json', state)
        temp = root / 'tmp'
        temp.mkdir(exist_ok=True)
        os.environ.update(TEMP=str(temp), TMP=str(temp), PYTHONUTF8='1', DEPOT_TOOLS_WIN_TOOLCHAIN='0')
        command = compile_command(root, platform_report['toolchain'])
        print(f'Continuing chrome with four jobs under {POLICY["name"]}; budget {minutes} minutes', flush=True)
        outcome = run_compiler(command, source, output, minutes * 60, sampler)
        validate_prepared(prepared, identity, source, PREPARED_FILES)
        if outcome['status'] == 'checkpoint-ready':
            state.update(status='checkpoint-ready', stopReason=outcome['stopReason'], finishedAt=now())
            result = state
            code = 124
        else:
            executable = source / 'out/Default/chrome.exe'
            if not executable.is_file():
                raise RuntimeError('Ninja returned success without chrome.exe')
            state.update(status='built', phase='built', browserCompiled=True, finishedAt=now())
            result = {**prepared, **state, 'builtAt': now(), 'executable': str(executable),
                      'executableSha256': digest(executable), 'kind': 'experimental-component-build'}
            code = 0
        write_json(root / 'build-result.json', result)
        write_json(root / 'build-state.json', state)
        print(f'Four-job continuation ended: {state["status"]}; {state.get("stopReason", "chrome built")}', flush=True)
        return code
    except BaseException as error:
        state.update(status='failed', error=f'{type(error).__name__}: {error}', finishedAt=now())
        write_json(root / 'build-result.json', state)
        write_json(root / 'build-state.json', state)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build-timeout-minutes', required=True, type=int)
    args = parser.parse_args()
    if sys.platform != 'win32' or not 1 <= args.build_timeout_minutes <= 330:
        parser.error('Windows and a compile budget between 1 and 330 minutes are required')
    root = validate_work_dir(args.work_dir, HERE.parent)
    with workspace_lock(root):
        raise SystemExit(continue_build(root, args.output, args.build_timeout_minutes))
