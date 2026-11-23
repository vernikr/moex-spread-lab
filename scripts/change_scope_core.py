#!/usr/bin/env python3
"""Core of the S2 change-scope router (docs/development/agent-performance-2/plan.md).

Pure decision logic shared by ``scripts/change_scope.py`` (CLI) and the local
launcher ``scripts/verify.py``. Rules, evaluated per changed path; any unmatched
path resolves to ``full``:

1. ``docs/**`` and root-level ``*.md`` are docs (Markdown inside ``packages/**``
   is not docs and takes rule 2's non-``.py`` branch).
2. ``packages/<p>/{src,tests}/**``: a ``.py`` file selects package ``p`` plus its
   reverse dependents; any other file (or a new/deleted file) selects the same
   legs with testmon deselection disabled.
3. Any other path inside a known package selects its reverse-dependency closure
   with testmon disabled.
4. Everything else resolves to ``full``.

The router is advisory only; the full gate and weekly full CI are the backstop.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PACKAGES = ("core", "data", "accounting", "strategies", "proof", "web")

# Reverse-dependency table (plan S2 rule 2): which legs run when a package changes.
# Pinned to the architecture gate's layer DAG by packages/core/tests/test_change_scope.py.
REVERSE_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "core": ("core", "data", "accounting", "strategies", "proof", "web"),
    "data": ("data", "strategies", "proof", "web"),
    "accounting": ("accounting", "strategies", "proof", "web"),
    "strategies": ("strategies", "proof", "web"),
    "proof": ("proof", "web"),
    "web": ("web",),
}

TIER2_PATTERNS = (
    ".clang-tidy",
    "packages/core/src/moex_spread_lab/core/_fastcore.c",
    "packages/core/src/moex_spread_lab/core/_fastcore_loader.py",
    "packages/core/tests/test_fastcore_sanitizers.py",
    "packages/proof/tests/test_fastcore_parity.py",
    "packages/proof/tests/test_perf_equivalence_pair.py",
    "packages/proof/tests/test_perf_equivalence_pool.py",
    "packages/proof/tests/test_perf_equivalence_stream.py",
    "packages/proof/tests/test_perf_equivalence_full.py",
    "scripts/gen_compile_commands.py",
    "scripts/run_sanitized_pytest.py",
    "scripts/should_run_c_tier2.py",
    ".github/workflows/ci.yml",
)

NULL_SHA = "0" * 40
_FULL_EVENTS = frozenset({"schedule", "workflow_dispatch"})
_PKG_RE = re.compile(r"packages/([^/]+)/")
_MAX_REASON_PATHS = 5


def norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./").lstrip("/")


def matches_c_tier2(paths: list[str]) -> bool:
    """Return True if any changed path requires C99 Tier 2 checks."""
    return any(
        fnmatch.fnmatch(norm_path(p), pat) for p in paths if norm_path(p) for pat in TIER2_PATTERNS
    )


def git_diff_names(base: str, head: str, root: Path):
    res = subprocess.run(
        ["git", "diff", "--name-only", "-z", f"{base}..{head}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        return None
    return sorted({line for line in res.stdout.split("\0") if line.strip()})


def git_diff_new_or_deleted(base: str, head: str, root: Path) -> list[str]:
    res = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ADR", "-z", f"{base}..{head}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        return []  # unknown; caller still routes by name list
    return sorted({line for line in res.stdout.split("\0") if line.strip()})


def conservative_ci_reason(event_name: str, base_sha: str) -> str | None:
    if event_name in _FULL_EVENTS:
        return f"event={event_name} resolves to full unconditionally"
    if not base_sha or base_sha == NULL_SHA:
        return "missing base SHA; resolving to full conservatively"
    return None


def decide_tier2(
    *,
    event_name: str,
    base_sha: str = "",
    head_sha: str = "HEAD",
    root: Path | None = None,
) -> tuple[bool, str]:
    """Return ``(should_run, reason)`` for the C99 Tier 2 checks."""
    if event_name in _FULL_EVENTS:
        return True, f"event={event_name} runs full Tier 2 unconditionally"
    if not base_sha or base_sha == NULL_SHA:
        return True, "missing base SHA; running Tier 2 conservatively"
    changed = git_diff_names(base_sha, head_sha, root or Path(__file__).resolve().parents[1])
    if changed is None:
        return True, f"git diff {base_sha}..{head_sha} unavailable; running Tier 2 conservatively"
    if matches_c_tier2(changed):
        return True, "C99 / sanitizer / CI paths changed"
    return False, f"no C99 Tier 2 paths in {len(changed)} changed file(s)"


@dataclass(frozen=True)
class ScopeDecision:
    """Result of routing a change set; see module docstring for the rules."""

    profile: str  # "docs" | "packages" | "full"
    packages: tuple[str, ...]
    docs_only: bool
    testmon: bool
    check_js_modules: bool
    run_tier2: bool
    perf_equivalence: bool
    reasons: tuple[str, ...]
    skipped: tuple[str, ...] = field(default=())

    def legs_label(self) -> str:
        return ",".join(self.packages) if self.packages else "none"

    def to_outputs(self) -> dict[str, str]:
        return {
            "docs_only": "true" if self.docs_only else "false",
            "packages": json.dumps(list(self.packages)),
            "has_packages": "true" if self.packages else "false",
            "run_tier2": "true" if self.run_tier2 else "false",
            "perf_equivalence": "true" if self.perf_equivalence else "false",
            "check_js_modules": "true" if self.check_js_modules else "false",
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            **self.to_outputs(),
            "packages_list": list(self.packages),
            "testmon": self.testmon,
            "reasons": list(self.reasons),
            "skipped": list(self.skipped),
        }


def is_docs_path(path: str) -> bool:
    norm = norm_path(path)
    return norm.startswith("docs/") or ("/" not in norm and norm.lower().endswith(".md"))


def package_of(path: str) -> str | None:
    match = _PKG_RE.match(norm_path(path))
    return match.group(1) if match and match.group(1) in PACKAGES else None


def touched_packages(paths: list[str]) -> list[str]:
    return sorted(
        {pkg for path in paths if (pkg := package_of(path)) is not None}, key=PACKAGES.index
    )


def _limited(paths: list[str]) -> str:
    shown = paths[:_MAX_REASON_PATHS]
    suffix = f" (+{len(paths) - len(shown)} more)" if len(paths) > len(shown) else ""
    return ", ".join(shown) + suffix


def _summary(docs, package_py, package_other) -> list[str]:
    reasons: list[str] = []
    if docs:
        reasons.append(f"docs-only path(s): {_limited(docs)}")
    for pkg in sorted(package_py, key=PACKAGES.index):
        reasons.append(f"package {pkg} .py: {_limited(package_py[pkg])}")
    for pkg in sorted(package_other, key=PACKAGES.index):
        reasons.append(f"package {pkg} non-.py/new/deleted: {_limited(package_other[pkg])}")
    return reasons


def _has_static_js(normed: list[str]) -> bool:
    return any(path.startswith("packages/") and "/static/" in f"/{path}" for path in normed)


def _classify(normed: list[str], nod: set[str]):
    docs: list[str] = []
    package_py: dict[str, list[str]] = {}
    package_other: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for path in normed:
        if is_docs_path(path):
            docs.append(path)
            continue
        pkg = package_of(path)
        if pkg is None:
            unmatched.append(path)
            continue
        in_code = path.startswith((f"packages/{pkg}/src/", f"packages/{pkg}/tests/"))
        if in_code and path.endswith(".py") and path not in nod:
            package_py.setdefault(pkg, []).append(path)
        else:
            package_other.setdefault(pkg, []).append(path)
    return docs, package_py, package_other, unmatched


def _full(reasons, *, run_tier2: bool, check_js_modules: bool = False) -> ScopeDecision:
    return ScopeDecision(
        profile="full",
        packages=PACKAGES,
        docs_only=False,
        testmon=False,
        check_js_modules=check_js_modules,
        run_tier2=run_tier2,
        perf_equivalence=True,
        reasons=tuple(reasons),
        skipped=(),
    )


def decide_paths(paths, *, new_or_deleted=()) -> ScopeDecision:
    """Route an explicit change set. Pure; does no I/O or git access."""
    normed = sorted({norm_path(p) for p in paths if norm_path(p)})
    nod = {norm_path(p) for p in new_or_deleted if norm_path(p)}
    if not normed:
        return _full(["empty change set; resolving to full conservatively"], run_tier2=False)
    docs, package_py, package_other, unmatched = _classify(normed, nod)
    run_tier2 = matches_c_tier2(normed)
    check_js = _has_static_js(normed)

    if unmatched:
        return _full(
            [
                f"unmatched path(s): {_limited(unmatched)} -> full",
                *_summary(docs, package_py, package_other),
            ],
            run_tier2=run_tier2,
            check_js_modules=check_js,
        )
    if run_tier2:
        return _full(
            [
                "C99/sanitizer/CI tier2 path(s) changed -> full",
                *_summary(docs, package_py, package_other),
            ],
            run_tier2=True,
            check_js_modules=check_js,
        )
    touched = sorted(set(package_py) | set(package_other), key=PACKAGES.index)
    if not touched:
        skipped = (
            "package test legs (docs-only)",
            "perf-equivalence (docs-only)",
            "ruff/basedpyright/pytest (docs-only)",
        )
        return ScopeDecision(
            profile="docs",
            packages=(),
            docs_only=True,
            testmon=False,
            check_js_modules=check_js,
            run_tier2=run_tier2,
            perf_equivalence=False,
            reasons=(f"docs-only change: {_limited(docs)}",),
            skipped=skipped,
        )
    legs = sorted({leg for pkg in touched for leg in REVERSE_DEPENDENTS[pkg]}, key=PACKAGES.index)
    testmon = not package_other
    reasons = [
        f"package change \u2192 legs {','.join(legs)}",
        *_summary(docs, package_py, package_other),
    ]
    if not testmon:
        reasons.append("non-.py or new/deleted package file(s): testmon deselection disabled")
    skipped = () if testmon else ("testmon deselection (non-.py or new/deleted input)",)
    return ScopeDecision(
        profile="packages",
        packages=tuple(legs),
        docs_only=False,
        testmon=testmon,
        check_js_modules=check_js,
        run_tier2=run_tier2,
        perf_equivalence=tuple(legs) != ("web",),
        reasons=tuple(reasons),
        skipped=skipped,
    )
