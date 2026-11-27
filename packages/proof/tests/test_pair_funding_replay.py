"""Characterize pair funding timing against the existing baseline, not a proof adapter."""

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.pair_funding_replay import replay_pair_funding
from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline

DATES = [f"2026-01-0{i}" for i in range(1, 6)]
RATES = dict(zip(DATES, [0.2, 0.3, 0.4, 0.5, 0.7], strict=False))


def trade(side="long_spread", terminal=False):
    return {
        "side": side,
        "entry": {"date": DATES[3]},
        "exit": {"date": DATES[4]},
        "open_at_end": terminal,
    }


@pytest.mark.parametrize("side,z", [("long_spread", -2.0), ("short_spread", 2.0)])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("fill", ["open", "close"])
def test_funding_matches_existing_baseline(side, z, terminal, fill):
    series = funding_series(DATES)
    report = run_mean_reversion_baseline(
        series,
        lookback=2,
        entry_z=1.5,
        exit_z=0.5,
        quantity=3,
        fill=fill,
        funding_swaprates=RATES,
        precomputed_z=[None, None, None, z, z if terminal else 0],
    )
    replay = replay_pair_funding(
        DATES, [trade(side, terminal)], RATES, near_lot_size=10, quantity=3
    )
    assert len(report["trades"]) == 1
    funding = known_funding(replay)
    assert [round(value, 8) for value in funding] == [row["funding_rub"] for row in report["daily"]]
    assert round(sum(funding), 8) == report["trades"][0]["funding_rub"]
    assert replay[-1]["position_at_clearing"] == (
        (1 if side == "long_spread" else -1) if terminal else 0
    )


def test_missing_rate_is_not_silently_zero():
    rates = {DATES[4]: 0.0}
    rows = replay_pair_funding(DATES, [trade(terminal=True)], rates, near_lot_size=10, quantity=1)
    assert rows[3]["funding_rub"] is None
    assert rows[4]["funding_rub"] == 0
    assert rows[0]["funding_rub"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"dates": DATES[::-1]},
        {"dates": [*DATES, DATES[-1]]},
        {"rates": {DATES[0]: float("nan")}},
        {"rates": {"unknown": 1}},
        {"near_lot_size": 0},
        {"quantity": True},
        {"quantity": 0},
        {"trades": [trade(), trade()]},
        {"trades": [{**trade(), "open_at_end": 1}]},
        {"trades": [{**trade(), "exit": {"date": DATES[2]}}]},
    ],
)
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
        replay_pair_funding(
            DATES,
            [first, {**second, "entry": {"date": DATES[1]}}],
            RATES,
            near_lot_size=1,
            quantity=1,
        )


def test_overflow_fails_closed():
    with pytest.raises(W0Error):
        replay_pair_funding(DATES, [trade()], {DATES[3]: 1e308}, near_lot_size=1e308, quantity=2)


def test_daily_resolver_keeps_estimates_zero_and_no_clearing_distinct():
    from moex_spread_lab.proof.pair_funding_replay import replay_daily_pair_funding
    from moex_spread_lab.strategies.funding_carry.proof_funding import (
        FundingTimeline,
        FundingWindow,
    )

    days = ["2025-08-15", "2025-08-16", "2025-08-17", "2025-08-18", "2025-08-19"]
    published = {days[0]: 0.0, days[3]: None, days[4]: -0.04}
    window = FundingWindow(FundingTimeline("GAZPF", published, days), days)
    position = {
        "side": "long_spread",
        "entry": {"date": days[0]},
        "exit": {"date": days[4]},
        "open_at_end": True,
    }
    result = replay_daily_pair_funding(days, [position], window, near_lot_size=10, quantity=2)
    rows = result["daily"]
    assert [r["rate_kind"] for r in rows] == [
        "published",
        "no_clearing",
        "no_clearing",
        "midpoint",
        "published",
    ]
    assert [r["rate"] for r in rows] == [0, None, None, -0.02, -0.04]
    assert [r["funding_status"] for r in rows] == [
        "applied",
        "no_clearing",
        "no_clearing",
        "applied",
        "applied",
    ]
    assert [r["funding_rub"] for r in rows] == [0, 0, 0, 0.4, 0.8]
    assert result["provenance"]["funding_estimated_dates"] == [days[3]]
    assert set(result["provenance"]["funding_no_clearing_dates"]) == set(days[1:3])
    result["provenance"]["funding_estimated_dates"].clear()
    assert window.resolve().provenance["funding_estimated_dates"] == [days[3]]


def test_daily_resolver_rejects_future_anchor_gap_and_partial_mismatch():
    from moex_spread_lab.proof.pair_funding_replay import replay_daily_pair_funding
    from moex_spread_lab.strategies.funding_carry.proof_funding import (
        FundingTimeline,
        FundingWindow,
    )

    days = DATES
    published = {days[0]: 0.1, days[1]: None, days[2]: 0.3, days[3]: 0.4, days[4]: 0.5}
    window = FundingWindow(FundingTimeline("TEST", published, days), days)
    call = lambda ds, cutoff=None: replay_daily_pair_funding(
        ds, [], window, through=cutoff, near_lot_size=1, quantity=1
    )
    with pytest.raises(W0Error):
        call(days[:2], days[1])  # midpoint's right anchor is after cutoff
    with pytest.raises(W0Error):
        call(days[1:], None)  # no silent loss of a required session
    with pytest.raises(W0Error):
        call([d + "T10:00:00" for d in days])
    assert len(call(days[:3], days[2])["daily"]) == 3
    missing = FundingWindow(FundingTimeline("TEST", {days[0]: None}, days[:1]), days[:1])
    with pytest.raises(W0Error):
        replay_daily_pair_funding(days[:1], [], missing, near_lot_size=1, quantity=1)


def test_training_cutoff_cannot_borrow_holdout_anchor():
    from moex_spread_lab.proof.pair_funding_replay import replay_daily_pair_funding
    from moex_spread_lab.strategies.funding_carry.proof_funding import (
        FundingTimeline,
        FundingWindow,
    )

    rates = {DATES[0]: 0.1, DATES[1]: 0.2, DATES[2]: None, DATES[3]: 0.4, DATES[4]: 0.5}
    window = FundingWindow(FundingTimeline("TEST", rates, DATES), DATES, validation_percent=40)
    with pytest.raises(W0Error):
        replay_daily_pair_funding(DATES, [], window, near_lot_size=1, quantity=1)


def test_no_clearing_requires_explicit_distinct_evidence():
    with pytest.raises(W0Error):
        replay_pair_funding(
            DATES, [trade()], RATES, near_lot_size=1, quantity=1, no_clearing_dates=[DATES[3]]
        )


def test_resolved_evidence_reconciles_with_baseline_including_nonclearing():
    from moex_spread_lab.proof.pair_funding_replay import replay_daily_pair_funding
    from moex_spread_lab.strategies.funding_carry.proof_funding import (
        FundingTimeline,
        FundingWindow,
    )

    days = [
        "2025-08-13",
        "2025-08-14",
        "2025-08-15",
        "2025-08-16",
        "2025-08-17",
        "2025-08-18",
        "2025-08-19",
    ]
    published = {days[0]: 0.01, days[1]: 0.02, days[2]: 0.03, days[5]: None, days[6]: 0.05}
    window = FundingWindow(FundingTimeline("GAZPF", published, days), days)
    series = funding_series(days)
    baseline = run_mean_reversion_baseline(
        series,
        lookback=2,
        entry_z=1.5,
        exit_z=0.5,
        quantity=2,
        fill="open",
        funding_swaprates=window.resolve().rates,
        precomputed_z=[None, None, None, -2, -2, -2, -2],
    )
    intent = {
        "side": "long_spread",
        "entry": {"date": days[3]},
        "exit": {"date": days[-1]},
        "open_at_end": True,
    }
    replay = replay_daily_pair_funding(days, [intent], window, near_lot_size=10, quantity=2)
    funding = known_funding(replay["daily"])
    assert [round(value, 8) for value in funding] == [r["funding_rub"] for r in baseline["daily"]]
    assert round(sum(funding), 8) == baseline["summary"]["funding_rub"]
    assert baseline["trades"][0]["funding_rub"] == baseline["summary"]["funding_rub"]


def funding_series(days):
    rows = [
        {
            "date": d,
            "near_open": 100 + i,
            "far_open": 100,
            "near_close": 100.5 + i,
            "far_close": 100.5,
            "spread_ticks": i,
            "benchmark_price": 1,
            "benchmark_source_kind": "observed",
        }
        for i, d in enumerate(days)
    ]
    contract = {"initial_margin": 100, "tick_value": 1, "tick_size": 1, "lot_size": 10}
    series = {
        "rows": rows,
        "contracts": {"N": contract, "F": contract},
        "near": "N",
        "far": "F",
        "provenance": {},
    }
    return series


def known_funding(rows):
    values = []
    for row in rows:
        value = row["funding_rub"]
        assert value is not None
        values.append(value)
    return values
