import json
import os
from pathlib import Path
import stat
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import digest, write_json
from build_windows import experimental_flags
import checkpoint


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_archive(self, extra=None):
        archive = self.root / 'checkpoint.zip'
        with zipfile.ZipFile(archive, 'w') as writer:
            for name in ['prepared.json', 'build-state.json', 'build-result.json', 'windows/build/src/out/Default/.ninja_log']:
                writer.writestr(name, '{}')
            if extra is not None:
                writer.writestr(extra, 'unsafe')
        return archive

    def test_archive_rejects_traversal_ads_duplicates_links_and_expansion(self):
        self.assertEqual(checkpoint.inspect_archive(self.make_archive()), (8, 4))
        for name in ['../escape', '/windows/escape', 'windows/a:ads', 'windows\\..\\escape',
                     'windows/a.', 'windows/a ', 'PREPARED.JSON']:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Invalid checkpoint entry'):
                checkpoint.inspect_archive(self.make_archive(name))
        link = zipfile.ZipInfo('windows/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(RuntimeError, 'Invalid checkpoint entry'):
            checkpoint.inspect_archive(self.make_archive(link))
        with patch('checkpoint.MAX_TREE', 1), self.assertRaisesRegex(RuntimeError, 'expansion'):
            checkpoint.inspect_archive(self.make_archive())

    def test_restore_rejects_wrong_manifest_or_context_before_touching_workspace(self):
        source = self.root / 'source'
        source.mkdir()
        write_json(source / 'manifest.json', {'schemaVersion': 1, 'context': {}})
        target = self.root / 'target'
        with self.assertRaisesRegex(RuntimeError, 'manifest digest'):
            checkpoint.restore(target, source, '0' * 64)
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            checkpoint.restore(target, source, digest(source / 'manifest.json'))
        self.assertFalse(target.exists())

    def test_real_output_snapshot_detects_changed_or_rebuilt_objects(self):
        output = self.root / 'windows/build/src/out/Default'
        output.mkdir(parents=True)
        obj = output / 'sample.obj'
        obj.write_bytes(b'object')
        (output / '.ninja_log').write_text('# ninja log v5\n0\t1\t1\tsample.obj\tabc\n')
        before = checkpoint.completed_outputs(self.root)
        self.assertEqual(before['completedOutputs'], 1)
        checkpoint.verify_samples(self.root, before['samples'])
        os.utime(obj, ns=(obj.stat().st_atime_ns, obj.stat().st_mtime_ns + 1_000_000_000))
        with self.assertRaisesRegex(RuntimeError, 'sample changed'):
            checkpoint.verify_samples(self.root, before['samples'])
        obj.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'sample changed'):
            checkpoint.verify_samples(self.root, before['samples'])

    def test_checkpoint_requires_stopped_builder_and_recoverable_state(self):
        (self.root / 'build.lock').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'still owns'):
            checkpoint.create(self.root, self.root / 'artifact')
        (self.root / 'build.lock').unlink()
        write_json(self.root / 'prepared.json', {'inputs': {}})
        write_json(self.root / 'build-state.json', {'status': 'failed', 'inputs': {}})
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            checkpoint.create(self.root, self.root / 'artifact')
        self.assertFalse((self.root / 'artifact').exists())

    @unittest.skipUnless(shutil.which('7z'), '7z not installed')
    def test_real_zip_roundtrip_preserves_ninja_state_and_object_timestamps(self):
        tree = (self.root / 'tree').resolve()
        output = tree / 'windows/build/src/out/Default'
        output.mkdir(parents=True)
        obj = output / 'sample.obj'
        obj.write_bytes(b'completed object')
        os.utime(obj, (1767225600, 1767225600))
        (output / '.ninja_log').write_text('# ninja log v5\n0\t1\t1\tsample.obj\tabc\n')
        write_json(tree / 'prepared.json', {'inputs': {'fixture': True}})
        state = {'status': 'checkpoint-ready', 'inputs': {'fixture': True}}
        write_json(tree / 'build-state.json', state)
        write_json(tree / 'build-result.json', state)
        artifact = self.root / 'artifact'
        with patch('checkpoint.shutil.disk_usage', return_value=SimpleNamespace(free=1024**4)):
            checksum = checkpoint.create(tree, artifact)
            backup = tree.with_name('original-tree')
            self.assertEqual(backup.parent, self.root.resolve())
            tree.rename(backup)
            result = checkpoint.restore(tree, artifact, checksum)
        self.assertEqual(result['completedOutputs'], 1)
        self.assertEqual(obj.read_bytes(), b'completed object')
        self.assertEqual(obj.stat().st_mtime_ns, 1767225600 * 10**9)

    def test_experimental_flags_disable_pgo_and_lto_without_duplicate_assignments(self):
        value = experimental_flags('chrome_pgo_phase=2\nis_official_build=true\nis_component_build=false\nuse_thin_lto=true\n')
        self.assertIn('chrome_pgo_phase=0\n', value)
        self.assertIn('use_thin_lto=false\n', value)
        self.assertIn('is_component_build=true\n', value)
        self.assertIn('is_official_build=false\n', value)
        self.assertEqual(experimental_flags(value), value)
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            experimental_flags('chrome_pgo_phase=2\nchrome_pgo_phase=1\n')


if __name__ == '__main__':
    unittest.main()
