"""Unit tests for Layer 0 ``StrategyPlugin`` and data contracts in ``msl-core``."""

from __future__ import annotations

import unittest
from typing import ClassVar, cast

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

    def test_parameter_spec_contains_rejects_huge_ints(self) -> None:
        spec = ParameterSpec("a", "a", "int", 10, 1, 100, 1)
        self.assertFalse(spec.contains(10**400))

    def test_window_snapshot_truncation_preserves_metadata(self) -> None:
        rows = ({"date": "2026-01-01", "spread": 10.0}, {"date": "2026-01-02", "spread": 12.0})
        snap = WindowSnapshot(
            universe="calendar_native",
            pair="SRH6/SRM6",
            timeframe="1d",
            rows=rows,
            extra={"monthly": False},
            carry_study={"rows": [{"date": "a"}, {"date": "b"}], "name": "carry"},
            near="SRH6",
            initial_capital_rub=12345,
        )
        prefix = snap.truncated(1)
        self.assertEqual(len(prefix.rows), 1)
        self.assertEqual(prefix.rows[0]["date"], "2026-01-01")
        self.assertEqual(prefix.extra, {"monthly": False})
        self.assertEqual(prefix.carry_study, {"rows": [{"date": "a"}], "name": "carry"})
        self.assertEqual(prefix.near, snap.near)
        self.assertEqual(prefix.initial_capital_rub, snap.initial_capital_rub)
        self.assertEqual(len(snap.rows), 2)

    def test_window_snapshot_truncation_filters_future_funding_rates(self) -> None:
        rows = tuple({"date": f"2026-01-{day:02d}", "spread": 1.0} for day in range(1, 6))
        snap = WindowSnapshot(
            universe="calendar_native",
            pair="SRH6/SRM6",
            rows=rows,
            funding_swaprates={"2026-01-01": 1.0, "2026-01-05": 9.0},
            carry_study={"rows": ("r0", "r1", "r2", "r3", "r4"), "name": "carry"},
        )
        prefix = snap.truncated(2)
        self.assertEqual(prefix.funding_swaprates, {"2026-01-01": 1.0})
        self.assertEqual(prefix.carry_study, {"rows": ("r0", "r1"), "name": "carry"})
        self.assertEqual(snap.funding_swaprates, {"2026-01-01": 1.0, "2026-01-05": 9.0})

    def test_window_snapshot_truncation_rejects_negative_count(self) -> None:
        snap = WindowSnapshot(
            universe="calendar_native",
            pair="SRH6/SRM6",
            rows=({"date": "2026-01-01"},),
        )
        with self.assertRaises(ValueError):
            snap.truncated(-1)

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
                del context
                return (ParameterSpec("lookback", "Окно", "int", 10, 5, 20, 1),)

            def validate_parameters(self, raw, context: InstrumentContext | None = None):
                del context
                return {"lookback": int(raw.get("lookback", 10))}

            def check_compatibility(self, _window: WindowSnapshot, settings=None):
                del settings
                return CompatibilityVerdict(True)

            def evaluate_intents(self, _window: WindowSnapshot, params, fill: str):
                return StrategyIntentRun(
                    strategy_id=self.strategy_id,
                    fill=fill,
                    parameters=dict(params),
                    intents=(),
                )

            def compute_entry_bands(self, series, params, fill: str, *, far_point_value_rub=1.0):
                del far_point_value_rub
                return [EntryBandPoint(date=str(r.get("date", ""))) for r in series]

        self.assertIsInstance(DummyPlugin(), StrategyPlugin)

    def test_declarative_plugin_metadata_accessors(self) -> None:
        from moex_spread_lab.core.contracts import (
            plugin_parameter_aliases,
            plugin_proof_adapter,
            plugin_required_asset_capability,
            plugin_trust_tier,
            plugin_ui_field_order,
        )

        class MetaPlugin:
            proof_adapter = "pair_price_intents.v1"
            family_parameter_aliases: ClassVar[dict[str, str]] = {"window": "lookback"}
            ui_field_order = ("entry_z", "exit_z", "lookback")
            required_asset_capability = "convergence"
            trust_tier = "builtin"

        plugin = cast(StrategyPlugin, MetaPlugin())
        self.assertEqual(plugin_proof_adapter(plugin), "pair_price_intents.v1")
        self.assertEqual(dict(plugin_parameter_aliases(plugin)), {"window": "lookback"})
        self.assertEqual(
            plugin_ui_field_order(plugin, ("fallback",)), ("entry_z", "exit_z", "lookback")
        )
        self.assertEqual(plugin_required_asset_capability(plugin), "convergence")
        self.assertEqual(plugin_trust_tier(plugin), "builtin")

        class BarePlugin:
            pass

        bare = cast(StrategyPlugin, BarePlugin())
        self.assertIsNone(plugin_proof_adapter(bare))
        self.assertEqual(dict(plugin_parameter_aliases(bare)), {})
        self.assertEqual(plugin_ui_field_order(bare, ("fallback",)), ("fallback",))
        self.assertIsNone(plugin_required_asset_capability(bare))
        self.assertEqual(plugin_trust_tier(bare), "plugin")

    def test_plugin_is_builtin_follows_implementation_origin(self) -> None:
        from moex_spread_lab.core.contracts import plugin_is_builtin

        class Shipped:
            __module__ = "moex_spread_lab.strategies.demo.plugin"
            trust_tier = "plugin"

        class DropIn:
            trust_tier = "builtin"

        self.assertTrue(plugin_is_builtin(cast(StrategyPlugin, Shipped())))
        # The declared tier alone cannot identify the engine: a drop-in may inherit it.
        self.assertFalse(plugin_is_builtin(cast(StrategyPlugin, DropIn())))


if __name__ == "__main__":
    unittest.main()
