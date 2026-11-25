"""Unit tests for the S2 change-scope router (scripts/change_scope.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts import change_scope, docs_scan, verify

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


def test_empty_change_set_resolves_to_full() -> None:
    decision = _decide([])
    assert (decision.profile, decision.packages) == ("full", ALL_SIX)


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


def _repo_with_web_server(tmp_path: Path):
    def git(*args: str) -> str:
        cmd = ["git", *args]
        return subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    target = tmp_path / "packages/web/src/moex_spread_lab/web/server.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    return git, git("rev-parse", "HEAD").strip(), target


def _commit(git, message: str) -> None:
    git("add", "-A")
    git("commit", "-q", "-m", message)


def test_decide_ci_against_real_repo_commits(tmp_path: Path) -> None:
    git, base_sha, target = _repo_with_web_server(tmp_path)
    target.write_text("x = 2\n", encoding="utf-8")
    _commit(git, "edit")
    decision = change_scope.decide_ci("push", base_sha=base_sha, head_sha="HEAD", root=tmp_path)
    assert (decision.profile, decision.packages, decision.perf_equivalence) == (
        "packages",
        ("web",),
        False,
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("hello\n", encoding="utf-8")
    _commit(git, "docs")
    # docs paths never widen a package change into a docs-only or full scope
    docs_decision = change_scope.decide_ci(
        "push", base_sha=base_sha, head_sha="HEAD", root=tmp_path
    )
    assert (docs_decision.profile, docs_decision.packages) == ("packages", ("web",))


def test_decide_ci_detects_deleted_files(tmp_path: Path) -> None:
    git, base_sha, target = _repo_with_web_server(tmp_path)
    target.unlink()
    _commit(git, "delete")
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
    assert verify.sanitize("task-to-verified-main") == "task-to-verified-main"
    assert verify.sanitize("sk-1234567890abcdef") == "sk-<redacted>"


# Router regression table (formerly the shadow scenario catalogue): each row is a
# change set -> (profile, legs, testmon). "+path" marks a new file.
_WEB, _PROOF = "packages/web/src/moex_spread_lab/web", "packages/proof/src/moex_spread_lab/proof"
_ACCT = "packages/accounting/src/moex_spread_lab/accounting/ledger.py"
_CORE = "packages/core/src/moex_spread_lab/core"
_DATA = "packages/data/src/moex_spread_lab/data/cli.py"
_STRAT = "packages/strategies/src/moex_spread_lab/strategies/funding_carry/engine.py"
_W, _PW, _SPW = ("web",), ("proof", "web"), ("strategies", "proof", "web")
_APW, _DPW = ("accounting", "strategies", "proof", "web"), ("data", "strategies", "proof", "web")
ROUTING_TABLE = [
    (["+docs/development/shadow-test.md"], "docs", (), False),
    (["+docs/guides/shadow.rst", "README.md", "AGENTS.md", "+docs/shadow.md"], "docs", (), False),
    ([f"{_WEB}/server.py"], "packages", _W, True),
    (["packages/web/tests/test_screener_app.py"], "packages", _W, True),
    ([f"{_PROOF}/cohort_models.py"], "packages", _PW, True),
    ([_STRAT], "packages", _SPW, True),
    ([_ACCT], "packages", _APW, True),
    ([_DATA], "packages", _DPW, True),
    ([f"{_CORE}/contracts.py"], "packages", ALL_SIX, True),
    ([f"{_WEB}/static/js/app.js"], "packages", _W, False),
    ([f"{_WEB}/static/css/layout.css"], "packages", _W, False),
    ([f"{_WEB}/static/index.html"], "packages", _W, False),
    (["packages/proof/manifest.json"], "packages", _PW, False),
    (["+packages/web/README.md"], "packages", _W, False),
    ([f"+{_WEB}/shadow_new.py"], "packages", _W, False),
    ([f"+{_CORE}/shadow_new.py"], "packages", ALL_SIX, False),
    ([f"{_WEB}/server.py", _ACCT], "packages", _APW, True),
    ([f"{_PROOF}/cohort_models.py", f"{_WEB}/server.py"], "packages", _PW, True),
    ([f"{_CORE}/contracts.py", _DATA], "packages", ALL_SIX, True),
    ([_STRAT, _DATA], "packages", _DPW, True),
    (["+docs/shadow.md", f"{_WEB}/server.py"], "packages", _W, True),
    (["packages/proof/tests/golden_output.json"], "packages", _PW, False),
    ([f"+{_WEB}/gone.py"], "packages", _W, False),
    (["packages/data/src/moex_spread_lab/data/lake.py"], "packages", _DPW, True),
    *(
        ([path], "full", ALL_SIX, False)
        for path in (
            "pyproject.toml",
            ".github/workflows/ci.yml",
            "scripts/change_scope.py",
            "scripts/check_health.sh",
            "uv.lock",
            ".jscpd.json",
            "packages/unknown_package/src/moex_spread_lab/x.py",
            "packages/proof/tests/test_fastcore_parity.py",
            "fixtures/research/offline.json",
            "tests/proof_fixtures.py",
            "config/code-health-baseline.json",
            ".basedpyright/baseline.json",
            "src/moex_spread_lab/cli.py",
            ".clang-tidy",
            f"{_CORE}/_fastcore.c",
        )
    ),
    (["+docs/shadow.md", "pyproject.toml"], "full", ALL_SIX, False),
    (["docs/development/ci.md", "scripts/verify.py"], "full", ALL_SIX, False),
]
_TIER2 = (".clang-tidy", "_fastcore.c", "test_fastcore_parity.py", "workflows/ci.yml")


@pytest.mark.parametrize(("changes", "profile", "legs", "testmon"), ROUTING_TABLE)
def test_routing_regression_table(
    changes: list[str], profile: str, legs: tuple, testmon: bool
) -> None:
    paths = [change.removeprefix("+") for change in changes]
    decision = _decide(paths, [change[1:] for change in changes if change.startswith("+")])
    assert (decision.profile, decision.packages, decision.testmon) == (profile, legs, testmon)
    assert decision.docs_only is (profile == "docs")
    assert decision.perf_equivalence is (legs not in ((), _W))
    assert decision.check_js_modules is any("/static/" in path for path in paths)
    assert decision.run_tier2 is any(path.endswith(_TIER2) for path in paths)
