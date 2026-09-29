"""Inspect the experimental kernel build host without starting a build."""
import argparse
import json
from pathlib import Path
from build_support import preflight, validate_work_dir, write_json

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--phase', choices=['prepare', 'build'], default='prepare')
    parser.add_argument('--jobs', type=int, choices=range(1, 9), default=2)
    parser.add_argument('--output', type=Path, default=Path('release/native-preflight/report.json'))
    args = parser.parse_args()
    try:
        root = validate_work_dir(args.work_dir, Path(__file__).resolve().parent.parent)
    except ValueError as error:
        parser.error(str(error))
    report = preflight(root, args.phase, args.jobs, (root / 'prepared.json').is_file())
    write_json(args.output, report)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['ready'] else 1)
