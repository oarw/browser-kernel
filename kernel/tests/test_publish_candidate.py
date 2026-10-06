import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from kernel.publish_candidate import prepare, digest


class PublishTests(unittest.TestCase):
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
