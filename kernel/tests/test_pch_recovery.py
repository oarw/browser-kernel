import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pch_recovery

CLANG = shutil.which('clang')


class PchRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'windows/build/src'
        self.output = self.source / 'out/Default'
        self.output.mkdir(parents=True)
        compiler = self.source / 'third_party/llvm-build/Release+Asserts/bin/clang-cl.exe'
        compiler.parent.mkdir(parents=True)
        compiler.write_bytes(b'fixture: validation delegates to installed clang')
        (self.root / 'prepared.json').write_text('{}')
        self.report = self.root / 'pch-recovery.json'

    @unittest.skipUnless(CLANG, 'clang not installed')
    def test_real_clang_mtime_failure_is_rebuilt_without_removing_objects_or_valid_pch(self):
        def build(name):
            header = self.output / (name + '.h')
            header.write_text('inline int value() { return 7; }\n')
            pch = self.output / (name + '.pch')
            subprocess.run([CLANG, '-cc1', '-x', 'c++-header', '-emit-pch', str(header), '-o', str(pch)], check=True, capture_output=True)
            return header, pch

        header, stale = build('stale')
        _, valid = build('valid')
        obj = self.output / 'preserved.obj'
        obj.write_bytes(b'completed object')
        old_mtime = obj.stat().st_mtime_ns
        previous = header.stat().st_mtime_ns
        os.utime(header, ns=(previous + 60_000_000_000, previous + 60_000_000_000))
        validator = pch_recovery.validate_pch
        self.assertIn('mtime changed', validator(CLANG, stale, self.output))
        with patch.object(pch_recovery, 'validate_pch', side_effect=lambda _, path, output: validator(CLANG, path, output)):
            result = pch_recovery.recover(self.root, self.report)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(result['invalidated']), 1)
        self.assertEqual(len(result['validated']), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(valid.exists())
        self.assertEqual(obj.read_bytes(), b'completed object')
        self.assertEqual(obj.stat().st_mtime_ns, old_mtime)
        subprocess.run([CLANG, '-cc1', '-x', 'c++-header', '-emit-pch', str(header), '-o', str(stale)], check=True, capture_output=True)
        unit = self.output / 'unit.cc'
        unit.write_text('int main() { return value(); }\n')
        subprocess.run([CLANG, '-cc1', '-x', 'c++', '-include-pch', str(stale), '-emit-obj', str(unit),
                        '-o', str(self.output / 'new.obj')], check=True, capture_output=True)
        self.assertIsNone(validator(CLANG, stale, self.output))
        self.assertTrue((self.output / 'new.obj').is_file())

    def test_unexpected_validation_failure_does_not_delete_any_pch(self):
        first, second = self.output / 'a.pch', self.output / 'b.pch'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        with patch.object(pch_recovery, 'validate_pch', side_effect=['mtime failure', RuntimeError('compiler crashed')]), \
                self.assertRaisesRegex(RuntimeError, 'compiler crashed'):
            pch_recovery.recover(self.root, self.report)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse((self.root / 'build.lock').exists())

    def test_corruption_missing_compiler_and_active_builder_are_not_mtime_recovery(self):
        response = subprocess.CompletedProcess([], 1, '', 'fatal error: invalid PCH file')
        with patch.object(pch_recovery.subprocess, 'run', return_value=response), self.assertRaisesRegex(RuntimeError, 'Unexpected'):
            pch_recovery.validate_pch('clang', self.output / 'broken.pch', self.output)
        (self.root / 'build.lock').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'locked'):
            pch_recovery.recover(self.root, self.report)
        (self.root / 'build.lock').unlink()
        (self.root / 'prepared.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'prepared workspace'):
            pch_recovery.recover(self.root, self.report)

    def test_links_cannot_remove_files_outside_output_tree(self):
        outside = self.root / 'outside.pch'
        outside.write_bytes(b'keep')
        link = self.output / 'linked.pch'
        link.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Invalid precompiled'):
            pch_recovery.recover(self.root, self.report)
        self.assertEqual(outside.read_bytes(), b'keep')
        link.unlink()
        (self.output / 'linked-directory').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Linked directories'):
            pch_recovery.recover(self.root, self.report)


if __name__ == '__main__':
    unittest.main()
