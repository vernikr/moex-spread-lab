"""Unit tests for the S2 change-scope router (scripts/change_scope.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts import change_scope, docs_scan, verify_record

from test_architecture_dag import ALLOWED_LAYER_TARGETS

PACKAGES = change_scope.PACKAGES
ROOT = Path(__file__).resolve().parents[3]

ALL_SIX = ("core", "data", "accounting", "strategies", "proof", "web")


def _decide(
    paths: list[str], new_or_deleted: list[str] | None = None
) -> change_scope.ScopeDecision:
    return change_scope.decide_paths(paths, new_or_deleted=new_or_deleted or ())


def test_docs_only_change_selects_docs_profile() -> None:
    decision = _decide(["docs/development/ci.md", "README.md", "AGENTS.md", "docs/guides/x.rst"])
    assert decision.profile == "docs"
    assert decision.docs_only is True
    assert decision.packages == ()
    assert decision.testmon is False
    assert decision.perf_equivalence is False
    assert decision.run_tier2 is False
    assert decision.skipped  # skipped checks are stated, never silent


def test_markdown_inside_packages_is_not_docs() -> None:
    decision = _decide(["packages/web/README.md"])
    assert decision.profile == "packages"
    assert decision.packages == ("web",)
    assert decision.docs_only is False
    assert decision.testmon is False  # non-.py rule: no testmon deselection


def test_core_py_change_runs_all_six_legs_with_testmon() -> None:
    decision = _decide(["packages/core/src/moex_spread_lab/core/contracts.py"])
    assert decision.profile == "packages"
    assert decision.packages == ALL_SIX
    assert decision.testmon is True
    assert decision.perf_equivalence is True


def test_reverse_dependency_closures() -> None:
    cases = {
        "packages/data/src/moex_spread_lab/data/lake.py": ("data", "strategies", "proof", "web"),
        "packages/accounting/src/moex_spread_lab/accounting/ledger.py": (
            "accounting",
            "strategies",
            "proof",
            "web",
        ),
        "packages/strategies/src/moex_spread_lab/strategies/carry.py": (
            "strategies",
            "proof",
            "web",
        ),
        "packages/proof/src/moex_spread_lab/proof_runtime/cohort_models.py": ("proof", "web"),
        "packages/web/src/moex_spread_lab/web/server.py": ("web",),
    }
    for path, legs in cases.items():
        decision = _decide([path])
        assert decision.packages == tuple(legs), path
        assert decision.testmon is True, path


def test_package_py_change_touching_multiple_packages_unions_legs() -> None:
    decision = _decide(
        [
            "packages/accounting/src/moex_spread_lab/accounting/ledger.py",
            "packages/web/src/moex_spread_lab/web/server.py",
        ]
    )
    assert decision.packages == ("accounting", "strategies", "proof", "web")


def test_web_only_push_skips_perf_equivalence() -> None:
    decision = _decide(["packages/web/src/moex_spread_lab/web/server.py"])
    assert decision.perf_equivalence is False


def test_non_py_package_file_runs_full_legs_without_testmon(tmp_path: Path) -> None:
    decision = _decide(["packages/web/src/moex_spread_lab/web/static/js/app.js"])
    assert decision.profile == "packages"
    assert decision.packages == ("web",)
    assert decision.testmon is False
    assert decision.check_js_modules is True


def test_non_py_snapshot_inside_package_selects_package_closure() -> None:
    decision = _decide(["packages/proof/tests/golden_output.json"])
    assert decision.packages == ("proof", "web")
    assert decision.testmon is False


def test_non_py_file_outside_static_does_not_trigger_js_modules() -> None:
    decision = _decide(["packages/proof/tests/golden_output.json"])
    assert decision.check_js_modules is False


def test_new_py_file_disables_testmon_but_keeps_package_scope() -> None:
    path = "packages/core/src/moex_spread_lab/core/brand_new_module.py"
    decision = _decide([path], new_or_deleted=[path])
    assert decision.packages == ALL_SIX
    assert decision.testmon is False


def test_deleted_py_file_disables_testmon() -> None:
    path = "packages/web/src/moex_spread_lab/web/gone.py"
    decision = _decide([path], new_or_deleted=[path])
    assert decision.packages == ("web",)
    assert decision.testmon is False


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
        "config/code-health-baseline.json",
        "fixtures/research/offline.json",
        "scripts/check_health.sh",
        "scripts/change_scope.py",
        ".basedpyright/baseline.json",
        ".jscpd.json",
        "src/moex_spread_lab/cli.py",
        "tests/proof_fixtures.py",
        "packages/unknown_package/src/moex_spread_lab/x.py",
        ".clang-tidy",
    ],
)
def test_unknown_path_resolves_to_full(path: str) -> None:
    decision = _decide([path])
    assert decision.profile == "full"
    assert decision.packages == ALL_SIX
    assert decision.testmon is False
    assert decision.perf_equivalence is True


def test_empty_change_set_resolves_to_full() -> None:
    decision = _decide([])
    assert decision.profile == "full"
    assert decision.packages == ALL_SIX


def test_mixed_docs_and_package_change_selects_package_legs() -> None:
    decision = _decide(["docs/development/ci.md", "packages/web/src/moex_spread_lab/web/server.py"])
    assert decision.profile == "packages"
    assert decision.packages == ("web",)


def test_docs_mixed_with_unmatched_resolves_to_full() -> None:
    decision = _decide(["docs/development/ci.md", "scripts/verify.py"])
    assert decision.profile == "full"


def test_tier2_trigger_paths() -> None:
    assert _decide(["packages/core/src/moex_spread_lab/core/_fastcore.c"]).run_tier2 is True
    assert _decide(["packages/proof/tests/test_fastcore_parity.py"]).run_tier2 is True
    assert _decide([".clang-tidy"]).run_tier2 is True
    assert _decide(["packages/web/src/moex_spread_lab/web/server.py"]).run_tier2 is False


def test_reverse_dependency_table_matches_architecture_dag() -> None:
    """The six-entry table must not drift from the architecture gate's layer DAG."""
    targets = {layer: ALLOWED_LAYER_TARGETS[layer] for layer in PACKAGES}

    def transitive_reach(layer: str) -> set[str]:
        seen = {layer}
        stack = [layer]
        while stack:
            current = stack.pop()
            for nxt in targets[current]:
                if nxt in targets and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    reach = {layer: transitive_reach(layer) for layer in PACKAGES}
    derived = {layer: tuple(pkg for pkg in PACKAGES if layer in reach[pkg]) for layer in PACKAGES}
    assert derived == change_scope.REVERSE_DEPENDENTS


def test_decide_ci_resolves_schedule_and_missing_base_to_full() -> None:
    for event, base in (("schedule", "abc123"), ("workflow_dispatch", ""), ("push", "")):
        decision = change_scope.decide_ci(event, base_sha=base, head_sha="HEAD", root=ROOT)
        assert decision.profile == "full", (event, base)
        assert decision.run_tier2 is True
    null_decision = change_scope.decide_ci("push", base_sha="0" * 40, head_sha="HEAD", root=ROOT)
    assert null_decision.profile == "full"


def test_decide_ci_resolves_unavailable_diff_to_full(tmp_path: Path) -> None:
    decision = change_scope.decide_ci(
        "push", base_sha="deadbeef" * 5, head_sha="HEAD", root=tmp_path
    )
    assert decision.profile == "full"
    assert decision.run_tier2 is True


def test_decide_ci_head_vs_head_empty_diff_is_full() -> None:
    decision = change_scope.decide_ci("push", base_sha="HEAD", head_sha="HEAD", root=ROOT)
    assert decision.profile == "full"


def test_decide_ci_against_real_repo_commits(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    src = tmp_path / "packages/web/src/moex_spread_lab/web"
    src.mkdir(parents=True)
    target = src / "server.py"
    target.write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD")
    target.write_text("x = 2\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "edit")

    decision = change_scope.decide_ci("push", base_sha=base_sha, head_sha="HEAD", root=tmp_path)
    assert decision.profile == "packages"
    assert decision.packages == ("web",)
    assert decision.perf_equivalence is False
    # Writing to docs only is docs-only.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "docs")
    docs_decision = change_scope.decide_ci(
        "push", base_sha=base_sha, head_sha="HEAD", root=tmp_path
    )
    # docs paths never widen a package change into a docs-only or full scope
    assert docs_decision.profile == "packages"
    assert docs_decision.packages == ("web",)


def test_decide_ci_detects_deleted_files(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    src = tmp_path / "packages/web/src/moex_spread_lab/web"
    src.mkdir(parents=True)
    target = src / "server.py"
    target.write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD")
    target.unlink()
    git("add", "-A")
    git("commit", "-q", "-m", "delete")

    decision = change_scope.decide_ci("push", base_sha=base_sha, head_sha="HEAD", root=tmp_path)
    assert decision.packages == ("web",)
    assert decision.testmon is False  # deleted file: no deselection


def test_github_output_writer(tmp_path: Path) -> None:
    decision = _decide(["packages/web/src/moex_spread_lab/web/server.py"])
    out = tmp_path / "output.txt"
    change_scope.write_github_output(out, decision)
    values = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert values["docs_only"] == "false"
    assert json.loads(values["packages"]) == ["web"]
    assert values["has_packages"] == "true"
    assert values["run_tier2"] == "false"
    assert values["perf_equivalence"] == "false"


def test_outputs_for_docs_only(tmp_path: Path) -> None:
    decision = _decide(["docs/development/ci.md"])
    values = decision.to_outputs()
    assert values["docs_only"] == "true"
    assert json.loads(values["packages"]) == []
    assert values["has_packages"] == "false"
    assert values["perf_equivalence"] == "false"


def test_render_report_states_skipped(tmp_path: Path) -> None:
    report = change_scope.render_report(_decide(["docs/development/ci.md"]))
    assert "scope: docs" in report
    assert "skipped:" in report


def test_scan_docs_files_flags_problems(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("hello world\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text(
        "trailing space   \n<<<<<<< HEAD\nsecret: ghp_" + "a" * 36 + "\n", encoding="utf-8"
    )

    findings, _ = docs_scan.scan_docs_files([str(clean), str(dirty)])
    text = "\n".join(findings)
    assert "trailing whitespace" in text
    assert "conflict marker" in text
    assert "credential-shaped" in text

    findings_clean, _ = docs_scan.scan_docs_files([str(clean)])
    assert findings_clean == []


def test_credential_scanner_does_not_match_sk_substrings_in_normal_prose(tmp_path: Path) -> None:
    prose = tmp_path / "prose.md"
    prose.write_text("task-to-verified-main is a workflow metric\n", encoding="utf-8")

    findings, _ = docs_scan.scan_docs_files([str(prose)])

    assert findings == []
    assert verify_record.sanitize("task-to-verified-main") == "task-to-verified-main"
    assert verify_record.sanitize("sk-1234567890abcdef") == "sk-<redacted>"
