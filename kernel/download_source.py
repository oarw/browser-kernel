import argparse
import json
from pathlib import Path
import urllib.request

from build_support import digest, validate_work_dir


def download(root):
    lock = json.loads(Path(__file__).with_name('source-lock.json').read_text(encoding='utf8'))
    spec = lock['sourceArchive']
    target = root / 'downloads' / f"chromium-{lock['chromiumVersion']}-lite.tar.xz"
    if target.exists():
        if target.stat().st_size == spec['size'] and digest(target) == spec['sha256']:
            return target
        raise RuntimeError('Existing archive does not match the source lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + '.part')
    if partial.exists():
        raise RuntimeError('Incomplete download exists; inspect it before retrying')
    with urllib.request.urlopen(spec['url'], timeout=60) as response, partial.open('xb') as stream:
        size = 0
        while chunk := response.read(8 * 1024 * 1024):
            size += len(chunk)
            if size > spec['size']:
                raise RuntimeError('Source download exceeds locked size')
            stream.write(chunk)
    if partial.stat().st_size != spec['size'] or digest(partial) != spec['sha256']:
        raise RuntimeError('Source download does not match the source lock')
    partial.rename(target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    print(download(validate_work_dir(args.work_dir, Path(__file__).resolve().parent.parent)))
