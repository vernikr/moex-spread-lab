#!/usr/bin/env python3
"""Synthetic change scenarios for shadow routing verification."""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Scenario:
    name: str
    expected_profile: str
    expected_packages: tuple[str, ...]
    expected_testmon: bool
    description: str
    apply: Callable[[Path], object]


def _touch(p: Path, text: str = "# shadow\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _app(p: Path, text: str) -> None:
    existing = p.read_text(encoding="utf-8") if p.is_file() else ""
    if text.startswith("\n") and existing.endswith("\n"):
        text = text[1:]
    p.write_text(existing + text, encoding="utf-8")


def _docs(root: Path, rel: str, *, new: bool = False) -> None:
    p = root / rel
    if new:
        _touch(p, "<!-- shadow -->\n")
    else:
        _app(p, "\n<!-- shadow -->\n")


def _py(root: Path, rel: str) -> None:
    """Append a shadow comment to a .py file with ruff-format-safe spacing."""
    p = root / rel
    existing = p.read_text(encoding="utf-8") if p.is_file() else ""
    if not existing:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# shadow marker\n", encoding="utf-8")
        return
    last_nonblank = next((ln for ln in reversed(existing.split("\n")) if ln.strip() != ""), "")
    indented = last_nonblank.startswith((" ", "\t"))
    prefix = "\n\n" if indented else ""
    if not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + prefix + "# shadow marker\n", encoding="utf-8")


def _asset(root: Path, rel: str, suffix: str = "\n/* shadow */\n") -> None:
    _app(root / rel, suffix)


def _new(root: Path, rel: str) -> None:
    _touch(root / rel, "# shadow new file\n")


def _unmatched(root: Path, rel: str) -> None:
    """Break an unmatched-path file so verification fails."""
    if rel.endswith(".py"):
        _py(root, rel)
        return
    if rel.endswith((".json", ".toml", ".yml", ".yaml")):
        suffix = "\nSHADOW_BREAK\n"
    elif rel.endswith((".c", ".h", ".js", ".css")):
        suffix = "\n/* shadow break */\n"
    else:
        suffix = "\n# shadow\n"
    _app(root / rel, suffix)


ALL = ("core", "data", "accounting", "strategies", "proof", "web")
W = ("web",)
P_W = ("proof", "web")
A_P_W = ("accounting", "strategies", "proof", "web")
D_P_W = ("data", "strategies", "proof", "web")
S_P_W = ("strategies", "proof", "web")
S_D_P_W = D_P_W
WEB_SRV = "packages/web/src/moex_spread_lab/web/server.py"
WEB_TST = "packages/web/tests/test_screener_app.py"
PROOF_CM = "packages/proof/src/moex_spread_lab/proof_runtime/cohort_models.py"
STRAT_ENG = "packages/strategies/src/moex_spread_lab/strategies/funding_carry/engine.py"
ACCT_LED = "packages/accounting/src/moex_spread_lab/accounting/ledger.py"
DATA_CLI = "packages/data/src/moex_spread_lab/data/cli.py"
CORE_CON = "packages/core/src/moex_spread_lab/core/contracts.py"
WEB_JS = "packages/web/src/moex_spread_lab/web/static/js/app.js"
WEB_CSS = "packages/web/src/moex_spread_lab/web/static/css/layout.css"
WEB_HTML = "packages/web/src/moex_spread_lab/web/static/index.html"
PROOF_MF = "packages/proof/manifest.json"


def _S(name, prof, pkgs, tm, desc, fn):
    """Shorthand Scenario constructor to keep line count under limits."""
    return Scenario(name, prof, pkgs, tm, desc, fn)


SCENARIOS: tuple[Scenario, ...] = (
    _S(
        "docs-development-md",
        "docs",
        (),
        False,
        "edit docs/development/*.md",
        lambda r: _docs(r, "docs/development/shadow-test.md", new=True),
    ),
    _S(
        "docs-guide-new",
        "docs",
        (),
        False,
        "new file in docs/guides/",
        lambda r: _touch(r / "docs/guides/shadow.rst", "shadow\n"),
    ),
    _S(
        "root-readme-md",
        "docs",
        (),
        False,
        "append to README.md",
        lambda r: _app(r / "README.md", "\n<!-- shadow -->\n"),
    ),
    _S(
        "root-agents-md",
        "docs",
        (),
        False,
        "append to AGENTS.md",
        lambda r: _app(r / "AGENTS.md", "\n<!-- shadow -->\n"),
    ),
    _S(
        "docs-subdir-new-md",
        "docs",
        (),
        False,
        "new markdown in docs/",
        lambda r: _touch(r / "docs/shadow.md", "# shadow\n"),
    ),
    _S("web-server-py", "packages", W, True, "edit web/server.py", lambda r: _py(r, WEB_SRV)),
    _S("web-test-py", "packages", W, True, "edit a web test", lambda r: _py(r, WEB_TST)),
    _S("proof-py", "packages", P_W, True, "edit proof runtime", lambda r: _py(r, PROOF_CM)),
    _S(
        "strategies-py",
        "packages",
        S_P_W,
        True,
        "edit funding_carry engine",
        lambda r: _py(r, STRAT_ENG),
    ),
    _S(
        "accounting-py",
        "packages",
        A_P_W,
        True,
        "edit accounting/ledger.py",
        lambda r: _py(r, ACCT_LED),
    ),
    _S("data-py", "packages", D_P_W, True, "edit data/cli.py", lambda r: _py(r, DATA_CLI)),
    _S(
        "core-contracts-py",
        "packages",
        ALL,
        True,
        "edit core/contracts.py (all six legs)",
        lambda r: _py(r, CORE_CON),
    ),
    _S(
        "web-static-js",
        "packages",
        W,
        False,
        "edit static JS (no testmon, check_js_modules)",
        lambda r: _asset(r, WEB_JS),
    ),
    _S("web-static-css", "packages", W, False, "edit static CSS", lambda r: _asset(r, WEB_CSS)),
    _S(
        "web-static-html",
        "packages",
        W,
        False,
        "edit static HTML",
        lambda r: _asset(r, WEB_HTML, "\n<!-- shadow -->\n"),
    ),
    _S(
        "proof-manifest-json",
        "packages",
        P_W,
        False,
        "edit proof/manifest.json (non-.py)",
        lambda r: _asset(r, PROOF_MF, "\n// shadow\n"),
    ),
    _S(
        "pkg-readme-md",
        "packages",
        W,
        False,
        "Markdown inside packages/ is NOT docs",
        lambda r: _touch(r / "packages/web/README.md", "# web\n"),
    ),
    _S(
        "new-py-in-web",
        "packages",
        W,
        False,
        "new .py in web",
        lambda r: _new(r, "packages/web/src/moex_spread_lab/web/shadow_new.py"),
    ),
    _S(
        "new-py-in-core",
        "packages",
        ALL,
        False,
        "new .py in core (all six legs)",
        lambda r: _new(r, "packages/core/src/moex_spread_lab/core/shadow_new.py"),
    ),
    _S(
        "web+accounting",
        "packages",
        A_P_W,
        True,
        "web + accounting .py (union closure)",
        lambda r: (_py(r, WEB_SRV), _py(r, ACCT_LED)),
    ),
    _S(
        "proof+web",
        "packages",
        P_W,
        True,
        "proof + web .py",
        lambda r: (_py(r, PROOF_CM), _py(r, WEB_SRV)),
    ),
    _S(
        "core+data",
        "packages",
        ALL,
        True,
        "core + data .py (all six)",
        lambda r: (_py(r, CORE_CON), _py(r, DATA_CLI)),
    ),
    _S(
        "strategies+data",
        "packages",
        D_P_W,
        True,
        "strategies + data .py (union)",
        lambda r: (_py(r, STRAT_ENG), _py(r, DATA_CLI)),
    ),
    _S(
        "pyproject-toml",
        "full",
        ALL,
        False,
        "pyproject.toml -> full",
        lambda r: _unmatched(r, "pyproject.toml"),
    ),
    _S(
        "ci-workflow",
        "full",
        ALL,
        False,
        ".github/workflows/ci.yml -> full",
        lambda r: _unmatched(r, ".github/workflows/ci.yml"),
    ),
    _S(
        "scripts-change",
        "full",
        ALL,
        False,
        "scripts/ -> full",
        lambda r: _unmatched(r, "scripts/changed_files.py"),
    ),
    _S(
        "fixtures-json",
        "full",
        ALL,
        False,
        "root fixtures/ -> full",
        lambda r: _unmatched(r, "fixtures/research/offline.json"),
    ),
    _S(
        "root-tests",
        "full",
        ALL,
        False,
        "root tests/ -> full",
        lambda r: _py(r, "tests/proof_fixtures.py"),
    ),
    _S(
        "config-baseline",
        "full",
        ALL,
        False,
        "config/code-health-baseline.json -> full",
        lambda r: _unmatched(r, "config/code-health-baseline.json"),
    ),
    _S(
        "basedpyright-baseline",
        "full",
        ALL,
        False,
        ".basedpyright/baseline.json -> full",
        lambda r: _unmatched(r, ".basedpyright/baseline.json"),
    ),
    _S(
        "src-cli-py",
        "full",
        ALL,
        False,
        "src/ (root CLI) -> full, run_tier2",
        lambda r: _unmatched(r, "src/moex_spread_lab/cli.py"),
    ),
    _S(
        "clang-tidy",
        "full",
        ALL,
        False,
        ".clang-tidy -> full, run_tier2",
        lambda r: _unmatched(r, ".clang-tidy"),
    ),
    _S(
        "fastcore-c",
        "full",
        ALL,
        False,
        "_fastcore.c -> full, run_tier2",
        lambda r: _app(r / "packages/core/src/moex_spread_lab/core/_fastcore.c", "\n// shadow\n"),
    ),
    _S(
        "docs+web-py",
        "packages",
        W,
        True,
        "docs + web.py -> packages (docs does not override)",
        lambda r: (_docs(r, "docs/shadow.md", new=True), _py(r, WEB_SRV)),
    ),
    _S(
        "docs+unmatched",
        "full",
        ALL,
        False,
        "docs + unmatched -> full",
        lambda r: (_docs(r, "docs/shadow.md", new=True), _unmatched(r, "pyproject.toml")),
    ),
)


def apply_scenario(root: Path, name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            s.apply(root)
            return s
    avail = ", ".join(s.name for s in SCENARIOS)
    raise KeyError(f"unknown scenario '{name}'; available: {avail}")


def get_scenario(name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            return s
    avail = ", ".join(s.name for s in SCENARIOS)
    raise KeyError(f"unknown scenario '{name}'; available: {avail}")


def reset_worktree(root: Path) -> None:
    """Reset a worktree; refuses to reset the main repo root (safety)."""
    if root.resolve() == ROOT.resolve():
        raise RuntimeError("refusing to reset_worktree on the main repo root")
    subprocess.run(
        ["git", "reset", "--hard", "-q", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "clean", "-fd", "-q"], cwd=root, check=False, capture_output=True, timeout=120
    )


def list_scenarios() -> list[tuple[str, str, str]]:
    return [(s.name, s.expected_profile, s.description) for s in SCENARIOS]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("name", nargs="?")
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()
    if args.list:
        for n, prof, d in list_scenarios():
            print(f"  {n:30s} [{prof:8s}] {d}")
    elif args.reset:
        reset_worktree(Path(".").resolve())
        print("reset")
    elif args.name:
        s = apply_scenario(Path(".").resolve(), args.name)
        print(f"applied: {s.name} [{s.expected_profile}]")
    else:
        p.print_help()
