import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from build_support import preflight, write_json


def choose_volume(volumes):
    candidates = [v for v in volumes if v.get('FileSystem') == 'NTFS'
                  and v.get('DeviceID') in ('C:', 'D:') and isinstance(v.get('FreeSpace'), int)]
    if not candidates:
        raise RuntimeError('No supported fixed NTFS volume')
    return max(candidates, key=lambda v: v['FreeSpace'])


def collect(output):
    if sys.platform != 'win32':
        raise RuntimeError('Windows required')
    command = "@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' | Select-Object DeviceID,FileSystem,Size,FreeSpace) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            check=True, capture_output=True, text=True, timeout=30)
    volumes = json.loads(result.stdout)
    if isinstance(volumes, dict):
        volumes = [volumes]
    volume = choose_volume(volumes)
    root = Path(volume['DeviceID'] + '/fb-kernel')
    report = preflight(root, 'prepare', 1)
    report['volumes'] = volumes
    report['runner'] = {k: os.environ.get(k) for k in ('ImageOS', 'ImageVersion', 'RUNNER_OS', 'RUNNER_ARCH')}
    report['browserCompiled'] = False
    report['browserAcceptancePassed'] = False
    write_json(output, report)
    print(json.dumps({'ready': report['ready'], 'workDirectory': str(root), 'issues': report['issues']}))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
            stream.write(f"ready={str(report['ready']).lower()}\n")
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    collect(parser.parse_args().output)
