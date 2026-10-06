"""Resource guards for bounded experiments; production build policy is unchanged."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from build_support import GIB, now, windows_memory, write_json
from measure_build import sample_resources


class FastSampler:
    def __init__(self, root):
        self.root = root
        self.previous = None

    def __call__(self):
        from ctypes import wintypes

        class Performance(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                ('commit', 'limit', 'peak', 'physical', 'available', 'cache',
                 'kernel', 'paged', 'nonpaged', 'pageSize')] + [
                (name, wintypes.DWORD) for name in ('handles', 'processes', 'threads')]

        info = Performance()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            raise OSError('GetPerformanceInfo failed')
        idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError('GetSystemTimes failed')
        current = (idle.value, kernel.value + user.value)
        cpu = None
        if self.previous and current[1] > self.previous[1]:
            cpu = 100 * (1 - (current[0] - self.previous[0]) / (current[1] - self.previous[1]))
        self.previous = current
        return {**windows_memory(), 'checkedAt': now(), 'cpuPercent': cpu,
                'committedBytes': info.commit * info.pageSize,
                'commitLimitBytes': info.limit * info.pageSize,
                'freeDiskBytes': shutil.disk_usage(self.root).free}


def guard_reason(sample, low_count):
    available = sample['availableMemoryBytes']
    headroom = sample['commitLimitBytes'] - sample['committedBytes']
    if sample['freeDiskBytes'] < 20 * GIB:
        return 'disk-below-20-GiB', low_count
    if min(available, headroom) < GIB:
        return 'memory-headroom-below-1-GiB', low_count
    low_count = low_count + 1 if min(available, headroom) < 2 * GIB else 0
    return ('memory-headroom-below-2-GiB-for-three-samples' if low_count >= 3 else None), low_count


def stop_tree(process):
    if process.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/pid', str(process.pid), '/t', '/f'],
                           capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.kill()
        process.wait(timeout=20)


def run_guarded(command, cwd, output, timeout_seconds, sampler=None, detailed=True):
    """One-second fail-closed guards plus independent ten-second CIM diagnostics."""
    output.mkdir(parents=True, exist_ok=True)
    sampler = sampler or FastSampler(cwd)
    report = {'startedAt': now(), 'status': 'running', 'sampleCount': 0,
              'sampleErrors': 0, 'detailedSampleErrors': 0, 'minAvailableMemoryBytes': None,
              'minCommitHeadroomBytes': None, 'maxCommittedBytes': None}
    write_json(output / 'performance.json', report)
    stopped = threading.Event()

    def collect_detail():
        with (output / 'resources.jsonl').open('w', encoding='utf8') as stream:
            while not stopped.is_set():
                try:
                    sample = sample_resources(cwd)
                except Exception as error:
                    report['detailedSampleErrors'] += 1
                    sample = {'checkedAt': now(), 'error': str(error)}
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                stopped.wait(10)

    worker = threading.Thread(target=collect_detail, daemon=True) if detailed else None
    process = None
    started = time.monotonic()
    try:
        # Guard before launch as well as throughout execution.
        initial = sampler()
        reason, low_count = guard_reason(initial, 0)
        if reason or low_count:
            raise RuntimeError('Insufficient starting resource headroom')
        with (output / 'compiler.log').open('w', encoding='utf8') as log, \
                (output / 'guards.jsonl').open('w', encoding='utf8') as stream:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if worker:
                worker.start()
            while True:
                try:
                    sample = sampler()
                except Exception:
                    report['sampleErrors'] += 1
                    raise
                report['sampleCount'] += 1
                headroom = sample['commitLimitBytes'] - sample['committedBytes']
                for key, value, reducer in (
                    ('minAvailableMemoryBytes', sample['availableMemoryBytes'], min),
                    ('minCommitHeadroomBytes', headroom, min),
                    ('maxCommittedBytes', sample['committedBytes'], max),
                ):
                    report[key] = value if report[key] is None else reducer(report[key], value)
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                reason, low_count = guard_reason(sample, low_count)
                if reason:
                    raise RuntimeError(reason)
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError('Benchmark trial timed out; incomplete work is not a speed result')
                code = process.poll()
                if code is not None:
                    report['exitCode'] = code
                    if code:
                        raise RuntimeError(f'Compiler exited with code {code}; see compiler.log')
                    break
                time.sleep(1)
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        # Excludes the detailed sampler's shutdown latency from measured execution.
        report['elapsedSeconds'] = time.monotonic() - started
        if process:
            stop_tree(process)
        stopped.set()
        if worker and worker.ident:
            worker.join(timeout=15)
            if worker.is_alive():
                report['detailedSampleErrors'] += 1
        report['samplingComplete'] = report['sampleCount'] > 0 and report['sampleErrors'] == 0
        report['finishedAt'] = now()
        write_json(output / 'performance.json', report)
    return report
