import unittest
from datetime import date, timedelta

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.strategies.calendar.calendar_spreads import (
    _positive_spread_volume_rows,
    _stress_ok,
    build_windows,
    contract_chain,
)

TERMS = {"mult": 1.0, "margin_rate": 0.15}
RUONIA = (["2022-01-01"], {"2022-01-01": 0.15})


def _days(start: date, end: date) -> list[str]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


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


if __name__ == "__main__":
    unittest.main()
