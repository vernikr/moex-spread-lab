"""Regression coverage for the strict typing of ``proof_runtime.funding_engine`` (Issue #84).

The change under test is typing-only; these tests pin the runtime behaviour that the
annotations describe so a future edit cannot silently change it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from moex_spread_lab.core.universe import MIN_DAILY_VOLUME
from moex_spread_lab.proof_runtime.funding_engine import (
    FundingSpec,
    carry_plugin_of,
    strategy_name_of,
    study_cut,
)

NEAR = "CNYRUBF"
FAR = "CRZ6"
PAIR = {"pair_id": "CNYRUBF/CRZ6", "near": NEAR, "far": FAR}
DAYS = [f"2026-01-0{i}" for i in range(1, 7)]


def _study_rows(volume_scale: float = 1.0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(DAYS):
        rows.append(
            {
                "date": day,
                "near_settle": 100.0 + i,
                "far_settle": 101.0 + i,
                "daily_gross_carry_pnl": 12.0,
                "daily_funding_received_short_near": 7.0,
                "near_volume": float(MIN_DAILY_VOLUME) * volume_scale + 150.0 * i,
                "far_volume": float(MIN_DAILY_VOLUME) * volume_scale + 120.0 * i + 20.0,
            }
        )
    return rows


class _FakeWindow:
    """Duck-typed stand-in for ``FundingWindow`` covering the cut logic only."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def resolve(self, through: str | None = None) -> dict[str, Any]:
        self.calls.append(through)
        return {"resolved_through": through}


def _terms() -> dict[str, dict[str, Any]]:
    return {
        NEAR: {"initial_margin": 100.0, "tick_size": 0.5, "tick_value": 0.05},
        FAR: {"initial_margin": 200.0},
    }


def _spec(
    rows: list[dict[str, Any]],
    *,
    through: str | None = None,
    cache: dict[str, Any] | None = None,
    terms: dict[str, dict[str, Any]] | None = None,
) -> tuple[FundingSpec, _FakeWindow]:
    window = _FakeWindow()
    study = {"rows": rows}
    spec = FundingSpec(
        inputs=object(),
        pair=dict(PAIR),
        execution={"quantity": 2, "slippage_ticks": 1, "initial_capital_rub": 30000},
        params={"lookback": 5},
        terms=terms if terms is not None else _terms(),
        risk={},
        prepared=(study, "near-provenance", "far-provenance", window),
        through=through,
        cache=cache,
    )
    return spec, window


def test_study_cut_filters_rows_and_derives_reference_prices() -> None:
    spec, window = _spec(_study_rows(), through=DAYS[2])
    state = study_cut(spec)
    assert [row["date"] for row in state.study["rows"]] == DAYS[:3]
    assert state.study_by_date == {row["date"]: row for row in state.study["rows"]}
    assert state.ref_near == 102.0
    assert state.ref_far == 103.0
    assert state.funding_window is window
    assert window.calls == [DAYS[2]]
    assert state.evidence == {"resolved_through": DAYS[2]}
    # Near leg prices off its own tick terms; the far leg falls back to the near multiplier.
    assert state.near_mult == 0.1
    assert state.far_mult == 0.1
    assert state.margin_pair == 600.0


def test_study_cut_without_through_keeps_full_window() -> None:
    spec, window = _spec(_study_rows())
    state = study_cut(spec)
    assert len(state.study["rows"]) == len(DAYS)
    assert window.calls == [None]
    assert state.ref_near == 100.0 + len(DAYS) - 1
    assert state.ref_far == 101.0 + len(DAYS) - 1


def test_study_cut_window_meta_tracks_minimum_leg_volume() -> None:
    rows = _study_rows()
    rows[1]["near_volume"] = float(MIN_DAILY_VOLUME) - 40.0
    spec, _ = _spec(rows)
    state = study_cut(spec)
    expected_min = min(min(row["near_volume"], row["far_volume"]) for row in rows)
    assert state.window_meta["min_leg_volume"] == expected_min
    assert state.window_meta["tradable_every_day"] is False
    spec_ok, _ = _spec(_study_rows())
    state_ok = study_cut(spec_ok)
    assert state_ok.window_meta["tradable_every_day"] is True


def test_study_cut_uses_cache_without_resolving() -> None:
    sentinel_evidence = object()
    by_date = {"cached": True}
    meta = {"min_leg_volume": 7.0, "tradable_every_day": False}
    cache = {"funding_through": {DAYS[1]: (sentinel_evidence, by_date, 11.0, 22.0, meta)}}
    spec, window = _spec(_study_rows(), through=DAYS[1], cache=cache)
    state = study_cut(spec)
    assert state.evidence is sentinel_evidence
    assert state.study_by_date is by_date
    assert state.ref_near == 11.0
    assert state.ref_far == 22.0
    assert state.window_meta is meta
    assert window.calls == []


def test_study_cut_populates_cache_on_miss() -> None:
    cache: dict[str, Any] = {"funding_through": {}}
    spec, window = _spec(_study_rows(), through=DAYS[1], cache=cache)
    state = study_cut(spec)
    assert window.calls == [DAYS[1]]
    stored = cache["funding_through"][DAYS[1]]
    assert stored[0] is state.evidence
    assert stored[1] is state.study_by_date
    assert stored[2] == state.ref_near
    assert stored[3] == state.ref_far
    assert stored[4] is state.window_meta


def test_carry_plugin_of_and_strategy_name_of() -> None:
    spec, _ = _spec(_study_rows())
    assert carry_plugin_of(spec) is None
    assert strategy_name_of(spec) == "funding_only"
    plugin = SimpleNamespace(strategy_id="trusted_carry_candidate")
    spec_plugin, _ = _spec(_study_rows(), cache={"carry_plugin": plugin})
    assert carry_plugin_of(spec_plugin) is plugin
    assert strategy_name_of(spec_plugin) == "trusted_carry_candidate"


def test_funding_inputs_end_to_end(tmp_path) -> None:
    from funding_fixtures import PAIR as FX_PAIR
    from funding_fixtures import fixture
    from moex_spread_lab.proof_runtime.payload_tools import load_instruments
    from moex_spread_lab.proof_runtime.service import ProofService
    from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
    from moex_spread_lab.strategies.pair_screener.pair_screener import _pair

    fixture(tmp_path)
    service = ProofService(tmp_path)
    inputs = service.screener_inputs
    pair = _pair(FX_PAIR)
    study, near_provenance, far_provenance, window = service._funding_inputs(
        inputs, pair, ProofFunding(inputs), 0
    )
    assert study["rows"]
    assert near_provenance["raw_response_sha256"]
    assert far_provenance["raw_response_sha256"]
    assert list(window.days)

    terms = load_instruments(inputs.contracts)
    spec = FundingSpec(
        inputs=inputs,
        pair=pair,
        execution={"quantity": 1, "slippage_ticks": 1, "initial_capital_rub": 30000},
        params={"lookback": 5},
        terms={secid: terms.get(secid, {}) for secid in (pair["near"], pair["far"])},
        risk={},
        prepared=(study, near_provenance, far_provenance, window),
        through=None,
        cache=None,
    )
    state = study_cut(spec)
    expected_margin = (
        float(terms[pair["near"]].get("initial_margin") or 0.0)
        + float(terms[pair["far"]].get("initial_margin") or 0.0)
    ) * 1
    assert state.margin_pair == expected_margin
    assert state.ref_near == float(study["rows"][-1]["near_settle"])
    assert state.ref_far == float(study["rows"][-1]["far_settle"])
