from collections import Counter
import copy
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import digest, write_json
import migrate_prepared as migration
from source_overlays import install_overlays, source_digest


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'windows/build/src'
        self.repository = self.root / 'recipe'
        self.repository.mkdir()
        (self.source / 'out/Default').mkdir(parents=True)
        subprocess.run(['git', 'init', str(self.source)], capture_output=True, check=True)
        for name, data in [('input.cc', 'old source\n'), ('out/Default/args.gn', 'unchanged flags\n'),
                           ('out/Default/build.ninja', 'old graph\n')]:
            (self.source / name).write_text(data, encoding='utf8', newline='\n')
        self.original = {'schemaVersion': 1, 'inputs': {'buildScriptSha256': 'old',
                         'lockSha256': 'source-lock', 'toolchain': {'compiler': 'locked'}},
                         'preparedFiles': {name: digest(self.source / name) for name in
                                           ['out/Default/args.gn', 'out/Default/build.ninja']}}
        write_json(self.root / 'prepared.json', self.original)
        write_json(self.repository / 'migration-lock.json', self.original)
        self.identity = copy.deepcopy(self.original['inputs'])
        self.identity['buildScriptSha256'] = 'new'
        self.overlays = [{'patch': 'fix.patch', 'seriesName': 'extra/fix.patch', 'files': [{
            'path': 'input.cc', 'postUpstreamSha256': source_digest(self.source / 'input.cc'),
            'patchedSha256': hashlib.sha256(b'new source\n').hexdigest()}]}]
        (self.repository / 'fix.patch').write_text(''.join(difflib.unified_diff(
            ['old source\n'], ['new source\n'], fromfile='a/input.cc', tofile='b/input.cc')),
            encoding='utf8', newline='\n')
        self.files = [*self.original['preparedFiles'], 'input.cc']

    def run_migration(self, commands=None, regenerate=None, query='  ungoogled_switches.dll.lib\n'):
        original_run = subprocess.run
        def run(command, **kwargs):
            if str(command[0]).endswith('ninja.exe'):
                return subprocess.CompletedProcess(command, 0, stdout=query)
            return original_run(command, **kwargs)
        def default_regenerate():
            (self.source / 'out/Default/build.ninja').write_text('new graph\n')
        with patch.object(migration, 'HERE', self.repository), \
             patch.object(migration, 'command_fingerprint', side_effect=commands or [Counter({'same': 3})] * 2), \
             patch.object(migration.subprocess, 'run', side_effect=run):
            migration.migrate(self.root, self.identity, self.overlays, self.files, regenerate or default_regenerate)

    def test_real_patch_migration_preserves_options_and_records_both_identities(self):
        self.run_migration()
        marker = json.loads((self.root / 'prepared.json').read_text())
        self.assertEqual(marker['inputs'], self.identity)
        self.assertEqual(marker['migration']['fromInputs'], self.original['inputs'])
        self.assertEqual(marker['migration']['commandsPreserved'], 3)
        self.assertEqual(marker['preparedFiles']['input.cc'], digest(self.source / 'input.cc'))
        self.assertNotEqual(marker['migration']['oldBuildGraphSha256'], marker['migration']['newBuildGraphSha256'])
        self.assertFalse(marker['migration']['browserCompiled'])
        self.assertEqual((self.source / 'out/Default/args.gn').read_text(), 'unchanged flags\n')

    def test_unknown_prepared_identity_and_toolchain_fail_before_any_source_edit(self):
        for key in ('buildScriptSha256', 'lockSha256', 'toolchain'):
            with self.subTest(key=key):
                altered = copy.deepcopy(self.original)
                altered['inputs'][key] = 'unexpected'
                write_json(self.root / 'prepared.json', altered)
                with self.assertRaisesRegex(RuntimeError, 'reviewed legacy'):
                    self.run_migration()
                self.assertEqual((self.source / 'input.cc').read_text(), 'old source\n')
        write_json(self.root / 'prepared.json', self.original)
        self.identity['toolchain'] = {'compiler': 'upgraded'}
        with self.assertRaisesRegex(RuntimeError, 'unsupported input: toolchain'):
            self.run_migration()

    def test_source_changes_and_changed_prepared_files_are_rejected(self):
        for name in ('input.cc', 'out/Default/args.gn', 'out/Default/build.ninja'):
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_text('unreviewed modification\n')
                with self.assertRaises(RuntimeError):
                    self.run_migration()
                self.assertEqual(json.loads((self.root / 'prepared.json').read_text()), self.original)
                path.write_bytes(original)

    def test_changed_commands_cannot_publish_a_new_prepared_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'changed commands'):
            self.run_migration(commands=[Counter({'same': 3}), Counter({'changed': 3})])
        self.assertEqual(json.loads((self.root / 'prepared.json').read_text()), self.original)
        self.assertEqual(json.loads((self.root / 'migration.json').read_text())['status'], 'failed')
        with self.assertRaisesRegex(RuntimeError, 'changed|differs'):
            self.run_migration()  # A partial migration must be restored, not silently adopted.

    def test_gn_failure_does_not_publish_new_identity(self):
        def fail():
            raise RuntimeError('GN failed')
        with self.assertRaisesRegex(RuntimeError, 'GN failed'):
            self.run_migration(regenerate=fail)
        self.assertEqual(json.loads((self.root / 'prepared.json').read_text()), self.original)

    def test_missing_link_dependency_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'missing its direct import library'):
            self.run_migration(query='other.dll.lib\n')
        self.assertEqual(json.loads((self.root / 'prepared.json').read_text()), self.original)

    def test_regeneration_cannot_change_gn_options(self):
        def alter_flags():
            (self.source / 'out/Default/args.gn').write_text('different compiler flags\n')
        with self.assertRaisesRegex(RuntimeError, 'protected input'):
            self.run_migration(regenerate=alter_flags)
        self.assertEqual(json.loads((self.root / 'prepared.json').read_text()), self.original)

    def test_existing_overlay_is_not_overwritten(self):
        core = self.root / 'upstream'
        overlay = core / 'patches/extra/fix.patch'
        overlay.parent.mkdir(parents=True)
        overlay.write_text('user change')
        with patch('source_overlays.HERE', self.repository), self.assertRaisesRegex(RuntimeError, 'Existing overlay'):
            install_overlays(core, self.overlays)
        self.assertEqual(overlay.read_text(), 'user change')


if __name__ == '__main__':
    unittest.main()
