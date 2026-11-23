"""Unit tests for Step 3 CI Tier 2 path filtering, weekly digest rendering, and workflow wiring."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import render_code_health_digest as digest
from scripts import should_run_c_tier2 as tier2

ROOT = Path(__file__).resolve().parents[3]


def test_matches_c_tier2_filters_c_and_python_paths() -> None:
    assert tier2.matches_c_tier2(["packages/core/src/moex_spread_lab/core/_fastcore.c"])
    assert tier2.matches_c_tier2(["packages/core/src/moex_spread_lab/core/_fastcore_loader.py"])
    assert tier2.matches_c_tier2([".clang-tidy"])
    assert tier2.matches_c_tier2(["scripts/run_sanitized_pytest.py"])
    assert not tier2.matches_c_tier2(
        [
            "packages/web/src/moex_spread_lab/web/server.py",
            "docs/development/ci.md",
        ]
    )


def test_decide_tier2_handles_schedule_and_missing_base(tmp_path: Path) -> None:
    run_sched, reason_sched = tier2.decide_tier2(event_name="schedule", root=tmp_path)
    assert run_sched is True
    assert "schedule" in reason_sched

    run_null, _ = tier2.decide_tier2(
        event_name="push", base_sha="0" * 40, head_sha="HEAD", root=tmp_path
    )
    assert run_null is True


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


def test_ci_workflows_and_jscpd_config_stay_aligned() -> None:
    ci_yml = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    weekly_yml = (ROOT / ".github/workflows/code-health-weekly.yml").read_text(encoding="utf-8")
    macos_yml = (ROOT / ".github/workflows/ci-macos.yml").read_text(encoding="utf-8")
    jscpd_cfg = json.loads((ROOT / ".jscpd.json").read_text(encoding="utf-8"))

    assert "code-health:" in ci_yml
    assert "./scripts/check_health.sh" in ci_yml
    assert "scripts/should_run_c_tier2.py" in ci_yml
    assert "scripts/run_sanitized_pytest.py" in ci_yml
    # S2 scoped CI: path router gates the package matrix and perf-equivalence;
    # ci-ok gives one stable required-check name (plan agent-performance-2/S2).
    assert "scripts/change_scope.py" in ci_yml
    assert "scope:" in ci_yml
    assert "has_packages" in ci_yml
    assert "ci-ok" in ci_yml

    assert 'cron: "30 4 * * 1"' in weekly_yml
    assert "jscpd" in weekly_yml
    assert "repowise" in weekly_yml
    assert "GITHUB_STEP_SUMMARY" in weekly_yml

    assert "check_health.sh" not in macos_yml
    assert jscpd_cfg["threshold"] <= 3.0
    assert "c" in jscpd_cfg["format"]
