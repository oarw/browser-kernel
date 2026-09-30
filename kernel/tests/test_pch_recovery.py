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
NINJA = shutil.which('ninja')


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

    def make_producer(self, name):
        producer = self.output / name / 'precompile.cc.obj'
        producer.parent.mkdir(exist_ok=True)
        producer.write_bytes(b'PCH producer')
        (self.output / (name + '.ninja')).write_text(
            f'cflags_cc = /Fp{name}_cc.pch /Yubuild/precompile.h\n'
            f'build {name}/precompile.cc.obj: cxx ../../build/precompile.cc\n'
            '  cflags_cc = ${cflags_cc} /Ycbuild/precompile.h\n')
        return producer

    @unittest.skipUnless(CLANG, 'clang not installed')
    def test_real_clang_mtime_failure_is_rebuilt_without_removing_objects_or_valid_pch(self):
        def build(name):
            header = self.output / (name + '.h')
            header.write_text('inline int value() { return 7; }\n')
            pch = self.output / (name + '_cc.pch')
            subprocess.run([CLANG, '-cc1', '-x', 'c++-header', '-emit-pch', str(header), '-o', str(pch)], check=True, capture_output=True)
            self.make_producer(name)
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
        self.assertFalse((self.output / 'stale/precompile.cc.obj').exists())
        self.assertTrue(valid.exists())
        self.assertTrue((self.output / 'valid/precompile.cc.obj').exists())
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
        first, second = self.output / 'a_cc.pch', self.output / 'b_cc.pch'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        self.make_producer('a')
        self.make_producer('b')
        with patch.object(pch_recovery, 'validate_pch', side_effect=['mtime failure', RuntimeError('compiler crashed')]), \
                self.assertRaisesRegex(RuntimeError, 'compiler crashed'):
            pch_recovery.recover(self.root, self.report)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse((self.root / 'build.lock').exists())

    def test_unknown_or_unsafe_producer_cannot_be_removed(self):
        pch = self.output / 'target_cc.pch'
        pch.write_bytes(b'PCH')
        producer = self.make_producer('target')
        definition = self.output / 'target.ninja'
        definition.write_text(definition.read_text().replace('/Ycbuild/precompile.h', '/Ycother.h'))
        with patch.object(pch_recovery, 'validate_pch', return_value='mtime failure'), self.assertRaisesRegex(RuntimeError, 'expected GN producer'):
            pch_recovery.recover(self.root, self.report)
        self.assertTrue(pch.exists())
        self.assertTrue(producer.exists())
        producer.unlink()
        outside = self.root / 'outside.obj'
        outside.write_bytes(b'keep')
        producer.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'unsafe PCH producer'):
            pch_recovery.pch_producer(pch, self.output)
        self.assertEqual(outside.read_bytes(), b'keep')

    @unittest.skipUnless(CLANG and NINJA, 'clang or ninja not installed')
    def test_ninja_recreates_pch_side_effect_before_compiling_its_consumer(self):
        (self.source / 'build').mkdir()
        (self.source / 'build/precompile.cc').write_text('// fixture producer source\n')
        header = self.output / 'header.h'
        header.write_text('inline int value() { return 7; }\n')
        (self.output / 'unit.cc').write_text('int main() { return value(); }\n')
        (self.output / 'target').mkdir()
        script = self.output / 'compile.py'
        script.write_text(
            'from pathlib import Path\nimport subprocess, sys\n'
            f'clang = {CLANG!r}\n'
            'if sys.argv[1] == "producer":\n'
            '    subprocess.run([clang, "-cc1", "-x", "c++-header", "-emit-pch", "header.h", "-o", "target_cc.pch"], check=True)\n'
            '    Path("target/precompile.cc.obj").write_bytes(b"producer")\n'
            'else:\n'
            '    subprocess.run([clang, "-cc1", "-x", "c++", "-include-pch", "target_cc.pch", "-emit-obj", "unit.cc", "-o", "unit.obj"], check=True)\n')
        (self.output / 'build.ninja').write_text(
            f'rule cxx\n  command = "{sys.executable}" compile.py $mode\ninclude target.ninja\n')
        (self.output / 'target.ninja').write_text(
            'cflags_cc = /Fptarget_cc.pch /Yubuild/precompile.h\n'
            'build target/precompile.cc.obj: cxx ../../build/precompile.cc\n'
            '  cflags_cc = ${cflags_cc} /Ycbuild/precompile.h\n  mode = producer\n'
            'build unit.obj: cxx unit.cc | target/precompile.cc.obj\n  mode = consumer\n')
        def ninja():
            return subprocess.run([NINJA, 'unit.obj'], cwd=self.output, capture_output=True, text=True)
        initial = ninja()
        self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
        previous = header.stat().st_mtime_ns
        os.utime(header, ns=(previous + 60_000_000_000, previous + 60_000_000_000))
        (self.output / 'unit.obj').unlink()
        broken = ninja()
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn('mtime changed', broken.stdout + broken.stderr)
        validator = pch_recovery.validate_pch
        with patch.object(pch_recovery, 'validate_pch', side_effect=lambda _, path, output: validator(CLANG, path, output)):
            result = pch_recovery.recover(self.root, self.report)
        self.assertEqual(result['invalidated'][0]['producer'], 'windows/build/src/out/Default/target/precompile.cc.obj')
        fixed = ninja()
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertTrue((self.output / 'target_cc.pch').is_file())
        self.assertTrue((self.output / 'unit.obj').is_file())

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
