import argparse
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path

from build_support import write_json


def compile_budget(started, current):
    if started.utcoffset() is None or current.utcoffset() is None or current < started:
        raise ValueError('Expected timezone-aware timestamps in chronological order')
    deadline = started + timedelta(minutes=360)
    # 35 minutes to pack, 10 to upload, 5 for reports, 5 for runner overhead.
    compile_deadline = deadline - timedelta(minutes=55)
    minutes = math.floor((compile_deadline - current).total_seconds() / 60)
    if minutes < 1:
        raise RuntimeError('No compile time remains within the job budget')
    return {'jobStartedAt': started.isoformat(), 'measuredAt': current.isoformat(),
            'jobDeadline': deadline.isoformat(), 'compileDeadline': compile_deadline.isoformat(),
            'jobMinutes': 360, 'saveMinutes': 50, 'safetyMinutes': 5,
            'elapsedMinutes': (current - started).total_seconds() / 60,
            'compileMinutes': minutes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--started-at', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = compile_budget(datetime.fromisoformat(args.started_at), datetime.now(timezone.utc))
    write_json(args.output, report)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
        stream.write(f'minutes={report["compileMinutes"]}\n')
    print(f'Compile budget: {report["compileMinutes"]} minutes', flush=True)
