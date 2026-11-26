"""Universal ``StrategyPlugin`` conformance suite.

Every built-in and dynamically discovered strategy plugin in ``msl-strategies``
must pass these behavioral invariants automatically:
1. Auto-discovery & synthetic external folder drop-in registration (no central switchboard edits).
2. Protocol structural conformance & parameter grid bounds validation.
3. Determinism and causality / zero lookahead, delegated to
   ``conformance.assert_causal_conformance`` (single source of truth for both).
4. Fail-closed validation on invalid parameters, unsupported fills, incompatible universes,
   or missing/corrupt input series.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from conformance_fixtures import (
    BUILTIN_PARAMS,
    CONFORMANCE_CUTOFFS,
    PAIR_PARAMS,
    RUONIA_PARAMS,
    ZSCORE_PARAMS,
    builtin_fixture,
    calendar_fixture,
    pair_mean_reversion_fixture,
)
from moex_spread_lab.core.contracts import (
    EntryBandPoint,
    StrategyIntentRun,
    StrategyPlugin,
    WindowSnapshot,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.conformance import assert_causal_conformance

EXPECTED_BUILTINS = frozenset(BUILTIN_PARAMS)

SYNTHETIC_PLUGIN_SOURCE = '''"""Plugin written by a foreign package into a discovered folder."""
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)


class SyntheticPlugin:
    strategy_id = "synthetic_drop_in"
    family_id = "synthetic_drop_in"
    label_ru = "Синтетический плагин"
    short_label_ru = "синтетика"
    supported_universes = frozenset({"calendar_native"})
    supported_fills = ("next_open",)
    requires_daily_bars = False

    def parameter_schema(self, context=None):
        return (ParameterSpec("threshold", "Порог", "float", 2.0, 1.0, 5.0, 0.5),)

    def validate_parameters(self, raw, context=None):
        return {"threshold": float(raw.get("threshold", 2.0))}

    def check_compatibility(self, window: WindowSnapshot, settings=None):
        return CompatibilityVerdict(True)

    def evaluate_intents(self, window: WindowSnapshot, params, fill: str):
        return StrategyIntentRun(
            strategy_id=self.strategy_id,
            fill=fill,
            parameters=self.validate_parameters(params),
            intents=(),
        )

    def compute_entry_bands(self, series, params, fill: str, *, far_point_value_rub=1.0):
        return [EntryBandPoint(date=str(r.get("date", ""))) for r in series]


PLUGIN = SyntheticPlugin()
'''


class StrategyPluginConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_auto_discovers_all_builtin_strategy_plugins(self) -> None:
        registry = discover_strategies()
        self.assertTrue(set(registry.ids()) >= EXPECTED_BUILTINS)
        for plugin in registry.values():
            self.assertIsInstance(plugin, StrategyPlugin)
            self.assertTrue(plugin.label_ru)
            self.assertTrue(plugin.supported_universes)
            self.assertTrue(plugin.supported_fills)

    def test_synthetic_drop_in_strategy_folder_registers_without_core_edits(self) -> None:
        pkg_dir = self.tmp_path / "synthetic_spread"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "plugin.py").write_text(SYNTHETIC_PLUGIN_SOURCE, encoding="utf-8")
        registry = discover_strategies(extra_paths=(self.tmp_path,))
        self.assertIn("synthetic_drop_in", registry)
        plugin = registry.get("synthetic_drop_in")
        self.assertIsInstance(plugin, StrategyPlugin)
        snap, series = calendar_fixture()
        run = plugin.evaluate_intents(snap, {"threshold": 2.0}, "next_open")
        self.assertEqual(run.strategy_id, "synthetic_drop_in")
        self.assertEqual(
            len(plugin.compute_entry_bands(series, {"threshold": 2.0}, "next_open")), len(series)
        )

    def test_parameter_specs_and_fail_closed_validation(self) -> None:
        registry = discover_strategies()
        for strategy_id in sorted(EXPECTED_BUILTINS):
            plugin = registry.get(strategy_id)
            with self.subTest(strategy=strategy_id):
                schema = plugin.parameter_schema()
                validated = plugin.validate_parameters({})
                for spec in schema:
                    self.assertIn(spec.name, validated)
                    self.assertTrue(spec.contains(spec.default))
                with self.assertRaises(W0Error):
                    plugin.validate_parameters({"unknown_axis": 999})
                first_spec = schema[0]
                with self.assertRaises(W0Error):
                    plugin.validate_parameters({first_spec.name: first_spec.max_value + 100})

    def test_builtin_fixtures_are_deterministic_and_causal(self) -> None:
        registry = discover_strategies()
        for strategy_id, params in sorted(BUILTIN_PARAMS.items()):
            with self.subTest(strategy=strategy_id):
                snap, series = builtin_fixture(strategy_id, self.tmp_path)
                assert_causal_conformance(
                    registry.get(strategy_id),
                    snap,
                    series,
                    params,
                    cutoffs=CONFORMANCE_CUTOFFS,
                )

    def test_compatibility_checks_fail_closed(self) -> None:
        registry = discover_strategies()
        funding = registry.get("funding_only")
        self.assertFalse(
            funding.check_compatibility(
                WindowSnapshot(universe="futures_pair", pair="CNYRUBF_CRH6", timeframe="1h")
            ).compatible
        )
        ruonia = registry.get("convergence_to_ruonia")
        self.assertFalse(
            ruonia.check_compatibility(
                WindowSnapshot(
                    universe="calendar_native",
                    pair="LKH6/LKM6",
                    asset="LK",
                    timeframe="1d",
                    rows=(),
                    extra={"convergence": False},
                )
            ).compatible
        )
        for strategy_id in sorted(EXPECTED_BUILTINS):
            plugin = registry.get(strategy_id)
            with self.assertRaises(W0Error):
                plugin.evaluate_intents(
                    WindowSnapshot(universe="unknown_universe", pair="X"),
                    {},
                    "unsupported_fill_mode",
                )


def test_calendar_intent_adapter_matches_existing_engines():
    from moex_spread_lab.strategies.base import PRICE_FILLS, ZSCORE_CALENDAR_OPTIONS
    from moex_spread_lab.strategies.calendar.intents import calendar_intent_trades
    from moex_spread_lab.strategies.calendar_ruonia.engine import convergence_trades
    from moex_spread_lab.strategies.calendar_zscore.engine import zscore_trades

    window, _ = calendar_fixture()
    registry = discover_strategies()
    for sid, params in (
        ("zscore_mean_reversion", ZSCORE_PARAMS),
        ("convergence_to_ruonia", RUONIA_PARAMS),
    ):
        plugin = registry.get(sid)
        normalized = plugin.validate_parameters(params)
        for fill, (delay, quote) in PRICE_FILLS.items():
            actual = calendar_intent_trades(plugin, window, normalized, fill)
            if sid == "zscore_mean_reversion":
                options = {**ZSCORE_CALENDAR_OPTIONS["quarterly"], **normalized}
                expected = zscore_trades(
                    list(window.rows),
                    delay=delay,
                    fill=quote,
                    skip_dividend_windows=False,
                    close_at_end=True,
                    **options,
                )
            else:
                expected = convergence_trades(
                    list(window.rows),
                    delay=delay,
                    fill=quote,
                    threshold=normalized["threshold_pp"],
                    close_at_end=True,
                )
            assert actual == expected


def test_pair_intent_boundary_reproduces_legacy_price_intents():
    """The legacy plugin's timing is made explicit without changing its output."""
    from dataclasses import replace

    from moex_spread_lab.strategies.base import PRICE_FILLS
    from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades

    window, _ = pair_mean_reversion_fixture()
    original = discover_strategies().get("mean_reversion")

    class WithExplicitSignal:
        strategy_id = original.strategy_id
        supported_fills = original.supported_fills

        def evaluate_intents(self, snapshot, params, fill):
            result = original.evaluate_intents(snapshot, params, fill)
            delay = PRICE_FILLS[fill][0]
            days = [str(row["date"])[:10] for row in snapshot.rows]
            enriched = []
            for item in result.intents:
                i = days.index(item["entry_date"][:10])
                enriched.append(
                    {
                        **item,
                        "signal": {
                            "date": days[i - delay],
                            "kind": "z",
                            "value": item["entry_signal_z"],
                        },
                    }
                )
            return replace(result, intents=tuple(enriched))

    for fill in original.supported_fills:
        expected = original.evaluate_intents(window, PAIR_PARAMS, fill).intents
        actual = pair_price_intent_trades(WithExplicitSignal(), window, PAIR_PARAMS, fill)
        assert len(actual) == len(expected) > 0
        for got, old in zip(actual, expected, strict=True):
            assert got["entry"]["date"] == old["entry_date"][:10]
            assert got["exit"]["date"] == old["exit_date"][:10]
            assert got["entry"]["near_price"] == old["entry_near_fill"]
            assert got["entry"]["far_price"] == old["entry_far_fill"]
            assert got["exit"]["near_price"] == old["exit_near_fill"]
            assert got["exit"]["far_price"] == old["exit_far_fill"]
            assert got["exit_reason"] == old["reason"]
            assert got["open_at_end"] == (old["reason"] == "end_of_sample")


class _FixturePlugin:
    """Minimal plugin whose two failure modes the conformance helper must catch."""

    strategy_id = "fixture"
    supported_fills: ClassVar[tuple[str, ...]] = ("next_open",)

    def __init__(self, *, lookahead: bool = False, nondeterministic: bool = False) -> None:
        self.lookahead = lookahead
        self.nondeterministic = nondeterministic
        self.calls = 0

    def evaluate_intents(self, window, params, fill) -> StrategyIntentRun:
        self.calls += 1
        value = self.calls if self.nondeterministic else 1
        return StrategyIntentRun(
            self.strategy_id,
            fill,
            {},
            (
                {
                    "side": "long_spread",
                    "entry_date": "2026-01-01",
                    "exit_date": "2026-01-02",
                    "reason": "exit",
                    "value": value,
                },
            ),
        )

    def compute_entry_bands(self, series, params, fill, **_options: Any):
        value = len(series) if self.lookahead else 1
        return [EntryBandPoint(date=r["date"], buy_level=value) for r in series]


def _helper_fixture() -> tuple[WindowSnapshot, list[dict[str, Any]]]:
    rows = [{"date": f"2026-01-{i:02}"} for i in range(1, 9)]
    return WindowSnapshot(universe="calendar_native", pair="A/B", rows=rows), rows


def test_conformance_helper_accepts_a_causal_plugin():
    window, rows = _helper_fixture()
    assert_causal_conformance(_FixturePlugin(), window, rows, {}, cutoffs=(4, 6))


def test_conformance_helper_rejects_future_dependent_bands():
    window, rows = _helper_fixture()
    with unittest.TestCase().assertRaisesRegex(AssertionError, "future-dependent bands"):
        assert_causal_conformance(_FixturePlugin(lookahead=True), window, rows, {}, cutoffs=(4,))


def test_conformance_helper_rejects_nondeterministic_intents():
    window, rows = _helper_fixture()
    with unittest.TestCase().assertRaisesRegex(AssertionError, "nondeterministic intents"):
        assert_causal_conformance(
            _FixturePlugin(nondeterministic=True), window, rows, {}, cutoffs=(4,)
        )
