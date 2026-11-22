"""Regression tests for repository code-health ratchets."""

from __future__ import annotations

from scripts import code_health_gate as gate
from scripts import code_health_gate_common as common
from scripts import code_health_gate_rules as rules


def _baseline() -> dict:
    return {"files": {}, "complexity": {}, "c_functions": {}, "guard_files": []}


def test_existing_file_growth_is_blocked_for_loc_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    source = tmp_path / "src" / "existing.py"
    source.parent.mkdir()
    source.write_text("x" * 400 + "\nsecond\n", encoding="utf-8")
    baseline = _baseline()
    baseline["files"]["src/existing.py"] = {"baseline_loc": 1, "baseline_tokens": 1}

    failures, _ = rules._check_files([source], baseline)

    assert any("loc grew" in failure for failure in failures)
    assert any("tokens grew" in failure for failure in failures)


def test_new_file_loc_budget_and_c_budget_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    python_file = tmp_path / "scripts" / "new.py"
    c_file = tmp_path / "src" / "new.c"
    python_file.parent.mkdir()
    c_file.parent.mkdir()
    python_file.write_text("line\n" * 401, encoding="utf-8")
    c_file.write_text("int value;\n" * 401, encoding="utf-8")

    failures, _ = rules._check_files([python_file, c_file], _baseline())

    assert any("scripts/new.py: new file has 401 LOC" in failure for failure in failures)
    assert not any("src/new.c" in failure for failure in failures)


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
        "old.py": {"baseline_loc": 500, "baseline_tokens": 4200},
        "resolved.py": {"baseline_loc": 450, "baseline_tokens": 3800},
    }
    baseline["complexity"] = {"old.py::still_high": 20, "old.py::resolved": 18}
    baseline["c_functions"] = {
        "old.c::still_high": {"baseline_nloc": 120, "baseline_ccn": 18, "baseline_parameters": 8},
        "old.c::resolved": {"baseline_nloc": 120, "baseline_ccn": 16, "baseline_parameters": 8},
    }

    updated = rules._updated_baseline(
        baseline,
        {
            "old.py": {"loc": 460, "tokens": 3900},
            "resolved.py": {"loc": 90, "tokens": 700},
            "new.py": {"loc": 10, "tokens": 20},
        },
        {"old.py::still_high": 17, "old.py::resolved": 15},
        {
            "old.c::still_high": {"nloc": 110, "ccn": 17, "parameters": 8},
            "old.c::resolved": {"nloc": 100, "ccn": 14, "parameters": 7},
        },
    )

    assert updated["files"] == {"old.py": {"baseline_loc": 460, "baseline_tokens": 3900}}
    assert updated["complexity"] == {"old.py::still_high": 17}
    assert updated["c_functions"] == {
        "old.c::still_high": {"baseline_nloc": 110, "baseline_ccn": 17, "baseline_parameters": 8}
    }


def test_main_fails_when_any_ratchet_reports_a_regression(monkeypatch, capsys):
    monkeypatch.setattr(gate, "_load_baseline", _baseline)
    monkeypatch.setattr(gate, "_source_files", list)
    monkeypatch.setattr(gate, "_check_files", lambda _files, _baseline: (["probe regression"], {}))
    monkeypatch.setattr(gate, "_run_complexipy", dict)
    monkeypatch.setattr(gate, "_run_lizard", dict)
    monkeypatch.setattr(gate, "_complexity_failures", lambda _current, _baseline: [])
    monkeypatch.setattr(gate, "_c_function_failures", lambda _current, _baseline: [])
    monkeypatch.setattr(gate, "_fastcore_parity_failures", list)
    monkeypatch.setattr(gate, "_exception_failures", list)

    assert gate.main([]) == 1
    assert "probe regression" in capsys.readouterr().out
