"""Unit tests for Layer 0 ``StrategyPlugin`` and data contracts in ``msl-core``."""

from __future__ import annotations

import unittest

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    StrategyPlugin,
    WindowSnapshot,
)


class CoreContractsTests(unittest.TestCase):
    def test_parameter_spec_validates_grid_and_finiteness(self) -> None:
        int_spec = ParameterSpec("lookback", "Окно · набл.", "int", 20, 5, 80, 1)
        self.assertTrue(int_spec.contains(5))
        self.assertTrue(int_spec.contains(20))
        self.assertFalse(int_spec.contains(20.0))
        self.assertFalse(int_spec.contains(True))
        self.assertFalse(int_spec.contains(4))
        self.assertEqual(int_spec.to_dict()["name"], "lookback")

        float_spec = ParameterSpec("entry_z", "Вход · z", "float", 1.5, 0.5, 5.0, 0.1)
        self.assertTrue(float_spec.contains(1.5))
        self.assertFalse(float_spec.contains(0.4))
        self.assertFalse(float_spec.contains(float("nan")))

    def test_window_snapshot_truncation_preserves_metadata(self) -> None:
        rows = ({"date": "2026-01-01", "spread": 10.0}, {"date": "2026-01-02", "spread": 12.0})
        snap = WindowSnapshot(
            universe="calendar_native",
            pair="SRH6/SRM6",
            timeframe="1d",
            rows=rows,
            extra={"monthly": False},
        )
        prefix = snap.truncated(1)
        self.assertEqual(len(prefix.rows), 1)
        self.assertEqual(prefix.rows[0]["date"], "2026-01-01")
        self.assertEqual(prefix.extra, {"monthly": False})

    def test_compatibility_verdict_fields(self) -> None:
        self.assertTrue(CompatibilityVerdict(True).compatible)
        rejected = CompatibilityVerdict(False, "requires daily funding")
        self.assertFalse(rejected.compatible)
        self.assertEqual(rejected.reason, "requires daily funding")

    def test_runtime_checkable_strategy_plugin_protocol(self) -> None:
        class DummyPlugin:
            strategy_id = "dummy"
            family_id = "dummy"
            label_ru = "Тестовая стратегия"
            short_label_ru = "тест"
            supported_universes = frozenset({"futures_pair"})
            supported_fills = ("next_open",)
            requires_daily_bars = False

            def parameter_schema(self, context: InstrumentContext | None = None):
                return (ParameterSpec("lookback", "Окно", "int", 10, 5, 20, 1),)

            def validate_parameters(self, raw, context: InstrumentContext | None = None):
                return {"lookback": int(raw.get("lookback", 10))}

            def check_compatibility(self, window: WindowSnapshot, settings=None):
                return CompatibilityVerdict(True)

            def evaluate_intents(self, window: WindowSnapshot, params, fill: str):
                return StrategyIntentRun(
                    strategy_id=self.strategy_id,
                    fill=fill,
                    parameters=dict(params),
                    intents=(),
                )

            def compute_entry_bands(self, series, params, fill: str, *, far_point_value_rub=1.0):
                return [EntryBandPoint(date=str(r.get("date", ""))) for r in series]

        self.assertIsInstance(DummyPlugin(), StrategyPlugin)


if __name__ == "__main__":
    unittest.main()
