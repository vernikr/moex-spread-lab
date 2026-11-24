"""Pin the inner-loop ``code_inspect`` helper to the code-health gate's tiered budgets (#158)."""

from __future__ import annotations

import pytest
from scripts import code_health_gate_common as common
from scripts import code_health_gate_rules as rules
from scripts import code_inspect

ARCHETYPE_PATHS = [
    ("production", "packages/core/src/moex_spread_lab/core/probe.py"),
    ("declarative", "packages/core/src/moex_spread_lab/core/contracts.py"),
    ("tests", "packages/core/tests/test_probe.py"),
    ("kernel", "packages/core/src/moex_spread_lab/core/_fastcore.c"),
]
SOURCE = "def probe(value):\n\n    return 'тест' if value else None\n\n\n# trailing comment\n"


def _empty_baseline() -> dict:
    return {
        "files": {},
        "complexity": {},
        "c_functions": {},
        "packages": {},
        "guard_files": [],
        "legacy_fragment_files": [],
        "legacy_shallow_shards": [],
    }


def _gate_band(relative: str, metrics: dict[str, int]) -> str:
    failures, advisories = rules._budget_outcome(relative, metrics)
    return "over-hard" if failures else "advisory" if advisories else "green"


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_helper_bands_match_the_gate_budgets(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]
    measures = [measure for measure in code_inspect.SIZE_MEASURES if f"hard_{measure}" in budget]

    for measure in measures:
        for value in (budget[f"soft_{measure}"], budget[f"soft_{measure}"] + 1):
            metrics = {"loc": 1, "tokens": 1, measure: value}
            helper_archetype, band, helper_budget = code_inspect._band(relative, metrics)
            assert (helper_archetype, helper_budget) == (archetype, budget)
            assert band == _gate_band(relative, metrics)
        over = {"loc": 1, "tokens": 1, measure: budget[f"hard_{measure}"] + 1}
        assert code_inspect._band(relative, over)[1] == _gate_band(relative, over) == "over-hard"


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_helper_prints_the_gate_loc_and_tokens(tmp_path, monkeypatch, capsys, archetype, relative):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(code_inspect, "ROOT", tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SOURCE * 3, encoding="utf-8")

    _, gate_metrics, _ = rules._check_files([path], _empty_baseline())
    assert code_inspect.main(["check", str(path), "--metrics-only"]) == 0

    expected = gate_metrics[relative]
    output = capsys.readouterr().out
    assert f"archetype={archetype} band=green" in output
    assert f"loc={expected['loc']} (soft {common.FILE_BUDGETS[archetype]['soft_loc']}," in output
    assert f"tokens={expected['tokens']} (" in output


def test_strict_limits_fail_only_over_the_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(code_inspect, "ROOT", tmp_path)
    budget = common.FILE_BUDGETS["production"]
    path = tmp_path / "scripts" / "borderline.py"
    path.parent.mkdir()

    path.write_text("x = 1\n" * (budget["soft_loc"] + 1), encoding="utf-8")
    assert code_inspect.main(["check", str(path), "--metrics-only", "--strict-limits"]) == 0

    path.write_text("x = 1\n" * (budget["hard_loc"] + 1), encoding="utf-8")
    assert code_inspect.main(["check", str(path), "--metrics-only", "--strict-limits"]) == 1
