"""Regression tests for the code-health ratchets, tiered budgets and the code_inspect helper."""

from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath

import pytest
from scripts import code_health_gate as gate
from scripts import code_health_gate_common as common
from scripts import code_health_gate_rules as rules
from scripts import code_inspect

CORE = "packages/core/src/moex_spread_lab/core"
KERNEL = f"{CORE}/_fastcore.c"
ARCHETYPE_PATHS = [
    ("production", f"{CORE}/probe.py"),
    ("declarative", f"{CORE}/contracts.py"),
    ("tests", "packages/core/tests/test_probe.py"),
    ("kernel", KERNEL),
]
SOURCE = "def probe(value):\n\n    return 'тест' if value else None\n\n\n# trailing comment\n"


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
    source = _write(tmp_path, "src/existing.py", "x" * 400 + "\nsecond\n")
    baseline = _baseline()
    baseline["files"]["src/existing.py"] = {"baseline_loc": 1, "baseline_tokens": 1}

    failures, _, _ = rules._check_files([source], baseline)

    assert any("loc grew" in failure for failure in failures)
    assert any("tokens grew" in failure for failure in failures)


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_file_archetypes_are_classified_from_the_path(archetype, relative):
    assert common._archetype_for(relative) == archetype


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_budget_bands_are_green_advisory_or_hard(archetype, relative):
    budget = common.FILE_BUDGETS[archetype]

    def outcome(loc: int, tokens: int = 1, band: str = "green") -> tuple[list[str], list[str]]:
        metrics = _metrics(loc, tokens)
        # The inner-loop helper reads the same policy and must report the same band.
        assert code_inspect._band(relative, metrics) == (archetype, band, budget)
        return rules._budget_outcome(relative, metrics)

    assert outcome(budget["soft_loc"]) == ([], [])  # green stays silent
    failures, advisories = outcome(budget["soft_loc"] + 1, band="advisory")
    assert failures == [] and "advisory band" in advisories[0]
    failures, advisories = outcome(budget["hard_loc"] + 1, band="over-hard")
    assert "hard ceiling" in failures[0] and advisories == []
    if archetype == "kernel":  # the C99 unit is governed by LOC and per-function limits only
        assert outcome(1, 10**6) == ([], [])
    else:
        assert outcome(1, budget["soft_tokens"]) == ([], [])
        failures, advisories = outcome(1, budget["soft_tokens"] + 1, band="advisory")
        assert failures == [] and "tokens in the" in advisories[0]
        assert "hard ceiling" in outcome(1, budget["hard_tokens"] + 1, band="over-hard")[0][0]


def test_check_files_reports_yellow_band_without_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = _write(tmp_path, "scripts/borderline.py", "line\n" * 401)

    failures, metrics, advisories = rules._check_files([source], _baseline())

    assert failures == []
    assert "scripts/borderline.py" in metrics
    assert any("advisory band" in advisory for advisory in advisories)


@pytest.mark.parametrize(("archetype", "relative"), ARCHETYPE_PATHS)
def test_inspect_check_prints_the_gate_loc_and_tokens(
    tmp_path, monkeypatch, capsys, archetype, relative
):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(code_inspect, "ROOT", tmp_path)
    path = _write(tmp_path, relative, SOURCE * 3)

    _, gate_metrics, _ = rules._check_files([path], _baseline())
    assert code_inspect.main(["check", str(path), "--metrics-only"]) == 0

    expected = gate_metrics[relative]
    output = capsys.readouterr().out
    assert f"archetype={archetype} band=green" in output
    assert f"loc={expected['loc']} (soft {common.FILE_BUDGETS[archetype]['soft_loc']}," in output
    assert f"tokens={expected['tokens']} (" in output


def test_inspect_strict_limits_fail_only_over_hard_and_complexity_ceilings(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(code_inspect, "ROOT", tmp_path)
    budget = common.FILE_BUDGETS["production"]
    path = _write(tmp_path, "scripts/borderline.py", "x = 1\n" * (budget["soft_loc"] + 1))
    strict = ["check", str(path), "--metrics-only", "--strict-limits"]

    assert code_inspect.main(strict) == 0  # the advisory band never fails
    path.write_text("x = 1\n" * (budget["hard_loc"] + 1), encoding="utf-8")
    assert code_inspect.main(strict) == 1
    branches = "\n".join(f"    if value == {index}: return {index}" for index in range(17))
    path.write_text(f"def choose(value):\n{branches}\n    return -1\n", encoding="utf-8")
    assert code_inspect.main(strict) == 1
    assert "complexity ceiling 15" in capsys.readouterr().out


def test_inspect_complexity_excludes_nested_functions():
    tree = ast.parse("""
def outer(value):
    if value and value > 0:
        def inner(other):
            if other:
                return 1
        return inner(value)
    return 0
""")
    outer, inner = code_inspect._function_nodes(tree)
    assert code_inspect._complexity(outer) == 3
    assert code_inspect._complexity(inner) == 2


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
        f"{CORE}/contracts.py",
        "packages/core/tests/test_oversized.py",
    ):
        over_hard = common.FILE_BUDGETS[common._archetype_for(relative)]["hard_loc"] + 1
        code, out = _run_gate(monkeypatch, tmp_path, relative, "x = 1\n", over_hard, capsys)
        assert code == 1, relative
        assert "hard ceiling" in out, relative


def test_forbidden_overflow_filenames_fail_unless_grandfathered(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    p72 = _write(tmp_path, "packages/data/tests/test_helpers_p72_lake.py", "def test_x(): pass\n")
    numbered = _write(tmp_path, "packages/proof/tests/test_runner_119.py", "def test_y(): pass\n")
    encoded = _write(tmp_path, f"{CORE}/helpers_packages_core_src_w0.py", "X = 1\n")
    clean = _write(tmp_path, "scripts/change_scope.py", "X = 1\n")
    baseline = _baseline()
    baseline["legacy_fragment_files"] = ["packages/data/tests/test_helpers_p72_lake.py"]

    failures = rules._fragment_name_failures([p72, numbered, encoded, clean], baseline)

    assert not any("p72" in failure.split(":")[0] for failure in failures)
    assert len(failures) == 2
    assert any("issue-numbered shard" in failure for failure in failures)
    assert any("path-encoded shard" in failure for failure in failures)
    assert rules._fragment_name_failures([clean], _baseline()) == []


def test_shallow_single_caller_shard_is_an_advisory_only(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    shard = _write(tmp_path, f"{CORE}/helper_bits.py", "VALUE = 1\n")
    caller = _write(
        tmp_path,
        f"{CORE}/engine.py",
        "# padding\n" * 40 + "from moex_spread_lab.core.helper_bits import VALUE\n",
    )
    files = [shard, caller]
    baseline = _baseline()
    _, metrics, _ = rules._check_files(files, baseline)

    advisories = rules._shallow_shard_advisories(files, metrics, baseline)
    assert any("helper_bits.py" in advisory for advisory in advisories)
    assert any("intra-package caller" in advisory for advisory in advisories)
    grandfathered = {**baseline, "legacy_shallow_shards": [f"{CORE}/helper_bits.py"]}
    assert rules._shallow_shard_advisories(files, metrics, grandfathered) == []

    second = _write(
        tmp_path,
        f"{CORE}/cli.py",
        "# padding\n" * 40 + "from moex_spread_lab.core import helper_bits\n",
    )
    files.append(second)
    _, metrics, _ = rules._check_files(files, baseline)
    assert rules._shallow_shard_advisories(files, metrics, baseline) == []


def test_package_token_growth_is_an_advisory_outside_the_tolerance():
    baseline = _baseline()
    baseline["packages"]["packages/core"] = {"baseline_tokens": 1000}

    advisories = rules._package_token_advisories(
        {"packages/core": 1300, "packages/data": 20}, baseline
    )

    assert any(
        "packages/core: source tokens grew from 1000 to 1300 (+300)" in a for a in advisories
    )
    assert any("packages/data" in advisory for advisory in advisories)
    assert rules._package_token_advisories({"packages/core": 1019}, baseline) == []


def test_shipped_baseline_is_justified_by_the_tiered_policy():
    baseline = json.loads(common.BASELINE_PATH.read_text(encoding="utf-8"))

    for relative, entry in baseline["files"].items():
        metrics = {"loc": int(entry["baseline_loc"]), "tokens": int(entry["baseline_tokens"])}
        assert not rules._baseline_entry_is_obsolete(relative, metrics), relative
    # The fragment inventory may be empty; any entry that remains must still be an
    # offending name, never a stale allowlist slot.
    for relative in baseline["legacy_fragment_files"]:
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
    source = _write(
        tmp_path, "protected/guarded.py", "try:\n    pass\nexcept Exception:\n    pass\n"
    )
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(rules, "PROTECTED_EXCEPTION_ROOTS", (source.parent,))

    assert rules._exception_failures()

    source.write_text(
        "try:\n    pass\nexcept Exception:  # code-health: allow broad exception: test fallback\n    pass\n",
        encoding="utf-8",
    )
    assert rules._exception_failures() == []


def test_fastcore_exports_require_a_python_parity_inventory(tmp_path, monkeypatch):
    _write(
        tmp_path,
        KERNEL,
        "static PyMethodDef FastCoreMethods[] = {\n"
        '    {"probe_method", probe, METH_VARARGS, "probe"},\n'
        "    {NULL, NULL, 0, NULL}\n};\n",
    )
    test_file = _write(
        tmp_path,
        "packages/proof/tests/test_fastcore_parity.py",
        "FASTCORE_EXPORT_PARITY_TESTS = {\n"
        "    'probe_method': 'packages/proof/tests/test_fastcore_parity.py::test_probe'\n"
        "}\n"
        "def test_probe():\n"
        "    return True\n",
    )
    monkeypatch.setattr(rules, "ROOT", tmp_path)

    assert rules._fastcore_parity_failures() == []
    test_file.write_text(
        "FASTCORE_EXPORT_PARITY_TESTS = {}\ndef test_probe():\n    return True\n",
        encoding="utf-8",
    )
    assert any("probe_method" in failure for failure in rules._fastcore_parity_failures())


def test_baseline_update_only_lowers_or_removes_existing_entries():
    huge, fits = f"{CORE}/huge.py", f"{CORE}/fits.py"
    baseline = _baseline()
    baseline["files"] = {
        huge: {"baseline_loc": 720, "baseline_tokens": 7400},
        fits: {"baseline_loc": 450, "baseline_tokens": 3800},
    }
    baseline["complexity"] = {"old.py::still_high": 20, "old.py::resolved": 18}
    baseline["c_functions"] = {
        "old.c::still_high": {"baseline_nloc": 120, "baseline_ccn": 18, "baseline_parameters": 8},
        "old.c::resolved": {"baseline_nloc": 120, "baseline_ccn": 16, "baseline_parameters": 8},
    }

    updated = rules._updated_baseline(
        baseline,
        {
            huge: {"loc": 700, "tokens": 7200},
            fits: {"loc": 430, "tokens": 3700},
            "new.py": {"loc": 10, "tokens": 20},
        },
        {"old.py::still_high": 17, "old.py::resolved": 15},
        {
            "old.c::still_high": {"nloc": 110, "ccn": 17, "parameters": 8},
            "old.c::resolved": {"nloc": 100, "ccn": 14, "parameters": 7},
        },
    )

    assert updated["files"] == {huge: {"baseline_loc": 700, "baseline_tokens": 7200}}
    assert updated["complexity"] == {"old.py::still_high": 17}
    assert updated["c_functions"] == {
        "old.c::still_high": {"baseline_nloc": 110, "baseline_ccn": 17, "baseline_parameters": 8}
    }


def test_baseline_update_keeps_the_kernel_ratchet_above_the_soft_target(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    kernel = _write(tmp_path, KERNEL, "int x;\n")
    baseline = _baseline()
    baseline["files"][KERNEL] = {"baseline_loc": 1400, "baseline_tokens": 13000}

    above = {KERNEL: {"loc": 1314, "tokens": 12500}}
    updated = rules._updated_baseline(baseline, above, {}, {}, [kernel])
    assert updated["files"][KERNEL] == {"baseline_loc": 1314, "baseline_tokens": 12500}

    inside = {KERNEL: {"loc": 880, "tokens": 8000}}
    assert rules._updated_baseline(baseline, inside, {}, {}, [kernel])["files"] == {}


def test_baseline_update_shrinks_legacy_fragment_and_shard_inventories(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    p72 = _write(tmp_path, f"{CORE}/legacy_p72_shard.py", "VALUE = 1\n")
    tiny = _write(tmp_path, f"{CORE}/tiny.py", "VALUE = 2\n")
    baseline = _baseline()
    baseline["legacy_fragment_files"] = [f"{CORE}/legacy_p72_shard.py", f"{CORE}/gone_p72.py"]
    baseline["legacy_shallow_shards"] = [f"{CORE}/tiny.py", f"{CORE}/gone.py"]
    baseline["packages"] = {
        "packages/core": {"baseline_tokens": 9000},
        "packages/gone": {"baseline_tokens": 10},
    }
    metrics = {
        f"{CORE}/legacy_p72_shard.py": {"loc": 3, "tokens": 30},
        f"{CORE}/tiny.py": {"loc": 1, "tokens": 10},
    }

    updated = rules._updated_baseline(baseline, metrics, {}, {}, [p72, tiny])

    assert updated["legacy_fragment_files"] == [f"{CORE}/legacy_p72_shard.py"]
    assert updated["legacy_shallow_shards"] == [f"{CORE}/tiny.py"]
    assert updated["packages"] == {"packages/core": {"baseline_tokens": 40}}
