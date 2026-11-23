"""Regression coverage for the typed carry intent accounting boundary."""

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.carry_intent_accounting import replay_carry_intents

DAYS = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
STUDY = {
    "rows": [
        {
            "date": day,
            "near_settle": 100,
            "far_settle": 101,
            "daily_gross_carry_pnl": 1,
            "daily_funding_received_short_near": 1,
        }
        for day in DAYS
    ]
}
TRADE = {
    "side": "short_spread",
    "signal_date": DAYS[0],
    "entry_date": DAYS[1],
    "exit_date": DAYS[3],
    "reason": "exit",
    "open_at_end": False,
}


def _replay(**overrides):
    params = {"quantity": 1, "side_cost": 1, "far_expiry": "2026-02-01"}
    params.update(overrides)
    return replay_carry_intents(STUDY, [TRADE], **params)


def test_p72_carry_intent_accounting_preserves_replay_payload():
    report = _replay()

    assert [row["date"] for row in report["daily"]] == DAYS
    assert [row["position"] for row in report["daily"]] == [0, 1, 1, 0]
    assert report["trades"][0]["status"] == "closed_proxy_round_trip"
    assert [signal["action"] for signal in report["signals"]] == ["entry", "exit"]


@pytest.mark.parametrize("study", [None, {}, {"rows": []}, {"rows": None}])
def test_p72_carry_intent_accounting_rejects_invalid_study(study):
    with pytest.raises(W0Error):
        replay_carry_intents(study, [TRADE], quantity=1, side_cost=1, far_expiry="2026-02-01")


@pytest.mark.parametrize("intents", [None, (), {"entry_date": DAYS[1]}])
def test_p72_carry_intent_accounting_rejects_non_list_intents(intents):
    with pytest.raises(W0Error):
        replay_carry_intents(STUDY, intents, quantity=1, side_cost=1, far_expiry="2026-02-01")


@pytest.mark.parametrize("trade", [None, "trade", {"entry_date": DAYS[1]}])
def test_p72_carry_intent_accounting_rejects_invalid_intent_items(trade):
    with pytest.raises(W0Error):
        replay_carry_intents(STUDY, [trade], quantity=1, side_cost=1, far_expiry="2026-02-01")


def test_p72_carry_intent_accounting_rejects_partial_account_and_expiry():
    with pytest.raises(W0Error):
        _replay(far_expiry=None)
    with pytest.raises(W0Error):
        _replay(initial_capital=100)
