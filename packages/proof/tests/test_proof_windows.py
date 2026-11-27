"""Exact signal boundaries, explicit same-bar reference and compact table results.

Covers the window-summary diagnostics end to end: band/fill expectations on both
universes, compact-row equivalence with the chart proof, body limits, catalog
screen checks and the progress contract shared by the local and worker paths.
"""

import json
import math
import statistics
from collections.abc import Iterator
from concurrent.futures import Future
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.core.settings import save_settings, settings_path
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import worker_cohort
from moex_spread_lab.proof.proof_search import StudyWindow, research_run
from moex_spread_lab.proof.proof_windows import MAX_WINDOW_REQUESTS, window_summaries
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_mean_reversion.engine import (
    AnalyticsError,
    run_mean_reversion_baseline,
)
from moex_spread_lab.strategies.signals import attach_entry_levels, pair_fill
from proof_fixtures import CAL, FLAT, PAIR, two_calendar_windows

FILLS = ("next_open", "next_close", "reference_same_bar")


@pytest.fixture
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProofService]:
    root = tmp_path_factory.mktemp("windows-root")
    two_calendar_windows(root)
    yield ProofService(root)


def run_proof(service: ProofService, req):
    return research_run(service, req, optimize=False, include_plateau=False)


def group_request(fill: str) -> dict[str, Any]:
    """Pair request widened to a checked cross-universe group for the given fill."""
    return {
        **PAIR,
        "fill": fill,
        "targets": [
            {k: v for k, v in r.items() if k in ("universe", "asset", "pair")} for r in (PAIR, CAL)
        ],
    }


def band_levels(
    base: dict[str, Any], fill: str, series: list[dict[str, Any]]
) -> tuple[dict[str, Any], float, float]:
    """Return the first full-window row and the entry/exit bands it must carry.

    The pair universe prices a RUB-neutral spread, so it uses the population
    deviation of the raw spread; the calendar universe prices a RUB point value,
    so it uses the sample deviation of the RUB-scaled spread. ``reference_same_bar``
    consumes one extra leading row because entry happens on the signal bar.
    """
    native = base["universe"] == "calendar_native"
    rows = [r for r in series if r.get("volume_eligible", True)]
    window = base["parameters"]["window" if native else "lookback"]
    i = window + 1
    end = i + 1 if native and fill == "reference_same_bar" else i
    sample = [
        r["spread"] * (r["point_value_rub"] if native else 1) for r in rows[end - window : end]
    ]
    mean = statistics.mean(sample)
    deviation = statistics.stdev(sample) if native else statistics.pstdev(sample)
    point = rows[i]["point_value_rub"] if native else 1
    entry_z = base["parameters"]["entry_z"]
    return rows[i], (mean - entry_z * deviation) / point, (mean + entry_z * deviation) / point


def assert_fill_selection(service: ProofService, proof: dict[str, Any], fill: str) -> None:
    """The selected fill drives execution reference, replay identity and entry prices."""
    same_bar = fill == "reference_same_bar"
    assert proof["selection"]["fill"] == fill
    assert proof["execution_reference"] == same_bar
    assert proof["evaluation"]["execution_reference"] == same_bar
    assert proof["summary"]["ledger_reconciliation_rub"] == 0
    assert run_proof(service, proof["selection"])["run_id"] == proof["run_id"]
    assert proof["trades"]
    rows = {r["date"]: r for r in proof["series"]}
    for trade in proof["trades"]:
        row = rows[trade["entry"]["date"]]
        assert trade["entry"]["near_price"] == row["near_open" if fill == "next_open" else "near"]
        if same_bar:
            assert trade["signal"]["date"] == trade["entry"]["date"]
    if same_bar:
        assert any("не является проверкой исполнимой" in note for note in proof["notes"])


def assert_entry_matches_signal_band(trade: dict[str, Any], signal: dict[str, Any]) -> None:
    """A long entry triggers at or below the buy band, a short one at or above the sell."""
    long_side = trade["side"] == "long_spread"
    bound = signal["buy_level" if long_side else "sell_level"]
    assert bound is not None
    if long_side:
        assert signal["spread"] - bound <= 1e-6
    else:
        assert signal["spread"] - bound >= -1e-6


def assert_zero_volume_row_is_ignored(proof: dict[str, Any]) -> None:
    """A zero-volume row cannot poison later bands, even with an absurd spread."""
    altered = deepcopy(proof)
    altered["series"][0]["volume_eligible"] = False
    altered["series"][0]["spread"] = 1e10
    attach_entry_levels(altered)
    without = deepcopy(proof)
    without["series"] = without["series"][1:]
    attach_entry_levels(without)
    assert [(r["buy_level"], r["sell_level"]) for r in altered["series"]][1:] == [
        (r["buy_level"], r["sell_level"]) for r in without["series"]
    ]


def assert_bands_are_prefix_causal(proof: dict[str, Any]) -> None:
    """Bands recomputed on a truncated series reproduce the full-series prefix."""
    prefix = deepcopy(proof)
    prefix["series"] = prefix["series"][:30]
    attach_entry_levels(prefix)
    assert [(r["buy_level"], r["sell_level"]) for r in proof["series"]][:30] == [
        (r["buy_level"], r["sell_level"]) for r in prefix["series"]
    ]


def assert_bands_follow_point_value(proof: dict[str, Any]) -> None:
    """Changing the RUB point value moves the bands, so it is not ignored."""
    fx = deepcopy(proof)
    for i, row in enumerate(fx["series"]):
        row["point_value_rub"] = 2 + i / 10
    attach_entry_levels(fx)
    assert fx["series"][20]["buy_level"] != proof["series"][20]["buy_level"]


def assert_compact_row_matches_proof(row: dict[str, Any], proof: dict[str, Any]) -> None:
    """A compact window row is the exact chart result with the heavy payloads omitted."""
    assert row["status"] == "measured"
    assert row["run_id"] == proof["run_id"]
    assert row["trades"] == proof["summary"]["trades"]
    assert row["annualized_excess_pp"] == proof["evaluation"]["result"].get("annualized_excess_pp")
    assert row["evaluation"] == proof["evaluation"]
    assert "ledger" not in row
    assert "plateau" not in row


@pytest.mark.parametrize("fill", FILLS)
@pytest.mark.parametrize("base", [PAIR, CAL], ids=["pair", "calendar"])
def test_fill_selection_replays_identically_in_both_universes(service, base, fill):
    p = run_proof(service, {**base, "fill": fill})
    assert_fill_selection(service, p, fill)


@pytest.mark.parametrize("base", [PAIR, CAL], ids=["pair", "calendar"])
def test_each_universe_produces_three_distinct_fill_run_ids(service, base):
    ids = [run_proof(service, {**base, "fill": fill})["run_id"] for fill in FILLS]
    assert len(set(ids)) == 3


def test_pair_default_fill_matches_reference_same_bar_run_id(service):
    assert (
        run_proof(service, PAIR)["run_id"]
        == run_proof(service, {**PAIR, "fill": "reference_same_bar"})["run_id"]
    )


@pytest.mark.parametrize("fill", FILLS)
@pytest.mark.parametrize("base", [PAIR, CAL], ids=["pair", "calendar"])
def test_every_entry_agrees_with_its_signal_date_band_not_the_later_fill(service, base, fill):
    p = run_proof(service, {**base, "fill": fill})
    rows = {r["date"]: r for r in p["series"]}
    for t in p["trades"]:
        assert_entry_matches_signal_band(t, rows[t["signal"]["date"]])
        if fill != "reference_same_bar":
            assert t["signal"]["date"] < t["entry"]["date"]


@pytest.mark.parametrize(
    "fill",
    ("next_open", "reference_same_bar"),
)
@pytest.mark.parametrize("base", [PAIR, CAL], ids=["pair", "calendar"])
def test_pair_uses_population_deviation_and_calendar_uses_rub_sample_deviation(service, base, fill):
    p = run_proof(service, {**base, "fill": fill})
    row, buy, sell = band_levels(base, fill, p["series"])
    assert row["buy_level"] == pytest.approx(buy, abs=5e-8)
    assert row["sell_level"] == pytest.approx(sell, abs=5e-8)


def test_bands_are_prefix_causal_and_zero_volume_poison_cannot_enter_the_window(service):
    p = run_proof(service, CAL)
    assert_zero_volume_row_is_ignored(p)
    assert_bands_are_prefix_causal(p)
    assert_bands_follow_point_value(p)


def test_ruonia_lines_invert_exactly_to_the_two_rate_thresholds(service):
    p = run_proof(
        service,
        {
            **CAL,
            "strategy": "convergence_to_ruonia",
            "parameters": {"threshold_pp": 0.7},
        },
    )
    for r in p["series"]:
        if r.get("buy_level") is None:
            continue
        assert r["dev_pp"] + (r["buy_level"] - r["spread"]) / r["rate_price_per_pp"] == (
            pytest.approx(-0.7)
        )
        assert r["dev_pp"] + (r["sell_level"] - r["spread"]) / r["rate_price_per_pp"] == (
            pytest.approx(0.7)
        )
        assert r["buy_level_rel"] == pytest.approx(r["buy_level"] / r["far"] * 100)


def test_funding_has_no_fabricated_price_bands(service):
    req = {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}}
    p = run_proof(service, req)
    assert p["signal_levels"]["kind"] == "not_price_based"
    assert not any(r.get("buy_level") is not None for r in p["series"])


@pytest.mark.parametrize(
    "bad",
    ("reference_same_bar", "next_close", "invalid", None),
    ids=["same-bar", "next-close", "invalid", "none"],
)
def test_funding_rejects_price_fills(bad):
    with pytest.raises(W0Error):
        pair_fill({"fill": bad}, "funding_only")


def test_invalid_request_fill_is_rejected(service):
    with pytest.raises(W0Error):
        run_proof(service, {**PAIR, "fill": "invalid"})


def test_primary_analytics_still_forbid_same_bar_execution(service):
    # Validation must happen before any market data is consumed.
    with pytest.raises(AnalyticsError):
        run_mean_reversion_baseline({}, decision_delay_bars=0)
    with pytest.raises(AnalyticsError):
        run_mean_reversion_baseline(
            {}, decision_delay_bars=0, analysis_role="diagnostic_only", fill="open"
        )


@pytest.mark.parametrize("fill", ("next_close", "reference_same_bar"))
def test_checked_pair_and_calendar_no_longer_reject_common_close_fills(service, fill):
    result = research_run(service, group_request(fill), optimize=True)
    assert result["status"] == "maximum_found"
    assert result["group"]["member_count"] == 2
    assert result["group"]["execution_reference"] == (fill == "reference_same_bar")
    assert all(m["selection"]["fill"] == fill for m in result["group"]["members"])
    assert all(
        m["selection"]["execution_reference"] == (fill == "reference_same_bar")
        for m in result["group"]["members"]
    )


def test_every_compact_result_matches_the_exact_chart_and_does_not_mutate_requests(service):
    requests = [PAIR, CAL, FLAT]
    original = deepcopy(requests)
    for share in (0, 25):
        save_settings(service.root, {"validation_percent": share, "slippage_ticks": 2.5})
        result = window_summaries(service, {"requests": requests})
        assert result["validation_percent"] == share
        for req, row in zip(requests, result["rows"], strict=True):
            assert_compact_row_matches_proof(row, run_proof(service, req))
        assert result["rows"][-1]["trades"] == 0
        assert result["rows"][-1]["annualized_excess_pp"] == 0
    assert requests == original
    assert not (service.root / "data/analytics/proof-searches.jsonl").exists()


def test_unavailable_row_is_not_a_zero_and_never_removes_another_row(service):
    bad = {**PAIR, "pair": "BTCUSDF/BTZ6"}
    invalid = {**CAL, "parameters": {"window": None}}
    result = window_summaries(service, {"requests": [PAIR, bad, invalid]})
    assert result["rows"][0]["status"] == "measured"
    for row in result["rows"][1:]:
        assert row["status"] == "unavailable"
        assert row["trades"] is None
        assert row["annualized_excess_pp"] is None
        assert row["reason"]


def test_accepted_trade_count_does_not_count_unfunded_intents(service):
    path = service.root / "data/w1/current-intake.json"
    data = json.loads(path.read_text())
    for contract in data["instruments"]:
        if contract["secid"] in ("CNYRUBF", "CRZ6"):
            contract["initial_margin"] = 16000
    path.write_text(json.dumps(data))
    row = window_summaries(service, {"requests": [PAIR]})["rows"][0]
    assert row["trades"] == 0
    assert row["annualized_excess_pp"] == 0


def test_all_rows_share_one_settings_snapshot_and_cancellation_is_observed(service):
    def changed(*args):
        save_settings(service.root, {"slippage_ticks": 4})
        return StudyWindow(*args)

    with patch("moex_spread_lab.proof.proof_windows.StudyWindow", side_effect=changed):
        result = window_summaries(service, {"requests": [PAIR, CAL]})
    assert result["slippage_ticks"] == 1
    assert all(r["selection"]["slippage_ticks"] == 1 for r in result["rows"])
    with pytest.raises(ConnectionAbortedError):
        window_summaries(
            service,
            {"requests": [PAIR, CAL]},
            check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
        )


@pytest.mark.parametrize(
    "body",
    (
        None,
        {},
        {"requests": []},
        {"requests": [PAIR] * (MAX_WINDOW_REQUESTS + 1)},
        {"requests": [PAIR, PAIR]},
        {"requests": [{"pair": None}]},
        {"requests": [PAIR], "targets": []},
    ),
    ids=["none", "empty", "no-requests", "overflow", "duplicates", "null-pair", "targets-set"],
)
def test_body_limits_reject_invalid_window_summary_requests(service, body):
    with pytest.raises(W0Error):
        window_summaries(service, body)


def test_catalog_checks_match_the_window_screen(service):
    catalog = service.catalog()
    pair = next(p for p in catalog["pair_screen"]["pairs"] if p["id"] == PAIR["pair"])
    proof = run_proof(service, PAIR)
    assert pair["passes_capital_screen"] == proof["window"]["passes_capital_screen"]
    assert pair["tradable_every_day"] == proof["window"]["tradable_every_day"]
    assert math.isfinite(pair["max_pair_margin_rub"])


def test_window_summaries_is_read_only_and_returns_requested_rows(service):
    result = window_summaries(service, {"requests": [PAIR, CAL]})
    assert len(result["rows"]) == 2
    assert not settings_path(service.root).exists()


class _InlineExecutor:
    def submit(self, fn, *args):
        fut = Future()
        fut.set_result(fn(*args))
        return fut


def test_progress_is_identical_for_local_and_worker_paths(service):
    requests = {"requests": [PAIR, CAL]}
    local_events = []
    with patch("moex_spread_lab.proof.proof_windows.get_worker_pool") as get_pool:
        get_pool.return_value.should_use.return_value = False
        local = window_summaries(service, requests, on_progress=local_events.append)

    worker_events = []
    try:
        with patch("moex_spread_lab.proof.proof_windows.get_worker_pool") as get_pool:
            pool = get_pool.return_value
            pool.max_workers = 2
            pool.should_use.return_value = True
            pool.ensure_workers.side_effect = lambda count: [
                _InlineExecutor() for _ in range(count)
            ]
            worker = window_summaries(service, requests, on_progress=worker_events.append)
    finally:
        worker_cohort._WORKER_SERVICES.pop(str(service.root.resolve()), None)
    assert worker == local
    assert worker_events == local_events
    assert [event["percent"] for event in local_events] == [4.0, 51.0, 98.0]
