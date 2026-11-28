import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import pytest

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
                periods = p.metadata()["periods"]
                assert isinstance(periods, dict)
                self.assertEqual(
                    periods["validation"],
                    [dates[-n], dates[-1]] if n else None,
                )
        for dates, percent in (([], 0), (["d"], 1), (["d"], 50)):
            with self.assertRaisesRegex(W0Error, "Недостаточно наблюдений"):
                EvaluationSplit.from_dates(dates, percent)

    def test_save_settings_serializes_concurrent_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for _ in range(30):
                # Reset to defaults between rounds to expose the lost update.
                base = save_settings(root, {"slippage_ticks": 1.0, "initial_capital_rub": 30000.0})
                assert base["slippage_ticks"] == 1.0
                barrier = threading.Barrier(2)

                def worker(patch: dict[str, object], barrier: threading.Barrier = barrier) -> None:
                    barrier.wait(timeout=5)
                    save_settings(root, patch)

                first = threading.Thread(target=worker, args=({"slippage_ticks": 3.0},))
                second = threading.Thread(target=worker, args=({"initial_capital_rub": 50000.0},))
                first.start()
                second.start()
                first.join(timeout=10)
                second.join(timeout=10)
                final = read_settings(root)
                self.assertEqual(final["slippage_ticks"], 3.0)
                self.assertEqual(final["initial_capital_rub"], 50000.0)

    def test_save_settings_fsyncs_before_replace(self):
        import moex_spread_lab.core.settings as settings_module

        with (
            tempfile.TemporaryDirectory() as directory,
            unittest.mock.patch.object(
                settings_module.os, "fsync", wraps=settings_module.os.fsync
            ) as fsync_mock,
        ):
            root = Path(directory)
            save_settings(root, {"slippage_ticks": 2.5})
            self.assertGreaterEqual(fsync_mock.call_count, 1)

    def test_from_dates_rejects_unsorted_and_duplicate_dates(self):
        for dates in (
            ("2026-01-05", "2026-01-01", "2026-01-03"),
            ("2026-01-01", "2026-01-01", "2026-01-03"),
            ("b", "a"),
        ):
            with (
                self.subTest(dates=dates),
                self.assertRaisesRegex(W0Error, "строго возрастающими"),
            ):
                EvaluationSplit.from_dates(dates, 30)

    def test_metadata_reports_effective_validation_percent(self):
        dates = tuple(f"d{i:02}" for i in range(10))
        split = EvaluationSplit.from_dates(dates, 0.01)
        self.assertEqual(len(split.validation), 1)
        self.assertEqual(split.metadata()["validation_percent"], 0.01)
        self.assertEqual(split.metadata()["effective_validation_percent"], 10.0)
        zero = EvaluationSplit.from_dates(dates, 0)
        self.assertEqual(zero.metadata()["effective_validation_percent"], 0.0)

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


class ResearchSettingsBoundaryTests(unittest.TestCase):
    def test_invalid_values_are_rejected_at_construction(self):
        bad = (
            {"validation_percent": 99},
            {"validation_percent": -1},
            {"slippage_ticks": -5},
            {"slippage_ticks": 11},
            {"initial_capital_rub": -1},
            {"initial_capital_rub": 0},
            {"timeframe": "fortnight"},
            {"price_fill": "same_tick"},
            {"allow_failed_volume": "yes"},
            {"allow_failed_capital": 1},
            {"random_seed": -1},
            {"random_seed": 2_147_483_648},
            {"random_seed": 1.5},
        )
        for kwargs in bad:
            with self.subTest(case=kwargs), self.assertRaises(W0Error):
                ResearchSettings(**kwargs)

    def test_construction_normalizes_to_loader_semantics(self):
        settings = ResearchSettings(validation_percent=10.0, slippage_ticks=2)
        self.assertEqual(settings.validation_percent, 10)
        self.assertEqual(settings.slippage_ticks, 2.0)

    def test_metadata_carries_schema_marker_and_survives_roundtrip(self):
        settings = ResearchSettings()
        self.assertEqual(settings.metadata()["schema"], "research-settings.v1")
        for key in ResearchSettings.__dataclass_fields__:
            self.assertIn(key, settings.metadata())

    def test_settings_with_override_accepts_metadata_roundtrip(self):
        from moex_spread_lab.core.settings import settings_with_override

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = save_settings(root, ResearchSettings().metadata())
            self.assertEqual(saved["validation_percent"], 0)
            override = settings_with_override(root, {"validation_percent": 25})
            self.assertEqual(override.validation_percent, 25)


def test_nonfinite_preferences_are_rejected_without_changing_saved_settings(tmp_path):
    saved = save_settings(tmp_path, {"validation_percent": 5})
    for key in ("validation_percent", "initial_capital_rub", "slippage_ticks", "random_seed"):
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(W0Error):
                save_settings(tmp_path, {key: value})
            assert read_settings(tmp_path) == saved


def test_failed_atomic_replace_preserves_settings_and_removes_temp(tmp_path, monkeypatch):
    from moex_spread_lab.core import settings

    saved = save_settings(tmp_path, {"validation_percent": 5})
    path = settings_path(tmp_path)

    def fail_replace(*_paths: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(settings.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        save_settings(tmp_path, {"validation_percent": 10})
    assert read_settings(tmp_path) == saved
    assert sorted(path.parent.iterdir()) == sorted([path, path.with_name(path.name + ".lock")])
