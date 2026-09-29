import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_probe
from build_support import digest, write_json
from runner_probe import choose_volume


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.checksum = artifact_probe.create(self.source)

    def test_artifact_roundtrip_keeps_content_hidden_files_and_timestamps(self):
        with patch('artifact_probe.socket.gethostname', return_value='other-runner'):
            result = artifact_probe.verify(self.source, self.root / 'restored', self.checksum, True)
        self.assertTrue(result['differentHost'])
        self.assertTrue(result['timestampsVerified'])
        self.assertTrue((self.root / 'restored/out/.ninja_log').is_file())
        self.assertFalse(result['browserCompiled'])

    def test_tampering_and_wrong_run_are_rejected_before_restoring(self):
        with self.assertRaisesRegex(RuntimeError, 'Manifest digest'):
            artifact_probe.verify(self.source, self.root / 'restored', '0' * 64)
        with patch.dict('os.environ', {'GITHUB_RUN_ID': 'another-run'}):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                artifact_probe.verify(self.source, self.root / 'restored', self.checksum)
        with self.assertRaisesRegex(RuntimeError, 'another runner'):
            artifact_probe.verify(self.source, self.root / 'restored', self.checksum, True)
        with (self.source / 'fixture.zip').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'Archive size or digest'):
            artifact_probe.verify(self.source, self.root / 'restored', self.checksum)
        self.assertFalse((self.root / 'restored').exists())

    def test_archive_extra_entries_cannot_write_outside_fixture(self):
        with zipfile.ZipFile(self.source / 'fixture.zip', 'a') as archive:
            archive.writestr('../escape', b'invalid')
        manifest = json.loads((self.source / 'manifest.json').read_text())
        manifest['archiveSha256'] = digest(self.source / 'fixture.zip')
        write_json(self.source / 'manifest.json', manifest)
        with self.assertRaisesRegex(RuntimeError, 'archive entries'):
            artifact_probe.verify(self.source, self.root / 'restored', digest(self.source / 'manifest.json'))
        self.assertFalse((self.root / 'escape').exists())

    def test_selects_largest_supported_ntfs_volume(self):
        volumes = [{'DeviceID': 'C:', 'FileSystem': 'NTFS', 'FreeSpace': 5},
                   {'DeviceID': 'D:', 'FileSystem': 'NTFS', 'FreeSpace': 10},
                   {'DeviceID': 'E:', 'FileSystem': 'NTFS', 'FreeSpace': 100}]
        self.assertEqual(choose_volume(volumes)['DeviceID'], 'D:')
        with self.assertRaisesRegex(RuntimeError, 'NTFS'):
            choose_volume([{'DeviceID': 'C:', 'FileSystem': 'FAT32', 'FreeSpace': 100}])


if __name__ == '__main__':
    unittest.main()
