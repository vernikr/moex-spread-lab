import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.evaluation_periods import (
    EvaluationSplit,
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.w0 import W0Error


class GlobalPeriodTests(unittest.TestCase):
    def test_chronological_exact_cover_rounding_and_endpoints(self):
        dates = tuple(f"d{i:02}" for i in range(19))
        for percent, n in [(0, 0), (0.1, 1), (1, 1), (12.5, 2), (50, 9)]:
            with self.subTest(percent=percent):
                p = EvaluationSplit.from_dates(dates, percent)
                self.assertEqual(p.training + p.validation, dates)
                self.assertEqual(len(p.validation), n)
                self.assertEqual(p.basis, "validation" if n else "training")
                self.assertEqual(
                    p.metadata()["periods"]["validation"],
                    [dates[-n], dates[-1]] if n else None,
                )
        for dates, percent in (([], 0), (["d"], 1), (["d"], 50)):
            with self.assertRaisesRegex(W0Error, "Недостаточно наблюдений"):
                EvaluationSplit.from_dates(dates, percent)

    def test_project_settings_persist_without_touching_input_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                read_settings(root),
                {
                    "validation_percent": 0,
                    "allow_failed_volume": True,
                    "allow_failed_capital": True,
                    "slippage_ticks": 1.0,
                },
            )
            self.assertFalse(settings_path(root).exists())
            saved = save_settings(root, {"validation_percent": 12.5})
            for value in (
                -1,
                50.001,
                True,
                False,
                None,
                "2",
                [10],
                float("nan"),
                float("inf"),
            ):
                with self.subTest(value=value), self.assertRaises(W0Error):
                    save_settings(root, {"validation_percent": value})
                self.assertEqual(read_settings(root), saved)
            for body in ([], None, {}, {"validation_percent": 0, "pair": "x"}):
                with self.assertRaises(W0Error):
                    save_settings(root, body)
            settings_path(root).write_text("corrupt")
            with self.assertRaises(W0Error):
                read_settings(root)
            save_settings(root, ResearchSettings().metadata())
            self.assertEqual(read_settings(root)["validation_percent"], 0)
