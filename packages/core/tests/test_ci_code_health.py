"""Tooling tests: CI Tier 2 path filter, the weekly code-health digest and the typecheck view."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from scripts import change_scope, typecheck_workspace
from scripts import render_code_health_digest as digest


def test_matches_c_tier2_filters_c_and_python_paths() -> None:
    tiers = ("pair", "pool", "stream", "full")
    for path in (
        "packages/core/src/moex_spread_lab/core/_fastcore.c",
        "packages/core/src/moex_spread_lab/core/_fastcore_loader.py",
        ".clang-tidy",
        "scripts/run_sanitized_pytest.py",
        *(f"packages/proof/tests/test_perf_equivalence_{tier}.py" for tier in tiers),
    ):
        assert change_scope.matches_c_tier2([path]), path
    assert not change_scope.matches_c_tier2(
        ["packages/web/src/moex_spread_lab/web/server.py", "docs/development/ci.md"]
    )


def test_render_code_health_digest_produces_markdown_tables(tmp_path: Path) -> None:
    hotspot, partner = "packages/proof/service.py", "packages/web/server.py"
    for relative in (hotspot, partner):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("x = 1\n", encoding="utf-8")
    db_path = tmp_path / "wiki.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE git_metadata (file_path TEXT, commit_count_90d INTEGER, "
            "lines_added_90d INTEGER, lines_deleted_90d INTEGER, co_change_partners_json TEXT, "
            "history_only INTEGER)"
        )
        partners = json.dumps(
            [{"file_path": partner, "frequency": 9, "co_change_count": 2.15, "structural": "x"}]
        )
        conn.execute(
            "INSERT INTO git_metadata VALUES (?, ?, ?, ?, ?, 0)", (hotspot, 12, 300, 120, partners)
        )
        conn.commit()
    health_path = tmp_path / "repowise-health.json"
    health = {"metrics": [{"file_path": hotspot, "score": 3.2, "nloc": 900, "max_ccn": 40}]}
    health_path.write_text(json.dumps(health), encoding="utf-8")
    jscpd_path = tmp_path / "jscpd-report.json"
    total = {"sources": 10, "lines": 1000, "clones": 1, "duplicatedLines": 18, "percentage": 1.8}
    clone = {
        "format": "python",
        "lines": 18,
        "firstFile": {"name": hotspot, "start": 1, "end": 18},
        "secondFile": {"name": partner, "start": 5, "end": 22},
    }
    report = {"statistics": {"total": total}, "duplicates": [clone]}
    jscpd_path.write_text(json.dumps(report), encoding="utf-8")

    payload = digest.build_digest_payload(
        jscpd_path=jscpd_path,
        repowise_health_path=health_path,
        repowise_db_path=db_path,
        root=tmp_path,
    )
    md = digest.render_digest_markdown(payload)

    assert "## Top-10 Hotspots" in md and f"`{hotspot}`" in md
    assert "## Top Change-Coupled File Pairs" in md and f"`{partner}`" in md
    assert "1.80%" in md


@pytest.fixture
def namespace_root(tmp_path: Path) -> Path:
    packages = ("core", "data", "accounting", "strategies", "proof", "web")
    roots = [f"packages/{name}/src/moex_spread_lab/{name}" for name in packages]
    for relative in (*roots, "src/moex_spread_lab"):
        directory = tmp_path / relative
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("\n", encoding="utf-8")
    return tmp_path


def test_typecheck_view_unifies_namespace_roots_and_is_reproducible(namespace_root: Path) -> None:
    for relative in (
        "packages/core/src/moex_spread_lab/.DS_Store",
        "src/moex_spread_lab/.DS_Store",
    ):
        (namespace_root / relative).write_bytes(b"macOS metadata")

    view = typecheck_workspace.build_view(namespace_root)

    # one symlink per namespace entry; macOS metadata never enters the view
    assert {path.name for path in (view / "moex_spread_lab").iterdir()} == {
        "__init__.py",
        "accounting",
        "core",
        "data",
        "proof",
        "strategies",
        "web",
    }
    core = view / "moex_spread_lab/core"
    assert core.is_symlink()
    assert core.resolve() == namespace_root / "packages/core/src/moex_spread_lab/core"

    stale = view / "moex_spread_lab/stale.py"
    stale.symlink_to(namespace_root / "src/moex_spread_lab/__init__.py")
    typecheck_workspace.build_view(namespace_root)
    assert not stale.exists()


def test_typecheck_view_refuses_unmanaged_directories_and_namespace_collisions(
    namespace_root: Path,
) -> None:
    generated = namespace_root / ".typecheck_typings"
    generated.mkdir()
    marker = generated / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    with pytest.raises(typecheck_workspace.ViewError, match="unmarked"):
        typecheck_workspace.build_view(namespace_root)
    assert marker.read_text(encoding="utf-8") == "user data"

    shutil.rmtree(generated)
    (namespace_root / "packages/data/src/moex_spread_lab/core").mkdir()
    with pytest.raises(typecheck_workspace.ViewError, match="duplicate namespace"):
        typecheck_workspace.build_view(namespace_root)
