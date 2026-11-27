"""Settlement cash-flow semantics and expiry enforcement."""

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.carry_intent_accounting import replay_carry_intents

DAYS = [f"2026-01-0{i}" for i in range(1, 7)]
STUDY = {
    "rows": [
        {
            "date": d,
            "near_settle": 100,
            "far_settle": 101,
            "daily_gross_carry_pnl": 12,
            "daily_funding_received_short_near": 7,
            "near_volume": 100,
            "far_volume": 100,
        }
        for d in DAYS
    ]
}
T = {
    "side": "short_spread",
    "signal_date": DAYS[0],
    "entry_date": DAYS[1],
    "exit_date": DAYS[4],
    "reason": "exit",
    "open_at_end": False,
}


def replay(trades, expiry="2026-03-01"):
    return replay_carry_intents(STUDY, trades, quantity=2, side_cost=3, far_expiry=expiry)


def test_entry_exit_and_final_mark_funding():
    r = replay([T])
    assert [x["daily_funding_included_rub"] for x in r["daily"]] == [0, 0, 14, 14, 0, 0]
    assert r["trades"][0]["gross_pnl"] == 58
    assert r["trades"][0]["transaction_costs"] == 12
    terminal = {**T, "exit_date": DAYS[-1], "open_at_end": True}
    r = replay([terminal])
    assert r["daily"][-1]["daily_funding_included_rub"] == 14
    assert r["trades"][0]["gross_pnl"] == 96
    assert r["trades"][0]["transaction_costs"] == 6


def test_expiry_and_assignment_cannot_be_overridden():
    with pytest.raises(W0Error):
        replay([T], "2026-01-07")
    with pytest.raises(W0Error):
        replay([{**T, "open_at_end": True, "exit_date": DAYS[-1]}], "2026-01-10")
    with pytest.raises(W0Error):
        replay([T, T])


def test_nonfinite_inputs_fail_closed():
    bad = {
        "rows": [*STUDY["rows"][:-1], {**STUDY["rows"][-1], "daily_gross_carry_pnl": float("nan")}]
    }
    with pytest.raises(W0Error):
        replay_carry_intents(bad, [T], quantity=1, side_cost=1, far_expiry="2026-03-01")


@pytest.mark.parametrize("quantity", [1, 3])
@pytest.mark.parametrize("prefix", [None, 35])
@pytest.mark.parametrize("negative_late", [False, True])
def test_replay_matches_native_cash_flows_on_real_study(tmp_path, quantity, prefix, negative_late):
    from funding_fixtures import PAIR, change, fixture
    from moex_spread_lab.accounting.costs import side_cost, tariff_profile
    from moex_spread_lab.proof.service import ProofService
    from moex_spread_lab.strategies.funding_carry.carry import build_carry_strategy, load_contracts
    from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
    from moex_spread_lab.strategies.pair_screener.pair_screener import _pair

    dates = fixture(tmp_path)
    if negative_late:
        change(tmp_path, "CNYRUBF", dict.fromkeys(dates[20:], -0.04))
    service = ProofService(tmp_path)
    inputs = service.screener_inputs
    pair = _pair(PAIR)
    study, _, _, _ = service._funding_inputs(inputs, pair, ProofFunding(inputs), 0)
    if prefix is not None:
        study = {**study, "rows": study["rows"][:prefix]}
    native = build_carry_strategy(
        study,
        inputs.benchmark,
        inputs.contracts,
        transaction_cost_scenario="tariff",
        warmup_observations=5,
        strategy_variant="funding_only",
        initial_capital=30000,
        quantity=quantity,
        execution_delay_rows=0,
        cash_in_lqdt=True,
        slippage_ticks=1,
        metrics_only=True,
    )
    reasons = {(s["date"], s["action"]): s["reason"] for s in native["signals"]}
    trades = [
        {
            "side": "short_spread",
            "entry_date": t["entry_execution_date"],
            "exit_date": t["exit_execution_date"] or study["rows"][-1]["date"],
            "signal_date": t["entry_decision_date"],
            "reason": reasons.get((t["exit_decision_date"], "exit"), t["status"]),
            "open_at_end": t["exit_execution_date"] is None,
        }
        for t in native["trades"]
    ]
    contracts, _ = load_contracts(inputs.contracts, pair["near"], pair["far"])
    last = study["rows"][-1]
    fee = sum(
        side_cost(
            tariff_profile(1),
            [
                (contracts[pair["near"]], last["near_settle"]),
                (contracts[pair["far"]], last["far_settle"]),
            ],
            1,
        ).values()
    )
    actual = replay_carry_intents(
        study,
        trades,
        quantity=quantity,
        side_cost=round(fee, 8),
        far_expiry=contracts[pair["far"]]["expiry"],
    )
    assert actual["trades"] == native["trades"]
    from moex_spread_lab.proof.payload_tools import benchmark_marks as _benchmark_marks

    days, prices = _benchmark_marks(inputs.benchmark)
    full = replay_carry_intents(
        study,
        trades,
        quantity=quantity,
        side_cost=round(fee, 8),
        far_expiry=contracts[pair["far"]]["expiry"],
        initial_capital=30000,
        margin_per_pair=contracts[pair["near"]]["initial_margin"]
        + contracts[pair["far"]]["initial_margin"],
        benchmark=prices,
    )
    full_native = build_carry_strategy(
        study,
        inputs.benchmark,
        inputs.contracts,
        transaction_cost_scenario="tariff",
        warmup_observations=5,
        strategy_variant="funding_only",
        initial_capital=30000,
        quantity=quantity,
        execution_delay_rows=0,
        cash_in_lqdt=True,
        slippage_ticks=1,
    )
    for got, expected in zip(full["daily"], full_native["daily"], strict=True):
        for key in (
            "cumulative_gross_pnl",
            "cumulative_transaction_costs",
            "strategy_value_proxy",
            "benchmark_value",
            "X",
            "H",
            "D",
        ):
            assert got[key] == expected[key], (key, got, expected)
    if negative_late:
        assert any(t["exit_execution_date"] for t in actual["trades"])
    for got, expected in zip(actual["daily"], native["daily"], strict=True):
        assert all(got[k] == v for k, v in expected.items()), (got, expected)
