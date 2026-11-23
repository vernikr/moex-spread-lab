"""Historical exports and live patch contracts across P7.2 proof boundaries."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.proof_runtime import (
    pair_funding_replay,
    pool_reports,
    proof_kernel,
    proof_pool,
    request_validation,
    strategy_search_prepare,
)
from moex_spread_lab.proof_runtime import (
    pair_plugin_accounting as accounting,
)
from moex_spread_lab.proof_runtime import (
    proof_strategy_search as search,
)
from pair_plugin_pipeline_fixtures_p72_packages_proof_tests_test_pair_plugin_pipeline import (
    DAYS,
    PairPlugin,
    fixture,
)


class HookHit(Exception):
    pass


def _hit(*args, **kwargs):
    raise HookHit


def test_historical_wildcard_names_and_identities_are_restored():
    expected = {
        accounting: {"attach_daily_unit_funding", "date", "datetime"},
        proof_kernel: {"date", "datetime", "time"},
        proof_pool: {"window_summaries"},
        search: {
            "CALENDAR_UNIVERSE",
            "get_proof_adapter_ids",
            "prepare_candidates",
            "record_trials",
            "winner_is_better",
            "no_prepared_result",
        },
        request_validation: {"annotations"},
    }
    for module, names in expected.items():
        namespace = {}
        exec(f"from {module.__name__} import *", namespace)
        assert names <= set(namespace)
    assert accounting.attach_daily_unit_funding is pair_funding_replay.attach_daily_unit_funding
    assert accounting._run_id is run_id
    assert proof_pool.window_summaries is pool_reports.window_summaries
    for name in ("prepare_candidates", "record_trials", "winner_is_better", "no_prepared_result"):
        assert getattr(search, name) is getattr(strategy_search_prepare, name)
        assert getattr(search, "_" + name) is getattr(search, name)


@pytest.mark.parametrize("hook", ["window_summaries", "_window_summaries"])
def test_pool_method_observes_both_hook_bindings(monkeypatch, tmp_path, hook):
    monkeypatch.setattr(proof_pool, hook, _hit)
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    try:
        with pytest.raises(HookHit):
            pool.window_summaries(tmp_path, ResearchSettings(), ([], {}), [], lambda: None)
    finally:
        pool.shutdown()


@pytest.mark.parametrize("hook", ["attach_daily_unit_funding", "_ATTACH_DAILY_UNIT_FUNDING"])
def test_pair_replay_observes_both_funding_hook_bindings(monkeypatch, hook):
    window, evidence, _ = fixture()
    monkeypatch.setattr(accounting, hook, _hit)
    with pytest.raises(HookHit):
        accounting.prepare_daily_pair_unit(
            PairPlugin("long_spread", False),
            window,
            {},
            "next_open",
            evidence,
            dict.fromkeys(DAYS, 1.0),
            ResearchSettings(),
        )


@pytest.mark.parametrize("hook", ["run_id", "_run_id"])
def test_pair_replay_observes_both_run_id_hook_bindings(monkeypatch, hook):
    window, evidence, _ = fixture()
    monkeypatch.setattr(accounting, hook, lambda *_args: "patched-id")
    unit, _ = accounting.prepare_daily_pair_unit(
        PairPlugin("long_spread", False),
        window,
        {},
        "next_open",
        evidence,
        dict.fromkeys(DAYS, 1.0),
        ResearchSettings(),
    )
    assert unit["run_id"] == "patched-id"


def _run_search(monkeypatch):
    monkeypatch.setattr(search, "_targets", lambda _request: [])
    monkeypatch.setattr(search, "_families", lambda *_args: [])
    monkeypatch.setattr(search, "_base_payload", lambda *_args: {})
    return search.strategy_search(
        SimpleNamespace(root=Path("/tmp"), _calendar=object()),
        {},
        ResearchSettings(random_seed=41),
        {},
        lambda: None,
    )


@pytest.mark.parametrize("hook", ["prepare_candidates", "_prepare_candidates"])
def test_search_observes_both_prepare_hook_bindings(monkeypatch, hook):
    monkeypatch.setattr(search, hook, _hit)
    with pytest.raises(HookHit):
        _run_search(monkeypatch)


@pytest.mark.parametrize("hook", ["no_prepared_result", "_no_prepared_result"])
def test_search_observes_both_empty_hook_bindings(monkeypatch, hook):
    monkeypatch.setattr(search, "_prepare_candidates", lambda *_args: ([], [], []))
    monkeypatch.setattr(search, hook, _hit)
    with pytest.raises(HookHit):
        _run_search(monkeypatch)


@pytest.mark.parametrize(
    "hook", ["record_trials", "_record_trials", "winner_is_better", "_winner_is_better"]
)
def test_search_family_observes_both_record_and_winner_hooks(monkeypatch, hook):
    cohort = SimpleNamespace(
        active="x",
        land=object(),
        seed={},
        axes={},
        members=[({}, SimpleNamespace(split=SimpleNamespace(training=[1])))],
    )
    run = SimpleNamespace(
        check_cancelled=lambda: None,
        labels={},
        base_seed=41,
        on_progress=None,
        records=[],
        top=0,
        winner=None,
        active="x",
        registry=None,
    )
    monkeypatch.setattr(
        search, "_call_with_optional_progress", lambda *_args, **_kwargs: (None, 0, [])
    )
    monkeypatch.setattr(search, hook, _hit)
    if "winner" in hook:
        monkeypatch.setattr(search, "_record_trials", lambda *_args: None)
    with pytest.raises(HookHit):
        search._search_family(run, 0, [({}, cohort, {})], 1)


def test_family_selection_observes_historical_adapter_patch(monkeypatch):
    from moex_spread_lab.core.timeframes import get_timeframe
    from moex_spread_lab.proof_runtime.strategy_catalog import get_strategy_registry

    monkeypatch.setattr(search, "get_proof_adapter_ids", lambda _registry: frozenset())
    assert (
        search._families(
            {}, [{"universe": "pair_screen"}], get_timeframe("1d"), get_strategy_registry()
        )
        == []
    )


def test_single_pair_universe_does_not_prefer_calendar_for_multi_universe_plugin():
    from moex_spread_lab.proof_runtime import (
        families_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search as families,
    )

    plugin = SimpleNamespace(supported_universes={"calendar_native", "pair_screen"})
    registry = {"multi": plugin}
    assert families._preferred_universe("multi", {"pair_screen"}, registry) == "pair_screen"
    assert (
        families._preferred_universe("multi", {"pair_screen", "calendar_native"}, registry)
        == "calendar_native"
    )
