import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.request

from build_support import GIB, now, write_json


def positive_id(value):
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('Expected a positive run, attempt or artifact ID')
    return int(value)


def checksum(value, length):
    if not re.fullmatch(r'[0-9a-f]{' + str(length) + '}', value):
        raise ValueError('Expected a full lowercase hexadecimal digest')
    return value


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Expected a timezone-aware API timestamp')
    return result


def verify_metadata(repository, run, artifact, comparison, expected, current, checked_at):
    repo = current['GITHUB_REPOSITORY']
    if (repository.get('full_name') != repo or repository.get('visibility') != 'public'
            or repository.get('default_branch') != 'main'):
        raise RuntimeError('Expected the public source repository and main branch')
    if (run.get('id') != expected['runId'] or run.get('run_attempt') != expected['attempt']
            or run.get('head_sha') != expected['commit'] or run.get('head_branch') != 'main'
            or run.get('event') != 'workflow_dispatch' or run.get('status') != 'completed'
            or run.get('conclusion') != 'success'
            or run.get('path') != '.github/workflows/build-kernel.yml'
            or run.get('repository', {}).get('id') != repository['id']
            or run.get('head_repository', {}).get('id') != repository['id']):
        raise RuntimeError('Source run is not a successful trusted build attempt')
    if (comparison.get('status') not in ('ahead', 'identical')
            or comparison.get('merge_base_commit', {}).get('sha') != expected['commit']):
        raise RuntimeError('Source commit is not an ancestor of the current build')
    source = artifact.get('workflow_run', {})
    if (artifact.get('id') != expected['artifactId']
            or artifact.get('name') != f"checkpoint-{expected['runId']}-{expected['attempt']}-1"
            or artifact.get('expired') is not False
            or source.get('id') != expected['runId']
            or source.get('repository_id') != repository['id']
            or source.get('head_repository_id') != repository['id']
            or source.get('head_sha') != expected['commit'] or source.get('head_branch') != 'main'
            or not 0 < artifact.get('size_in_bytes', 0) <= 20 * GIB + 1024 * 1024
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))):
        raise RuntimeError('Artifact does not belong to the selected build attempt')
    if (timestamp(artifact['expires_at']) <= checked_at
            or not timestamp(run['run_started_at']) <= timestamp(artifact['created_at']) <= timestamp(run['updated_at'])):
        raise RuntimeError('Artifact is expired or outside the source attempt time window')
    if str(expected['runId']) == current['GITHUB_RUN_ID']:
        raise RuntimeError('Cross-run source must be a previous run')
    return {'schemaVersion': 1, 'verifiedAt': checked_at.isoformat(), 'targetContext': current,
            'sourceContext': {'GITHUB_REPOSITORY': repo, 'GITHUB_SHA': expected['commit'],
                              'GITHUB_RUN_ID': str(expected['runId']),
                              'GITHUB_RUN_ATTEMPT': str(expected['attempt'])},
            'artifactId': expected['artifactId'], 'artifactDigest': artifact['digest'],
            'artifactExpiresAt': artifact['expires_at'], 'manifestSha256': expected['manifestSha256']}


def github_api(path):
    request = urllib.request.Request('https://api.github.com/' + path, headers={
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'User-Agent': 'kernel-checkpoint-source'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def verify_source(expected, current):
    if (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', current['GITHUB_REPOSITORY'])):
        raise RuntimeError('Cross-run recovery requires a main-branch workflow dispatch')
    checksum(current['GITHUB_SHA'], 40)
    positive_id(current['GITHUB_RUN_ID'])
    positive_id(current['GITHUB_RUN_ATTEMPT'])
    base = 'repos/' + current['GITHUB_REPOSITORY']
    repository = github_api(base)
    run = github_api(f"{base}/actions/runs/{expected['runId']}")
    artifact = github_api(f"{base}/actions/artifacts/{expected['artifactId']}")
    comparison = github_api(f"{base}/compare/{expected['commit']}...{current['GITHUB_SHA']}")
    return verify_metadata(repository, run, artifact, comparison, expected, current, datetime.now(timezone.utc))


def receipt_context(receipt, current, manifest_sha):
    source = receipt.get('sourceContext', {})
    if (receipt.get('schemaVersion') != 1 or receipt.get('targetContext') != current
            or receipt.get('manifestSha256') != manifest_sha
            or source.get('GITHUB_REPOSITORY') != current['GITHUB_REPOSITORY']
            or source.get('GITHUB_RUN_ID') == current['GITHUB_RUN_ID']):
        raise RuntimeError('Trusted source receipt does not match this recovery')
    checksum(source.get('GITHUB_SHA', ''), 40)
    positive_id(source.get('GITHUB_RUN_ID', ''))
    positive_id(source.get('GITHUB_RUN_ATTEMPT', ''))
    positive_id(receipt.get('artifactId', ''))
    return source


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True, type=positive_id)
    parser.add_argument('--attempt', required=True, type=positive_id)
    parser.add_argument('--commit', required=True, type=lambda value: checksum(value, 40))
    parser.add_argument('--artifact-id', required=True, type=positive_id)
    parser.add_argument('--manifest-sha256', required=True, type=lambda value: checksum(value, 64))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    current = {key: os.environ[key] for key in
               ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')}
    expected = {'runId': args.run_id, 'attempt': args.attempt, 'commit': args.commit,
                'artifactId': args.artifact_id, 'manifestSha256': args.manifest_sha256}
    report = verify_source(expected, current)
    write_json(args.output, report)
    print(json.dumps({'verified': True, 'artifactId': args.artifact_id, 'checkedAt': now()}))
