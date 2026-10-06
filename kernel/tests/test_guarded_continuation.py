import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import GIB, digest, resource_requirements, workspace_lock
from guarded_continuation import continue_build, run_compiler, starting_issues
from measure_build import measured_command


def healthy():
    return dict(totalMemoryBytes=16 * GIB, availableMemoryBytes=11 * GIB,
                committedBytes=6 * GIB, commitLimitBytes=20 * GIB,
                freeDiskBytes=100 * GIB, checkedAt='fixture')


class GuardedContinuationTests(unittest.TestCase):
    def test_four_job_dispatch_requires_the_guarded_launcher(self):
        four = measured_command(Path('work'), Path('report'), 4, 300)
        self.assertEqual(Path(four[2]).name, 'guarded_continuation.py')
        self.assertIn('--output', four)
        for jobs in (2, 3):
            command = measured_command(Path('work'), Path('report'), jobs, 300)
            self.assertEqual(Path(command[2]).name, 'build_windows.py')
            self.assertEqual(command[command.index('--jobs') + 1], str(jobs))
            self.assertIn('--checkpoint-on-timeout', command)
        with self.assertRaises(ValueError):
            measured_command(Path('work'), Path('report'), 8, 300)

    def test_explicit_start_policy_checks_commit_disk_and_ram(self):
        self.assertEqual(starting_issues(healthy()), [])
        for field in ('availableMemoryBytes', 'totalMemoryBytes', 'freeDiskBytes', 'commitLimitBytes'):
            sample = healthy()
            sample[field] = GIB
            self.assertTrue(starting_issues(sample), field)
        self.assertEqual(resource_requirements('build', 4, True)['availableMemoryBytes'], 12 * GIB)

    def test_real_compiler_exit_failure_is_preserved(self):
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                command = [sys.executable, '-c', f'import time; time.sleep(.1); raise SystemExit({code})']
                if code:
                    with self.assertRaises(subprocess.CalledProcessError) as caught:
                        run_compiler(command, root, root / 'evidence', 5, healthy, interval=.02)
                    self.assertEqual(caught.exception.returncode, code)
                else:
                    result = run_compiler(command, root, root / 'evidence', 5, healthy, interval=.02)
                    self.assertEqual(result['status'], 'completed')
                report = json.loads((root / 'evidence/guard-report.json').read_text())
                self.assertEqual(report['compilerExitCode'], code)
                self.assertNotEqual(report['status'], 'checkpoint-ready')

    def test_budget_stops_compiler_before_checkpoint_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_compiler([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  root, root / 'evidence', .1, healthy, interval=.02)
            self.assertEqual(result['status'], 'checkpoint-ready')
            self.assertEqual(result['stopReason'], 'compile-budget-ended')
            self.assertIsNotNone(result['terminatedCompilerExitCode'])
            self.assertLess(result['elapsedSeconds'], 5)

    def test_low_resources_stop_running_compiler_and_sampler_errors_fail_closed(self):
        for mode in ('memory', 'commit', 'disk', 'sampling'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = 0
                def sample():
                    nonlocal calls
                    calls += 1
                    value = healthy()
                    if calls > 1:
                        if mode == 'sampling':
                            raise OSError('counter unavailable')
                        field = {'memory': 'availableMemoryBytes', 'commit': 'commitLimitBytes',
                                 'disk': 'freeDiskBytes'}[mode]
                        value[field] = GIB // 2
                    return value
                command = [sys.executable, '-c', 'import time; time.sleep(60)']
                if mode == 'sampling':
                    with self.assertRaisesRegex(OSError, 'counter unavailable'):
                        run_compiler(command, root, root / 'evidence', 5, sample, interval=.02)
                else:
                    result = run_compiler(command, root, root / 'evidence', 5, sample, interval=.02)
                    self.assertEqual(result['status'], 'checkpoint-ready')
                    self.assertNotEqual(result['stopReason'], 'compile-budget-ended')
                report = json.loads((root / 'evidence/guard-report.json').read_text())
                self.assertLess(report['elapsedSeconds'], 5)
                self.assertEqual(report['sampleErrors'], int(mode == 'sampling'))

    def test_natural_failure_during_stop_is_not_a_checkpoint(self):
        class Process:
            returncode = None
            pid = 123
            def poll(self):
                return self.returncode
        process = Process()
        def ended_naturally(child):
            child.returncode = 7
            return False
        calls = 0
        def sample():
            nonlocal calls
            calls += 1
            value = healthy()
            if calls > 1:
                value['availableMemoryBytes'] = GIB // 2
            return value
        with tempfile.TemporaryDirectory() as directory, \
                patch('guarded_continuation.subprocess.Popen', return_value=process), \
                patch('guarded_continuation.stop_tree', side_effect=ended_naturally):
            root = Path(directory)
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                run_compiler(['fixture'], root, root, 5, sample)
            self.assertEqual(caught.exception.returncode, 7)

    def test_state_transition_preserves_prepared_bytes_and_requires_real_executable(self):
        for outcome in ('checkpoint-ready', 'completed-missing-exe', 'completed', 'modified-input'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'windows/build/src'
                output = source / 'out/Default'
                output.mkdir(parents=True)
                graph = output / 'build.ninja'
                graph.write_text('pool link_pool\n  depth = 2\n')
                identity = {'fixture': 'locked'}
                prepared = dict(inputs=identity, preparedFiles={'out/Default/build.ninja': digest(graph)})
                marker = root / 'prepared.json'
                marker.write_text(json.dumps(prepared))
                before = marker.read_bytes()
                (root / 'build-state.json').write_text(json.dumps(dict(status='prepared', inputs=identity)))
                inspection = dict(issues=[], machine={'cpus': 4}, python={'version': 'fixture'}, toolchain={})
                def compile_fixture(*args):
                    if outcome == 'completed':
                        (output / 'chrome.exe').write_bytes(b'fixture-executable')
                    if outcome == 'modified-input':
                        graph.write_text('changed graph')
                    return dict(status='checkpoint-ready' if outcome == 'checkpoint-ready' else 'completed',
                                stopReason='compile-budget-ended')
                with patch('guarded_continuation.preflight', return_value=inspection), \
                        patch('guarded_continuation.FastSampler', return_value=healthy), \
                        patch('guarded_continuation.prepared_inputs', return_value=identity), \
                        patch('guarded_continuation.PREPARED_FILES', ['out/Default/build.ninja']), \
                        patch('guarded_continuation.validate_sources'), \
                        patch('guarded_continuation.compile_command', return_value=['fixture']), \
                        patch('guarded_continuation.run_compiler', side_effect=compile_fixture), \
                        patch.dict(os.environ):
                    with workspace_lock(root):
                        if outcome in ('completed-missing-exe', 'modified-input'):
                            with self.assertRaises(RuntimeError):
                                continue_build(root, root / 'evidence', 1)
                        else:
                            code = continue_build(root, root / 'evidence', 1)
                            self.assertEqual(code, 124 if outcome == 'checkpoint-ready' else 0)
                state = json.loads((root / 'build-state.json').read_text())
                self.assertEqual(state['browserCompiled'], outcome == 'completed')
                self.assertEqual(state['status'], {'checkpoint-ready': 'checkpoint-ready',
                    'completed': 'built'}.get(outcome, 'failed'))
                self.assertEqual(marker.read_bytes(), before)
                self.assertFalse((root / 'build.lock').exists())


if __name__ == '__main__':
    unittest.main()
