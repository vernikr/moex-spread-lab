"""Unit tests for the change-scope router (scripts/change_scope.py) and its docs-scan leg."""

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


def test_docs_only_profile_states_skipped_legs_in_outputs_and_report() -> None:
    decision = _decide(["docs/development/ci.md", "README.md", "AGENTS.md", "docs/guides/x.rst"])
    assert (decision.profile, decision.docs_only, decision.packages) == ("docs", True, ())
    assert not (decision.testmon or decision.perf_equivalence or decision.run_tier2)
    assert decision.skipped  # skipped checks are stated, never silent
    values = decision.to_outputs()
    assert (values["docs_only"], values["has_packages"]) == ("true", "false")
    assert values["perf_equivalence"] == "false" and json.loads(values["packages"]) == []
    report = change_scope.render_report(decision)
    assert "scope: docs" in report and "skipped:" in report


def test_github_output_writer_and_empty_change_set(tmp_path: Path) -> None:
    assert (_decide([]).profile, _decide([]).packages) == ("full", ALL_SIX)
    out = tmp_path / "output.txt"
    change_scope.write_github_output(
        out, _decide(["packages/web/src/moex_spread_lab/web/server.py"])
    )
    values = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert (values["docs_only"], values["has_packages"]) == ("false", "true")
    assert json.loads(values["packages"]) == ["web"]
    assert (values["run_tier2"], values["perf_equivalence"]) == ("false", "false")


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


def test_decide_ci_resolves_degenerate_inputs_to_full(tmp_path: Path) -> None:
    for event, base in (("schedule", "abc123"), ("workflow_dispatch", ""), ("push", "")):
        decision = change_scope.decide_ci(event, base_sha=base, head_sha="HEAD", root=ROOT)
        assert (decision.profile, decision.run_tier2) == ("full", True), (event, base)
        assert decision.reasons[0].startswith(("event=", "missing base SHA")), decision.reasons
    # the null SHA, an unavailable diff (no repository) and an empty HEAD..HEAD diff
    for base, root in (("0" * 40, ROOT), ("deadbeef" * 5, tmp_path), ("HEAD", ROOT)):
        decision = change_scope.decide_ci("push", base_sha=base, head_sha="HEAD", root=root)
        assert decision.profile == "full", base
        assert decision.run_tier2 is (base != "HEAD"), base  # an empty diff needs no Tier 2


def test_decide_ci_against_real_repo_commits(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    def commit(message: str) -> None:
        git("add", "-A")
        git("commit", "-q", "-m", message)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    target = tmp_path / "packages/web/src/moex_spread_lab/web/server.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    commit("base")
    base_sha = git("rev-parse", "HEAD").strip()

    def decide() -> change_scope.ScopeDecision:
        return change_scope.decide_ci("push", base_sha=base_sha, head_sha="HEAD", root=tmp_path)

    target.write_text("x = 2\n", encoding="utf-8")
    commit("edit")
    edited = decide()
    assert (edited.profile, edited.packages, edited.testmon) == ("packages", ("web",), True)
    assert edited.perf_equivalence is False
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("hello\n", encoding="utf-8")
    commit("docs")
    # docs paths never widen a package change into a docs-only or full scope
    with_docs = decide()
    assert (with_docs.profile, with_docs.packages) == ("packages", ("web",))
    target.unlink()
    commit("delete")
    deleted = decide()
    assert (deleted.packages, deleted.testmon) == (("web",), False)  # deleted file: no deselection


def test_scan_docs_files_flags_problems_but_not_sk_prose(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("task-to-verified-main is a workflow metric\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text(
        "trailing space   \n<<<<<<< HEAD\nsecret: ghp_" + "a" * 36 + "\n", encoding="utf-8"
    )

    findings, _ = docs_scan.scan_docs_files([str(clean), str(dirty)])
    text = "\n".join(findings)
    assert "trailing whitespace" in text and "conflict marker" in text
    assert "credential-shaped" in text
    assert docs_scan.scan_docs_files([str(clean)]) == ([], [])
    assert verify.sanitize("task-to-verified-main") == "task-to-verified-main"
    assert verify.sanitize("sk-1234567890abcdef") == "sk-<redacted>"


def test_scan_links_reports_broken_targets_and_skips_preserved_evidence(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Setup notes\n\n## Second part\n", encoding="utf-8")
    page = tmp_path / "docs/page.md"
    page.parent.mkdir()
    page.write_text(
        "[ok](../guide.md#second-part)\n"
        "[missing file](../absent.md)\n"
        "[missing anchor](../guide.md#absent)\n"
        "[external](https://example.com/x.md)\n",
        encoding="utf-8",
    )

    findings, notes = docs_scan.scan_links([str(page), "docs/inputs/server-spec.redacted.md"])

    text = "\n".join(findings)
    assert "unresolved link target ../absent.md" in text
    assert "unresolved link anchor ../guide.md#absent" in text
    assert "guide.md#second-part" not in text and "example.com" not in text
    assert len(notes) == 1 and "preserved as received" in notes[0]


def test_repo_docs_surface_is_authored_only_and_link_clean() -> None:
    paths = docs_scan.repo_doc_paths()

    assert {
        "README.md",
        "AGENTS.md",
        "docs/development/github-project.md",
        "packages/proof/docs/execution-proof.md",
        "packages/core/docs/README.md",
        "packages/web/docs/README.md",
    } <= set(paths)
    assert not [path for path in paths if path.startswith(docs_scan._EXCLUDED_PREFIXES)]
    findings, _ = docs_scan.scan_links(paths)
    assert findings == []


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
