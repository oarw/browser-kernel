import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import GIB, digest, resource_issues, resource_requirements, validate_prepared, validate_work_dir, workspace_lock, write_json
import build_windows


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.addCleanup(self.scratch.cleanup)

    def test_atomic_report_preserves_previous_evidence_on_replace_failure(self):
        report = self.root / 'report.json'
        write_json(report, {'status': 'prepared'})
        with patch('build_support.os.replace', side_effect=PermissionError('file busy')):
            with self.assertRaises(PermissionError):
                write_json(report, {'status': 'built'})
        self.assertEqual(json.loads(report.read_text()), {'status': 'prepared'})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_lock_rejects_second_builder_and_releases_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with workspace_lock(self.root):
                original = (self.root / 'build.lock').read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'already locked'):
                    with workspace_lock(self.root):
                        self.fail('second builder entered')
                self.assertEqual((self.root / 'build.lock').read_bytes(), original)
                raise RuntimeError('fixture failure')
        self.assertFalse((self.root / 'build.lock').exists())
        with workspace_lock(self.root):
            self.assertTrue((self.root / 'build.lock').is_file())

    def test_missing_modified_or_unidentified_prepared_files_cannot_resume(self):
        files = ['args.gn', 'build.ninja', 'gn.exe']
        for name in files:
            (self.root / name).write_bytes(name.encode())
        identity = {'lock': 'fixture-lock', 'toolchain': 'fixture-toolchain'}
        marker = {'inputs': identity, 'preparedFiles': {name: digest(self.root / name) for name in files}}
        validate_prepared(marker, identity, self.root, files)
        with self.assertRaisesRegex(RuntimeError, 'different inputs'):
            validate_prepared(marker, {**identity, 'toolchain': 'updated'}, self.root, files)
        with self.assertRaisesRegex(RuntimeError, 'manifest'):
            validate_prepared({'inputs': identity}, identity, self.root, files)
        (self.root / 'build.ninja').write_text('incomplete regeneration')
        with self.assertRaisesRegex(RuntimeError, 'build.ninja'):
            validate_prepared(marker, identity, self.root, files)
        (self.root / 'build.ninja').write_bytes(b'build.ninja')
        (self.root / 'gn.exe').unlink()
        with self.assertRaisesRegex(RuntimeError, 'gn.exe'):
            validate_prepared(marker, identity, self.root, files)

    def test_resource_gate_uses_available_ram_and_retains_incremental_disk_reserve(self):
        machine = {'totalMemoryBytes': 32 * GIB, 'availableMemoryBytes': 3 * GIB, 'freeDiskBytes': 200 * GIB}
        issues = resource_issues(machine, resource_requirements('prepare', 2))
        self.assertEqual(len(issues), 1)
        self.assertIn('availableMemoryBytes', issues[0])
        machine['availableMemoryBytes'] = 7 * GIB
        self.assertEqual(resource_issues(machine, resource_requirements('build', 1)), [])
        self.assertTrue(resource_issues(machine, resource_requirements('build', 2)))
        machine['freeDiskBytes'] = 39 * GIB
        self.assertTrue(any('freeDiskBytes' in item for item in resource_issues(machine, resource_requirements('build', 1, True))))
        for jobs in (0, 9):
            with self.assertRaises(ValueError):
                resource_requirements('build', jobs)

    def test_workspace_rejects_repository_and_ancestors(self):
        repository = self.root / 'project'
        for invalid in (repository, self.root, repository / 'build', Path(self.root.anchor), self.root / 'bad&path'):
            with self.assertRaises(ValueError):
                validate_work_dir(invalid, repository)

    def test_preflight_failure_is_recorded_without_cloning_or_claiming_compilation(self):
        args = argparse.Namespace(prepare_only=True, jobs=2, build_timeout_minutes=210)
        with patch('build_windows.preflight', return_value={'ready': False, 'issues': ['insufficient RAM']}), \
             patch('build_windows.checkout') as checkout:
            with self.assertRaisesRegex(RuntimeError, 'insufficient RAM'):
                build_windows.build(args, self.root)
        checkout.assert_not_called()
        state = json.loads((self.root / 'build-state.json').read_text())
        self.assertEqual((state['status'], state['phase']), ('failed', 'preflight'))
        self.assertFalse(state['browserCompiled'])
        self.assertFalse(state['browserAcceptancePassed'])
        self.assertFalse((self.root / 'prepared.json').exists())
        self.assertFalse((self.root / 'build-result.json').exists())

    def test_unexpected_upstream_edits_are_rejected_without_overwriting_them(self):
        upstream = self.root / 'upstream'
        upstream.mkdir()
        (upstream / '.git').mkdir()
        spec = {'repository': 'https://example.invalid/source.git', 'commit': 'locked', 'ref': 'v1'}
        with patch('build_windows.git', side_effect=[str(upstream), spec['repository'], spec['commit'], 'build.py\nflags.windows.gn']):
            with self.assertRaisesRegex(RuntimeError, 'build.py'):
                build_windows.checkout(upstream, spec, ['flags.windows.gn'])

    def test_empty_nested_checkout_is_cloned_instead_of_inspecting_parent_git(self):
        source = self.root / 'source'
        build_windows.run(['git', 'init', source], capture_output=True)
        (source / 'fixture').write_text('locked fixture')
        build_windows.git(source, 'add', 'fixture')
        build_windows.git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                          'commit', '-m', 'fixture')
        build_windows.git(source, 'tag', 'v1')
        spec = {'repository': source.as_posix(), 'commit': build_windows.git(source, 'rev-parse', 'HEAD'), 'ref': 'v1'}
        parent = self.root / 'parent'
        build_windows.run(['git', 'init', parent], capture_output=True)
        nested = parent / 'ungoogled-chromium'
        nested.mkdir()
        build_windows.checkout(nested, spec, [])
        self.assertEqual(Path(build_windows.git(nested, 'rev-parse', '--show-toplevel')).resolve(), nested.resolve())
        self.assertEqual((nested / 'fixture').read_text(), 'locked fixture')
        build_windows.checkout(nested, spec, [])  # Re-entry must verify the same nested checkout.

    def test_nonempty_directory_cannot_inherit_parent_checkout_or_be_overwritten(self):
        parent = self.root / 'parent'
        build_windows.run(['git', 'init', parent], capture_output=True)
        nested = parent / 'ungoogled-chromium'
        nested.mkdir()
        note = nested / 'unfinished-work.txt'
        note.write_text('preserve me')
        with patch('build_windows.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'dedicated Git checkout'):
                build_windows.checkout(nested, {}, [])
        run.assert_not_called()
        self.assertEqual(note.read_text(), 'preserve me')


if __name__ == '__main__':
    unittest.main()
