import argparse
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path

from build_support import write_json


def compile_budget(started, current, save_minutes=30):
    if save_minutes not in (30, 50):
        raise ValueError('Expected a reviewed save budget of 30 or 50 minutes')
    if started.utcoffset() is None or current.utcoffset() is None or current < started:
        raise ValueError('Expected timezone-aware timestamps in chronological order')
    deadline = started + timedelta(minutes=360)
    # Fast: 2 progress + 20 pack + 5 upload + 3 reports, plus 5 runner minutes.
    compile_deadline = deadline - timedelta(minutes=save_minutes + 5)
    minutes = math.floor((compile_deadline - current).total_seconds() / 60)
    if minutes < 1:
        raise RuntimeError('No compile time remains within the job budget')
    return {'jobStartedAt': started.isoformat(), 'measuredAt': current.isoformat(),
            'jobDeadline': deadline.isoformat(), 'compileDeadline': compile_deadline.isoformat(),
            'jobMinutes': 360, 'saveMinutes': save_minutes, 'safetyMinutes': 5,
            'packMinutes': 20 if save_minutes == 30 else 35,
            'uploadMinutes': 5 if save_minutes == 30 else 10,
            'elapsedMinutes': (current - started).total_seconds() / 60,
            'compileMinutes': minutes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--started-at', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--save-minutes', type=int, choices=(30, 50), default=30)
    args = parser.parse_args()
    report = compile_budget(datetime.fromisoformat(args.started_at), datetime.now(timezone.utc), args.save_minutes)
    write_json(args.output, report)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as stream:
        stream.write(f'minutes={report["compileMinutes"]}\n')
        stream.write(f'pack_minutes={report["packMinutes"]}\n')
        stream.write(f'upload_minutes={report["uploadMinutes"]}\n')
    print(f'Compile budget: {report["compileMinutes"]} minutes', flush=True)
