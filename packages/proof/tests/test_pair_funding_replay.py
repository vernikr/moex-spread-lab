"""Characterize pair funding timing against the existing baseline, not a proof adapter."""
import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.pair_funding_replay import replay_pair_funding
from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline

DATES = [f"2026-01-0{i}" for i in range(1, 6)]
RATES = dict(zip(DATES, [0.2, 0.3, 0.4, 0.5, 0.7]))


def trade(side="long_spread", terminal=False):
    return {"side": side, "entry": {"date": DATES[3]}, "exit": {"date": DATES[4]}, "open_at_end": terminal}


@pytest.mark.parametrize("side,z", [("long_spread", -2.0), ("short_spread", 2.0)])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("fill", ["open", "close"])
def test_funding_matches_existing_baseline(side, z, terminal, fill):
    rows = [{"date": d, "near_open": 100 + i, "far_open": 100,
             "near_close": 100.5 + i, "far_close": 100.5,
             "spread_ticks": i, "benchmark_price": 1, "benchmark_source_kind": "observed"}
            for i, d in enumerate(DATES)]
    contract = {"initial_margin": 100, "tick_value": 1, "tick_size": 1, "lot_size": 10}
    series = {"rows": rows, "contracts": {"N": contract, "F": contract}, "near": "N", "far": "F", "provenance": {}}
    report = run_mean_reversion_baseline(series, lookback=2, entry_z=1.5, exit_z=.5,
        quantity=3, fill=fill, funding_swaprates=RATES,
        precomputed_z=[None, None, None, z, z if terminal else 0])
    replay = replay_pair_funding(DATES, [trade(side, terminal)], RATES, near_lot_size=10, quantity=3)
    assert len(report["trades"]) == 1
    assert [round(row["funding_rub"], 8) for row in replay] == [row["funding_rub"] for row in report["daily"]]
    assert round(sum(row["funding_rub"] for row in replay), 8) == report["trades"][0]["funding_rub"]
    assert replay[-1]["position_at_clearing"] == ((1 if side == "long_spread" else -1) if terminal else 0)


def test_missing_rate_is_not_silently_zero():
    rates = {DATES[4]: 0.0}
    rows = replay_pair_funding(DATES, [trade(terminal=True)], rates, near_lot_size=10, quantity=1)
    assert rows[3]["funding_rub"] is None
    assert rows[4]["funding_rub"] == 0
    assert rows[0]["funding_rub"] == 0


@pytest.mark.parametrize("changes", [
    {"dates": DATES[::-1]}, {"dates": DATES + [DATES[-1]]},
    {"rates": {DATES[0]: float("nan")}}, {"rates": {"unknown": 1}},
    {"near_lot_size": 0}, {"quantity": True}, {"quantity": 0},
    {"trades": [trade(), trade()]},
    {"trades": [{**trade(), "open_at_end": 1}]},
    {"trades": [{**trade(), "exit": {"date": DATES[2]}}]},
])
def test_invalid_inputs_fail_closed(changes):
    args = {"dates": DATES, "trades": [trade()], "rates": RATES, "near_lot_size": 10, "quantity": 1}
    args.update(changes)
    with pytest.raises(W0Error):
        replay_pair_funding(**args)


def test_multiple_trades_and_same_bar_reentry():
    first = {**trade(), "entry": {"date": DATES[0]}, "exit": {"date": DATES[1]}}
    second = {**trade("short_spread"), "entry": {"date": DATES[2]}, "exit": {"date": DATES[4]}}
    replay = replay_pair_funding(DATES, [first, second], RATES, near_lot_size=1, quantity=1)
    assert [row["position_at_clearing"] for row in replay] == [1, 0, -1, -1, 0]
    with pytest.raises(W0Error):
        replay_pair_funding(DATES, [first, {**second, "entry": {"date": DATES[1]}}], RATES, near_lot_size=1, quantity=1)


def test_overflow_fails_closed():
    with pytest.raises(W0Error):
        replay_pair_funding(DATES, [trade()], {DATES[3]: 1e308}, near_lot_size=1e308, quantity=2)
