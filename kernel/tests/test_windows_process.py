from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows process semantics')
class WindowsProcessTests(unittest.TestCase):
    def run_parent(self, body):
        support = str(Path(__file__).resolve().parents[1])
        source = f'import sys, subprocess\nsys.path.insert(0, {support!r})\nfrom build_support import run_windows_process\n' + body
        return subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, timeout=15)

    def test_hidden_child_keeps_stdout_and_stderr_when_parent_is_redirected(self):
        result = self.run_parent("run_windows_process([sys.executable, '-c', \"import sys; print('child-stdout'); print('child-stderr', file=sys.stderr)\"], timeout=5)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('child-stdout', result.stdout)
        self.assertIn('child-stderr', result.stderr)

    def test_nonzero_exit_is_not_reclassified_as_a_timeout(self):
        result = self.run_parent("try:\n run_windows_process([sys.executable, '-c', 'raise SystemExit(7)'], timeout=5)\nexcept subprocess.CalledProcessError as error:\n print('exit', error.returncode)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exit 7', result.stdout)

    def test_timeout_is_reported_after_terminating_owned_process(self):
        result = self.run_parent("try:\n run_windows_process([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.2)\nexcept subprocess.TimeoutExpired:\n print('timed-out')\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('timed-out', result.stdout)


if __name__ == '__main__':
    unittest.main()
