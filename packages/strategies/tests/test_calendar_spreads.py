import json
import unittest
from datetime import date
from typing import ClassVar

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.strategies.calendar.calendar_spreads import (
    _positive_spread_volume_rows,
    _stress_ok,
    build_windows,
    contract_chain,
)
from moex_spread_lab.strategies.calendar.proof_payload import build_proof_payload
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
        eligible = _positive_spread_volume_rows(window["rows"])
        self.assertNotIn(day, {r["date"] for r in eligible})
        self.assertEqual(len(window["rows"]), len(eligible) + 1)

    def test_native_spread_slippage_is_charged_once(self):
        leg = {"secid": "SR", "tariff_group": "stock", "tick_value": 1.0, "tick_size": 1.0}
        profile = tariff_profile(2.0)
        legs = [(leg, 30000.0), (leg, 31000.0)]
        separate = side_cost(profile, legs, 1)
        native = side_cost(profile, legs, 1, native_spread=True)
        self.assertEqual(separate["slippage"], 4.0)
        self.assertEqual(native["slippage"], 2.0)
        self.assertEqual(separate["exchange_clearing"], native["exchange_clearing"])

    def test_capital_screen_uses_stress_multiples_and_buffers(self):
        self.assertTrue(_stress_ok(6000))
        self.assertFalse(_stress_ok(10500))
        self.assertFalse(_stress_ok(4000, quantity=3))


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


if __name__ == "__main__":
    unittest.main()
