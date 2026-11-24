"""Candidate data provisioning: APFS clones, private copies, symlink farms."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from moex_spread_lab.core import candidate_data, workstation_remote


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    (root / "data" / "analytics").mkdir(parents=True)
    (root / "data" / "w1").mkdir(parents=True)
    (root / "data-lake").mkdir(parents=True)
    (root / "data" / "w1" / "candles.json").write_text('{"rows":[]}')
    (root / "data" / "analytics" / "research-settings.json").write_text('{"random_seed":41}')
    (root / "data" / "analytics" / "screener-runs.jsonl").write_text("private\n")
    (root / "data" / "input.txt").write_text("original")
    seal = {
        "schema_version": "h1-sealed-read.v1",
        "root": str(root.resolve()),
        "plan": {},
        "records": {},
    }
    from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes

    marker = {
        "schema_version": "h1-sealed-read.v1",
        "seal_sha256": sha256_bytes(canonical_bytes(seal)),
    }
    (root / "data-lake" / "h1-seal.json").write_text(json.dumps(seal))
    (root / "data-lake" / "h1-seal.enabled.json").write_text(json.dumps(marker))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    return root, candidate


def test_copy_mode_is_private_and_skips_runtime_state(tmp_path):
    root, candidate = _checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "copy") == "copy"
    assert (candidate / "data" / "input.txt").read_text() == "original"
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (
        candidate / "data" / "analytics" / "research-settings.json"
    ).read_text() == '{"random_seed":41}'
    (candidate / "data" / "input.txt").write_text("changed")
    assert (root / "data" / "input.txt").read_text() == "original"


def test_clone_falls_back_to_copy_when_cp_fails(tmp_path, monkeypatch):
    root, candidate = _checkout(tmp_path)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "cp: unsupported")

    monkeypatch.setattr(candidate_data.subprocess, "run", fake_run)
    assert candidate_data.provision_data(root, candidate, "clone") == "copy"
    assert (candidate / "data" / "input.txt").read_text() == "original"


def test_clone_mode_uses_cow_clone_and_keeps_analytics_private(tmp_path, monkeypatch):
    root, candidate = _checkout(tmp_path)
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
    root, candidate = _checkout(tmp_path)
    assert candidate_data.provision_data(root, candidate, "symlink") == "symlink"
    assert (candidate / "data" / "input.txt").is_symlink()
    assert os.readlink(candidate / "data" / "input.txt") == str(root / "data" / "input.txt")
    seal = candidate / "data-lake" / "h1-seal.json"
    assert seal.is_file() and not seal.is_symlink()
    assert not (candidate / "data" / "analytics" / "screener-runs.jsonl").exists()
    assert (candidate / "data" / "analytics" / "research-settings.json").is_file()


def test_symlink_seal_rebind_never_touches_the_live_seal(tmp_path):
    root, candidate = _checkout(tmp_path)
    candidate_data.provision_data(root, candidate, "symlink")
    candidate_data.relocate_copied_seal(root, candidate)
    live = json.loads((root / "data-lake" / "h1-seal.json").read_text())
    private = json.loads((candidate / "data-lake" / "h1-seal.json").read_text())
    assert live["root"] == str(root.resolve())
    assert private["root"] == str(candidate.resolve())


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
    from moex_spread_lab.core.candidate_python import deps_inputs_touched as _deps_inputs_touched

    assert _deps_inputs_touched(["uv.lock"])
    assert _deps_inputs_touched(["packages/core/pyproject.toml"])
    assert _deps_inputs_touched(["packages/core/src/moex_spread_lab/core/_fastcore.c"])
    assert _deps_inputs_touched(["src/pkg/extension.pyx"])
    assert _deps_inputs_touched(["docs/notes.md"]) is False
    assert _deps_inputs_touched(["packages/proof/src/moex_spread_lab/proof/service.py"]) is False


def test_assert_candidate_python_accepts_shadowed_module(tmp_path):
    from moex_spread_lab.core.candidate_python import (
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
    from moex_spread_lab.core.candidate_python import (
        assert_candidate_python as _assert_candidate_python,
    )

    candidate = tmp_path / "checkout"
    candidate.mkdir()
    with pytest.raises(RuntimeError, match="not inside"):
        _assert_candidate_python(Path(sys.executable), "", candidate)
