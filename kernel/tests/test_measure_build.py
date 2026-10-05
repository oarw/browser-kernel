import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from measure_build import run_measured, sample_resources


class BuildMeasurementTests(unittest.TestCase):
    def test_hidden_compiler_keeps_redirected_output(self):
        with tempfile.TemporaryDirectory() as directory:
            support = str(Path(__file__).resolve().parents[1])
            code = (f'import sys\nfrom pathlib import Path\nsys.path.insert(0, {support!r})\n'
                    'from measure_build import run_measured\n'
                    f'root = Path({directory!r})\n'
                    'def sample(_root):\n return dict(availableMemoryBytes=1, committedBytes=2, freeDiskBytes=3)\n'
                    'raise SystemExit(run_measured([sys.executable, "-c", '
                    '"import sys; print(\'compiler-stdout\'); print(\'compiler-stderr\', file=sys.stderr)"], '
                    'root, root / "evidence", sampler=sample))\n')
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('compiler-stdout', result.stdout)
            self.assertIn('compiler-stderr', result.stderr)

    def test_preserves_real_process_exit_and_retains_diagnostics(self):
        for exit_code in (0, 1, 124):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ninja = root / 'windows/build/src/out/Default'
                ninja.mkdir(parents=True)
                (ninja / '.ninja_log').write_text('# ninja log v5\n', encoding='utf8')
                graph = 'pool link_pool\n  depth = 2\n'
                (ninja / 'build.ninja').write_text(graph, encoding='utf8')
                output = root / 'evidence'
                def sample(_root):
                    return {'availableMemoryBytes': 100, 'committedBytes': 200, 'freeDiskBytes': 300}
                result = run_measured([sys.executable, '-c', f'import time; time.sleep(.15); raise SystemExit({exit_code})'],
                                      root, output, interval=.05, sampler=sample)
                report = json.loads((output / 'performance.json').read_text())
                self.assertEqual(result, exit_code)
                self.assertEqual(report['exitCode'], exit_code)
                self.assertTrue(report['samplingComplete'])
                self.assertEqual(report['pools'], [['link_pool', '2']])
                self.assertEqual(report['minAvailableMemoryBytes'], 100)
                self.assertEqual((ninja / 'build.ninja').read_text(), graph)
                self.assertEqual((output / 'ninja-log.txt').read_text(), '# ninja log v5\n')

    def test_sampler_error_is_explicit_and_does_not_mask_compiler_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def broken(_root):
                raise RuntimeError('CIM unavailable')
            result = run_measured([sys.executable, '-c', 'import time; time.sleep(.15); raise SystemExit(7)'],
                                  root, root / 'evidence', interval=.05, sampler=broken)
            report = json.loads((root / 'evidence/performance.json').read_text())
            self.assertEqual(result, 7)
            self.assertFalse(report['samplingComplete'])
            self.assertGreater(report['sampleErrors'], 0)
            self.assertIsNone(report['minAvailableMemoryBytes'])

    @unittest.skipUnless(os.name == 'nt', 'Windows resource counters')
    def test_real_windows_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = sample_resources(Path(directory))
        self.assertGreater(sample['availableMemoryBytes'], 0)
        self.assertGreater(sample['commitLimitBytes'], 0)
        self.assertGreater(sample['freeDiskBytes'], 0)
        self.assertGreaterEqual(sample['cpuPercent'], 0)
        self.assertIsInstance(sample['processes'], list)
