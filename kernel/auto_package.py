"""Dispatch packaging only for a completed, trusted build (never a checkpoint)."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REPO = 'oarw/browser-kernel'


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPO}/{path}'], text=True))


def completed(manifest, state, run):
    context = dict(GITHUB_REPOSITORY=REPO, GITHUB_SHA=run['head_sha'],
                   GITHUB_RUN_ID=str(run['id']), GITHUB_RUN_ATTEMPT=str(run['run_attempt']))
    if manifest.get('context') != context:
        raise ValueError('Manifest context differs from completed build run')
    if manifest.get('buildStatus') == 'checkpoint-ready' and state.get('status') == 'checkpoint-ready':
        return False
    if (manifest.get('buildStatus') != 'built' or manifest.get('browserCompiled') is not True
            or state.get('status') != 'built' or state.get('browserCompiled') is not True):
        raise ValueError('Build state and manifest do not confirm a compiled browser')
    return True


def dispatch(run_id, output):
    if not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Invalid run ID')
    repository, run = api(''), api(f'actions/runs/{run_id}')
    if (repository['visibility'] != 'public' or repository['default_branch'] != 'main'
            or run['path'] != '.github/workflows/build-kernel.yml' or run['event'] != 'workflow_dispatch'
            or run['conclusion'] != 'success' or run['status'] != 'completed' or run['head_branch'] != 'main'
            or run['head_repository']['id'] != repository['id']):
        raise ValueError('Expected a successful public main build')
    output.mkdir(parents=True, exist_ok=False)
    name = f"build-state-{run_id}-{run['run_attempt']}"
    subprocess.run(['gh', 'run', 'download', run_id, '--repo', REPO, '--name', name, '--dir', str(output)], check=True)
    manifest_path = output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    state = json.loads((output / 'build-state.json').read_text(encoding='utf8'))
    if not completed(manifest, state, run):
        print('Checkpoint saved; compilation is incomplete, skipping release.')
        return
    name = f"checkpoint-{run_id}-{run['run_attempt']}-1"
    artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    matches = [item for item in artifacts if item['name'] == name and not item['expired']]
    if len(matches) != 1:
        raise ValueError('Expected exactly one unexpired checkpoint artifact')
    command = ['gh', 'workflow', 'run', 'package-candidate.yml', '--repo', REPO, '--ref', 'main']
    inputs = dict(source_run=run_id, source_attempt=str(run['run_attempt']), source_commit=run['head_sha'],
                  source_artifact=str(matches[0]['id']), manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  auto_publish='true')
    for key, value in inputs.items():
        command.extend(['-f', f'{key}={value}'])
    subprocess.run(command, check=True)
    print(json.dumps(inputs, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dispatch(args.run_id, args.output)
