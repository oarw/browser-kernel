"""Apply the real upstream patch chain and our overlay to the locked Chromium file.

This checks source compatibility, not a compiled browser's behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def check(output):
    lock = json.loads((HERE / 'source-lock.json').read_text(encoding='utf8'))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='scratch-', dir=output))
    result = {'sourceCompatible': False, 'browserCompiled': False, 'browserAcceptancePassed': False}
    try:
        upstream = scratch / 'upstream'
        run(['git', 'clone', '--depth', '1', '--branch', lock['fingerprint']['ref'], '--single-branch', lock['fingerprint']['repository'], upstream], capture_output=True)
        commit = run(['git', '-C', upstream, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        if commit != lock['fingerprint']['commit']:
            raise RuntimeError('Upstream patch tag no longer matches the locked commit')
        tree = scratch / 'source'
        target = tree / lock['targetFile']
        target.parent.mkdir(parents=True)
        url = f"https://raw.githubusercontent.com/chromium/chromium/{lock['chromiumVersion']}/{lock['targetFile']}"
        with urllib.request.urlopen(url, timeout=30) as response:
            target.write_bytes(response.read())
        run(['git', 'init', tree], capture_output=True)
        applied = []
        for name in (upstream / 'patches/series').read_text(encoding='utf8').splitlines():
            name = name.strip()
            if not name or name.startswith('#'):
                continue
            data = (upstream / 'patches' / name).read_text(encoding='utf8')
            if lock['targetFile'] not in data:
                continue
            run(['git', 'apply', '--whitespace=nowarn', '--ignore-space-change', '--ignore-whitespace', '--include=' + lock['targetFile'], '-'], cwd=tree, input=data.encode())
            applied.append(name)
        before = hashlib.sha256(target.read_text(encoding='utf8').encode()).hexdigest()
        if before != lock['postUpstreamSha256']:
            raise RuntimeError(f'Unexpected source after upstream patches: {before}')
        patch = HERE / lock['patch']
        run(['git', 'apply', '--check', patch], cwd=tree)
        run(['git', 'apply', patch], cwd=tree)
        after = hashlib.sha256(target.read_text(encoding='utf8').encode()).hexdigest()
        if after != lock['patchedSha256']:
            raise RuntimeError(f'Unexpected source after native fix: {after}')
        result.update(sourceCompatible=True, chromiumVersion=lock['chromiumVersion'], fingerprintCommit=commit,
                      upstreamPatches=applied, beforeSha256=before, afterSha256=after,
                      patchSha256=hashlib.sha256(patch.read_bytes()).hexdigest())
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        (output / 'report.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        if scratch.parent.resolve() != output or not scratch.name.startswith('scratch-'):
            raise RuntimeError('Unexpected source fixture cleanup path')
        # Read-only Git objects on Windows need their write bit restored before unlinking.
        def on_error(function, path, _error):
            Path(path).chmod(0o700)
            function(path)
        shutil.rmtree(scratch, onexc=on_error)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('release/native-patch-check'))
    check(parser.parse_args().output)
