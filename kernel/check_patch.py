"""Apply the real upstream patch chain and our overlay to the locked Chromium file.

This checks source compatibility, not a compiled browser's behavior.
"""
import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from source_overlays import load_overlays, targets, validate_sources

HERE = Path(__file__).resolve().parent


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def substitute_sources(upstream, tree, names):
    """Run the pinned recipe's domain substitution on the selected real files."""
    utilities = upstream / 'utils'
    sys.path.insert(0, str(utilities))
    try:
        spec = importlib.util.spec_from_file_location('pinned_domain_substitution', utilities / 'domain_substitution.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        listed = set((upstream / 'domain_substitution.list').read_text(encoding='utf8').splitlines())
        selected = tree.parent / (tree.name + '-domains.list')
        selected.write_text(''.join(name + '\n' for name in names if name in listed), encoding='utf8')
        module.apply_substitution(upstream / 'domain_regex.list', selected, tree, None)
    finally:
        sys.path.pop(0)


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
        overlays = load_overlays()
        names = [lock['targetFile'], *targets(overlays)]
        devtools_prefix = 'third_party/devtools-frontend/src/'
        with urllib.request.urlopen(
                f"https://raw.githubusercontent.com/chromium/chromium/{lock['chromiumVersion']}/DEPS",
                timeout=30) as response:
            deps = response.read().decode('utf8')
        revision = re.search(r"'devtools_frontend_revision':\s*'([0-9a-f]{40})'", deps)
        if revision is None or revision[1] != lock['devtools']['commit']:
            raise RuntimeError('DevTools source lock differs from Chromium DEPS')
        for name in names:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://raw.githubusercontent.com/chromium/chromium/{lock['chromiumVersion']}/{name}"
            if name.startswith(devtools_prefix):
                url = (f"https://raw.githubusercontent.com/ChromeDevTools/devtools-frontend/"
                       f"{lock['devtools']['commit']}/{name.removeprefix(devtools_prefix)}")
            with urllib.request.urlopen(url, timeout=30) as response:
                target.write_bytes(response.read())
        target = tree / lock['targetFile']
        run(['git', 'init', tree], capture_output=True)
        applied = []
        for name in (upstream / 'patches/series').read_text(encoding='utf8').splitlines():
            name = name.strip()
            if not name or name.startswith('#'):
                continue
            data = (upstream / 'patches' / name).read_text(encoding='utf8')
            if not any(name in data for name in names):
                continue
            run(['git', 'apply', '--whitespace=nowarn', '--ignore-space-change', '--ignore-whitespace',
                 *['--include=' + name for name in names], '-'], cwd=tree, input=data.encode())
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
        validate_sources(tree, overlays, 'postUpstreamSha256')
        resumed = scratch / 'resumed'
        shutil.copytree(tree, resumed, ignore=shutil.ignore_patterns('.git'))
        run(['git', 'init', resumed], capture_output=True)
        substitute_sources(upstream, resumed, names)
        validate_sources(resumed, overlays, 'preparedBeforeSha256')
        for item in overlays:
            run(['git', 'apply', '--check', HERE / item['patch']], cwd=tree)
            run(['git', 'apply', HERE / item['patch']], cwd=tree)
            run(['git', 'apply', '--check', HERE / item['patch']], cwd=resumed)
            run(['git', 'apply', HERE / item['patch']], cwd=resumed)
        validate_sources(tree, overlays, 'patchedSha256')
        substitute_sources(upstream, tree, names)
        validate_sources(tree, overlays)
        validate_sources(resumed, overlays)
        if any((tree / name).read_bytes() != (resumed / name).read_bytes() for name in names):
            raise RuntimeError('Fresh and migrated source trees differ')
        result.update(sourceCompatible=True, chromiumVersion=lock['chromiumVersion'], fingerprintCommit=commit,
                      devtoolsCommit=lock['devtools']['commit'],
                      upstreamPatches=applied, beforeSha256=before, afterSha256=after,
                      patchSha256=hashlib.sha256(patch.read_bytes()).hexdigest(), overlays=overlays,
                      freshAndResumedSourcesMatch=True)
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
