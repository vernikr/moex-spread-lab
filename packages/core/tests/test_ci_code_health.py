"""Tooling tests: CI Tier 2 path filter, the weekly code-health digest and the typecheck view."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from scripts import change_scope, typecheck_workspace
from scripts import render_code_health_digest as digest


def test_matches_c_tier2_filters_c_and_python_paths() -> None:
    assert change_scope.matches_c_tier2(["packages/core/src/moex_spread_lab/core/_fastcore.c"])
    assert change_scope.matches_c_tier2(
        ["packages/core/src/moex_spread_lab/core/_fastcore_loader.py"]
    )
    assert change_scope.matches_c_tier2([".clang-tidy"])
    assert change_scope.matches_c_tier2(["scripts/run_sanitized_pytest.py"])
    for tier in (
        "test_perf_equivalence_pair.py",
        "test_perf_equivalence_pool.py",
        "test_perf_equivalence_stream.py",
        "test_perf_equivalence_full.py",
    ):
        assert change_scope.matches_c_tier2([f"packages/proof/tests/{tier}"])
    assert not change_scope.matches_c_tier2(
        [
            "packages/web/src/moex_spread_lab/web/server.py",
            "docs/development/ci.md",
        ]
    )


def test_render_code_health_digest_produces_markdown_tables(tmp_path: Path) -> None:
    file_a = tmp_path / "packages/proof/service.py"
    file_b = tmp_path / "packages/web/server.py"
    file_a.parent.mkdir(parents=True)
    file_b.parent.mkdir(parents=True)
    file_a.write_text("x = 1\n", encoding="utf-8")
    file_b.write_text("y = 2\n", encoding="utf-8")

    db_path = tmp_path / "wiki.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE git_metadata ("
        "file_path TEXT, commit_count_90d INTEGER, lines_added_90d INTEGER, "
        "lines_deleted_90d INTEGER, co_change_partners_json TEXT, history_only INTEGER)"
    )
    partners = json.dumps(
        [
            {
                "file_path": "packages/web/server.py",
                "frequency": 9,
                "co_change_count": 2.15,
                "structural": "unexplained",
            }
        ]
    )
    conn.execute(
        "INSERT INTO git_metadata VALUES (?, ?, ?, ?, ?, 0)",
        ("packages/proof/service.py", 12, 300, 120, partners),
    )
    conn.commit()
    conn.close()

    health_path = tmp_path / "repowise-health.json"
    health_path.write_text(
        json.dumps(
            {
                "kpis": {"production_average": 6.1, "hotspot_health": 3.4},
                "metrics": [
                    {
                        "file_path": "packages/proof/service.py",
                        "score": 3.2,
                        "nloc": 900,
                        "max_ccn": 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    jscpd_path = tmp_path / "jscpd-report.json"
    jscpd_path.write_text(
        json.dumps(
            {
                "statistics": {
                    "total": {
                        "sources": 10,
                        "lines": 1000,
                        "clones": 1,
                        "duplicatedLines": 18,
                        "percentage": 1.8,
                        "percentageTokens": 1.5,
                    }
                },
                "duplicates": [
                    {
                        "format": "python",
                        "lines": 18,
                        "firstFile": {"name": "packages/proof/service.py", "start": 1, "end": 18},
                        "secondFile": {"name": "packages/web/server.py", "start": 5, "end": 22},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = digest.build_digest_payload(
        jscpd_path=jscpd_path,
        repowise_health_path=health_path,
        repowise_db_path=db_path,
        root=tmp_path,
    )
    md = digest.render_digest_markdown(payload)

    assert "## Top-10 Hotspots" in md
    assert "`packages/proof/service.py`" in md
    assert "## Top Change-Coupled File Pairs" in md
    assert "`packages/web/server.py`" in md
    assert "1.80%" in md


@pytest.fixture
def namespace_root(tmp_path: Path) -> Path:
    for relative in (
        "packages/core/src/moex_spread_lab/core",
        "packages/data/src/moex_spread_lab/data",
        "packages/accounting/src/moex_spread_lab/accounting",
        "packages/strategies/src/moex_spread_lab/strategies",
        "packages/proof/src/moex_spread_lab/proof",
        "packages/web/src/moex_spread_lab/web",
        "src/moex_spread_lab",
    ):
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


def test_typecheck_view_refuses_to_delete_unmanaged_directory(namespace_root: Path) -> None:
    generated = namespace_root / ".typecheck_typings"
    generated.mkdir()
    marker = generated / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(typecheck_workspace.ViewError, match="unmarked"):
        typecheck_workspace.build_view(namespace_root)
    assert marker.read_text(encoding="utf-8") == "user data"


def test_typecheck_view_reports_namespace_collisions(namespace_root: Path) -> None:
    (namespace_root / "packages/data/src/moex_spread_lab/core").mkdir()
    with pytest.raises(typecheck_workspace.ViewError, match="duplicate namespace"):
        typecheck_workspace.build_view(namespace_root)
