from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_budget import compile_budget


class BuildBudgetTests(unittest.TestCase):
    def test_slow_preparation_shortens_compile_without_spending_save_budget(self):
        started = datetime(2026, 9, 29, tzinfo=timezone.utc)
        for elapsed in (0, 9.2, 150, 303.9):
            current = started + timedelta(minutes=elapsed)
            report = compile_budget(started, current)
            end = current + timedelta(minutes=report['compileMinutes'])
            self.assertLessEqual(end, started + timedelta(minutes=305))
            self.assertLess((started + timedelta(minutes=305) - end).total_seconds(), 60)
            self.assertGreaterEqual((started + timedelta(minutes=360) - end).total_seconds(), 55 * 60)

    def test_exhausted_budget_and_invalid_clock_do_not_start_a_build(self):
        started = datetime(2026, 9, 29, tzinfo=timezone.utc)
        for elapsed in (304.1, 305, 360, 400):
            with self.assertRaises(RuntimeError):
                compile_budget(started, started + timedelta(minutes=elapsed))
        with self.assertRaises(ValueError):
            compile_budget(started, started - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            compile_budget(started.replace(tzinfo=None), started)
