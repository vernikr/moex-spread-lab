"""Isolated benchmark worktree and result comparison tests."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest

from moex_spread_lab.core import workstation_bench as bench


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.test")
    git(root, "config", "user.name", "Test")
    (root / "code.txt").write_text("old\n")
    git(root, "add", "code.txt")
    git(root, "commit", "-m", "fixture")
    data = root / "data/analytics"
    data.mkdir(parents=True)
    (data / "research-settings.json").write_text('{"random_seed":41}')
    (data / "private-history.jsonl").write_text("private")
    (root / "data/input.txt").write_text("original")
    return root


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
    with (
        pytest.raises(RuntimeError, match="trigger cleanup"),
        bench.candidate_worktree(root, revision, patch) as candidate,
    ):
        assert (candidate / "code.txt").read_text() == "new\n"
        assert not (candidate / "data/analytics/private-history.jsonl").exists()
        (candidate / "data/input.txt").write_text("changed")
        raise RuntimeError("trigger cleanup")
    assert (root / "data/input.txt").read_text() == "original"
    assert len(git(root, "worktree", "list", "--porcelain").strip().split("\n\n")) == 1


def test_copy_rejects_symlinks(tmp_path):
    root = repository(tmp_path)
    (root / "data/link").symlink_to(root / "data/input.txt")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(RuntimeError, match="symlinks"):
        bench._copy_data(root, target)


def test_benchmark_reports_exact_match_and_refuses_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_cmd", lambda *_args, **_kwargs: "abc")
    monkeypatch.setattr(bench, "_health", lambda: {"locks": {}})

    @contextmanager
    def worktree(*args):
        yield tmp_path

    @contextmanager
    def server(*args):
        yield 8766

    monkeypatch.setattr(bench, "candidate_worktree", worktree)
    monkeypatch.setattr(bench, "candidate_server", server)
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, payload: {"result": {"run_id": "same"}, "telemetry": {"median_ms": 1}},
    )
    result = bench.benchmark(tmp_path, {"suite": "calendar_proof", "revision": "abc"})
    assert result["exact_match"] is True
    monkeypatch.setattr(
        bench,
        "_post",
        lambda port, payload: {"result": {"run_id": str(port)}, "telemetry": {"median_ms": 1}},
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


def test_seal_copy_rebound_only_after_integrity_validation(tmp_path):
    from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes

    root = repository(tmp_path)
    lake = root / "data-lake"
    lake.mkdir()
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
    import json

    (lake / "h1-seal.json").write_text(json.dumps(seal))
    (lake / "h1-seal.enabled.json").write_text(json.dumps(marker))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    bench._copy_data(root, candidate)
    bench._relocate_copied_seal(root, candidate)
    copy = json.loads((candidate / "data-lake/h1-seal.json").read_text())
    activation = json.loads((candidate / "data-lake/h1-seal.enabled.json").read_text())
    assert copy["root"] == str(candidate.resolve())
    assert activation["seal_sha256"] == sha256_bytes(canonical_bytes(copy))
    assert json.loads((lake / "h1-seal.json").read_text()) == seal
    with pytest.raises(RuntimeError, match="invalid source"):
        bench._relocate_copied_seal(root, candidate)
