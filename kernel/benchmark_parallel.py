"""Bounded ABBA experiment on a trusted checkpoint, never a production continuation."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import subprocess
import sys
import time

from benchmark_resources import FastSampler, run_guarded
from build_support import GIB, digest, now, preflight, validate_prepared, workspace_lock, write_json
from build_windows import PREPARED_FILES
from checkpoint import context, restore, verify_samples


HERE = Path(__file__).resolve().parent
ORDER = (3, 4, 4, 3)
LINKS = ('blink_core.dll', 'blink_modules.dll', 'blink_controller.dll')
POLICY = {
    'name': 'bounded-abba-v1', 'availableMemoryBytes': 10 * GIB,
    'commitHeadroomBytes': 10 * GIB, 'totalMemoryBytes': 15 * GIB,
    'freeDiskBytes': 40 * GIB, 'maxCxxTargets': 240, 'maxMixedCxxTargets': 12,
    'maxLinkTargets': 3, 'guardIntervalSeconds': 1, 'detailedIntervalSeconds': 10,
    'productionFourJobMinimumBytes': 12 * GIB,
    'productionSaveMinutes': 30, 'exportsCheckpoint': False,
}


def checked_output(output, name):
    path = PurePosixPath(name)
    if (not re.fullmatch(r'[A-Za-z0-9_./+-]+', name) or path.is_absolute()
            or '..' in path.parts or not path.parts):
        raise ValueError(f'Unsafe benchmark target: {name}')
    result = output.joinpath(*path.parts)
    if result.is_symlink() or not result.resolve().is_relative_to(output.resolve()):
        raise ValueError(f'Benchmark target escaped output directory: {name}')
    return result


def planned_edges(text):
    result = []
    for line in text.splitlines():
        match = re.match(r'^\[\d+/\d+\] (.+)$', line.strip())
        if match:
            result.append(match.group(1))
    return result


def audit_plan(text, cxx, links=()):
    """Reject hidden generators, PCH builds, extra compilation and extra linking."""
    expected = set(cxx) | set(links)
    seen = set()
    for edge in planned_edges(text):
        if edge.startswith('CXX '):
            name = edge[4:]
            if name not in cxx:
                raise RuntimeError(f'Unexpected C++ dependency: {edge}')
        elif edge.startswith('LINK(DLL) '):
            outputs = edge[len('LINK(DLL) '):].split()
            name = outputs[0]
            if name not in links or any(item not in (name, name + '.lib', name + '.pdb') for item in outputs):
                raise RuntimeError(f'Unexpected link dependency: {edge}')
        else:
            raise RuntimeError(f'Benchmark requires an already prepared dependency graph: {edge}')
        if name in seen:
            raise RuntimeError('Repeated benchmark edge')
        seen.add(name)
    if seen != expected:
        raise RuntimeError(f'Planned tasks differ: missing={sorted(expected - seen)}, extra={sorted(seen - expected)}')
    return sorted(seen)


def choose_targets(text, output, count=240):
    # Use a bounded prefix of pending work, spread over its output directories.
    groups = defaultdict(list)
    for edge in planned_edges(text):
        if edge.startswith('CXX obj/') and edge.endswith('.obj'):
            name = edge[4:]
            if not checked_output(output, name).exists() and 'precompile' not in name.lower():
                groups[str(PurePosixPath(name).parent)].append(name)
                if sum(map(len, groups.values())) >= count * 2:
                    break
    selected = []
    while len(selected) < count:
        progressed = False
        for names in groups.values():
            if names and len(selected) < count:
                selected.append(names.pop(0))
                progressed = True
        if not progressed:
            raise RuntimeError(f'Only {len(selected)} eligible pending C++ targets; require {count}')
    return selected


def audit_commands(text, count):
    commands = planned_edges(text)
    if len(commands) != count:
        raise RuntimeError('Verbose command count does not match the C++ task list')
    for command in commands:
        if (not re.match(r'^"?(?:\.\./)+third_party/llvm-build/Release\+Asserts/bin/clang-cl\.exe"? ', command)
                or not re.search(r'(?:^| )/c ', command)
                or re.search(r'(?:^| )/Yc', command)
                or re.search(r'[&|<>\r\n]', command)):
            raise RuntimeError('Expected a direct clang-cl compilation without PCH creation or shell chaining')


def experiment_preflight(root, jobs, output):
    if jobs not in (3, 4):
        raise ValueError('Only the reviewed 3/4 comparison is supported')
    # Reuse platform/toolchain/disk checks, but explicitly label this as a
    # separate limited-workload policy. Do not change build_support.py or
    # pretend the production four-job gate passed.
    report = preflight(root, 'build', 3, True)
    report.update(jobs=jobs, policy=POLICY, experimental=True)
    fast = FastSampler(root)()
    report['machine'].update(fast)
    if fast['commitLimitBytes'] - fast['committedBytes'] < POLICY['commitHeadroomBytes']:
        report['issues'].append('Experimental start requires 10 GiB commit headroom')
    report['ready'] = not report['issues']
    write_json(output, report)
    if not report['ready']:
        raise RuntimeError('Experiment preflight failed: ' + '; '.join(report['issues']))
    return report


def write_wrapper(output, targets):
    for name in targets:
        checked_output(output, name)
    path = output / 'benchmark-only.ninja'
    path.write_text('subninja build.ninja\nbuild __bounded_benchmark__: phony '
                    + ' '.join(targets) + '\n', encoding='utf8', newline='\n')


def ninja(source, *args):
    return [str(source / 'third_party/ninja/ninja.exe'), '-C', 'out/Default', *args]


def query(source, *args):
    result = subprocess.run(ninja(source, *args), cwd=source, capture_output=True,
                            text=True, encoding='utf8', errors='replace', timeout=120, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return result.stdout


def build_command(root, inspection, jobs):
    chain = inspection['toolchain']
    script = root / 'benchmark-step.cmd'
    source = root / 'windows/build/src'
    temporary = root / 'tmp'
    temporary.mkdir(exist_ok=True)
    lines = ['@echo off', f'call "{chain["vcvars"]}" {chain["sdk"]} -vcvars_ver=14.44 >nul',
             'if errorlevel 1 exit /b %errorlevel%', 'set DEPOT_TOOLS_WIN_TOOLCHAIN=0',
             'set GYP_MSVS_VERSION=2022', f'set "vs2022_install={chain["visualStudio"]}"',
             f'set "TEMP={temporary}"', f'set "TMP={temporary}"',
             subprocess.list2cmdline(ninja(source, '-j', str(jobs), '-f', 'benchmark-only.ninja',
                                           '__bounded_benchmark__')), 'exit /b %errorlevel%']
    script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf8')
    return ['cmd.exe', '/d', '/v:off', '/c', str(script)]


def remove_owned_workspace(root, owner, expected):
    # The only recursive deletion in this experiment: exact, fixed, owned path.
    if (str(root) != r'D:\fb-kernel' or root.is_symlink() or root.resolve() != root
            or json.loads(owner.read_text(encoding='utf8')) != expected
            or (root / 'build.lock').exists()):
        raise RuntimeError('Refusing to reset a workspace not owned by this benchmark')
    if root.exists():
        shutil.rmtree(root)


def reset_outputs(output, names):
    paths = [checked_output(output, name) for name in names]
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f'Cannot invalidate an unbuilt benchmark output: {path}')
    for path in paths:
        path.unlink()


def run_phase(root, trial, phase, jobs, cxx, links, prepared, timeout):
    source = root / 'windows/build/src'
    output = source / 'out/Default'
    evidence = trial / phase
    evidence.mkdir()
    write_wrapper(output, [*cxx, *links])
    plan = query(source, '-n', '-f', 'benchmark-only.ninja', '__bounded_benchmark__')
    (evidence / 'plan.txt').write_text(plan, encoding='utf8')
    audit_plan(plan, cxx, links)
    if not links:
        verbose = query(source, '-n', '-v', '-f', 'benchmark-only.ninja', '__bounded_benchmark__')
        (evidence / 'commands.txt').write_text(verbose, encoding='utf8')
        audit_commands(verbose, len(cxx))
    inspection = experiment_preflight(root, jobs, evidence / 'preflight.json')
    validate_prepared(prepared, prepared['inputs'], source, PREPARED_FILES)
    result = run_guarded(build_command(root, inspection, jobs), source, evidence, timeout)
    actual = (evidence / 'compiler.log').read_text(encoding='utf8', errors='replace')
    audit_plan(actual, cxx, links)
    for target in [*cxx, *links]:
        if not checked_output(output, target).is_file():
            raise RuntimeError(f'Build returned success without target: {target}')
    validate_prepared(prepared, prepared['inputs'], source, PREPARED_FILES)
    log = output / '.ninja_log'
    if log.stat().st_size <= 20 * 1024 * 1024:
        shutil.copyfile(log, evidence / 'ninja-log.txt')
    result.update(jobs=jobs, taskCount=len(cxx) + len(links), phase=phase)
    write_json(evidence / 'result.json', result)
    return result


def compare(trials):
    if len(trials) != 4 or tuple(item['jobs'] for item in trials) != ORDER:
        raise RuntimeError('A speed conclusion requires all four ABBA trials')
    result = {}
    for phase in ('cxx', 'mixed'):
        samples = {jobs: [item[phase] for item in trials if item['jobs'] == jobs] for jobs in (3, 4)}
        if any(item['status'] != 'passed' or not item['samplingComplete']
               or item['detailedSampleErrors'] for group in samples.values() for item in group):
            raise RuntimeError('Incomplete performance evidence cannot support adoption')
        times = {jobs: [item['elapsedSeconds'] for item in group] for jobs, group in samples.items()}
        median3, median4 = (statistics.median(times[jobs]) for jobs in (3, 4))
        # Both four-job trials must beat their adjacent three-job comparison.
        pairs = [1 - times[4][i] / times[3][i] for i in (0, 1)]
        result[phase] = {'secondsByJobs': times, 'medianTimeReduction': 1 - median4 / median3,
                         'pairTimeReductions': pairs, 'bothPairsFaster': min(pairs) > 0}
    result['worthContinuationTrial'] = (result['cxx']['medianTimeReduction'] >= .10
                                         and result['cxx']['bothPairsFaster']
                                         and result['mixed']['bothPairsFaster'])
    result['productionFourJobsEnabled'] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--trusted-source', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if (os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        parser.error('Run this bounded experiment in its main-branch Windows Actions workflow')
    root = Path(r'D:\fb-kernel')
    evidence = args.output.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    receipt = json.loads(args.trusted_source.read_text(encoding='utf8'))
    owner = root.parent / 'fb-kernel-benchmark-owner.json'
    expected_owner = {'workspace': str(root), 'context': context()}
    if root.exists() or owner.exists():
        raise RuntimeError('Benchmark requires a fresh runner and absent workspace')
    write_json(owner, expected_owner)
    report = {'schemaVersion': 1, 'startedAt': now(), 'status': 'running', 'context': context(),
              'source': receipt, 'policy': POLICY, 'order': ORDER, 'trials': [],
              'browserCompiled': False, 'browserAcceptancePassed': False}
    write_json(evidence / 'benchmark.json', report)
    started = time.monotonic()
    targets = None
    baseline = None
    try:
        for index, jobs in enumerate(ORDER, 1):
            if time.monotonic() - started > 140 * 60:
                raise TimeoutError('Insufficient time for another complete benchmark trial')
            trial = evidence / f'trial-{index}-j{jobs}'
            trial.mkdir()
            print(f'Trial {index}/4: restoring the original checkpoint for -j{jobs}', flush=True)
            preparation_started = time.monotonic()
            if index > 1:
                remove_owned_workspace(root, owner, expected_owner)
            manifest = restore(root, args.source.resolve(), args.manifest_sha256, receipt)
            with (trial / 'prepare.log').open('w', encoding='utf8') as log:
                for command in (
                    [sys.executable, str(HERE / 'pch_recovery.py'), '--work-dir', str(root), '--output', str(trial / 'pch.json')],
                    [sys.executable, str(HERE / 'download_source.py'), '--work-dir', str(root)],
                    [sys.executable, '-u', str(HERE / 'build_windows.py'), '--work-dir', str(root), '--jobs', '2',
                     '--prepare-only', '--migrate-prepared'],
                ):
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True,
                                   timeout=20 * 60, creationflags=subprocess.CREATE_NO_WINDOW)
            pch = json.loads((trial / 'pch.json').read_text(encoding='utf8'))
            if pch['invalidated']:
                raise RuntimeError('PCH rebuilds would contaminate this experiment; inspect the recovery report')
            prepared = json.loads((root / 'prepared.json').read_text(encoding='utf8'))
            identity = {'inputs': prepared['inputs'], 'preparedFiles': prepared['preparedFiles'],
                        'ninjaLogSha256': digest(root / 'windows/build/src/out/Default/.ninja_log')}
            if baseline is None:
                baseline = identity
                write_json(evidence / 'baseline.json', baseline)
            elif identity != baseline:
                raise RuntimeError('Trial did not restore the same prepared inputs and initial Ninja log')
            write_json(root / 'build-state.json', {**prepared, 'status': 'benchmark-only',
                       'browserCompiled': False, 'browserAcceptancePassed': False})
            source = root / 'windows/build/src'
            output = source / 'out/Default'
            if targets is None:
                dry = query(source, '-n', 'chrome')
                (evidence / 'remaining-plan.txt').write_text(dry, encoding='utf8')
                targets = choose_targets(dry, output)
                write_json(evidence / 'tasks.json', {'cxx': targets, 'mixedCxx': targets[:12], 'links': LINKS,
                           'sha256': hashlib.sha256('\n'.join(targets).encode()).hexdigest()})
            if any(checked_output(output, name).exists() for name in targets):
                raise RuntimeError('Expected identical absent C++ outputs at the start of every trial')
            entry = {'index': index, 'jobs': jobs, 'restoreAndPrepareSeconds': time.monotonic() - preparation_started}
            with workspace_lock(root):
                print(f'Trial {index}/4: {len(targets)} fixed C++ tasks, -j{jobs}', flush=True)
                entry['cxx'] = run_phase(root, trial, 'cxx', jobs, targets, (), prepared, 20 * 60)
                # Same post-C++ state each time; relink already-built Blink DLLs
                # alongside twelve identical C++ tasks. Original link pool stays 2.
                reset_outputs(output, [*targets[:12], *LINKS])
                print(f'Trial {index}/4: mixed C++ / Blink link qualification, -j{jobs}', flush=True)
                entry['mixed'] = run_phase(root, trial, 'mixed', jobs, targets[:12], LINKS, prepared, 10 * 60)
                verify_samples(root, manifest['samples'])
            report['trials'].append(entry)
            write_json(evidence / 'benchmark.json', report)
            print(f'Trial {index}/4 complete: C++ {entry["cxx"]["elapsedSeconds"]:.1f}s, '
                  f'mixed {entry["mixed"]["elapsedSeconds"]:.1f}s', flush=True)
        report.update(status='completed', comparison=compare(report['trials']))
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        report.update(finishedAt=now(), elapsedSeconds=time.monotonic() - started)
        write_json(evidence / 'benchmark.json', report)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as summary:
                summary.write('## Bounded 3/4-job experiment\n\n```json\n' + json.dumps(report, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
