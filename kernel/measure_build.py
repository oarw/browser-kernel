"""Collect bounded diagnostics without changing the locked compilation inputs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from build_support import now, write_json


SAMPLE = r'''
$ErrorActionPreference = 'Stop'
$cpu = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
$mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$processes = @(Get-Process -Name clang-cl,lld-link,ninja -ErrorAction SilentlyContinue | ForEach-Object {
    @{ name=$_.ProcessName; pid=$_.Id; workingSetBytes=$_.WorkingSet64; privateBytes=$_.PrivateMemorySize64; cpuSeconds=$_.CPU }
})
@{ cpuPercent=[double]$cpu.PercentProcessorTime; availableMemoryBytes=[long]$mem.AvailableBytes;
   committedBytes=[long]$mem.CommittedBytes; commitLimitBytes=[long]$mem.CommitLimit;
   pagesInputPerSecond=[double]$mem.PagesInputPersec; processes=$processes } | ConvertTo-Json -Depth 4 -Compress
'''


def sample_resources(root):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', SAMPLE],
                            capture_output=True, text=True, check=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    data = json.loads(result.stdout)
    data.update(checkedAt=now(), freeDiskBytes=shutil.disk_usage(root).free)
    return data


def run_measured(command, root, output, interval=30, sampler=sample_resources):
    output.mkdir(parents=True, exist_ok=True)
    report = {'startedAt': now(), 'sampleCount': 0, 'sampleErrors': 0,
              'minAvailableMemoryBytes': None, 'maxCommittedBytes': None, 'minFreeDiskBytes': None}
    source_output = root / 'windows/build/src/out/Default'
    graph = source_output / 'build.ninja'
    if graph.is_file():
        # Read only: do not rewrite the prepared graph or change its link pool.
        report['pools'] = re.findall(r'^pool ([^\n]+)\n\s+depth = (\d+)', graph.read_text(encoding='utf8'), re.M)
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        with (output / 'resources.jsonl').open('w', encoding='utf8') as stream:
            while process.poll() is None:
                tick = time.monotonic()
                try:
                    sample = sampler(root)
                    report['sampleCount'] += 1
                    for key, field, aggregate in (
                        ('minAvailableMemoryBytes', 'availableMemoryBytes', min),
                        ('maxCommittedBytes', 'committedBytes', max),
                        ('minFreeDiskBytes', 'freeDiskBytes', min),
                    ):
                        value = sample[field]
                        report[key] = value if report[key] is None else aggregate(report[key], value)
                except Exception as error:
                    report['sampleErrors'] += 1
                    sample = {'checkedAt': now(), 'error': str(error)}
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                try:
                    process.wait(timeout=max(0.01, interval - (time.monotonic() - tick)))
                except subprocess.TimeoutExpired:
                    pass
        report['exitCode'] = process.returncode
        return process.returncode
    finally:
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/pid', str(process.pid), '/t', '/f'], capture_output=True, timeout=20)
            else:
                process.kill()
            process.wait(timeout=20)
        report['finishedAt'] = now()
        report['samplingComplete'] = report['sampleCount'] > 0 and report['sampleErrors'] == 0
        # Completed edges only; this log does not measure unfinished work at timeout.
        log = source_output / '.ninja_log'
        if log.is_file() and log.stat().st_size <= 20 * 1024 * 1024:
            shutil.copyfile(log, output / 'ninja-log.txt')
        else:
            report['ninjaLogNotice'] = 'Missing or exceeds the 20 MiB evidence limit'
        write_json(output / 'performance.json', report)


def measured_command(root, output, jobs, minutes):
    if jobs == 4:
        return [sys.executable, '-u', str(Path(__file__).with_name('guarded_continuation.py')),
                '--work-dir', str(root), '--output', str(output), '--build-timeout-minutes', str(minutes)]
    if jobs not in (2, 3):
        raise ValueError('Expected two, three or four jobs')
    return [sys.executable, '-u', str(Path(__file__).with_name('build_windows.py')),
            '--work-dir', str(root), '--jobs', str(jobs),
            '--build-timeout-minutes', str(minutes), '--checkpoint-on-timeout']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, choices=(2, 3, 4), default=3)
    parser.add_argument('--build-timeout-minutes', type=int, required=True)
    args = parser.parse_args()
    command = measured_command(args.work_dir, args.output, args.jobs, args.build_timeout_minutes)
    raise SystemExit(run_measured(command, args.work_dir, args.output))
