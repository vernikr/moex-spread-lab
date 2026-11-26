"""Candidate worktrees, data provisioning and benchmark comparison tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes
from moex_spread_lab.remote import bench, candidate_data, candidate_python
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
    original = bench._cmd

    def command(path, *args, **kwargs):
        commands.append(args)
        if args[:2] in (("uv", "sync"), ("git", "apply")):
            return ""
        return original(path, *args, **kwargs)

    monkeypatch.setattr(bench, "_cmd", command)
    monkeypatch.setattr(
        bench, "assert_candidate_python", lambda *a: pytest.fail("reuse must not be attempted")
    )
    patch = (
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-old\n+new\n"
    )
    with bench.candidate_worktree(root, revision, patch) as candidate:
        assert candidate.isolated_venv is True
        assert candidate.pythonpath is None
        assert candidate.python == candidate.path / ".venv/bin/python3"
    assert any(args[:2] == ("uv", "sync") for args in commands)


def _bench_fakes(monkeypatch, tmp_path, server):
    monkeypatch.setattr(bench, "_cmd", lambda *_args, **_kwargs: "abc")
    monkeypatch.setattr(bench, "_health", lambda: {"locks": {}})

    @contextmanager
    def worktree(*_args):
        yield bench.CandidateWorktree(tmp_path, tmp_path / "python", None, "copy", True)

    monkeypatch.setattr(bench, "candidate_worktree", worktree)
    monkeypatch.setattr(bench, "candidate_server", server)


def _post(result, median_ms):
    return lambda port, _payload, root: {
        "result": result(port),
        "telemetry": {"median_ms": median_ms(port)},
    }


def test_benchmark_reports_exact_match_and_refuses_drift(tmp_path, monkeypatch):
    @contextmanager
    def server(*_args, **_kwargs):
        yield 8766

    _bench_fakes(monkeypatch, tmp_path, server)
    spec = {"suite": "calendar_proof", "revision": "abc"}
    monkeypatch.setattr(bench, "_post", _post(lambda port: {"run_id": "same"}, lambda port: 1))
    assert bench.benchmark(tmp_path, spec)["exact_match"] is True
    monkeypatch.setattr(bench, "_post", _post(lambda port: {"run_id": str(port)}, lambda port: 1))
    with pytest.raises(RuntimeError, match="differ"):
        bench.benchmark(tmp_path, spec)


def test_fastcore_comparison_uses_isolated_servers_and_fails_on_drift(tmp_path, monkeypatch):
    modes = []

    @contextmanager
    def server(_candidate, overrides):
        modes.append(overrides)
        yield 8765 + len(modes)

    _bench_fakes(monkeypatch, tmp_path, server)
    spec = {"suite": "ab_fastcore", "revision": "abc"}
    monkeypatch.setattr(
        bench, "_post", _post(lambda port: {"sha256": "same"}, lambda port: port - 8764)
    )
    result = bench.benchmark(tmp_path, spec)
    assert result["exact_match"] and result["speedup_x"] == [1.5]
    assert [m["MOEX_PROOF_DISABLE_C"] for m in modes] == ["0", "1"]
    monkeypatch.setattr(bench, "_post", _post(lambda port: {"sha256": str(port)}, lambda port: 1))
    with pytest.raises(RuntimeError, match="differ"):
        bench.benchmark(tmp_path, spec)


def test_compact_result_hash_detects_drift_and_normalizes_root_bound_paths(tmp_path):
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

    a, b = tmp_path / "a", tmp_path / "b"
    value = {
        "action": "run",
        "result": {"provenance": {"candles_path": str(a / "data/a.json")}, "ledger": [1]},
        "telemetry": {},
    }
    digest = bench._compact(value, a)["result"]["sha256"]
    value["result"]["provenance"]["candles_path"] = str(b / "data/a.json")
    assert bench._compact(value, b)["result"]["sha256"] == digest  # root-bound path normalized
    value["result"]["provenance"]["candles_path"] = str(b / "data/other.json")
    assert bench._compact(value, b)["result"]["sha256"] != digest


def test_copy_mode_is_private_skips_runtime_state_and_rejects_symlinks(tmp_path):
    root, candidate = checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "copy") == "copy"
    assert (candidate / "data" / "input.txt").read_text() == "original"
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (
        candidate / "data" / "analytics" / "research-settings.json"
    ).read_text() == '{"random_seed":41}'
    (candidate / "data" / "input.txt").write_text("changed")
    assert (root / "data" / "input.txt").read_text() == "original"
    (root / "data/link").symlink_to(root / "data/input.txt")
    with pytest.raises(RuntimeError, match="symlinks"):
        candidate_data.copy_data(root, tmp_path / "second")


def test_clone_mode_uses_cow_clone_or_falls_back_to_copy(tmp_path, monkeypatch):
    root, candidate = checkout(tmp_path)
    cloned = []

    def cow_clone(args, **kwargs):
        source, dest = Path(args[-2]), Path(args[-1])
        assert args[1] == "-cR"
        cloned.append(source.name)
        if source.is_dir():
            dest.mkdir(parents=True)
            for child in source.iterdir():
                shutil.copy2(child, dest / child.name)
        else:
            shutil.copy2(source, dest)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(candidate_data.subprocess, "run", cow_clone)
    assert candidate_data.provision_data(root, candidate, "clone") == "clone"
    assert "analytics" not in cloned
    assert (candidate / "data" / "analytics" / "research-settings.json").is_file()

    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(
        candidate_data.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "cp: unsupported"),
    )
    assert candidate_data.provision_data(root, fallback, "clone") == "copy"
    assert (fallback / "data" / "input.txt").read_text() == "original"


def test_symlink_mode_links_live_tree_but_seal_and_analytics_stay_private(tmp_path):
    root, candidate = checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "symlink") == "symlink"
    assert (candidate / "data" / "input.txt").is_symlink()
    assert os.readlink(candidate / "data" / "input.txt") == str(root / "data" / "input.txt")
    seal = candidate / "data-lake" / "h1-seal.json"
    assert seal.is_file() and not seal.is_symlink()
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (candidate / "data" / "analytics" / "research-settings.json").is_file()
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


def test_patch_paths_and_dependency_input_detection():
    patch = (
        "diff --git a/README.md b/README.md\n"
        'diff --git "a/docs/c d.md" "b/docs/c d.md"\n'
        "diff --git a/old/name.py b/new/name.py\n"
    )
    paths = workstation_remote.patch_paths(patch)
    assert {"README.md", "docs/c d.md", "old/name.py", "new/name.py"} <= set(paths)
    assert candidate_python.deps_inputs_touched(["uv.lock"])
    assert candidate_python.deps_inputs_touched(["packages/core/pyproject.toml"])
    assert candidate_python.deps_inputs_touched(
        ["packages/core/src/moex_spread_lab/core/_fastcore.c"]
    )
    assert candidate_python.deps_inputs_touched(["docs/notes.md"]) is False
    assert (
        candidate_python.deps_inputs_touched(
            ["packages/proof/src/moex_spread_lab/proof/service.py"]
        )
        is False
    )


def test_candidate_python_guard_accepts_shadowed_module_and_rejects_escape(tmp_path):
    candidate = tmp_path / "checkout"
    module_dir = candidate / "packages" / "core" / "src" / "moex_spread_lab" / "core"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("")
    (module_dir / "w0.py").write_text("SENTINEL = True\n")
    candidate_python.assert_candidate_python(
        Path(sys.executable), str(candidate / "packages" / "core" / "src"), candidate
    )
    # Escape detection must hold with and without dist-info: the child interpreter
    # resolves moex_spread_lab.core from the repository's outside PYTHONPATH.
    outside = Path(__file__).resolve().parents[1] / "src"
    with pytest.raises(RuntimeError, match="not inside"):
        candidate_python.assert_candidate_python(Path(sys.executable), str(outside), candidate)


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


def test_suite_catalog_matches_specification():
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
    windows = bench._suite("window_table_batch", {}, 1, 0)[0]["payload"]["requests"]
    assert len(windows) == len({r["pair"] for r in windows}) == 10
    for name in ("cohort_optimize", "ab_fastcore"):
        job = bench._suite(name, {}, 1, 0)[0]
        assert job["payload"]["optimize_strategy"] is True
        assert len(job["payload"]["targets"]) == 10
        assert job["settings_override"]["random_seed"] == 41
