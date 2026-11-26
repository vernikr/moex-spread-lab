"""Filesystem-derived layer classification for the architecture DAG tests.

A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
or from the explicit lists in this module — splitting a module never requires editing
test_architecture_dag.py (plan S1.4). `cli` surfaces that HEAD classified as "cli"
keep it; root-level compatibility shims stay explicit.
"""

from __future__ import annotations

import ast
from pathlib import Path

# A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
# or from the explicit lists below — splitting a module never requires editing
# test_architecture_dag.py. `cli` surfaces whose HEAD classification was "cli" keep it;
# root-level compatibility shims stay explicit; everything else under a package tree
# takes the package's layer.
_CLI_LAYER_KEYS = frozenset({"cli", "core.cli", "data.cli", "accounting.cli"})
# Root application subpackages (src/moex_spread_lab/<name>/) sit in the cli layer.
_CLI_PACKAGES = frozenset({"remote"})
_PACKAGE_HEAD_LAYERS = {
    "core": "core",
    "proof": "proof",
    "data": "data",
    "accounting": "accounting",
    "strategies": "strategies",
    "web": "web",
}
_ROOT_SHIM_LAYERS = {
    "_fastcore_loader": "core",
    "w0": "core",
    "timeframes": "core",
    "evaluation_periods": "core",
    "data_lake": "data",
    "h1_migration": "data",
    "h1_seal": "data",
    "h1_archive": "data",
    "iss": "data",
    "benchmark": "data",
    "data_inventory": "data",
    "data_retention": "data",
    "data_archive_candidates": "data",
    "data_refresh": "data",
    "storage_policy": "data",
    "costs": "accounting",
    "portfolio": "accounting",
    "account_risk": "accounting",
    "position_sizing": "accounting",
    "ledger": "accounting",
    "run_artifacts": "accounting",
    "run_artifact_import": "accounting",
    "validation": "accounting",
    "research": "accounting",
    "analytics": "strategies",
    "carry": "strategies",
    "funding_screen": "strategies",
    "funding_recheck": "strategies",
    "proof_funding": "strategies",
    "proof_signals": "strategies",
    "calendar_spreads": "strategies",
    "s3_reference": "strategies",
    "screener": "strategies",
    "audit": "proof",
    "proof": "proof",
    "proof_pool": "proof",
    "proof_windows": "proof",
    "proof_batch": "proof",
    "proof_strategy_search": "proof",
    "proof_search": "proof",
    "screener_app": "web",
}


def _optional_compat_aliases(repo_root: Path | None = None) -> dict[str, str]:
    """Return optional compatibility root aliases mapped to canonical package heads."""
    if repo_root is None:
        return {"proof_runtime": "proof"}
    proof_runtime_dir = (
        repo_root / "packages" / "proof" / "src" / "moex_spread_lab" / "proof_runtime"
    )
    return {"proof_runtime": "proof"} if proof_runtime_dir.exists() else {}


def expand_compat_candidates(candidates: list[str], aliases: dict[str, str]) -> list[str]:
    """Append canonical module targets for compatibility-alias roots."""
    expanded: list[str] = []
    for cand in candidates:
        expanded.append(cand)
        head, sep, rest = cand.partition(".")
        if head in aliases:
            target = aliases[head]
            expanded.append(f"{target}.{rest}" if sep else target)
    return expanded


def package_head_layers(repo_root: Path | None = None) -> dict[str, str]:
    """Return canonical package heads plus optional compatibility heads present on disk."""
    layers = dict(_PACKAGE_HEAD_LAYERS)
    for alias, target in _optional_compat_aliases(repo_root).items():
        layers[alias] = _PACKAGE_HEAD_LAYERS[target]
    return layers


def derive_layer(key: str, *, repo_root: Path | None = None) -> str | None:
    if key in _CLI_LAYER_KEYS or key.split(".", 1)[0] in _CLI_PACKAGES:
        return "cli"
    head = key.split(".", 1)[0]
    layer = package_head_layers(repo_root).get(head)
    if layer is not None:
        return layer
    return _ROOT_SHIM_LAYERS.get(key)


# Package test suites map to their owning package's layer.
PACKAGE_TEST_LAYERS: dict[str, str] = {
    "core": "core",
    "data": "data",
    "accounting": "accounting",
    "strategies": "strategies",
    "proof": "proof",
    "web": "web",
}

# Frozen upper-bound allowlist of remaining cross-layer package test imports.
# Pre-W2 (#230) eliminated all upward test imports in data, owned accounting files,
# and proof->web. The two remaining accounting entries belong to unowned legacy W4/W5/W6
# analytical test suites scheduled for retirement in Wave 1 of the accounting reduction
# epic (#190, #191) and final allowlist cleanup in #168 (#197).
# Existing files on disk MUST exercise every allowlisted entry listed for them; stale
# entries on existing files fail the gate immediately. Entries whose test file was
# deleted by a package-scoped PR remain tolerated until #168 cleans deleted-file entries.
LEGACY_UPWARD_TEST_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "packages/accounting/tests/test_run_artifacts.py",
            "strategies.pair_mean_reversion.analytics",
        ),
        (
            "packages/accounting/tests/test_validation.py",
            "strategies.pair_mean_reversion.analytics",
        ),
    }
)


def extract_msl_import_targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "moex_spread_lab":
            return [alias.name for alias in node.names]
        if mod.startswith("moex_spread_lab."):
            return [mod.removeprefix("moex_spread_lab.")]
    elif isinstance(node, ast.Import):
        return [
            alias.name.removeprefix("moex_spread_lab.")
            for alias in node.names
            if alias.name.startswith("moex_spread_lab.")
        ]
    return []


def resolve_target_layer(target: str) -> str | None:
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        if (layer := derive_layer(".".join(parts[:i]))) is not None:
            return layer
    return None


def matched_legacy_test_import(rel: str, target: str) -> tuple[str, str] | None:
    parts = target.split(".")
    for i in range(1, len(parts) + 1):
        entry = (rel, ".".join(parts[:i]))
        if entry in LEGACY_UPWARD_TEST_IMPORTS:
            return entry
    return None


def is_legacy_test_import_exception(rel: str, target: str) -> bool:
    return matched_legacy_test_import(rel, target) is not None


def check_test_file_layers(
    path: Path,
    repo_root: Path,
    pkg_layer: str,
    allowed: frozenset[str],
    *,
    allowlist: frozenset[tuple[str, str]] = LEGACY_UPWARD_TEST_IMPORTS,
) -> list[str]:
    rel = path.relative_to(repo_root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    used_allowlist: set[tuple[str, str]] = set()
    parts_allowlist = {entry for entry in allowlist if entry[0] == rel}
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 1)
        for target in extract_msl_import_targets(node):
            layer = resolve_target_layer(target)
            if layer is None or layer in allowed:
                continue
            matched = next(
                (
                    (rel, ".".join(target.split(".")[:i]))
                    for i in range(1, len(target.split(".")) + 1)
                    if (rel, ".".join(target.split(".")[:i])) in parts_allowlist
                ),
                None,
            )
            if matched is not None:
                used_allowlist.add(matched)
            else:
                violations.append(
                    f"{rel}:{lineno} ({pkg_layer}) -> moex_spread_lab.{target} ({layer})"
                )
    for stale_rel, stale_target in sorted(parts_allowlist - used_allowlist):
        violations.append(
            f"{stale_rel} -> moex_spread_lab.{stale_target} "
            "(stale LEGACY_UPWARD_TEST_IMPORTS entry: file no longer imports target)"
        )
    return violations


def check_package_test_layers(
    pkg_dir: Path,
    repo_root: Path,
    allowed_targets: dict[str, frozenset[str]],
    *,
    allowlist: frozenset[tuple[str, str]] = LEGACY_UPWARD_TEST_IMPORTS,
) -> list[str]:
    tests_dir = pkg_dir / "tests"
    pkg_layer = PACKAGE_TEST_LAYERS.get(pkg_dir.name)
    if not tests_dir.is_dir() or not pkg_layer:
        return []
    allowed = allowed_targets.get(pkg_layer, frozenset()) | {"cli"}
    return [
        v
        for path in sorted(tests_dir.rglob("*.py"))
        for v in check_test_file_layers(path, repo_root, pkg_layer, allowed, allowlist=allowlist)
    ]
