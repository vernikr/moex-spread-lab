"""Universal ``StrategyPlugin`` conformance suite.

Every built-in and dynamically discovered strategy plugin in ``msl-strategies``
must pass these behavioral invariants automatically:
1. Auto-discovery & synthetic external folder drop-in registration (no central switchboard edits).
2. Protocol structural conformance & parameter grid bounds validation.
3. Determinism: identical inputs produce byte-identical ``StrategyIntentRun`` and entry-band payloads.
4. Causality / zero lookahead: ``evaluate_intents(window.truncated(t))`` and
   ``compute_entry_bands(series[:t])`` produce identical causal entry bands and
   identical closed trade intents strictly before ``t`` as the full window run.
5. Fail-closed validation on invalid parameters, unsupported fills, incompatible universes,
   or missing/corrupt input series.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.contracts import (
    StrategyPlugin,
    WindowSnapshot,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.funding_carry.carry import build_carry_study

EXPECTED_BUILTINS = {
    "mean_reversion",
    "funding_only",
    "zscore_mean_reversion",
    "convergence_to_ruonia",
}


def _pair_mean_reversion_fixture() -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 1, 5)
    rows = []
    series_rows = []
    offsets = [
        0.0, 2.0, -2.0, 3.0, -1.0, 1.5, -2.5, 0.5, 18.0, 16.0,
        2.0, 0.0, -1.0, -19.0, -15.0, -1.0, 0.5, 1.0, -0.5, 0.0,
        2.0, -1.5, 17.5, 3.0, 0.2, -0.4, 0.8, -0.2, 0.1, 0.0,
    ]
    for i, off in enumerate(offsets):
        d = (start + timedelta(days=i)).isoformat()
        near_px = 1000.0 + off
        far_px = 1000.0
        rows.append(
            {
                "date": d,
                "timestamp": f"{d} 00:00:00",
                "trade_date": d,
                "near_open": near_px,
                "near_high": near_px + 2.0,
                "near_low": near_px - 2.0,
                "near_close": near_px,
                "near_volume": 2500.0,
                "far_open": far_px,
                "far_high": far_px + 2.0,
                "far_low": far_px - 2.0,
                "far_close": far_px,
                "far_volume": 2500.0,
                "near_value_rub": near_px,
                "far_value_rub": far_px,
                "spread_value": off,
                "spread_ticks": off,
                "spread_mid": off,
                "benchmark_date": d,
                "benchmark_price": 1.5 + i * 0.0005,
                "benchmark_source_ticker": "LQDT",
                "benchmark_source_kind": "observed",
                "benchmark_mark_kind": "close",
                "near_price_proxy_kind": "close",
                "far_price_proxy_kind": "close",
                "benchmark_daily_return": 0.0005 if i else 0.0,
            }
        )
        series_rows.append(
            {
                "date": d,
                "spread": off,
                "far": far_px,
                "volume_eligible": True,
            }
        )
    contracts = {
        "NEAR": {
            "secid": "NEAR",
            "lot_size": 1,
            "lot_volume": 1,
            "step_price": 1.0,
            "min_step": 1.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": 5000.0,
            "tariff_group": "currency",
        },
        "FAR": {
            "secid": "FAR",
            "lot_size": 1,
            "lot_volume": 1,
            "step_price": 1.0,
            "min_step": 1.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": 5000.0,
            "tariff_group": "currency",
        },
    }
    snap = WindowSnapshot(
        universe="pair_screen",
        pair="NEAR_FAR",
        timeframe="1d",
        rows=tuple(rows),
        contracts=contracts,
        near="NEAR",
        far="FAR",
    )
    return snap, series_rows


def _funding_carry_fixture(tmp_dir: Path) -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 2, 1)
    rates = [2.5] * 10 + [-3.0] * 10 + [3.0] * 10
    near_rows = []
    far_rows = []
    bench_rows = []
    series_rows = []
    for i, rate in enumerate(rates):
        d = (start + timedelta(days=i)).isoformat()
        near_rows.append(
            {
                "TRADEDATE": d,
                "SECID": "CNYRUBF",
                "SETTLEPRICE": 13.50 + i * 0.01,
                "OPEN": 13.50 + i * 0.01,
                "HIGH": 13.55 + i * 0.01,
                "LOW": 13.45 + i * 0.01,
                "CLOSE": 13.50 + i * 0.01,
                "VOLUME": 5000,
                "OPENPOSITION": 10000,
                "SWAPRATE": rate,
            }
        )
        far_rows.append(
            {
                "TRADEDATE": d,
                "SECID": "CRH6",
                "SETTLEPRICE": 13.80 + i * 0.01,
                "OPEN": 13.80 + i * 0.01,
                "HIGH": 13.85 + i * 0.01,
                "LOW": 13.75 + i * 0.01,
                "CLOSE": 13.80 + i * 0.01,
                "VOLUME": 5000,
                "OPENPOSITION": 10000,
                "SWAPRATE": None,
            }
        )
        bench_rows.append(
            {
                "date": d,
                "price": round(1.50 + i * 0.0005, 6),
                "source_kind": "observed",
                "source_ticker": "LQDT",
            }
        )
        series_rows.append({"date": d, "spread": -300.0, "far": 13.80, "volume_eligible": True})

    contracts_payload = {
        "instruments": [
            {
                "secid": "CNYRUBF",
                "stable_id": "cny-perp",
                "board": "RFUD",
                "expiry": "2100-01-01",
                "lot_size": 1000,
                "tick_size": 0.001,
                "tick_value": 1.0,
                "initial_margin": 2000.0,
                "tariff_group": "currency",
                "source_vintage": "current_snapshot",
            },
            {
                "secid": "CRH6",
                "stable_id": "cny-far",
                "board": "RFUD",
                "expiry": "2026-06-18",
                "lot_size": 1000,
                "tick_size": 0.001,
                "tick_value": 1.0,
                "initial_margin": 2000.0,
                "tariff_group": "currency",
                "source_vintage": "current_snapshot",
            },
        ]
    }
    contracts_path = tmp_dir / "contracts.json"
    contracts_path.write_text(json.dumps(contracts_payload), encoding="utf-8")
    benchmark_path = tmp_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps({"schema_version": "lqdt-lineage.v1", "rows": bench_rows}),
        encoding="utf-8",
    )
    near_src = {
        "schema_version": "iss-history.v1",
        "secid": "CNYRUBF",
        "rows": near_rows,
        "source": {"raw_response_sha256": ["near-hash"]},
    }
    far_src = {
        "schema_version": "iss-history.v1",
        "secid": "CRH6",
        "rows": far_rows,
        "source": {"raw_response_sha256": ["far-hash"]},
    }
    study = build_carry_study(
        near_src,
        far_src,
        contracts_path,
        near="CNYRUBF",
        far="CRH6",
        funding_regimes=(),
    )
    snap = WindowSnapshot(
        universe="pair_screen",
        pair="CNYRUBF_CRH6",
        timeframe="1d",
        rows=tuple(study["rows"]),
        carry_study=study,
        benchmark=benchmark_path,
        near="CNYRUBF",
        far="CRH6",
        extra={"contracts_path": contracts_path},
    )
    return snap, series_rows


def _calendar_fixture() -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 1, 10)
    spreads = [
        1200.0, 1210.0, 1195.0, 1205.0, 1198.0, 1202.0, 1207.0, 1193.0, 1201.0, 1199.0,
        1550.0, 1540.0, 1202.0, 1200.0, 860.0, 865.0, 1200.0, 1201.0, 1199.0, 1200.0,
        1204.0, 1196.0, 1520.0, 1203.0, 1200.0, 1198.0, 1202.0, 1197.0, 1200.0, 1200.0,
    ]
    # Include near-expiry buffer exit around bar 18 for RUONIA convergence
    dtes = [
        45, 44, 43, 42, 41, 40, 39, 38, 37, 36,
        25, 24, 23, 22, 10, 2, 20, 19, 18, 17,
        16, 15, 14, 13, 12, 2, 10, 9, 8, 7,
    ]
    rows = []
    series_rows = []
    for i, (sp, dte) in enumerate(zip(spreads, dtes, strict=True)):
        d = (start + timedelta(days=i)).isoformat()
        near_val = 30_000.0
        far_val = near_val + sp
        rate = 0.16
        implied = sp / near_val * 365.0 / 91.0
        dev_pp = (implied - rate) * 100.0
        scale = near_val * 91.0 / 36500.0
        rows.append(
            {
                "date": d,
                "spread": sp,
                "open_spread": sp,
                "near_close": near_val,
                "far_close": far_val,
                "dev_pp": dev_pp,
                "days_to_near_expiry": dte,
                "dividend_in_window": False,
                "margin": 6000.0,
                "min_volume": 2500.0,
                "signal_eligible": True,
                "entry_eligible": True,
                "exit_eligible": True,
            }
        )
        series_rows.append(
            {
                "date": d,
                "spread": sp,
                "far": far_val,
                "point_value_rub": 1.0,
                "dev_pp": dev_pp,
                "rate_price_per_pp": scale,
                "volume_eligible": True,
            }
        )
    snap = WindowSnapshot(
        universe="calendar_native",
        pair="SRH6/SRM6",
        asset="SR",
        timeframe="1d",
        rows=tuple(rows),
        extra={"monthly": False, "convergence": True},
    )
    return snap, series_rows


class StrategyPluginConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fixture_for(self, strategy_id: str) -> tuple[WindowSnapshot, list[dict], dict]:
        if strategy_id == "mean_reversion":
            snap, series = _pair_mean_reversion_fixture()
            return snap, series, {"lookback": 6, "entry_z": 1.5, "exit_z": 0.5}
        if strategy_id == "funding_only":
            snap, series = _funding_carry_fixture(self.tmp_path)
            return snap, series, {"lookback": 5}
        if strategy_id == "zscore_mean_reversion":
            snap, series = _calendar_fixture()
            return snap, series, {"window": 6, "entry_z": 1.5, "exit_z": 0.5}
        if strategy_id == "convergence_to_ruonia":
            snap, series = _calendar_fixture()
            return snap, series, {"threshold_pp": 1.0}
        raise AssertionError(f"Unhandled strategy fixture: {strategy_id}")

    def test_auto_discovers_all_builtin_strategy_plugins(self) -> None:
        registry = discover_strategies()
        self.assertEqual(set(registry.ids()), EXPECTED_BUILTINS)
        for plugin in registry.values():
            self.assertIsInstance(plugin, StrategyPlugin)
            self.assertTrue(plugin.label_ru)
            self.assertTrue(plugin.supported_universes)
            self.assertTrue(plugin.supported_fills)

    def test_synthetic_drop_in_strategy_folder_registers_without_core_edits(self) -> None:
        pkg_dir = self.tmp_path / "synthetic_spread"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "plugin.py").write_text(
            """from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
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
""",
            encoding="utf-8",
        )
        registry = discover_strategies(extra_paths=(self.tmp_path,))
        self.assertIn("synthetic_drop_in", registry)
        plugin = registry.get("synthetic_drop_in")
        self.assertIsInstance(plugin, StrategyPlugin)
        snap, series = _calendar_fixture()
        run = plugin.evaluate_intents(snap, {"threshold": 2.0}, "next_open")
        self.assertEqual(run.strategy_id, "synthetic_drop_in")
        self.assertEqual(len(plugin.compute_entry_bands(series, {"threshold": 2.0}, "next_open")), len(series))

    def test_parameter_specs_and_fail_closed_validation(self) -> None:
        registry = discover_strategies()
        for strategy_id, plugin in registry.items():
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

    def test_determinism_across_repeated_evaluations(self) -> None:
        registry = discover_strategies()
        for strategy_id, plugin in registry.items():
            snap, series, params = self._fixture_for(strategy_id)
            for fill in plugin.supported_fills:
                with self.subTest(strategy=strategy_id, fill=fill):
                    first = plugin.evaluate_intents(snap, params, fill)
                    second = plugin.evaluate_intents(snap, params, fill)
                    self.assertEqual(
                        json.dumps(first.intents, sort_keys=True, default=str),
                        json.dumps(second.intents, sort_keys=True, default=str),
                    )
                    bands_1 = plugin.compute_entry_bands(series, params, fill)
                    bands_2 = plugin.compute_entry_bands(series, params, fill)
                    self.assertEqual(bands_1, bands_2)

    def test_truncation_causality_no_lookahead(self) -> None:
        """Truncating future bars must never alter causal entry bands or already-closed intents."""
        registry = discover_strategies()
        for strategy_id, plugin in registry.items():
            snap, series, params = self._fixture_for(strategy_id)
            fill = plugin.supported_fills[0]
            full_run = plugin.evaluate_intents(snap, params, fill)
            full_bands = plugin.compute_entry_bands(series, params, fill)
            self.assertGreaterEqual(
                len(full_run.intents),
                1,
                f"Fixture for {strategy_id} must exercise at least 1 trade intent",
            )
            for cutoff in (14, 20, 26):
                with self.subTest(strategy=strategy_id, fill=fill, cutoff=cutoff):
                    prefix_snap = snap.truncated(cutoff)
                    prefix_run = plugin.evaluate_intents(prefix_snap, params, fill)
                    prefix_bands = plugin.compute_entry_bands(series[:cutoff], params, fill)
                    self.assertEqual(prefix_bands, full_bands[:cutoff])

                    cutoff_date = str(series[cutoff - 1]["date"])[:10]
                    forced = {"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"}

                    def _closed_strictly_before(intents_seq, limit_day: str):
                        return [
                            (item["side"], str(item["entry_date"])[:10], str(item["exit_date"])[:10], item["reason"])
                            for item in intents_seq
                            if str(item["exit_date"])[:10] < limit_day
                            and item["reason"] not in forced
                            and not item.get("open_at_end")
                        ]

                    self.assertEqual(
                        _closed_strictly_before(prefix_run.intents, cutoff_date),
                        _closed_strictly_before(full_run.intents, cutoff_date),
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
        for plugin in registry.values():
            with self.assertRaises(W0Error):
                plugin.evaluate_intents(
                    WindowSnapshot(universe="unknown_universe", pair="X"),
                    {},
                    "unsupported_fill_mode",
                )


if __name__ == "__main__":
    unittest.main()


def test_calendar_intent_adapter_matches_existing_engines():
    from moex_spread_lab.strategies.base import PRICE_FILLS, ZSCORE_CALENDAR_OPTIONS
    from moex_spread_lab.strategies.calendar.intents import calendar_intent_trades
    from moex_spread_lab.strategies.calendar_ruonia.engine import convergence_trades
    from moex_spread_lab.strategies.calendar_zscore.engine import zscore_trades

    window, _ = _calendar_fixture()
    registry = discover_strategies()
    for sid, params in (
        ("zscore_mean_reversion", {"window": 6, "entry_z": 1.5, "exit_z": 0.5}),
        ("convergence_to_ruonia", {"threshold_pp": 1.0}),
    ):
        plugin = registry.get(sid)
        normalized = plugin.validate_parameters(params)
        for fill, (delay, quote) in PRICE_FILLS.items():
            actual = calendar_intent_trades(plugin, window, normalized, fill)
            if sid == "zscore_mean_reversion":
                options = {**ZSCORE_CALENDAR_OPTIONS["quarterly"], **normalized}
                expected = zscore_trades(list(window.rows), delay=delay, fill=quote,
                                         skip_dividend_windows=False, close_at_end=True, **options)
            else:
                expected = convergence_trades(list(window.rows), delay=delay, fill=quote,
                                              threshold=normalized["threshold_pp"], close_at_end=True)
            assert actual == expected
