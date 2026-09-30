import argparse
import os
from pathlib import Path
import re
import subprocess

from build_support import now, validate_work_dir, workspace_lock, write_json


def pch_files(output):
    output = output.resolve()
    files = []
    for directory, directories, names in os.walk(output, followlinks=False):
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise RuntimeError('Linked directories are not allowed in the output tree')
        for name in names:
            if not name.lower().endswith('.pch'):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output):
                raise RuntimeError('Invalid precompiled header path')
            files.append(path)
    if len(files) > 1000:
        raise RuntimeError('Unexpected number of precompiled headers')
    return sorted(files)


def validate_pch(compiler, path, output):
    result = subprocess.run([str(compiler), '-cc1', '-verify-pch', str(path)], cwd=output,
                            capture_output=True, text=True, encoding='utf8', errors='replace', timeout=120)
    diagnostic = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return None
    # Rebuild via Ninja rather than bypassing Clang's input validation or changing
    # system header timestamps. Other failures require investigation.
    if (result.returncode == 1 and 'has been modified since the precompiled header' in diagnostic
            and 'mtime changed (was ' in diagnostic
            and ('please rebuild precompiled file' in diagnostic or 'please rebuild precompiled header' in diagnostic)):
        return diagnostic[:4096]
    raise RuntimeError(f'Unexpected PCH validation failure for {path.name}: {diagnostic[:4096]}')


def pch_producer(path, output):
    # GN emits the PCH as a side effect of this object, not as a Ninja output.
    # Removing only the .pch would leave its producer considered up to date.
    if not path.name.endswith('_cc.pch'):
        raise RuntimeError('Unsupported PCH producer naming')
    target = path.name.removesuffix('_cc.pch')
    producer = path.parent / target / 'precompile.cc.obj'
    build_file = path.parent / (target + '.ninja')
    for candidate in (producer, build_file):
        if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(output.resolve()):
            raise RuntimeError('Missing or unsafe PCH producer evidence')
    definition = build_file.read_text(encoding='utf8')
    relative = producer.relative_to(output).as_posix()
    rule = re.search(r'^build ' + re.escape(relative)
                     + r': cxx ../../build/precompile\.cc[^\n]*\n((?:[ \t]+[^\n]*\n)*)', definition, re.MULTILINE)
    if (not rule or '/Ycbuild/precompile.h' not in rule.group(1)
            or '/Fp' + path.relative_to(output).as_posix() + ' ' not in definition):
        raise RuntimeError('PCH is not paired with the expected GN producer')
    return producer


def recover(root, report_path):
    root = root.resolve()
    source = root / 'windows/build/src'
    output = source / 'out/Default'
    compiler = source / 'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe'
    if (not output.is_dir() or not compiler.is_file() or not (root / 'prepared.json').is_file()
            or not output.resolve().is_relative_to(root) or not compiler.resolve().is_relative_to(root)):
        raise RuntimeError('Expected a restored prepared workspace and its bundled compiler')
    report = {'schemaVersion': 1, 'startedAt': now(), 'status': 'validating',
              'validated': [], 'invalidated': [], 'browserCompiled': False}
    with workspace_lock(root):
        try:
            invalid = []
            for path in pch_files(output):
                diagnostic = validate_pch(compiler, path, output)
                relative = path.relative_to(root).as_posix()
                if diagnostic is None:
                    report['validated'].append(relative)
                else:
                    producer = pch_producer(path, output)
                    invalid.append((path, producer, {'path': relative, 'producer': producer.relative_to(root).as_posix(),
                                                    'reason': 'input-mtime-changed', 'diagnostic': diagnostic}))
            report['status'] = 'invalidating'
            write_json(report_path, report)
            for path, producer, evidence in invalid:
                for candidate in (producer, path):
                    if candidate.is_symlink() or not candidate.resolve().is_relative_to(output.resolve()):
                        raise RuntimeError('PCH removal escaped the output tree')
                    candidate.unlink()
                report['invalidated'].append(evidence)
                write_json(report_path, report)
            report.update(status='ready', finishedAt=now())
            write_json(report_path, report)
        except BaseException as error:
            report.update(status='failed', error=f'{type(error).__name__}: {error}', finishedAt=now())
            write_json(report_path, report)
            raise
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = validate_work_dir(args.work_dir, Path(__file__).resolve().parent.parent)
    report = recover(root, args.output)
    print(f"PCH validation: {len(report['validated'])} reusable, {len(report['invalidated'])} scheduled for rebuild", flush=True)
