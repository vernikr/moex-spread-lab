"""Regression for strict-typed proof_payload (issue #102)."""

from __future__ import annotations

import unittest
from typing import Any

from moex_spread_lab.strategies.calendar import proof_payload as pp_module
from moex_spread_lab.strategies.calendar.proof_payload import (
    _clean,
    _rel_pct,
    build_proof_payload,
    proof_payload,
)


def _row(date: str, near: float, far: float, volume: int = 500) -> dict[str, Any]:
    spread = far - near
    return {
        "date": date,
        "session_date": date,
        "timestamp": date,
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


def _window(rows: list[dict[str, Any]], *, pair: str = "SRH3/SRM3") -> dict[str, Any]:
    return {
        "pair": pair,
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


def _account(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # one trade entry at rows[0] exit at rows[1]
    entry_row = rows[0]
    exit_row = rows[1]
    return {
        "trade_list": [
            {
                "direction": 1,
                "signal": "zscore",
                "reason": "exit",
                "entry_row": entry_row,
                "exit_row": exit_row,
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


def _meta() -> dict[str, Any]:
    return {
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


class ProofPayloadStrictRegressionTests(unittest.TestCase):
    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_build_full(
        self,
    ) -> None:
        rows = [
            _row("2023-01-10", 30000, 31000),
            _row("2023-01-11", 30050, 31050),
            _row("2023-01-12", 30100, 31100),
        ]
        window = _window(rows)
        account = _account(rows)
        meta = _meta()
        payload = build_proof_payload(window, account, meta)
        # structure
        self.assertEqual(payload["schema_version"], "calendar-proof.v1")
        self.assertEqual(payload["selection"]["asset"], "SR")
        self.assertEqual(payload["selection"]["pair"], "SRH3/SRM3")
        self.assertEqual(payload["window"]["from"], "2023-01-10")
        self.assertEqual(payload["window"]["to"], "2023-01-12")
        self.assertTrue(payload["window"]["passes_capital_screen"])
        self.assertEqual(payload["contract"]["tariff_group"], "stock")
        self.assertEqual(len(payload["series"]), 3)
        self.assertEqual(len(payload["trades"]), 1)
        self.assertEqual(len(payload["ledger"]), 3)
        self.assertIn("notes", payload)
        # trade fields
        trade = payload["trades"][0]
        self.assertEqual(trade["number"], 1)
        self.assertEqual(trade["side"], "long_spread")
        self.assertEqual(trade["legs"], "buy far, sell near")
        self.assertIn("gross_rub_static", trade)
        # summary reconciliation
        summary = payload["summary"]
        self.assertEqual(summary["trades"], 1)
        self.assertIn("gross_rub", summary)
        self.assertIn("net_rub", summary)
        self.assertIn("ledger_reconciliation_rub", summary)
        self.assertIn("fx_point_value_drift_rub", summary)
        # json serializable (NaN cleaned)
        import json

        json.dumps(payload, allow_nan=False)

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_metrics_only(
        self,
    ) -> None:
        rows = [_row("2023-01-10", 30000, 31000), _row("2023-01-11", 30050, 31050)]
        window = _window(rows)
        account = _account(rows)
        meta = _meta()
        payload = build_proof_payload(window, account, meta, metrics_only=True)
        self.assertEqual(payload["series"][0]["date"], "2023-01-10")
        self.assertIn("point_value_rub", payload["series"][0])
        self.assertGreaterEqual(len(payload["trades"]), 1)
        self.assertGreaterEqual(len(payload["ledger"]), 2)
        # summary winners
        self.assertIn("winners", payload["summary"])
        # compact ledger has only date/cumulative
        for entry in payload["ledger"]:
            self.assertEqual(set(entry.keys()), {"date", "cumulative_net_rub"})

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_metrics_only_sizing_intents(
        self,
    ) -> None:
        rows = [_row("2023-01-10", 30000, 31000), _row("2023-01-11", 30050, 31050)]
        window = _window(rows)
        account = _account(rows)
        meta = _meta()
        payload = build_proof_payload(window, account, meta, metrics_only=True, sizing_intents=True)
        self.assertEqual(payload["ledger"], [])
        self.assertEqual(payload["summary"], {})
        # sizing intents trades have far/near_price
        t = payload["trades"][0]
        self.assertIn("entry", t)
        self.assertIn("far_price", t["entry"])

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_series_cache(
        self,
    ) -> None:
        rows = [_row("2023-01-10", 30000, 31000), _row("2023-01-11", 30050, 31050)]
        window = _window(rows)
        account = _account(rows)
        meta = _meta()
        cache: dict[Any, Any] = {}
        key = ("2023-01-10", "next_open", 1)
        p1 = build_proof_payload(
            window, account, meta, metrics_only=True, series_cache=cache, series_key=key
        )
        self.assertIn(key, cache)
        p2 = build_proof_payload(
            window, account, meta, metrics_only=True, series_cache=cache, series_key=key
        )
        # should reuse cached object identity
        self.assertIs(p1["series"], p2["series"])

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_legacy_shim_and_aliases(
        self,
    ) -> None:
        rows = [
            _row("2023-01-10", 30000, 31000),
            _row("2023-01-11", 30050, 31050),
            _row("2023-01-12", 30100, 31100),
        ]
        window = _window(rows)
        account = _account(rows)
        meta = _meta()
        # legacy positional shim
        legacy = pp_module._proof_payload(
            window,
            account,
            meta["asset"],
            meta["strategy"],
            meta["fill_label"],
            meta["options"],
            meta["threshold"],
            meta["quantity"],
            meta["slippage"],
            meta["terms"],
            None,  # _prof unused
            meta["initial_capital"],
        )
        direct = build_proof_payload(window, account, meta)
        self.assertEqual(legacy["summary"], direct["summary"])
        self.assertEqual(legacy["selection"], direct["selection"])
        # public alias
        self.assertIs(proof_payload, pp_module._proof_payload)
        # clean / rel_pct aliases preserve contract
        self.assertIs(
            _clean, pp_module.clean_payload if hasattr(pp_module, "clean_payload") else _clean
        )
        # Actually _clean is clean_payload from proof_ledger
        from moex_spread_lab.strategies.calendar.proof_ledger import clean_payload, rel_pct

        self.assertIs(_clean, clean_payload)
        self.assertIs(_rel_pct, rel_pct)
        self.assertEqual(_rel_pct(5, 100), 5.0)
        self.assertTrue(_clean({"a": 1.0}) == {"a": 1.0})

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_fills_and_short(
        self,
    ) -> None:
        rows = [_row("2023-01-10", 30000, 31000), _row("2023-01-11", 30050, 31050)]
        window = _window(rows)
        account_short = {
            "trade_list": [
                {
                    "direction": -1,
                    "signal": "z",
                    "reason": "exit",
                    "entry_row": rows[0],
                    "exit_row": rows[1],
                    "entry_costs": {"exchange_clearing": 1.0},
                    "exit_costs": {"exchange_clearing": 1.0},
                    "margin_rub": 500.0,
                    "gross_rub": 50.0,
                    "costs_rub": 2.0,
                    "lqdt_on_margin_rub": 1.0,
                }
            ],
            "costs_rub": 2.0,
            "lqdt_on_pair_margin_while_open_rub": 1.0,
        }
        meta_settlement = {**_meta(), "fill_label": "settlement"}
        payload = build_proof_payload(window, account_short, meta_settlement, metrics_only=False)
        t = payload["trades"][0]
        self.assertEqual(t["side"], "short_spread")
        self.assertEqual(t["legs"], "sell far, buy near")

    def test_p72_packages_strategies_src_moex_spread_lab_strategies_calendar_proof_payload_zero_volume_exclusion(
        self,
    ) -> None:
        rows = [
            _row("2023-01-10", 30000, 31000, volume=500),
            _row("2023-01-11", 30050, 31050, volume=0),
            _row("2023-01-12", 30100, 31100, volume=500),
        ]
        # adapt rows for volume_eligible false on zero volume
        rows[1]["volume_eligible"] = False
        rows[1]["min_volume"] = 0
        window = _window(rows)
        # make trade not using zero day
        account = _account(rows)
        meta = _meta()
        payload = build_proof_payload(window, account, meta)
        self.assertEqual(payload["window"]["excluded_volume_observations"], 1)
        self.assertEqual(payload["window"]["min_spread_volume"], 500)
