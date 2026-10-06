import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from kernel.publish_candidate import prepare, digest, next_version
from kernel.auto_package import completed


class PublishTests(unittest.TestCase):
    def test_next_version_preserves_chromium_and_avoids_tags(self):
        self.assertEqual(next_version('149.0.1.1', []), '149.0.1.1-fb.1-pre.1')
        tags = ['149.0.1.1-fb.1-pre.1', '149.0.1.1-fb.1-pre.3', '148.0.1.1-fb.9-pre.9']
        self.assertEqual(next_version('149.0.1.1', tags), '149.0.1.1-fb.1-pre.4')
        self.assertEqual(next_version('149.0.1.1', tags + ['149.0.1.1-fb.1']), '149.0.1.1-fb.2-pre.1')

    def test_checkpoints_never_trigger_release(self):
        run = dict(id=123, run_attempt=1, head_sha='a' * 40)
        context = dict(GITHUB_REPOSITORY='oarw/browser-kernel', GITHUB_SHA='a' * 40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')
        manifest = dict(context=context, buildStatus='checkpoint-ready', browserCompiled=False)
        self.assertFalse(completed(manifest, dict(status='checkpoint-ready'), run))
        with self.assertRaises(ValueError):
            completed({**manifest, 'buildStatus': 'built'}, dict(status='built'), run)
        built = dict(context=context, buildStatus='built', browserCompiled=True)
        self.assertTrue(completed(built, dict(status='built', browserCompiled=True), run))
        with self.assertRaises(ValueError):
            completed(built, dict(status='built', browserCompiled=True), {**run, 'run_attempt': 2})

    def test_verified_package_and_wrong_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            archive = directory / 'browser-kernel-windows-x64.zip'
            with zipfile.ZipFile(archive, 'w') as writer:
                writer.writestr('148.0.1.1.manifest', '')
                writer.writestr('chrome.exe', b'chrome')
            report = dict(archiveSha256=digest(archive), archiveBytes=archive.stat().st_size,
                          browserCompiled=True, executableSha256=hashlib.sha256(b'chrome').hexdigest(),
                          source=dict(GITHUB_REPOSITORY='oarw/browser-kernel', GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123'))
            (directory / 'package-report.json').write_text(json.dumps(report))
            entry = prepare(directory, '148.0.1.1-fb.1-pre.1', digest(archive))
            self.assertEqual(entry['channel'], 'candidate')
            self.assertEqual(len((directory / 'SHA256SUMS.txt').read_text().splitlines()), 3)
            for version, sha in [('148.0.1.1-fb.1', digest(archive)), ('149.0.1.1-fb.1-pre.1', digest(archive)),
                                 ('148.0.1.1-fb.1-pre.1', '0' * 64)]:
                with self.assertRaises(ValueError):
                    prepare(directory, version, sha)
