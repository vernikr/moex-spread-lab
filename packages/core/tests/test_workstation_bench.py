"""Candidate worktrees, data provisioning and benchmark comparison tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes
from moex_spread_lab.remote import bench, candidate_data
from moex_spread_lab.remote import workstation as workstation_remote


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _h1_seal(root: Path) -> dict:
    """Write a valid h1-seal pair rooted at ``root``; return the seal payload."""
    seal = {
        "schema_version": "h1-sealed-read.v1",
        "root": str(root.resolve()),
        "plan": {},
        "records": {},
    }
    marker = {
        "schema_version": seal["schema_version"],
        "seal_sha256": sha256_bytes(canonical_bytes(seal)),
    }
    lake = root / "data-lake"
    lake.mkdir(parents=True, exist_ok=True)
    (lake / "h1-seal.json").write_text(json.dumps(seal))
    (lake / "h1-seal.enabled.json").write_text(json.dumps(marker))
    return seal


def _analytics_tree(root: Path, private_history: str) -> None:
    analytics = root / "data" / "analytics"
    analytics.mkdir(parents=True)
    (analytics / "research-settings.json").write_text('{"random_seed":41}')
    (analytics / private_history).write_text("private\n")


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.test")
    git(root, "config", "user.name", "Test")
    (root / "code.txt").write_text("old\n")
    git(root, "add", "code.txt")
    git(root, "commit", "-m", "fixture")
    _analytics_tree(root, "private-history.jsonl")
    (root / "data" / "input.txt").write_text("original")
    return root


def checkout(tmp_path):
    """Synthetic non-git root (h1-seal pair, private analytics) plus a candidate dir."""
    root = tmp_path / "root"
    (root / "data" / "w1").mkdir(parents=True)
    _analytics_tree(root, "screener-runs.jsonl")
    (root / "data" / "w1" / "candles.json").write_text('{"rows":[]}')
    (root / "data" / "input.txt").write_text("original")
    _h1_seal(root)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    return root, candidate


def test_worktree_isolated_copies_patch_and_cleanup(tmp_path, monkeypatch):
    root = repository(tmp_path)
    revision = git(root, "rev-parse", "HEAD").strip()
    (root / "code.txt").write_text("new\n")
    patch = git(root, "diff", "HEAD")
    original = bench._cmd

    def command(path, *args, **kwargs):
        if args[:2] == ("uv", "sync"):
            return ""
        return original(path, *args, **kwargs)

    monkeypatch.setattr(bench, "_cmd", command)
    monkeypatch.setattr(bench, "assert_candidate_python", lambda *args: None)
    with (
        pytest.raises(RuntimeError, match="trigger cleanup"),
        bench.candidate_worktree(root, revision, patch) as candidate,
    ):
        assert candidate.isolated_venv is False
        assert str(candidate.path) in (candidate.pythonpath or "")
        assert (candidate.path / "code.txt").read_text() == "new\n"
        assert not (candidate.path / "data/analytics/private-history.jsonl").exists()
        (candidate.path / "data/input.txt").write_text("changed")
        raise RuntimeError("trigger cleanup")
    assert (root / "data/input.txt").read_text() == "original"
    assert len(git(root, "worktree", "list", "--porcelain").strip().split("\n\n")) == 1


def test_patch_touching_dependency_inputs_forces_isolated_venv(tmp_path, monkeypatch):
    root = repository(tmp_path)
    revision = git(root, "rev-parse", "HEAD").strip()
    commands = []
    _original_cmd = bench._cmd

    def command(path, *args, **kwargs):
        commands.append(args)
        if args[:2] in (("uv", "sync"), ("git", "apply")):
            return ""
        return _original_cmd(path, *args, **kwargs)

    monkeypatch.setattr(bench, "_cmd", command)
    monkeypatch.setattr(
        bench, "assert_candidate_python", lambda *a: pytest.fail("reuse must not be attempted")
    )
    patch = "diff --git a/uv.lock b/uv.lock\nindex 0000000..1111111 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-old\n+new\n"
    with bench.candidate_worktree(root, revision, patch) as candidate:
        assert candidate.isolated_venv is True
        assert candidate.pythonpath is None
        assert candidate.python == candidate.path / ".venv/bin/python3"
    assert any(args[:2] == ("uv", "sync") for args in commands)


def test_copy_rejects_symlinks(tmp_path):
    root = repository(tmp_path)
    (root / "data/link").symlink_to(root / "data/input.txt")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(RuntimeError, match="symlinks"):
        candidate_data.copy_data(root, target)


def test_benchmark_reports_exact_match_and_refuses_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_cmd", lambda *_args, **_kwargs: "abc")
    monkeypatch.setattr(bench, "_health", lambda: {"locks": {}})

    @contextmanager
    def worktree(*args):
        yield bench.CandidateWorktree(tmp_path, tmp_path / "python", None, "copy", True)

    @contextmanager
    def server(*args, **kwargs):
        yield 8766

    monkeypatch.setattr(bench, "candidate_worktree", worktree)
    monkeypatch.setattr(bench, "candidate_server", server)
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, _payload, root: {
            "result": {"run_id": "same"},
            "telemetry": {"median_ms": 1},
        },
    )
    result = bench.benchmark(tmp_path, {"suite": "calendar_proof", "revision": "abc"})
    assert result["exact_match"] is True
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, _payload, root: {
            "result": {"run_id": str(port)},
            "telemetry": {"median_ms": 1},
        },
    )
    with pytest.raises(RuntimeError, match="differ"):
        bench.benchmark(tmp_path, {"suite": "calendar_proof", "revision": "abc"})


def test_full_result_hash_is_deterministic_and_detects_nested_drift():
    value = {
        "action": "run",
        "result": {"run_id": "one", "summary": {"ledger_reconciliation_rub": 0}, "ledger": [1, 2]},
        "telemetry": {"wall_ms": [1]},
    }
    first = bench._compact(value)
    value["result"]["ledger"] = [1, 3]
    second = bench._compact(value)
    assert first["result"]["sha256"] != second["result"]["sha256"]
    assert first["result"]["ledger_reconciliation_rub"] == 0
    assert "ledger" not in first["result"]


def test_copy_mode_is_private_and_skips_runtime_state(tmp_path):
    root, candidate = checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "copy") == "copy"
    assert (candidate / "data" / "input.txt").read_text() == "original"
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (
        candidate / "data" / "analytics" / "research-settings.json"
    ).read_text() == '{"random_seed":41}'
    (candidate / "data" / "input.txt").write_text("changed")
    assert (root / "data" / "input.txt").read_text() == "original"


def test_clone_falls_back_to_copy_when_cp_fails(tmp_path, monkeypatch):
    root, candidate = checkout(tmp_path)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "cp: unsupported")

    monkeypatch.setattr(candidate_data.subprocess, "run", fake_run)
    assert candidate_data.provision_data(root, candidate, "clone") == "copy"
    assert (candidate / "data" / "input.txt").read_text() == "original"


def test_clone_mode_uses_cow_clone_and_keeps_analytics_private(tmp_path, monkeypatch):
    root, candidate = checkout(tmp_path)
    cloned = []

    def fake_run(args, **kwargs):
        source = Path(args[-2])
        dest = Path(args[-1])
        assert args[1] == "-cR"
        cloned.append((source.name, dest.name))
        import shutil

        if source.is_dir():
            dest.mkdir(parents=True)
            for child in source.iterdir():
                shutil.copy2(child, dest / child.name)
        else:
            shutil.copy2(source, dest)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(candidate_data.subprocess, "run", fake_run)
    assert candidate_data.provision_data(root, candidate, "clone") == "clone"
    names = {name for name, _ in cloned}
    assert "analytics" not in names
    assert (candidate / "data" / "analytics" / "research-settings.json").is_file()


def test_symlink_mode_links_live_tree_but_seals_and_analytics_stay_private(tmp_path):
    root, candidate = checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "symlink") == "symlink"
    assert (candidate / "data" / "input.txt").is_symlink()
    assert os.readlink(candidate / "data" / "input.txt") == str(root / "data" / "input.txt")
    seal = candidate / "data-lake" / "h1-seal.json"
    assert seal.is_file() and not seal.is_symlink()
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (candidate / "data" / "analytics" / "research-settings.json").is_file()


def test_symlink_seal_rebind_never_touches_the_live_seal(tmp_path):
    root, candidate = checkout(tmp_path)
    candidate_data.provision_data(root, candidate, "symlink")
    candidate_data.relocate_copied_seal(root, candidate)
    live = json.loads((root / "data-lake" / "h1-seal.json").read_text())
    private = json.loads((candidate / "data-lake" / "h1-seal.json").read_text())
    assert live["root"] == str(root.resolve())
    assert private["root"] == str(candidate.resolve())


def test_seal_copy_rebound_only_after_integrity_validation(tmp_path):
    root = repository(tmp_path)
    seal = _h1_seal(root)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate_data.copy_data(root, candidate)
    candidate_data.relocate_copied_seal(root, candidate)
    copy = json.loads((candidate / "data-lake/h1-seal.json").read_text())
    activation = json.loads((candidate / "data-lake/h1-seal.enabled.json").read_text())
    assert copy["root"] == str(candidate.resolve())
    assert activation["seal_sha256"] == sha256_bytes(canonical_bytes(copy))
    assert json.loads((root / "data-lake/h1-seal.json").read_text()) == seal
    with pytest.raises(RuntimeError, match="invalid source"):
        candidate_data.relocate_copied_seal(root, candidate)


def test_patch_paths_parses_plain_and_quoted_diffs():
    patch = (
        "diff --git a/README.md b/README.md\n"
        "index 111..222 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "diff --git a/docs/a b.md b/docs/a b.md\n"  # invalid: unquoted space path is not produced by git
        'diff --git "a/docs/c d.md" "b/docs/c d.md"\n'
        "diff --git a/old/name.py b/new/name.py\n"
    )
    paths = workstation_remote.patch_paths(patch)
    assert "README.md" in paths
    assert "docs/c d.md" in paths
    assert "old/name.py" in paths and "new/name.py" in paths


def test_dependency_inputs_detection():
    from moex_spread_lab.remote.candidate_python import deps_inputs_touched as _deps_inputs_touched

    assert _deps_inputs_touched(["uv.lock"])
    assert _deps_inputs_touched(["packages/core/pyproject.toml"])
    assert _deps_inputs_touched(["packages/core/src/moex_spread_lab/core/_fastcore.c"])
    assert _deps_inputs_touched(["src/pkg/extension.pyx"])
    assert _deps_inputs_touched(["docs/notes.md"]) is False
    assert _deps_inputs_touched(["packages/proof/src/moex_spread_lab/proof/service.py"]) is False


def test_assert_candidate_python_accepts_shadowed_module(tmp_path):
    from moex_spread_lab.remote.candidate_python import (
        assert_candidate_python as _assert_candidate_python,
    )

    candidate = tmp_path / "checkout"
    module_dir = candidate / "packages" / "core" / "src" / "moex_spread_lab" / "core"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("")
    (module_dir / "w0.py").write_text("SENTINEL = True\n")
    pythonpath = str(candidate / "packages" / "core" / "src")
    _assert_candidate_python(Path(sys.executable), pythonpath, candidate)


def test_assert_candidate_python_rejects_escape(tmp_path):
    from moex_spread_lab.remote.candidate_python import (
        assert_candidate_python as _assert_candidate_python,
    )

    candidate = tmp_path / "checkout"
    candidate.mkdir()
    # Escape detection must hold with and without dist-info: the child interpreter
    # resolves moex_spread_lab.core from the repository's outside PYTHONPATH.
    outside = Path(__file__).resolve().parents[1] / "src"
    with pytest.raises(RuntimeError, match="not inside"):
        _assert_candidate_python(Path(sys.executable), str(outside), candidate)


def test_concurrent_benchmarks_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with (
        bench.benchmark_lock(tmp_path),
        pytest.raises(RuntimeError, match="another benchmark"),
        bench.benchmark_lock(tmp_path),
    ):
        pass
    with bench.benchmark_lock(tmp_path):
        pass


def test_curated_single_and_calendar_workloads_match_specification():
    jobs = bench._suite("single_proof_1d_1h", {}, 1, 0)
    assert len(jobs) == 4
    assert {job["payload"]["pair"] for job in jobs} == {"CNYRUBF/CRZ6", "GLDRUBF/GLZ6"}
    assert {job["settings_override"]["timeframe"] for job in jobs} == {"1d", "1h"}
    assert all(job["payload"]["sizing"] == "max_affordable" for job in jobs)
    calendar = bench._suite("calendar_proof", {}, 1, 0)
    assert {job["payload"]["strategy"] for job in calendar} == {
        "zscore_mean_reversion",
        "convergence_to_ruonia",
    }


def test_cohort_workloads_have_ten_distinct_windows_and_strategy_search():
    windows = bench._suite("window_table_batch", {}, 1, 0)[0]["payload"]["requests"]
    assert len(windows) == len({r["pair"] for r in windows}) == 10
    for name in ("cohort_optimize", "ab_fastcore"):
        job = bench._suite(name, {}, 1, 0)[0]
        assert job["payload"]["optimize_strategy"] is True
        assert len(job["payload"]["targets"]) == 10
        assert job["settings_override"]["random_seed"] == 41


def test_fastcore_comparison_uses_isolated_servers_and_fails_on_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_cmd", lambda *_args, **_kwargs: "abc")
    monkeypatch.setattr(bench, "_health", lambda: {"locks": {}})
    modes = []

    @contextmanager
    def worktree(*args):
        yield bench.CandidateWorktree(tmp_path, tmp_path / "python", None, "copy", True)

    @contextmanager
    def server(candidate, overrides):
        modes.append(overrides)
        yield 8765 + len(modes)

    monkeypatch.setattr(bench, "candidate_worktree", worktree)
    monkeypatch.setattr(bench, "candidate_server", server)
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, _payload, root: {
            "result": {"sha256": "same"},
            "telemetry": {"median_ms": port - 8764},
        },
    )
    result = bench.benchmark(tmp_path, {"suite": "ab_fastcore", "revision": "abc"})
    assert result["exact_match"] and result["speedup_x"] == [1.5]
    assert [m["MOEX_PROOF_DISABLE_C"] for m in modes] == ["0", "1"]
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, _payload, root: {
            "result": {"sha256": str(port)},
            "telemetry": {"median_ms": 1},
        },
    )
    with pytest.raises(RuntimeError, match="differ"):
        bench.benchmark(tmp_path, {"suite": "ab_fastcore", "revision": "abc"})


def test_full_result_hash_normalizes_only_root_bound_provenance(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    value = {
        "action": "run",
        "result": {"provenance": {"candles_path": str(a / "data/a.json")}, "ledger": [1]},
        "telemetry": {},
    }
    first = bench._compact(value, a)["result"]["sha256"]
    value["result"]["provenance"]["candles_path"] = str(b / "data/a.json")
    assert bench._compact(value, b)["result"]["sha256"] == first
    value["result"]["ledger"] = [2]
    assert bench._compact(value, b)["result"]["sha256"] != first
    value["result"]["ledger"] = [1]
    value["result"]["provenance"]["candles_path"] = str(b / "data/other.json")
    assert bench._compact(value, b)["result"]["sha256"] != first
