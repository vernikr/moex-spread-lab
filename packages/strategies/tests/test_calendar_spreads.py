import json
import math
import unittest
from datetime import date
from pathlib import Path
from typing import ClassVar

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data import universe as data_universe
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.calendar import contracts
from moex_spread_lab.strategies.calendar.calendar_spreads import (
    CalendarData,
    build_windows,
    contract_chain,
    positive_spread_volume_rows,
    stress_ok,
)
from moex_spread_lab.strategies.calendar.data_cache import slice_window_through
from moex_spread_lab.strategies.calendar.proof_payload import build_proof_payload
from moex_spread_lab.strategies.calendar.proof_runner import (
    _build_h1_eligible_rows,
    execute_calendar_proof,
)
from synthetic_history import iso_weekdays_until as _days

TERMS = {"mult": 1.0, "margin_rate": 0.15}
RUONIA = (["2022-01-01"], {"2022-01-01": 0.15})


def _row(day: str, settle: float, volume: int = 500, opening: float | None = None) -> dict:
    return {
        "TRADEDATE": day,
        "SETTLEPRICE": settle,
        "OPEN": settle if opening is None else opening,
        "VOLUME": volume,
    }


def _histories(thin_day: str | None = None) -> dict:
    near_days = _days(date(2022, 12, 12), date(2023, 3, 16))
    far_days = _days(date(2022, 12, 12), date(2023, 6, 15))
    spread = {d: _row(d, 1000.0, 5 if d == thin_day else 500) for d in near_days}
    return {
        "SRH3": {d: _row(d, 30000.0) for d in near_days},
        "SRM3": {d: _row(d, 31000.0, opening=31010.0) for d in far_days},
        "SRH3SRM3": spread,
    }


class CalendarSpreadTests(unittest.TestCase):
    def test_chain_ends_at_the_requested_far_contract(self):
        chain = contract_chain("SR")
        self.assertEqual((chain[0], chain[-1]), ("SRH3", "SRH7"))

    def test_window_prices_spread_from_leg_settlements_and_stops_before_expiry(self):
        windows = build_windows(_histories(), "SR", TERMS, RUONIA)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window["near_expiry"], "2023-03-16")
        self.assertLessEqual(window["last_date"], "2023-03-14")
        first = window["rows"][0]
        self.assertEqual(first["spread"], 1000.0)
        self.assertEqual(first["open_spread"], 1010.0)
        self.assertAlmostEqual(first["margin"], 0.15 * (30000 + 31000))
        self.assertEqual(window["min_spread_volume"], 500)
        self.assertTrue(first["dividend_in_window"])

    def test_one_thin_day_makes_the_window_ineligible_for_trading(self):
        thin = _days(date(2022, 12, 12), date(2023, 3, 16))[10]
        window = build_windows(_histories(thin_day=thin), "SR", TERMS, RUONIA)[0]
        self.assertEqual(window["min_spread_volume"], 5)

    def test_zero_volume_is_retained_for_journal_but_excluded_from_analysis_and_gate(self):
        day = _days(date(2022, 12, 12), date(2023, 3, 16))[10]
        histories = _histories()
        histories["SRH3SRM3"][day]["VOLUME"] = 0
        window = build_windows(histories, "SR", TERMS, RUONIA)[0]
        self.assertEqual(window["min_spread_volume"], 500)
        row = next(r for r in window["rows"] if r["date"] == day)
        self.assertEqual(row["min_volume"], 0)
        eligible = positive_spread_volume_rows(window["rows"])
        self.assertNotIn(day, {r["date"] for r in eligible})
        self.assertEqual(len(window["rows"]), len(eligible) + 1)

    def test_native_spread_slippage_is_charged_once(self):
        leg = {"secid": "SR", "tariff_group": "stock", "tick_value": 1.0, "tick_size": 1.0}
        profile = tariff_profile(2.0)
        legs = [(leg, 30000.0), (leg, 31000.0)]
        separate = side_cost(profile, legs, 1, as_of="2026-11-01")
        native = side_cost(profile, legs, 1, as_of="2026-11-01", native_spread=True)
        self.assertEqual(separate["slippage"], 4.0)
        self.assertEqual(native["slippage"], 2.0)
        self.assertEqual(separate["exchange_clearing"], native["exchange_clearing"])

    def test_capital_screen_uses_stress_multiples_and_buffers(self):
        self.assertTrue(stress_ok(6000))
        self.assertFalse(stress_ok(10500))
        self.assertFalse(stress_ok(4000, quantity=3))


def _proof_row(day: str, near: float, far: float, volume: int = 500) -> dict:
    spread = far - near
    return {
        "date": day,
        "session_date": day,
        "timestamp": day,
        "benchmark_mark_date": None,
        "near_price": near,
        "far_price": far,
        "near_open_price": near - 1,
        "far_open_price": far - 1,
        "spread_price": spread,
        "spread_open_price": spread - 1,
        "spread_last_trade": spread,
        "spread_trades": 20,
        "near_volume": volume,
        "far_volume": volume,
        "spread_volume": volume,
        "min_volume": volume,
        "volume_eligible": volume > 0,
        "entry_eligible": volume > 0,
        "exit_eligible": volume > 0,
        "signal_eligible": volume > 0,
        "days_to_near_expiry": 30,
        "dev_pp": 0.1,
        "ruonia_rate": 0.15,
        "ruonia_rate_date": "2023-01-01",
        "margin": 9000.0,
        "mult": 1.0,
    }


def _proof_window(rows: list[dict]) -> dict:
    return {
        "pair": "SRH3/SRM3",
        "spread_instrument": "SRH3SRM3",
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "near_expiry": "2023-03-16",
        "near": "SRH3",
        "far": "SRM3",
        "max_pair_margin_rub": 5000.0,
        "min_spread_volume": 500,
        "rows": rows,
        "timeframe": "1d",
        "price_source": "native_daily_spread",
        "liquidity_gate": 100,
        "has_native_spread_volume": True,
    }


def _proof_account(rows: list[dict], direction: int = 1) -> dict:
    return {
        "trade_list": [
            {
                "direction": direction,
                "signal": "zscore",
                "reason": "exit",
                "entry_row": rows[0],
                "exit_row": rows[1],
                "entry_costs": {"exchange_clearing": 5.0, "slippage": 2.0},
                "exit_costs": {"exchange_clearing": 5.0, "slippage": 2.0},
                "margin_rub": 1000.0,
                "gross_rub": 100.0,
                "costs_rub": 14.0,
                "lqdt_on_margin_rub": 10.0,
            }
        ],
        "costs_rub": 14.0,
        "lqdt_on_pair_margin_while_open_rub": 10.0,
    }


def _proof_meta(**overrides) -> dict:
    meta = {
        "asset": "SR",
        "strategy": "zscore_mean_reversion",
        "fill_label": "next_open",
        "options": {"window": 20, "entry_z": 1.5},
        "threshold": 1.0,
        "quantity": 1,
        "slippage": 1.0,
        "terms": {"mult": 1.0, "tick_size": 1.0, "reference_contract": "SRH3", "margin_rate": 0.15},
        "initial_capital": 35000.0,
    }
    meta.update(overrides)
    return meta


class ProofPayloadTests(unittest.TestCase):
    """Proof-payload contract folded back from the P7.2 shard."""

    ROWS: ClassVar[list[dict]] = [
        _proof_row("2023-01-10", 30000, 31000),
        _proof_row("2023-01-11", 30050, 31050),
    ]

    def test_payload_shape_reconciles_and_stays_json_clean(self):
        payload = build_proof_payload(
            _proof_window(self.ROWS), _proof_account(self.ROWS), _proof_meta()
        )
        self.assertEqual(payload["schema_version"], "calendar-proof.v1")
        self.assertEqual(payload["selection"]["pair"], "SRH3/SRM3")
        self.assertEqual(
            (payload["window"]["from"], payload["window"]["to"]), ("2023-01-10", "2023-01-11")
        )
        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(len(payload["trades"]), 1)
        self.assertEqual(payload["trades"][0]["side"], "long_spread")
        self.assertEqual(payload["summary"]["trades"], 1)
        self.assertTrue(
            {"gross_rub", "net_rub", "ledger_reconciliation_rub"} <= set(payload["summary"])
        )
        json.dumps(payload, allow_nan=False)

    def test_short_side_and_zero_volume_exclusion(self):
        rows = [
            _proof_row("2023-01-10", 30000, 31000),
            _proof_row("2023-01-11", 30050, 31050, volume=0),
            _proof_row("2023-01-12", 30100, 31100),
        ]
        rows[1]["volume_eligible"] = False
        rows[1]["min_volume"] = 0
        payload = build_proof_payload(
            _proof_window(rows), _proof_account(rows, direction=-1), _proof_meta()
        )
        self.assertEqual(payload["trades"][0]["side"], "short_spread")
        self.assertEqual(payload["trades"][0]["legs"], "sell far, buy near")
        self.assertEqual(payload["window"]["excluded_volume_observations"], 1)

    def test_full_ledger_reports_actual_held_quantity(self):
        rows = [_proof_row(f"2023-01-{day:02}", 30000 + day, 31000 + day) for day in range(10, 15)]
        rows[2]["volume_eligible"] = False
        rows[2]["min_volume"] = 0
        for direction in (-1, 1):
            for quantity in (1, 3):
                with self.subTest(direction=direction, quantity=quantity):
                    payload = build_proof_payload(
                        _proof_window(rows),
                        _proof_account([rows[1], rows[3]], direction=direction),
                        _proof_meta(quantity=quantity),
                    )
                    ledger = payload["ledger"]
                    self.assertEqual(
                        [r["position"] for r in ledger], [0, direction, direction, 0, 0]
                    )
                    self.assertEqual([r["quantity"] for r in ledger], [0, quantity, quantity, 0, 0])
                    self.assertTrue(all(type(r["quantity"]) is int for r in ledger))
                    self.assertEqual(payload["trades"][0]["quantity"], quantity)

    def test_metrics_only_variants(self):
        window, meta = _proof_window(self.ROWS), _proof_meta()
        metrics = build_proof_payload(window, _proof_account(self.ROWS), meta, metrics_only=True)
        self.assertEqual(
            {frozenset(entry) for entry in metrics["ledger"]},
            {frozenset({"date", "cumulative_net_rub"})},
        )
        self.assertIn("winners", metrics["summary"])
        sizing = build_proof_payload(
            window, _proof_account(self.ROWS), meta, metrics_only=True, sizing_intents=True
        )
        self.assertEqual(sizing["ledger"], [])
        self.assertEqual(sizing["summary"], {})
        self.assertIn("far_price", sizing["trades"][0]["entry"])
        self.assertFalse(sizing["trades"][0]["open_at_end"])

        forced_account = _proof_account(self.ROWS)
        forced_account["trade_list"][0]["reason"] = "end_of_window"
        forced_sizing = build_proof_payload(
            window, forced_account, meta, metrics_only=True, sizing_intents=True
        )
        self.assertTrue(forced_sizing["trades"][0]["open_at_end"])

    def test_same_bar_terminal_trade_reconciles_and_stays_flat_on_trailing_bars(self):
        r0 = _proof_row("2026-01-10T10:00:00", 100.0, 110.0)
        r1 = {**_proof_row("2026-01-10T11:00:00", 100.0, 113.0), "spread_open_price": 10.0}
        r2 = {
            **_proof_row("2026-01-10T12:00:00", 100.0, 190.0),
            "min_volume": 1,
            "volume_eligible": True,
            "entry_eligible": False,
            "exit_eligible": False,
        }
        window = {**_proof_window([r0, r1, r2]), "timeframe": "1h"}
        account = {
            "trade_list": [
                {
                    "direction": -1,
                    "signal": {"kind": "z", "date": r0["date"], "value": 2.0},
                    "reason": "end_of_window",
                    "entry_row": r1,
                    "exit_row": r1,
                    "entry_costs": {"exchange_clearing": 5.0, "slippage": 1.0},
                    "exit_costs": {"exchange_clearing": 5.0, "slippage": 1.0},
                    "margin_rub": 1000.0,
                    "gross_rub": 0.0,
                    "costs_rub": 12.0,
                    "lqdt_on_margin_rub": 0.0,
                }
            ],
            "costs_rub": 12.0,
            "lqdt_on_pair_margin_while_open_rub": 0.0,
        }
        payload = build_proof_payload(window, account, _proof_meta(quantity=2))
        self.assertEqual(payload["summary"]["gross_rub"], 0.0)
        self.assertEqual(payload["summary"]["net_rub"], -12.0)
        self.assertEqual(payload["summary"]["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(payload["summary"]["fx_point_value_drift_rub"], 0.0)
        self.assertEqual([row["position"] for row in payload["ledger"]], [0, 0, 0])
        self.assertEqual([row["quantity"] for row in payload["ledger"]], [0, 0, 0])
        self.assertEqual(payload["ledger"][-1]["mark_pnl_rub"], 0.0)

    def test_build_windows_resolves_ruonia_causally_and_handles_empty_or_future_rates(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 28)]
        histories = {
            "SRH6": {
                **{
                    d: {"TRADEDATE": d, "SETTLEPRICE": 30000.0, "OPEN": 30000.0, "VOLUME": 500}
                    for d in days
                },
                "2026-03-19": {
                    "TRADEDATE": "2026-03-19",
                    "SETTLEPRICE": 30000.0,
                    "OPEN": 30000.0,
                    "VOLUME": 500,
                },
            },
            "SRM6": {
                **{
                    d: {"TRADEDATE": d, "SETTLEPRICE": 30500.0, "OPEN": 30500.0, "VOLUME": 500}
                    for d in days
                },
                "2026-06-18": {
                    "TRADEDATE": "2026-06-18",
                    "SETTLEPRICE": 30500.0,
                    "OPEN": 30500.0,
                    "VOLUME": 500,
                },
            },
            "SRH6SRM6": {
                d: {"TRADEDATE": d, "SETTLEPRICE": 500.0, "OPEN": 500.0, "VOLUME": 500}
                for d in days
            },
            "SRU6": {
                "2026-09-17": {
                    "TRADEDATE": "2026-09-17",
                    "SETTLEPRICE": 31000.0,
                    "OPEN": 31000.0,
                    "VOLUME": 500,
                }
            },
        }
        sr_terms = {"mult": 1.0, "tick_size": 1.0, "tick_value": 1.0, "margin_rate": 0.15}

        empty_wins = build_windows(histories, "SR", sr_terms, ([], {}))
        self.assertTrue(math.isnan(empty_wins[0]["rows"][0]["dev_pp"]))

        future_wins = build_windows(
            histories, "SR", sr_terms, (["2026-12-31"], {"2026-12-31": 0.99})
        )
        self.assertTrue(math.isnan(future_wins[0]["rows"][0]["dev_pp"]))

        causal_wins = build_windows(
            histories,
            "SR",
            sr_terms,
            (["2025-12-30", "2026-01-01"], {"2025-12-30": 0.15, "2026-01-01": 0.50}),
        )
        prior_only = build_windows(
            histories, "SR", sr_terms, (["2025-12-30"], {"2025-12-30": 0.15})
        )
        self.assertAlmostEqual(
            causal_wins[0]["rows"][0]["dev_pp"], prior_only[0]["rows"][0]["dev_pp"]
        )

    def test_h1_fill_bar_eligibility_and_unknown_session_slice_invalidation(self):
        h1_win = {
            "liquidity_gate": 100,
            "rows": [
                {
                    "date": "2026-02-02T10:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 150,
                    "volume_eligible": True,
                    "liquidity_cumulative": 150,
                    "liquidity_cumulative_before": 0,
                    "prior_bar_volume": 0,
                },
                {
                    "date": "2026-02-02T11:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 0,
                    "volume_eligible": False,
                    "liquidity_cumulative": 150,
                    "liquidity_cumulative_before": 150,
                    "prior_bar_volume": 150,
                },
                {
                    "date": "2026-02-02T12:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 150,
                    "volume_eligible": True,
                    "liquidity_cumulative": 300,
                    "liquidity_cumulative_before": 150,
                    "prior_bar_volume": 0,
                },
                {
                    "date": "2026-02-02T13:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 150,
                    "volume_eligible": True,
                    "has_open_quote": False,
                    "liquidity_cumulative": 450,
                    "liquidity_cumulative_before": 300,
                    "prior_bar_volume": 150,
                },
            ],
        }
        _, elig_rows = _build_h1_eligible_rows(h1_win, "open", 1)
        self.assertFalse(elig_rows[1]["entry_eligible"])
        self.assertFalse(elig_rows[1]["exit_eligible"])
        self.assertTrue(elig_rows[2]["entry_eligible"])
        self.assertTrue(elig_rows[2]["exit_eligible"])
        self.assertFalse(elig_rows[3]["entry_eligible"])

        unk_win = {
            "timeframe": "1h",
            "liquidity_gate": 100,
            "has_native_spread_volume": True,
            "tradable_every_session": False,
            "tradable_every_day": False,
            "rows": [
                {
                    "date": "2026-02-02T10:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 150,
                    "spread_volume": 150,
                    "liquidity_cumulative": 150,
                    "margin": 5000.0,
                },
                {
                    "date": "2026-02-02T11:00:00",
                    "session_date": "2026-02-02",
                    "min_volume": 0,
                    "spread_volume": None,
                    "liquidity_cumulative": 150,
                    "margin": 5000.0,
                },
                {
                    "date": "2026-02-03T10:00:00",
                    "session_date": "2026-02-03",
                    "min_volume": 150,
                    "spread_volume": 150,
                    "liquidity_cumulative": 150,
                    "margin": 5000.0,
                },
            ],
        }
        sliced = slice_window_through(unk_win, "2026-02-02T23:59:59")
        self.assertFalse(sliced["tradable_every_session"])
        self.assertFalse(sliced["tradable_every_day"])

    def test_execute_calendar_proof_honors_threshold_pp_and_rejects_bad_params(self):
        days = [f"2026-01-{day:02d}" for day in range(1, 28)]
        histories = {
            "SRH6": {
                **{day: _row(day, 30000.0) for day in days},
                "2026-03-19": _row("2026-03-19", 30000.0),
            },
            "SRM6": {
                **{day: _row(day, 30500.0) for day in days},
                "2026-06-18": _row("2026-06-18", 30500.0),
            },
            "SRH6SRM6": {day: _row(day, 500.0) for day in days},
            "SRU6": {"2026-09-17": _row("2026-09-17", 31000.0)},
        }
        sr_terms = {**TERMS, "tick_size": 1.0, "tick_value": 1.0, "reference_contract": "SRH6"}
        window = build_windows(histories, "SR", sr_terms, (["2025-12-30"], {"2025-12-30": 0.04}))[0]
        data = {
            "terms": {"SR": sr_terms},
            "benchmark": dict.fromkeys(days, 1.5),
            "benchmark_dates": list(days),
        }
        registry = discover_strategies()

        def run(strategy: str, raw_params: dict) -> dict:
            return execute_calendar_proof(
                data,
                window,
                {
                    "asset": "SR",
                    "pair": window["pair"],
                    "timeframe": "1d",
                    "strategy": strategy,
                    "fill_label": "next_open",
                    "quantity": 1,
                    "slippage": 1.0,
                    "raw_params": raw_params,
                    "registry": registry,
                },
            )

        high = run("convergence_to_ruonia", {"threshold_pp": 9.0})
        self.assertEqual(high["selection"]["parameters"]["threshold_pp"], 9.0)
        self.assertEqual(high["trades"], [])

        low = run("convergence_to_ruonia", {"threshold_pp": 2.0})
        self.assertEqual(low["selection"]["parameters"]["threshold_pp"], 2.0)
        self.assertEqual(len(low["trades"]), 1)

        for strategy, bad in (
            ("convergence_to_ruonia", {"threshold_pp": float("nan")}),
            ("convergence_to_ruonia", {"threshold_pp": True}),
            ("zscore_mean_reversion", {"window": 10.9}),
            ("zscore_mean_reversion", {"window": True}),
        ):
            with self.subTest(strategy=strategy, bad=bad), self.assertRaises(W0Error):
                run(strategy, bad)

    def test_shared_proof_cache_isolates_assets_pairs_and_timeframes(self):
        days1 = [f"2026-01-{day:02d}" for day in range(1, 28)]
        days2 = [f"2026-04-{day:02d}" for day in range(1, 28)]
        histories = {
            "SRH6": {
                **{day: _row(day, 30000.0) for day in days1},
                "2026-03-19": _row("2026-03-19", 30000.0),
            },
            "SRM6": {
                **{day: _row(day, 30500.0) for day in days1},
                **{day: _row(day, 30000.0) for day in days2},
                "2026-06-18": _row("2026-06-18", 30500.0),
            },
            "SRH6SRM6": {day: _row(day, 500.0) for day in days1},
            "SRU6": {
                **{day: _row(day, 30000.0) for day in days2},
                "2026-09-17": _row("2026-09-17", 30000.0),
            },
            "SRM6SRU6": {day: _row(day, 0.0) for day in days2},
            "GZH6": {
                **{day: _row(day, 15000.0) for day in days1},
                "2026-03-19": _row("2026-03-19", 15000.0),
            },
            "GZM6": {
                **{day: _row(day, 15000.0) for day in days1},
                "2026-06-18": _row("2026-06-18", 15000.0),
            },
            "GZH6GZM6": {day: _row(day, 0.0) for day in days1},
        }
        sr_terms = {**TERMS, "tick_size": 1.0, "tick_value": 1.0, "reference_contract": "SRH6"}
        gz_terms = {**TERMS, "tick_size": 1.0, "tick_value": 1.0, "reference_contract": "GZH6"}
        ruonia = (["2025-12-30"], {"2025-12-30": 0.04})
        sr_wins = build_windows(histories, "SR", sr_terms, ruonia)
        gz_wins = build_windows(histories, "GZ", gz_terms, ruonia)
        all_days = sorted([*days1, *days2])
        benchmark = dict.fromkeys(all_days, 1.5)
        wmap = {
            ("SR", sr_wins[0]["pair"], "1d"): sr_wins[0],
            ("SR", sr_wins[1]["pair"], "1d"): sr_wins[1],
            ("GZ", gz_wins[0]["pair"], "1d"): gz_wins[0],
        }

        class FakeCalendarData(CalendarData):
            def __init__(self):
                self.cache = Path("/tmp/unused")

            def _load_asset(self, asset: str) -> dict:
                terms_map = {"SR": sr_terms} if asset == "SR" else {"GZ": gz_terms}
                return {
                    "asset": asset,
                    "terms": terms_map,
                    "benchmark": benchmark,
                    "benchmark_dates": all_days,
                }

            def window(self, asset: str, pair: str, timeframe: str = "1d") -> dict:
                return wmap[(asset, pair, timeframe)]

        cal = FakeCalendarData()
        shared: dict = {}
        first = cal.proof(
            {
                "asset": "SR",
                "pair": sr_wins[0]["pair"],
                "strategy": "convergence_to_ruonia",
                "fill": "next_open",
            },
            metrics_only=True,
            _cache=shared,
        )
        second = cal.proof(
            {
                "asset": "SR",
                "pair": sr_wins[1]["pair"],
                "strategy": "convergence_to_ruonia",
                "fill": "next_open",
            },
            metrics_only=True,
            _cache=shared,
        )
        third = cal.proof(
            {
                "asset": "GZ",
                "pair": gz_wins[0]["pair"],
                "strategy": "convergence_to_ruonia",
                "fill": "next_open",
            },
            metrics_only=True,
            _cache=shared,
        )
        self.assertEqual(first["selection"]["pair"], sr_wins[0]["pair"])
        self.assertEqual(first["trades"][0]["side"], "short_spread")
        self.assertEqual(second["selection"]["pair"], sr_wins[1]["pair"])
        self.assertEqual(second["window"]["from"], "2026-04-01")
        self.assertEqual(second["trades"], [])
        self.assertEqual(third["selection"]["asset"], "GZ")
        self.assertEqual(third["selection"]["pair"], gz_wins[0]["pair"])
        self.assertEqual(third["trades"][0]["side"], "long_spread")


def test_asset_catalog_is_reexported_from_contracts_not_redefined() -> None:
    for name in (
        "ASSETS",
        "CROSS_EXPIRY_PAIRS",
        "FX_SOURCE",
        "MONTH_CODES",
        "QUARTER_CODES",
        "SYNTHETIC_TRIANGLE_PAIRS",
        "contract_chain",
    ):
        assert getattr(contracts, name) is getattr(data_universe, name), name
    assert contracts.contract_chain("SR") == data_universe.contract_chain("SR")
    assert contracts.contract_chain("BR", 4, 5) == data_universe.contract_chain("BR", 4, 5)


if __name__ == "__main__":
    unittest.main()
