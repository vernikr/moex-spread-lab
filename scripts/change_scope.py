#!/usr/bin/env python3
"""Change-scope router and touched-file helpers (stdlib only; used by ci.yml, verify, gates).

Per changed path; any unmatched path resolves to ``full``:

1. ``docs/**`` and root-level ``*.md`` are docs (Markdown inside ``packages/**`` is not).
2. ``packages/<p>/{src,tests}/**/*.py`` selects ``p`` plus its reverse dependents; any
   other, new or deleted package file selects the same legs with testmon disabled.
3. Everything else is ``full``; C99/sanitizer/CI paths also set ``run_tier2``.

Without ``--event`` it routes the working tree (diff vs ``--base`` plus untracked
files); with ``--event`` it routes ``git diff base..head``, and any error,
``schedule``/``workflow_dispatch`` or a missing base resolves to full (CI contract).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("core", "data", "accounting", "strategies", "proof", "web")
# Which legs run when a package changes; pinned to the architecture DAG by
# packages/core/tests/test_change_scope.py.
REVERSE_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "core": PACKAGES,
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
    ".github/workflows/ci.yml",
)
NULL_SHA = "0" * 40
_FULL_EVENTS = frozenset({"schedule", "workflow_dispatch"})
_PKG_RE = re.compile(r"packages/([^/]+)/")
_IMPORT_RE = re.compile(r"^[+-]\s*(?:import\s|from\s+\S+\s+import\b)", re.MULTILINE)


def norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./").lstrip("/")


def matches_c_tier2(paths: list[str]) -> bool:
    """Return True if any changed path requires the C99 Tier 2 checks."""
    return any(fnmatch.fnmatch(norm_path(p), pat) for p in paths for pat in TIER2_PATTERNS)


def _git(args: list[str], root: Path = ROOT) -> str:
    try:
        res = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
    return res.stdout


def _names(output: str) -> list[str]:
    return sorted({name for name in output.split("\0") if name.strip()})


def git_diff_names(base: str, head: str, root: Path, *extra: str) -> list[str] | None:
    try:
        return _names(_git(["diff", "--name-only", *extra, "-z", f"{base}..{head}"], root))
    except RuntimeError:
        return None


def local_change_set(base: str = "HEAD", root: Path = ROOT) -> tuple[list[str], set[str]]:
    """Return ``(paths, new_or_deleted)`` for the working-tree change set vs ``base``."""
    try:
        tracked = _names(_git(["diff", "--name-only", "-z", base, "--"], root))
        untracked = _names(_git(["ls-files", "--others", "--exclude-standard", "-z"], root))
    except RuntimeError as exc:
        raise RuntimeError(f"cannot compute local change set (base {base}): {exc}") from exc
    nod = {path for path in tracked if not (root / path).is_file()} | set(untracked)
    return sorted({*tracked, *untracked}), nod


def changed_paths(
    base: str = "HEAD", existing_only: bool = True, suffixes: tuple[str, ...] | None = None
) -> list[str]:
    """Repo-relative touched paths (tracked diff vs ``base`` plus untracked), sorted."""
    paths, _ = local_change_set(_git(["rev-parse", "--verify", f"{base}^{{commit}}"]).strip())
    if suffixes:
        paths = [path for path in paths if path.lower().endswith(tuple(map(str.lower, suffixes)))]
    return [path for path in paths if (ROOT / path).is_file()] if existing_only else paths


def imports_changed(base: str = "HEAD") -> bool:
    """True when the change adds/removes/moves imports or Python modules."""
    base = _git(["rev-parse", "--verify", f"{base}^{{commit}}"]).strip()
    for line in _git(["diff", "--name-status", "-M", base, "--"]).splitlines():
        code, *paths = line.split("\t")
        if code[0] in "ADR" and any(path.endswith(".py") for path in paths):
            return True
    if _git(["ls-files", "--others", "--exclude-standard", "--", "*.py"]).strip():
        return True
    return bool(_IMPORT_RE.search(_git(["diff", "-M", base, "--", "*.py"])))


@dataclass(frozen=True)
class ScopeDecision:
    """Result of routing a change set; see the module docstring for the rules."""

    profile: str  # "docs" | "packages" | "full"
    packages: tuple[str, ...]
    docs_only: bool
    testmon: bool
    check_js_modules: bool
    run_tier2: bool
    perf_equivalence: bool
    reasons: tuple[str, ...]
    skipped: tuple[str, ...] = ()

    def to_outputs(self) -> dict[str, str]:
        flags = ("docs_only", "run_tier2", "perf_equivalence", "check_js_modules")
        return {
            "packages": json.dumps(list(self.packages)),
            "has_packages": str(bool(self.packages)).lower(),
            **{name: str(getattr(self, name)).lower() for name in flags},
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
    return sorted({pkg for path in paths if (pkg := package_of(path))}, key=PACKAGES.index)


def _limited(paths: list[str]) -> str:
    more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
    return ", ".join(paths[:5]) + more


def _summary(docs: list[str], groups: dict[str, dict[str, list[str]]]) -> list[str]:
    reasons = [f"docs-only path(s): {_limited(docs)}"] if docs else []
    for label, group in groups.items():
        reasons += [f"package {pkg} {label}: {_limited(group[pkg])}" for pkg in group]
    return reasons


def _classify(normed: list[str], nod: set[str]):
    docs, unmatched = [], []
    package_py: dict[str, list[str]] = {}
    package_other: dict[str, list[str]] = {}
    for path in normed:
        pkg = package_of(path)
        if is_docs_path(path) or pkg is None:
            (docs if is_docs_path(path) else unmatched).append(path)
            continue
        in_code = path.startswith((f"packages/{pkg}/src/", f"packages/{pkg}/tests/"))
        group = (
            package_py if in_code and path.endswith(".py") and path not in nod else package_other
        )
        group.setdefault(pkg, []).append(path)
    ordered = {
        ".py": dict(sorted(package_py.items(), key=lambda kv: PACKAGES.index(kv[0]))),
        "non-.py/new/deleted": dict(
            sorted(package_other.items(), key=lambda kv: PACKAGES.index(kv[0]))
        ),
    }
    return docs, ordered, unmatched


def _full(reasons, *, run_tier2: bool, check_js_modules: bool = False) -> ScopeDecision:
    return ScopeDecision(
        "full", PACKAGES, False, False, check_js_modules, run_tier2, True, tuple(reasons)
    )


def decide_paths(paths, *, new_or_deleted=()) -> ScopeDecision:
    """Route an explicit change set. Pure; does no I/O or git access."""
    normed = sorted({norm_path(p) for p in paths if norm_path(p)})
    nod = {norm_path(p) for p in new_or_deleted if norm_path(p)}
    if not normed:
        return _full(["empty change set; resolving to full conservatively"], run_tier2=False)
    docs, groups, unmatched = _classify(normed, nod)
    run_tier2 = matches_c_tier2(normed)
    check_js = any(p.startswith("packages/") and "/static/" in f"/{p}" for p in normed)
    summary = _summary(docs, groups)
    if unmatched or run_tier2:
        head = f"unmatched path(s): {_limited(unmatched)} -> full" if unmatched else ""
        head = head or "C99/sanitizer/CI tier2 path(s) changed -> full"
        return _full([head, *summary], run_tier2=run_tier2, check_js_modules=check_js)
    py, other = groups[".py"], groups["non-.py/new/deleted"]
    touched = sorted({*py, *other}, key=PACKAGES.index)
    if not touched:
        skipped = (
            "package test legs (docs-only)",
            "perf-equivalence (docs-only)",
            "ruff/basedpyright/pytest (docs-only)",
        )
        reasons = (f"docs-only change: {_limited(docs)}",)
        return ScopeDecision("docs", (), True, False, check_js, False, False, reasons, skipped)
    legs = tuple(
        sorted({leg for pkg in touched for leg in REVERSE_DEPENDENTS[pkg]}, key=PACKAGES.index)
    )
    testmon = not other
    reasons = [f"package change \u2192 legs {','.join(legs)}", *summary]
    skipped: tuple[str, ...] = ()
    if not testmon:
        reasons.append("non-.py or new/deleted package file(s): testmon deselection disabled")
        skipped = ("testmon deselection (non-.py or new/deleted input)",)
    perf = legs != ("web",)
    return ScopeDecision(
        "packages", legs, False, testmon, check_js, False, perf, tuple(reasons), skipped
    )


def decide_local(base: str = "HEAD", root: Path = ROOT) -> ScopeDecision:
    """Route the working-tree change set (tracked diff vs ``base`` + untracked files)."""
    try:
        paths, nod = local_change_set(base, root)
    except RuntimeError as exc:
        return _full([f"{exc}; resolving to full"], run_tier2=True)
    return decide_paths(paths, new_or_deleted=nod)


def decide_ci(event_name: str, base_sha: str = "", head_sha: str = "HEAD", root: Path = ROOT):
    """Route a CI change set. Any error resolves to full (with Tier 2)."""
    if event_name in _FULL_EVENTS:
        return _full([f"event={event_name} resolves to full unconditionally"], run_tier2=True)
    if not base_sha or base_sha == NULL_SHA:
        return _full(["missing base SHA; resolving to full conservatively"], run_tier2=True)
    changed = git_diff_names(base_sha, head_sha, root)
    if changed is None:
        reason = f"git diff {base_sha}..{head_sha} unavailable; resolving to full"
        return _full([reason], run_tier2=True)
    nod = git_diff_names(base_sha, head_sha, root, "--diff-filter=ADR") or []
    return decide_paths(changed, new_or_deleted=nod)


def render_report(decision: ScopeDecision) -> str:
    out = decision.to_outputs()
    lines = [
        f"scope: {decision.profile}",
        f"packages: {','.join(decision.packages) or 'none'}",
        f"docs_only: {out['docs_only']}",
        f"testmon: {'admissible (advisory)' if decision.testmon else 'disabled'}",
        f"run_tier2: {out['run_tier2']}",
        f"perf_equivalence: {'true' if decision.perf_equivalence else 'false (skipped)'}",
        f"check_js_modules: {out['check_js_modules']}",
        *(f"reason: {reason}" for reason in decision.reasons),
    ]
    if decision.skipped:
        lines.append(f"skipped: {'; '.join(decision.skipped)}")
    return "\n".join(lines)


def write_github_output(path: Path, decision: ScopeDecision) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in decision.to_outputs().items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD", help="base revision (default: HEAD)")
    parser.add_argument("--event", default=None, help="CI event name (enables CI mode)")
    parser.add_argument("--head", default="HEAD", help="CI head revision (default: HEAD)")
    parser.add_argument("--github-output", type=Path, default=None, help="$GITHUB_OUTPUT file")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        if args.event is None:
            decision = decide_local(args.base)
        else:
            decision = decide_ci(args.event, args.base, args.head)
    except Exception as exc:  # noqa: BLE001 — CI contract: any router error resolves to full
        decision = _full([f"router error: {exc}; resolving to full"], run_tier2=True)
    print(
        json.dumps(decision.to_dict(), sort_keys=True)
        if args.format == "json"
        else render_report(decision)
    )
    if args.github_output is not None:
        try:
            write_github_output(args.github_output, decision)
        except OSError as exc:
            print(f"change_scope: cannot write --github-output: {exc}", file=sys.stderr)
            return 64
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
