"""Tests for S3 shadow verification (shadow_runner.py, shadow_regressions.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts import shadow_regressions, shadow_runner  # noqa: E402

# ---------- Output parsing ----------


def _verify_passed():
    return (
        "verify (affected): PASSED in 5.2s\n"
        "  passed       health-changed    1.1s\n"
        "  passed       architecture      0.8s\n"
        "  passed       pytest-affected   3.0s\n"
    )


def _verify_failed():
    return "verify (full): FAILED in 63.0s\n  passed       health-full       11.4s\n  failed       suite-full        51.2s\n"


def test_parse_passed() -> None:
    outcome, lbl, per_step = shadow_runner._parse_verify_output(_verify_passed(), 0)
    assert outcome == "PASSED" and lbl == "affected"
    assert per_step["health-changed"] == "passed" and per_step["pytest-affected"] == "passed"


def test_parse_failed() -> None:
    outcome, lbl, per_step = shadow_runner._parse_verify_output(_verify_failed(), 1)
    assert outcome == "FAILED" and lbl == "full"
    assert per_step["health-full"] == "passed" and per_step["suite-full"] == "failed"


def test_parse_timeout() -> None:
    outcome, _, _ = shadow_runner._parse_verify_output("", 124)
    assert outcome == "TIMEOUT"


def test_parse_unavailable() -> None:
    outcome, _, _ = shadow_runner._parse_verify_output("", 127)
    assert outcome == "UNAVAILABLE"


# ---------- Missed-failure comparison ----------


def _r(outcome, steps):
    return shadow_runner.ProfileResult(outcome, 0.0, "test", steps)


def test_both_pass_no_miss():
    assert (
        shadow_runner.compare_results(_r("PASSED", {"h": "passed"}), _r("PASSED", {"h": "passed"}))
        == []
    )


def test_both_fail_no_miss():
    assert (
        shadow_runner.compare_results(_r("FAILED", {"h": "failed"}), _r("FAILED", {"h": "failed"}))
        == []
    )


def test_overall_miss_detected():
    missed = shadow_runner.compare_results(
        _r("PASSED", {"h": "passed"}), _r("FAILED", {"h": "failed"})
    )
    assert any("overall" in m for m in missed)


def test_per_step_miss_detected():
    missed = shadow_runner.compare_results(
        _r("PASSED", {"h": "passed", "s": "passed"}), _r("FAILED", {"h": "passed", "s": "failed"})
    )
    assert any("s" in m for m in missed)


def test_affected_fails_full_passes_safe_direction():
    """Affected catching extra failures is not a router miss."""
    assert (
        shadow_runner.compare_results(
            _r("FAILED", {"h": "passed", "s": "failed"}),
            _r("PASSED", {"h": "passed", "s": "passed"}),
        )
        == []
    )


def test_not_run_is_not_miss():
    assert (
        shadow_runner.compare_results(
            _r("PASSED", {"h": "passed", "s": "not run"}),
            _r("PASSED", {"h": "passed", "s": "passed"}),
        )
        == []
    )


# ---------- Regression catalog ----------


def test_catalog_has_four_categories():
    regs = shadow_regressions.list_injections()
    assert len(regs) == 4
    assert {c for _, c, _ in regs} == {"registry", "parity", "fixture", "js"}
    assert {s for s, _, _ in regs} == {
        "registry-discovery",
        "proof-parity",
        "fixture-change",
        "js-module",
    }


def test_get_injection_known():
    assert shadow_regressions.get_injection("registry-discovery").category == "registry"


def test_get_injection_unknown():
    with pytest.raises(KeyError, match="unknown regression"):
        shadow_regressions.get_injection("nope")


# ---------- Injection application in a temp repo ----------


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    for pkg in ("core", "data", "accounting", "strategies", "proof", "web"):
        d = repo / f"packages/{pkg}/src/moex_spread_lab/{pkg}"
        d.mkdir(parents=True)
        (d / "__init__.py").write_text(f"# {pkg}\n", encoding="utf-8")
    (repo / "packages/core/src/moex_spread_lab/core/contracts.py").write_text(
        'STRATEGY_DISCOVERY_ATTR = "engine"\n', encoding="utf-8"
    )
    acct = repo / "packages/accounting/src/moex_spread_lab/accounting"
    (acct / "ledger.py").write_text("def _round(x):\n    return round(x, 2)\n", encoding="utf-8")
    fix = repo / "fixtures/research"
    fix.mkdir(parents=True)
    (fix / "offline.json").write_text(json.dumps({"risk_free_rate": 0.05}) + "\n", encoding="utf-8")
    js = repo / "packages/web/src/moex_spread_lab/web/static/js"
    js.mkdir(parents=True)
    (js / "app.js").write_text("const APP = {};\n", encoding="utf-8")
    sd = repo / "packages/strategies/src/moex_spread_lab/strategies/carry"
    sd.mkdir(parents=True)
    (sd / "__init__.py").write_text("from .engine import run\n", encoding="utf-8")
    (sd / "engine.py").write_text("def run(): pass\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts/__init__.py").write_text("\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


def _is_dirty(repo):
    return bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
    )


@pytest.mark.parametrize(
    "slug", ["registry-discovery", "proof-parity", "fixture-change", "js-module"]
)
def test_injection_dirties_then_resets(tmp_path, slug):
    repo = _make_repo(tmp_path)
    assert shadow_regressions.apply_injection(repo, slug)
    assert _is_dirty(repo)
    shadow_regressions.reset_worktree(repo)
    assert not _is_dirty(repo)


def test_fixture_injection_mutates_value(tmp_path):
    repo = _make_repo(tmp_path)
    orig = json.loads((repo / "fixtures/research/offline.json").read_text())
    shadow_regressions.apply_injection(repo, "fixture-change")
    new = json.loads((repo / "fixtures/research/offline.json").read_text())
    assert new != orig
    shadow_regressions.reset_worktree(repo)


def test_js_injection_adds_break(tmp_path):
    repo = _make_repo(tmp_path)
    shadow_regressions.apply_injection(repo, "js-module")
    js = (repo / "packages/web/src/moex_spread_lab/web/static/js/app.js").read_text()
    assert "SHADOW_BREAK" in js
    shadow_regressions.reset_worktree(repo)


# ---------- Shadow log schema ----------


def test_append_log_writes_valid_json(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(shadow_runner, "SHADOW_LOG", log)
    aff = _r("PASSED", {"h": "passed"})
    full = _r("PASSED", {"h": "passed"})
    r = shadow_runner.ShadowResult(
        "abc",
        "commit",
        {"profile": "packages"},
        aff,
        full,
        [],
        ["n"],
        "2026-01-01T00:00:00+00:00",
        1.0,
    )
    shadow_runner.append_log(r)
    rec = json.loads(log.read_text().strip())
    assert rec["schema"] == "msl.shadow-record/1"
    assert rec["missed_failures"] == []


def test_summary_prints_miss(capsys):
    aff = _r("PASSED", {"h": "passed"})
    full = _r("FAILED", {"h": "failed"})
    shadow_runner.print_summary(
        [
            shadow_runner.ShadowResult(
                "s1", "commit", {"profile": "docs"}, aff, full, ["h: miss"], [], "", 0.0
            )
        ]
    )
    assert "MISSED FAILURES: 1/1" in capsys.readouterr().out
