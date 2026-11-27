"""The S4a tier split must never lose a parity test or its default coverage.

The equivalence suite is split by trial tier (plan S4a). ``test_perf_equivalence.py``
is the fast canary that runs in default discovery; ``_pair`` (1h pair-screen cohort),
``_pool`` (spawn-pool parity and cancellation recovery), ``_stream`` (NDJSON progress)
and ``_full`` (fastcore C parity) carry the full-resolution cases behind
``slow_equiv``. These guards pin the split so a later edit cannot quietly drop a
case from every file, duplicate one across tiers, or leave a full-resolution case
running in the default profile against the intent of the split.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROOF_TESTS = Path(__file__).resolve().parent
CANARY = PROOF_TESTS / "test_perf_equivalence.py"
PAIR = PROOF_TESTS / "test_perf_equivalence_pair.py"
POOL = PROOF_TESTS / "test_perf_equivalence_pool.py"
STREAM = PROOF_TESTS / "test_perf_equivalence_stream.py"
FULL = PROOF_TESTS / "test_perf_equivalence_full.py"
SLOW_TIERS = (PAIR, POOL, STREAM, FULL)
FIXTURES = PROOF_TESTS / "perf_equivalence_fixtures.py"
SCRIPTS = PROOF_TESTS.parents[2] / "scripts"

# Every equivalence case that existed before the split, in its new home. Adding a
# case is fine; dropping or moving one without updating the tier contract fails.
CANARY_CASES = {
    "test_1d_pair_funding_and_calendar_and_fixed_quantity_equivalence",
    "test_1h_calendar_proxy_and_native_cohort_and_mixed_universe",
    "test_file_caches_invalidate_immediately_on_in_place_same_size_edits",
    "test_default_reference_eval_is_fast_while_direct_through_replay_stays_full",
}
TIER_CASES = {
    "test_1h_pair_screen_cohort_search_plateau_windows_and_single_run": PAIR,
    "test_spawn_pool_matches_sequential_bit_for_bit": POOL,
    "test_spawn_pool_cooperative_cancellation_and_recovery": POOL,
    "test_streaming_progress_is_monotonic_and_matches_json_bit_for_bit": STREAM,
    "test_fastcore_c_matches_python_fallback_bit_for_bit": FULL,
}


def _test_methods(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_canary_file_keeps_exactly_the_default_tier_cases() -> None:
    assert _test_methods(CANARY) == CANARY_CASES


@pytest.mark.parametrize(("name", "path"), sorted(TIER_CASES.items()))
def test_every_full_resolution_case_keeps_its_tier(name: str, path: Path) -> None:
    assert name in _test_methods(path), f"{name} left {path.name}"


def test_no_equivalence_case_is_orphaned_or_duplicated() -> None:
    seen: dict[str, Path] = {}
    for path in (CANARY, *SLOW_TIERS):
        for name in _test_methods(path):
            assert name not in seen, f"{name} in both {seen.get(name)} and {path.name}"
            seen[name] = path
    assert seen.keys() == CANARY_CASES | TIER_CASES.keys()


@pytest.mark.parametrize("path", SLOW_TIERS, ids=lambda p: p.stem.removeprefix("test_"))
def test_slow_tiers_are_marked_and_the_canary_is_not(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.slow_equiv" in source, path.name
    assert "pytest.mark.slow_equiv" not in CANARY.read_text(encoding="utf-8")


def test_default_discovery_deselects_the_full_tier() -> None:
    pyproject = (PROOF_TESTS.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'addopts = ["-m", "not slow_equiv"]' in pyproject


@pytest.mark.parametrize("script", ["run_sanitized_pytest.py", "verify.py"])
def test_full_resolution_commands_override_the_default_deselection(script: str) -> None:
    """A sanitizer or pre-push run must pass ``-m ""`` or name every slow tier."""
    source = (SCRIPTS / script).read_text(encoding="utf-8")
    assert 'marker=""' in source or '-m ""' in source
    for path in SLOW_TIERS:
        assert path.name in source, f"{script} never names {path.name}"


@pytest.mark.parametrize("workflow", ["ci.yml", "code-health-weekly.yml"])
def test_whole_suite_workflows_name_every_slow_tier(workflow: str) -> None:
    source = (PROOF_TESTS.parents[2] / ".github" / "workflows" / workflow).read_text(
        encoding="utf-8"
    )
    for path in SLOW_TIERS:
        assert path.name in source, f"{workflow} never names {path.name}"


class _QuotaFile:
    """Stand-in for the cgroup v2 quota file that ``cpu_budget()`` reads."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self) -> str:
        return self._text


def test_worker_count_is_cgroup_aware_and_overridable(monkeypatch) -> None:
    """S4a item 5: the default follows the usable CPU budget, never the host count.

    ``os.sched_getaffinity`` exists on Linux only, and ``cpu_budget()`` guards its call
    with ``hasattr``; this test has to guard it the same way instead of assuming Linux.
    Either way the cgroup quota and the explicit override must still bound the budget,
    so the platform-independent half of the contract is asserted everywhere and the
    affinity half only where the platform actually provides affinity.
    """
    from scripts import verify

    monkeypatch.delenv("MSL_VERIFY_WORKERS", raising=False)
    assert verify.worker_count() >= 1
    monkeypatch.setattr(verify.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(verify, "Path", lambda _path: _QuotaFile("max 100000"))  # no quota
    if hasattr(verify.os, "sched_getaffinity"):
        monkeypatch.setattr(verify.os, "sched_getaffinity", lambda _pid: set(range(2)))
        assert verify.worker_count() == 2, "affinity must cap the host count"
    else:
        assert verify.worker_count() == 32, "without affinity the host count is the budget"

    budget = verify.worker_count()
    monkeypatch.setattr(verify, "Path", lambda _path: _QuotaFile("300000 100000"))
    assert verify.worker_count() == min(budget, 3), "a cgroup quota bounds the budget anywhere"
    monkeypatch.setenv("MSL_VERIFY_WORKERS", "5")
    assert verify.worker_count() == 5
    monkeypatch.setenv("MSL_VERIFY_WORKERS", "nonsense")
    assert verify.worker_count() == min(budget, 3), "a non-numeric override keeps the budget"


def test_shared_builders_live_outside_the_test_modules() -> None:
    """Every tier must clone identical bytes; the builders are not duplicated."""
    assert "def _build_h1_workspace(" in FIXTURES.read_text(encoding="utf-8")
    assert "def _build_pool_test_workspace(" in FIXTURES.read_text(encoding="utf-8")
    assert "class H1WorkspaceCase(" in FIXTURES.read_text(encoding="utf-8")
    for retired in ("perf_equivalence_case.py", "perf_equivalence_pool_fixtures.py"):
        assert not (PROOF_TESTS / retired).exists()
    for tier in (CANARY, PAIR, POOL, STREAM, FULL):
        source = tier.read_text(encoding="utf-8")
        assert "def _build_h1_workspace(" not in source, tier.name
        assert "def _build_pool_test_workspace(" not in source, tier.name
        assert "perf_equivalence" in source, tier.name


@pytest.mark.parametrize("kind", ("pair", "calendar", "carry"))
def test_plugin_harness_does_not_spawn_nested_python(kind):
    path = PROOF_TESTS / f"test_{kind}_plugin_integration.py"
    tree = ast.parse(path.read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in imports:
        if isinstance(node, ast.Import):
            assert all(alias.name != "subprocess" for alias in node.names)
        else:
            assert node.module != "subprocess"
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"exec", "eval"}
        for node in ast.walk(tree)
    )
