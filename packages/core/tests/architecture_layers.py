"""Filesystem-derived layer classification for the architecture DAG tests.

A module's layer follows from its package directory
(packages/<pkg>/src/moex_spread_lab/<top>/...); root compatibility shims and
`cli` surfaces stay explicit below, so splitting a module never requires
editing test_architecture_dag.py (plan S1.4).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_CLI_LAYER_KEYS = frozenset({"cli", "core.cli", "data.cli", "accounting.cli"})
# Root application subpackages (src/moex_spread_lab/<name>/) sit in the cli layer.
_CLI_PACKAGES = frozenset({"remote"})
_PACKAGE_HEADS = frozenset({"core", "data", "accounting", "strategies", "proof", "web"})
_PACKAGE_HEAD_LAYERS = {head: head for head in _PACKAGE_HEADS}
# Root compatibility shims grouped by the layer of their canonical target.
_SHIM_GROUPS = {
    "core": "_fastcore_loader w0 timeframes evaluation_periods",
    "data": (
        "data_lake h1_migration h1_seal h1_archive iss benchmark data_inventory "
        "data_retention data_archive_candidates data_refresh storage_policy"
    ),
    "accounting": (
        "costs portfolio account_risk position_sizing ledger run_artifacts "
        "run_artifact_import validation research"
    ),
    "strategies": (
        "analytics carry funding_screen funding_recheck proof_funding proof_signals "
        "calendar_spreads s3_reference screener"
    ),
    "proof": "audit proof proof_pool proof_windows proof_batch proof_strategy_search proof_search",
    "web": "screener_app",
}
_ROOT_SHIM_LAYERS = {name: layer for layer, names in _SHIM_GROUPS.items() for name in names.split()}


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
    if head in _PACKAGE_HEAD_LAYERS:
        return _PACKAGE_HEAD_LAYERS[head]
    layer = package_head_layers(repo_root).get(head)
    if layer is not None:
        return layer
    return _ROOT_SHIM_LAYERS.get(key)


# Package test suites map to their owning package's layer.
PACKAGE_TEST_LAYERS: dict[str, str] = dict(_PACKAGE_HEAD_LAYERS)

# Frozen upper-bound allowlist of remaining cross-layer package test imports
# (pre-W2 #230 eliminated the rest; the two entries belong to legacy accounting
# suites retiring under #190/#191, final cleanup in #168). Existing files must
# exercise every entry listed for them — stale entries fail the gate; entries
# for deleted files are tolerated until #168.
LEGACY_UPWARD_TEST_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    (f"packages/accounting/tests/{name}.py", "strategies.pair_mean_reversion.analytics")
    for name in ("test_run_artifacts", "test_validation")
)


def iter_import_nodes(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield import nodes without walking expression subtrees (imports are statements)."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case))
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


def matched_legacy_test_import(
    rel: str,
    target: str,
    *,
    allowlist: frozenset[tuple[str, str]] = LEGACY_UPWARD_TEST_IMPORTS,
) -> tuple[str, str] | None:
    parts = target.split(".")
    for i in range(1, len(parts) + 1):
        entry = (rel, ".".join(parts[:i]))
        if entry in allowlist:
            return entry
    return None


def check_test_file_layers(
    path: Path,
    repo_root: Path,
    pkg_layer: str,
    allowed: frozenset[str],
    *,
    allowlist: frozenset[tuple[str, str]] = LEGACY_UPWARD_TEST_IMPORTS,
    parsed: tuple[str, ast.Module] | None = None,
) -> list[str]:
    rel = path.relative_to(repo_root).as_posix()
    tree = (
        parsed[1]
        if parsed is not None
        else ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    )
    violations: list[str] = []
    used_allowlist: set[tuple[str, str]] = set()
    parts_allowlist = frozenset(entry for entry in allowlist if entry[0] == rel)
    for node in iter_import_nodes(tree):
        for target in extract_msl_import_targets(node):
            layer = resolve_target_layer(target)
            if layer is None or layer in allowed:
                continue
            matched = matched_legacy_test_import(rel, target, allowlist=parts_allowlist)
            if matched is not None:
                used_allowlist.add(matched)
            else:
                violations.append(
                    f"{rel}:{node.lineno} ({pkg_layer}) -> moex_spread_lab.{target} ({layer})"
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
    parsed_map: dict[Path, tuple[str, ast.Module]] | None = None,
) -> list[str]:
    tests_dir = pkg_dir / "tests"
    pkg_layer = PACKAGE_TEST_LAYERS.get(pkg_dir.name)
    if not tests_dir.is_dir() or not pkg_layer:
        return []
    allowed = allowed_targets.get(pkg_layer, frozenset()) | {"cli"}
    return [
        v
        for path in sorted(tests_dir.rglob("*.py"))
        for v in check_test_file_layers(
            path,
            repo_root,
            pkg_layer,
            allowed,
            allowlist=allowlist,
            parsed=parsed_map.get(path) if parsed_map is not None else None,
        )
    ]
