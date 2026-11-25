"""A bounded Optuna budget is opt-in, offline-only and never upwards (plan S4b, work 4)."""

from __future__ import annotations

import pytest

from moex_spread_lab.proof.pair_payload import TRIALS_PER_STRATEGY, budget


def test_default_budget_is_the_production_value(monkeypatch):
    monkeypatch.delenv("MOEX_UI_SMOKE_TRIALS", raising=False)
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "1")
    assert budget() == TRIALS_PER_STRATEGY


def test_offline_smoke_may_lower_the_budget(monkeypatch):
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "1")
    monkeypatch.setenv("MOEX_UI_SMOKE_TRIALS", "12")
    assert budget() == 12


def test_budget_is_ignored_without_the_offline_contract(monkeypatch):
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "0")
    monkeypatch.setenv("MOEX_UI_SMOKE_TRIALS", "12")
    assert budget() == TRIALS_PER_STRATEGY


@pytest.mark.parametrize("value", ["0", "-5", "many", "", "100000"])
def test_invalid_or_upward_values_fall_back_to_the_production_budget(monkeypatch, value):
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "1")
    monkeypatch.setenv("MOEX_UI_SMOKE_TRIALS", value)
    assert budget() == TRIALS_PER_STRATEGY
