"""Prepare an immutable, explicitly experimental Release from a verified CI package."""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(directory, version, expected_sha):
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+-fb\.\d+-pre\.\d+', version):
        raise ValueError('Experimental packages require a Chromium-fb.N-pre.N version')
    archive = directory / 'browser-kernel-windows-x64.zip'
    report = json.loads((directory / 'package-report.json').read_text(encoding='utf8'))
    if (not re.fullmatch(r'[a-f0-9]{64}', expected_sha)
            or digest(archive) != expected_sha or report['archiveSha256'] != expected_sha
            or archive.stat().st_size != report['archiveBytes'] or report['browserCompiled'] is not True
            or report['source']['GITHUB_REPOSITORY'] != 'oarw/browser-kernel'):
        raise ValueError('Package provenance, size or digest mismatch')
    commit = report['source']['GITHUB_SHA']
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Invalid source commit')
    with zipfile.ZipFile(archive) as reader:
        chromium = version.split('-')[0]
        if chromium + '.manifest' not in reader.namelist():
            raise ValueError('Version differs from packaged Chromium')
        with reader.open('chrome.exe') as executable:
            if hashlib.file_digest(executable, 'sha256').hexdigest() != report['executableSha256']:
                raise ValueError('Executable digest mismatch')
    root = 'https://github.com/oarw/browser-kernel'
    entry = dict(schemaVersion=1, version=version, platform='win32', arch='x64', channel='candidate',
                 archiveName=archive.name, size=report['archiveBytes'], sha256=expected_sha,
                 executableSha256=report['executableSha256'], sourceCommit=commit,
                 notice='实验候选：已修复 Canvas 文本测量并验证本机密码保存；真实显卡 WebGL、强密码生成菜单和旧环境完整迁移仍待验证。切换前请完整备份。')
    (directory / 'kernel.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    names = [archive.name, 'package-report.json', 'kernel.json']
    (directory / 'SHA256SUMS.txt').write_text(''.join(f'{digest(directory / name)}  {name}\n' for name in names), encoding='utf8')
    (directory / 'release-notes.md').write_text(
        f'# FingerBrowser kernel {version}\n\nExperimental Windows x64 component build.\n\n'
        f'{entry["notice"]}\n\nChromium: {chromium}\n\nSource: {root}/tree/{commit}\n\n'
        f'Build: {root}/actions/runs/{report["source"]["GITHUB_RUN_ID"]}\n\n'
        'This pre-release does not certify full browser acceptance or distribution review. '
        'Licenses and build provenance are included in the runtime ZIP. '
        'Install alongside older versions; do not overwrite profile backups.\n', encoding='utf8')
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory, args.version, args.sha256), indent=2))
