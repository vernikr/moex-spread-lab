import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.core.w0 import W0Error


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

    def test_timeframe_is_validated_and_survives_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = save_settings(root, {"timeframe": "1h"})
            self.assertEqual(saved["timeframe"], "1h")
            self.assertEqual(save_settings(root, {"slippage_ticks": 2.5})["timeframe"], "1h")
            for value in ("15m", 60, True, None, []):
                with self.subTest(value=value), self.assertRaises(W0Error):
                    save_settings(root, {"timeframe": value})
                self.assertEqual(read_settings(root)["timeframe"], "1h")

    def test_price_fill_is_validated_and_survives_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(read_settings(root)["price_fill"], "reference_same_bar")
            saved = save_settings(root, {"price_fill": "next_open"})
            self.assertEqual(saved["price_fill"], "next_open")
            self.assertEqual(
                save_settings(root, {"slippage_ticks": 2.5})["price_fill"], "next_open"
            )
            for value in ("bad", "same_day_settlement", 1, True, None, []):
                with self.subTest(value=value), self.assertRaises(W0Error):
                    save_settings(root, {"price_fill": value})
                self.assertEqual(read_settings(root)["price_fill"], "next_open")

    def test_random_seed_is_validated_and_survives_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 0 is the default: no fixed seed, a fresh one per run.
            self.assertEqual(read_settings(root)["random_seed"], 0)
            saved = save_settings(root, {"random_seed": 41})
            self.assertEqual(saved["random_seed"], 41)
            self.assertEqual(save_settings(root, {"validation_percent": 5})["random_seed"], 41)
            for value in (-1, 2_147_483_648, 1.5, True, None, "41", []):
                with self.subTest(value=value), self.assertRaises(W0Error):
                    save_settings(root, {"random_seed": value})
                self.assertEqual(read_settings(root)["random_seed"], 41)
            self.assertEqual(save_settings(root, {"random_seed": 0})["random_seed"], 0)

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
                    "initial_capital_rub": 30000.0,
                    "timeframe": "1d",
                    "price_fill": "reference_same_bar",
                    "random_seed": 0,
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
