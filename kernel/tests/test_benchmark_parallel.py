import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_parallel import (LINKS, ORDER, POLICY, audit_commands, audit_mixed_before_compile,
                                audit_plan, checked_output, choose_mixed_targets, compare, previous_trial,
                                remove_owned_workspace, reset_outputs, task_digest, write_wrapper)
from benchmark_resources import FastSampler, guard_reason, run_guarded
from build_support import GIB, resource_requirements


class BenchmarkTests(unittest.TestCase):
    def test_plan_rejects_extra_work_and_missing_tasks(self):
        good = '[1/2] CXX obj/a.obj\n[2/2] LINK(DLL) blink_core.dll blink_core.dll.lib blink_core.dll.pdb\n'
        self.assertEqual(audit_plan(good, ['obj/a.obj'], ['blink_core.dll']), ['blink_core.dll', 'obj/a.obj'])
        for bad in (good.replace('CXX obj/a.obj', 'ACTION generate'),
                    good.replace('obj/a.obj', 'obj/extra.obj'),
                    good.splitlines()[0], good + '[3/3] CXX obj/a.obj\n'):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                audit_plan(bad, ['obj/a.obj'], ['blink_core.dll'])

    def test_verbose_command_rejects_pch_and_shell_chains(self):
        command = '[1/1] ../../third_party/llvm-build/Release+Asserts/bin/clang-cl.exe /c ../../a.cc /Foobj/a.obj'
        audit_commands(command, 1)
        for bad in (command + ' /Ycfoo.h', command + ' & echo unsafe', command.replace('clang-cl.exe', 'other.exe')):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                audit_commands(bad, 1)

    def test_recorded_chromium_windows_command_is_accepted(self):
        path = Path(__file__).parent / 'fixtures/benchmark-clang-command.txt'
        command = path.read_text(encoding='utf8')
        audit_commands(command, 1)
        with self.assertRaises(RuntimeError):
            audit_commands(command.replace('clang-cl.exe', 'unexpected.exe'), 1)

    def test_real_ninja_wrapper_has_exact_selected_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'build.ninja').write_text(
                'rule cxx\n  command = echo unused\n  description = CXX $out\n'
                'build obj/a.obj: cxx\nbuild obj/b.obj: cxx\nbuild obj/unselected.obj: cxx\n')
            write_wrapper(output, ['obj/a.obj', 'obj/b.obj'])
            result = subprocess.run(['ninja', '-n', '-f', 'benchmark-only.ninja', '__bounded_benchmark__'],
                                    cwd=output, capture_output=True, text=True, check=True)
            audit_plan(result.stdout, ['obj/a.obj', 'obj/b.obj'])

    def test_real_ninja_blink_objects_avoid_downstream_ui_generator(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cxx = [f'obj/blink-{i}.obj' for i in range(12)]
            graph = ('rule cxx\n  command = echo unused\n  description = CXX $out\n'
                     'rule link\n  command = echo unused\n  description = LINK(DLL) $out\n'
                     'rule generate\n  command = echo unused\n  description = ACTION downstream-snapshot\n')
            graph += ''.join(f'build {name}: cxx\n' for name in cxx)
            for index, name in enumerate(LINKS):
                graph += f'build {name} {name}.lib {name}.pdb: link ' + ' '.join(cxx[index * 4:index * 4 + 4]) + '\n'
            graph += 'build snapshot.bin: generate blink_controller.dll\nbuild obj/ui.obj: cxx | snapshot.bin\n'
            (output / 'build.ninja').write_text(graph)
            write_wrapper(output, [*cxx, *LINKS])
            def plan():
                return subprocess.run(['ninja', '-n', '-f', 'benchmark-only.ninja', '__bounded_benchmark__'],
                                      cwd=output, capture_output=True, text=True, check=True).stdout
            audit_plan(plan(), cxx, LINKS)
            write_wrapper(output, ['obj/ui.obj', *LINKS])
            self.assertIn('ACTION downstream-snapshot', plan())
            with self.assertRaises(RuntimeError):
                audit_plan(plan(), ['obj/ui.obj'], LINKS)

    def test_early_mixed_audit_restores_outputs_and_ninja_state_on_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'windows/build/src/out/Default'
            output.mkdir(parents=True)
            trial = root / 'report'
            trial.mkdir()
            cxx = ['obj/a.obj']
            original = {}
            for name in [*cxx, *LINKS, '.ninja_log', '.ninja_deps']:
                path = output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
                original[name] = (path.read_bytes(), path.stat().st_mtime_ns)
            def rejected_query(*_args):
                self.assertFalse((output / cxx[0]).exists())
                (output / '.ninja_log').write_text('simulated recompact')
                return '[1/1] ACTION unexpected-generator\n'
            with self.assertRaisesRegex(RuntimeError, 'already prepared'):
                audit_mixed_before_compile(root, trial, cxx, query_fn=rejected_query)
            for name, expected in original.items():
                path = output / name
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)
            report = json.loads((trial / 'mixed-preflight.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertTrue(report['restored'])

    def test_mixed_selection_is_internal_existing_and_excludes_pch(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            lines = ['# ninja log v5']
            for group in ('core/core', 'modules/webaudio/webaudio', 'controller/controller'):
                for index, name in enumerate(['precompile.cc.obj', 'a.obj', 'b.obj', 'c.obj', 'd.obj']):
                    relative = 'obj/third_party/blink/renderer/' + group + '/' + name
                    path = output / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'fixture')
                    lines.append(f'0\t{index + 1}\t1\t{relative}\thash')
            (output / '.ninja_log').write_text('\n'.join(lines))
            selected = choose_mixed_targets(output)
            self.assertEqual(len(selected), 12)
            self.assertTrue(all('precompile' not in name for name in selected))

    def test_deletion_is_confined_and_checks_all_outputs_first(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            kept = output / 'keep.obj'
            kept.write_bytes(b'keep')
            for bad in ('../keep.obj', 'C:/keep.obj', '/tmp/keep.obj', 'a&b.obj'):
                with self.assertRaises(ValueError):
                    checked_output(output, bad)
            with self.assertRaises(RuntimeError):
                reset_outputs(output, ['keep.obj', 'missing.obj'])
            self.assertEqual(kept.read_bytes(), b'keep')
            with self.assertRaises(RuntimeError):
                remove_owned_workspace(output, output / 'missing-owner.json', {})
            self.assertTrue(kept.exists())

    def test_guard_requires_persistent_low_memory_but_stops_on_hard_floor(self):
        sample = dict(availableMemoryBytes=3 * GIB, commitLimitBytes=20 * GIB,
                      committedBytes=10 * GIB, freeDiskBytes=100 * GIB)
        self.assertEqual(guard_reason(sample, 2), (None, 0))
        sample['availableMemoryBytes'] = int(1.5 * GIB)
        self.assertIsNone(guard_reason(sample, 0)[0])
        self.assertIsNotNone(guard_reason(sample, 2)[0])
        sample['availableMemoryBytes'] = GIB // 2
        self.assertIsNotNone(guard_reason(sample, 0)[0])
        # Existing continuation policy remains unchanged.
        self.assertEqual(resource_requirements('build', 4, True)['availableMemoryBytes'], 12 * GIB)

    def test_failed_or_incomplete_trials_cannot_support_adoption(self):
        trials = []
        for jobs in ORDER:
            result = dict(status='passed', samplingComplete=True, detailedSampleErrors=0,
                          elapsedSeconds=100 if jobs == 3 else 75)
            trials.append(dict(jobs=jobs, cxx=result.copy(), mixed=result.copy()))
        self.assertTrue(compare(trials)['worthContinuationTrial'])
        self.assertFalse(compare(trials)['productionFourJobsEnabled'])
        with self.assertRaises(RuntimeError):
            compare(trials[:3])
        trials[1]['cxx']['detailedSampleErrors'] = 1
        with self.assertRaises(RuntimeError):
            compare(trials)

    def test_sequential_steps_require_same_run_successful_predecessor_and_task_list(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            owner = evidence / 'owner.json'
            expected = {'workspace': 'fixture', 'context': {'GITHUB_RUN_ID': '123'}}
            receipt = {'artifactId': 456}
            report = dict(context=expected['context'], source=receipt, policy=POLICY,
                          status='running', trials=[dict(index=1, jobs=3)])
            targets = [f'obj/a{i}.obj' for i in range(240)]
            tasks = {'cxx': targets, 'mixedCxx': [f'obj/blink{i}.obj' for i in range(12)], 'links': list(LINKS)}
            owner.write_text(json.dumps(expected))
            (evidence / 'benchmark.json').write_text(json.dumps(report))
            (evidence / 'tasks.json').write_text(json.dumps({**tasks, 'sha256': task_digest(tasks)}))
            (evidence / 'baseline.json').write_text('{}')
            self.assertEqual(previous_trial(evidence, owner, expected, receipt, 2)[1],
                             {**tasks, 'sha256': task_digest(tasks)})
            for wrong in (dict(report, status='failed'), dict(report, trials=[]),
                          dict(report, context={'GITHUB_RUN_ID': '999'})):
                (evidence / 'benchmark.json').write_text(json.dumps(wrong))
                with self.assertRaises(RuntimeError):
                    previous_trial(evidence, owner, expected, receipt, 2)
            (evidence / 'benchmark.json').write_text(json.dumps(report))
            (evidence / 'tasks.json').write_text(json.dumps({**tasks, 'sha256': 'changed'}))
            with self.assertRaisesRegex(RuntimeError, 'task list changed'):
                previous_trial(evidence, owner, expected, receipt, 2)

    def test_compiler_failure_keeps_log_and_cannot_become_success(self):
        sample = dict(availableMemoryBytes=8 * GIB, committedBytes=5 * GIB,
                      commitLimitBytes=20 * GIB, freeDiskBytes=100 * GIB)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'code 7'):
                run_guarded([sys.executable, '-c', 'print("fixture compiler failure"); raise SystemExit(7)'],
                            root, root / 'evidence', 10, sampler=lambda: sample, detailed=False)
            report = json.loads((root / 'evidence/performance.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['exitCode'], 7)
            self.assertIn('fixture compiler failure', (root / 'evidence/compiler.log').read_text())

    def test_resource_failure_stops_a_running_process_and_records_reason(self):
        calls = 0
        def sample():
            nonlocal calls
            calls += 1
            return dict(availableMemoryBytes=8 * GIB if calls == 1 else GIB // 2,
                        committedBytes=5 * GIB, commitLimitBytes=20 * GIB, freeDiskBytes=100 * GIB)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'below-1-GiB'):
                run_guarded([sys.executable, '-c', 'import time; time.sleep(60)'], root,
                            root / 'evidence', 10, sampler=sample, detailed=False)
            report = json.loads((root / 'evidence/performance.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertLess(report['elapsedSeconds'], 5)

    @unittest.skipUnless(os.name == 'nt', 'Windows API counters')
    def test_real_fast_sampler(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler = FastSampler(Path(directory))
            first, second = sampler(), sampler()
        self.assertGreater(first['commitLimitBytes'], first['committedBytes'])
        self.assertGreater(second['availableMemoryBytes'], 0)
        self.assertGreater(second['freeDiskBytes'], 0)


if __name__ == '__main__':
    unittest.main()
