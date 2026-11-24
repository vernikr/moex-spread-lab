"""Regression tests for repository code-health ratchets and tiered file budgets."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

import pytest
from scripts import code_health_gate as gate
from scripts import code_health_gate_common as common
from scripts import code_health_gate_rules as rules

ARCHETYPE_PATHS = [
    ("production", "packages/core/src/moex_spread_lab/core/probe.py"),
    ("declarative", "packages/core/src/moex_spread_lab/core/contracts.py"),
    ("tests", "packages/core/tests/test_probe.py"),
    ("kernel", "packages/core/src/moex_spread_lab/core/_fastcore.c"),
]


def _baseline() -> dict:
    return {
        "files": {},
        "complexity": {},
        "c_functions": {},
        "packages": {},
        "guard_files": [],
        "legacy_fragment_files": [],
        "legacy_shallow_shards": [],
    }


def _metrics(loc: int, tokens: int = 1) -> dict[str, int]:
    return {"loc": loc, "tokens": tokens}


def _write(tmp_path, relative: str, text: str):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_existing_file_growth_is_blocked_for_loc_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = tmp_path / "src" / "existing.py"
    source.parent.mkdir()
    source.write_text("x" * 400 + "\nsecond\n", encoding="utf-8")
    baseline = _baseline()
    baseline["files"]["src/existing.py"] = {"baseline_loc": 1, "baseline_tokens": 1}

    failures, _, _ = rules._check_files([source], baseline)

    assert any("loc grew" in failure for failure in failures)
    assert any("tokens grew" in failure for failure in failures)


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_file_archetypes_are_classified_from_the_path(archetype, relative):
    assert common._archetype_for(relative) == archetype


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_green_band_files_stay_silent(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]

    assert rules._budget_outcome(relative, _metrics(budget["soft_loc"])) == ([], [])


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_yellow_band_loc_is_advisory_without_failure(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]

    failures, advisories = rules._budget_outcome(relative, _metrics(budget["soft_loc"] + 1))

    assert failures == []
    assert advisories and "advisory band" in advisories[0]


@pytest.mark.parametrize(
    ("archetype", "relative"),
    [case for case in ARCHETYPE_PATHS if case[0] != "kernel"],
)
def test_yellow_band_tokens_is_advisory_without_failure(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]

    failures, advisories = rules._budget_outcome(relative, _metrics(1, budget["soft_tokens"] + 1))

    assert failures == []
    assert advisories and "tokens in the" in advisories[0]


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_red_band_over_the_hard_ceiling_fails(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]

    failures, advisories = rules._budget_outcome(relative, _metrics(budget["hard_loc"] + 1))

    assert failures and "hard ceiling" in failures[0]
    assert advisories == []


def test_kernel_tokens_are_not_budgeted(monkeypatch):
    """The C99 translation unit is governed by LOC and per-function limits only."""
    failures, advisories = rules._budget_outcome(
        "packages/core/src/moex_spread_lab/core/_fastcore.c", _metrics(1, 10**6)
    )

    assert (failures, advisories) == ([], [])


def test_check_files_reports_yellow_band_without_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = _write(tmp_path, "scripts/borderline.py", "line\n" * 401)

    failures, metrics, advisories = rules._check_files([source], _baseline())

    assert failures == []
    assert "scripts/borderline.py" in metrics
    assert any("advisory band" in advisory for advisory in advisories)


def _run_gate(monkeypatch, tmp_path, relative: str, line: str, repeats: int, capsys):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = _write(tmp_path, relative, line * repeats)
    baseline = _baseline()
    baseline["packages"] = {
        group: {"baseline_tokens": 10**9} for group in ("scripts", "packages/core", "packages/data")
    }
    monkeypatch.setattr(gate, "_load_baseline", lambda: baseline)
    monkeypatch.setattr(gate, "_source_files", lambda: [source])
    monkeypatch.setattr(gate, "_run_complexipy", dict)
    monkeypatch.setattr(gate, "_run_lizard", dict)
    monkeypatch.setattr(gate, "_fastcore_parity_failures", list)
    monkeypatch.setattr(gate, "_exception_failures", list)

    code = gate.main([])
    return code, capsys.readouterr().out


def test_main_exits_zero_in_the_green_and_yellow_bands(monkeypatch, tmp_path, capsys):
    green, green_out = _run_gate(monkeypatch, tmp_path, "scripts/green.py", "x = 1\n", 400, capsys)
    yellow, yellow_out = _run_gate(
        monkeypatch, tmp_path, "packages/core/tests/test_big.py", "x = 1\n", 601, capsys
    )

    assert green == 0
    assert "advisory: " not in green_out
    assert yellow == 0
    assert "advisory: packages/core/tests/test_big.py" in yellow_out


def test_main_exits_one_above_every_archetype_hard_ceiling(monkeypatch, tmp_path, capsys):
    for relative in (
        "scripts/oversized.py",
        "packages/core/src/moex_spread_lab/core/contracts.py",
        "packages/core/tests/test_oversized.py",
    ):
        archetype = common._archetype_for(relative)
        code, out = _run_gate(
            monkeypatch,
            tmp_path,
            relative,
            "x = 1\n",
            common.FILE_BUDGETS[archetype]["hard_loc"] + 1,
            capsys,
        )
        assert code == 1, relative
        assert "hard ceiling" in out, relative


def test_forbidden_overflow_filenames_fail_unless_grandfathered(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    p72 = _write(tmp_path, "packages/data/tests/test_helpers_p72_lake.py", "def test_x(): pass\n")
    numbered = _write(tmp_path, "packages/proof/tests/test_runner_119.py", "def test_y(): pass\n")
    encoded = _write(
        tmp_path,
        "packages/core/src/moex_spread_lab/core/helpers_packages_core_src_w0.py",
        "X = 1\n",
    )
    baseline = _baseline()
    baseline["legacy_fragment_files"] = ["packages/data/tests/test_helpers_p72_lake.py"]

    failures = rules._fragment_name_failures([p72, numbered, encoded], baseline)

    assert not any("p72" in failure.split(":")[0] for failure in failures)
    assert len(failures) == 2
    assert any("issue-numbered shard" in failure for failure in failures)
    assert any("path-encoded shard" in failure for failure in failures)


def test_clean_filenames_pass_the_fragmentation_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = _write(tmp_path, "scripts/shadow_runner.py", "X = 1\n")

    assert rules._fragment_name_failures([source], _baseline()) == []


def test_shallow_single_caller_shard_is_an_advisory_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    package = "packages/core/src/moex_spread_lab/core"
    shard = _write(tmp_path, f"{package}/helper_bits.py", "VALUE = 1\n")
    caller = _write(
        tmp_path,
        f"{package}/engine.py",
        "# padding\n" * 40 + "from moex_spread_lab.core.helper_bits import VALUE\n",
    )
    files = [shard, caller]
    baseline = _baseline()
    _, metrics, _ = rules._check_files(files, baseline)

    advisories = rules._shallow_shard_advisories(files, metrics, baseline)
    assert any("helper_bits.py" in advisory for advisory in advisories)
    assert any("intra-package caller" in advisory for advisory in advisories)

    second = _write(
        tmp_path,
        f"{package}/cli.py",
        "# padding\n" * 40 + "from moex_spread_lab.core import helper_bits\n",
    )
    files.append(second)
    _, metrics, _ = rules._check_files(files, baseline)
    assert rules._shallow_shard_advisories(files, metrics, baseline) == []


def test_shallow_shard_advisory_respects_the_legacy_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    package = "packages/core/src/moex_spread_lab/core"
    shard = _write(tmp_path, f"{package}/helper_bits.py", "VALUE = 1\n")
    baseline = _baseline()
    baseline["legacy_shallow_shards"] = [f"{package}/helper_bits.py"]
    _, metrics, _ = rules._check_files([shard], baseline)

    assert rules._shallow_shard_advisories([shard], metrics, baseline) == []


def test_package_token_growth_is_surfaced_as_an_advisory():
    baseline = _baseline()
    baseline["packages"]["packages/core"] = {"baseline_tokens": 1000}

    advisories = rules._package_token_advisories(
        {"packages/core": 1300, "packages/data": 20}, baseline
    )

    assert any(
        "packages/core: source tokens grew from 1000 to 1300 (+300)" in a for a in advisories
    )
    assert any("packages/data" in advisory for advisory in advisories)


def test_package_token_growth_inside_the_tolerance_stays_silent():
    baseline = _baseline()
    baseline["packages"]["packages/core"] = {"baseline_tokens": 1000}

    assert rules._package_token_advisories({"packages/core": 1019}, baseline) == []


def test_shipped_baseline_entries_are_justified_by_the_tiered_policy():
    baseline = json.loads(common.BASELINE_PATH.read_text(encoding="utf-8"))

    for relative, entry in baseline["files"].items():
        metrics = {"loc": int(entry["baseline_loc"]), "tokens": int(entry["baseline_tokens"])}
        assert not rules._baseline_entry_is_obsolete(relative, metrics), relative


def test_shipped_legacy_inventories_only_list_offending_names():
    baseline = json.loads(common.BASELINE_PATH.read_text(encoding="utf-8"))
    offenders = set(baseline["legacy_fragment_files"])

    assert offenders
    for relative in offenders:
        assert common._fragment_name_problem(PurePosixPath(relative).name) is not None, relative


def test_complexity_and_c_function_ratchets_block_new_and_growing_outliers():
    baseline = _baseline()
    baseline["complexity"] = {"legacy.py::old": 21}
    baseline["c_functions"] = {
        "legacy.c::old": {"baseline_nloc": 120, "baseline_ccn": 16, "baseline_parameters": 7}
    }

    complexity = rules._complexity_failures({"new.py::new": 16, "legacy.py::old": 22}, baseline)
    c_functions = rules._c_function_failures(
        {
            "new.c::new": {"nloc": 101, "ccn": 16, "parameters": 8},
            "legacy.c::old": {"nloc": 121, "ccn": 16, "parameters": 7},
        },
        baseline,
    )

    assert any("new cognitive complexity 16" in failure for failure in complexity)
    assert any("grew from 21 to 22" in failure for failure in complexity)
    assert any("new C function exceeds limits" in failure for failure in c_functions)
    assert any("C nloc grew from 120 to 121" in failure for failure in c_functions)


def test_broad_exception_requires_adjacent_justification(tmp_path, monkeypatch):
    root = tmp_path / "protected"
    root.mkdir()
    source = root / "guarded.py"
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(rules, "PROTECTED_EXCEPTION_ROOTS", (root,))
    source.write_text("try:\n    pass\nexcept Exception:\n    pass\n", encoding="utf-8")

    assert rules._exception_failures()

    source.write_text(
        "try:\n    pass\nexcept Exception:  # code-health: allow broad exception: test fallback\n    pass\n",
        encoding="utf-8",
    )
    assert rules._exception_failures() == []


def test_fastcore_exports_require_a_python_parity_inventory(tmp_path, monkeypatch):
    root = tmp_path
    c_file = root / "packages/core/src/moex_spread_lab/core/_fastcore.c"
    test_file = root / "packages/proof/tests/test_fastcore_parity.py"
    c_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    c_file.write_text(
        "static PyMethodDef FastCoreMethods[] = {\n"
        '    {"probe_method", probe, METH_VARARGS, "probe"},\n'
        "    {NULL, NULL, 0, NULL}\n};\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "FASTCORE_EXPORT_PARITY_TESTS = {\n"
        "    'probe_method': 'packages/proof/tests/test_fastcore_parity.py::test_probe'\n"
        "}\n"
        "def test_probe():\n"
        "    return True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rules, "ROOT", root)

    assert rules._fastcore_parity_failures() == []
    test_file.write_text(
        "FASTCORE_EXPORT_PARITY_TESTS = {}\ndef test_probe():\n    return True\n",
        encoding="utf-8",
    )
    assert any("probe_method" in failure for failure in rules._fastcore_parity_failures())


def test_baseline_update_only_lowers_or_removes_existing_entries():
    baseline = _baseline()
    baseline["files"] = {
        "packages/core/src/moex_spread_lab/core/huge.py": {
            "baseline_loc": 720,
            "baseline_tokens": 7400,
        },
        "packages/core/src/moex_spread_lab/core/fits.py": {
            "baseline_loc": 450,
            "baseline_tokens": 3800,
        },
    }
    baseline["complexity"] = {"old.py::still_high": 20, "old.py::resolved": 18}
    baseline["c_functions"] = {
        "old.c::still_high": {"baseline_nloc": 120, "baseline_ccn": 18, "baseline_parameters": 8},
        "old.c::resolved": {"baseline_nloc": 120, "baseline_ccn": 16, "baseline_parameters": 8},
    }

    updated = rules._updated_baseline(
        baseline,
        {
            "packages/core/src/moex_spread_lab/core/huge.py": {"loc": 700, "tokens": 7200},
            "packages/core/src/moex_spread_lab/core/fits.py": {"loc": 430, "tokens": 3700},
            "new.py": {"loc": 10, "tokens": 20},
        },
        {"old.py::still_high": 17, "old.py::resolved": 15},
        {
            "old.c::still_high": {"nloc": 110, "ccn": 17, "parameters": 8},
            "old.c::resolved": {"nloc": 100, "ccn": 14, "parameters": 7},
        },
    )

    assert updated["files"] == {
        "packages/core/src/moex_spread_lab/core/huge.py": {
            "baseline_loc": 700,
            "baseline_tokens": 7200,
        }
    }
    assert updated["complexity"] == {"old.py::still_high": 17}
    assert updated["c_functions"] == {
        "old.c::still_high": {"baseline_nloc": 110, "baseline_ccn": 17, "baseline_parameters": 8}
    }


def test_baseline_update_keeps_the_kernel_ratchet_above_the_soft_target(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    kernel = _write(tmp_path, "packages/core/src/moex_spread_lab/core/_fastcore.c", "int x;\n")
    baseline = _baseline()
    baseline["files"]["packages/core/src/moex_spread_lab/core/_fastcore.c"] = {
        "baseline_loc": 1400,
        "baseline_tokens": 13000,
    }
    metrics = {"packages/core/src/moex_spread_lab/core/_fastcore.c": {"loc": 1314, "tokens": 12500}}

    updated = rules._updated_baseline(baseline, metrics, {}, {}, [kernel])
    assert updated["files"]["packages/core/src/moex_spread_lab/core/_fastcore.c"] == {
        "baseline_loc": 1314,
        "baseline_tokens": 12500,
    }

    inside = {"packages/core/src/moex_spread_lab/core/_fastcore.c": {"loc": 880, "tokens": 8000}}
    assert rules._updated_baseline(baseline, inside, {}, {}, [kernel])["files"] == {}


def test_baseline_update_shrinks_legacy_fragment_and_shard_inventories(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    package = "packages/core/src/moex_spread_lab/core"
    p72 = _write(tmp_path, f"{package}/legacy_p72_shard.py", "VALUE = 1\n")
    tiny = _write(tmp_path, f"{package}/tiny.py", "VALUE = 2\n")
    baseline = _baseline()
    baseline["legacy_fragment_files"] = [f"{package}/legacy_p72_shard.py", f"{package}/gone_p72.py"]
    baseline["legacy_shallow_shards"] = [f"{package}/tiny.py", f"{package}/gone.py"]
    baseline["packages"] = {
        "packages/core": {"baseline_tokens": 9000},
        "packages/gone": {"baseline_tokens": 10},
    }
    metrics = {
        f"{package}/legacy_p72_shard.py": {"loc": 3, "tokens": 30},
        f"{package}/tiny.py": {"loc": 1, "tokens": 10},
    }

    updated = rules._updated_baseline(baseline, metrics, {}, {}, [p72, tiny])

    assert updated["legacy_fragment_files"] == [f"{package}/legacy_p72_shard.py"]
    assert updated["legacy_shallow_shards"] == [f"{package}/tiny.py"]
    assert updated["packages"] == {"packages/core": {"baseline_tokens": 40}}


def test_main_fails_when_any_ratchet_reports_a_regression(monkeypatch, capsys):
    monkeypatch.setattr(gate, "_load_baseline", _baseline)
    monkeypatch.setattr(gate, "_source_files", list)
    monkeypatch.setattr(
        gate, "_check_files", lambda _files, _baseline: (["probe regression"], {}, [])
    )
    monkeypatch.setattr(gate, "_run_complexipy", dict)
    monkeypatch.setattr(gate, "_run_lizard", dict)
    monkeypatch.setattr(gate, "_complexity_failures", lambda _current, _baseline: [])
    monkeypatch.setattr(gate, "_c_function_failures", lambda _current, _baseline: [])
    monkeypatch.setattr(gate, "_fastcore_parity_failures", list)
    monkeypatch.setattr(gate, "_exception_failures", list)

    assert gate.main([]) == 1
    assert "probe regression" in capsys.readouterr().out
