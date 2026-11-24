"""Filesystem-derived layer classification for the architecture DAG tests.

A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
or from the explicit lists in this module — splitting a module never requires editing
test_architecture_dag.py (plan S1.4). `cli` surfaces that HEAD classified as "cli"
keep it; root-level compatibility shims stay explicit. See also
architecture_frozen_head.py for the frozen HEAD table used by the equivalence test.
"""

from __future__ import annotations

import ast
from pathlib import Path

# A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
# or from the explicit lists below — splitting a module never requires editing
# test_architecture_dag.py. `cli` surfaces whose HEAD classification was "cli" keep it;
# root-level compatibility shims stay explicit; everything else under a package tree
# takes the package's layer.
_CLI_LAYER_KEYS = frozenset({"cli", "cli_web", "core.cli", "data.cli", "accounting.cli"})
_PACKAGE_HEAD_LAYERS = {
    "core": "core",
    "proof": "proof",
    "data": "data",
    "accounting": "accounting",
    "strategies": "strategies",
    "proof_runtime": "proof",
    "web": "web",
}
_ROOT_SHIM_LAYERS = {
    "_fastcore_loader": "core",
    "w0": "core",
    "timeframes": "core",
    "evaluation_periods": "core",
    "universe": "core",
    "bar_types": "data",
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
    "pair_screener": "strategies",
    "audit": "proof",
    "screener_search": "proof",
    "proof": "proof",
    "proof_kernel": "proof",
    "proof_pool": "proof",
    "proof_windows": "proof",
    "proof_batch": "proof",
    "proof_strategy_search": "proof",
    "proof_search": "proof",
    "screener_app": "web",
}


def derive_layer(key: str) -> str | None:
    if key in _CLI_LAYER_KEYS:
        return "cli"
    layer = _PACKAGE_HEAD_LAYERS.get(key.split(".", 1)[0])
    return layer if layer is not None else _ROOT_SHIM_LAYERS.get(key)


# Package test suites map to their owning package's layer.
PACKAGE_TEST_LAYERS: dict[str, str] = {
    "core": "core",
    "data": "data",
    "accounting": "accounting",
    "strategies": "strategies",
    "proof": "proof",
    "web": "web",
}

# Legacy cross-layer integration imports in test suites awaiting their respective
# wave refactors (F7 / #14). Wave 1 strictly isolates msl-core (zero exceptions);
# strategies and web are also clean. Legacy imports for data, accounting and
# proof are ratcheted here so no new upward import can be introduced.
LEGACY_UPWARD_TEST_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("packages/data/tests/test_h1_archive.py", "proof.proof_pool"),
        ("packages/data/tests/test_h1_seal.py", "strategies.calendar.calendar_spreads"),
        ("packages/data/tests/test_h1_seal.py", "strategies.pair_screener.pair_screener"),
        ("packages/data/tests/test_storage_policy.py", "accounting.run_artifacts"),
        ("packages/data/tests/test_store_cutover.py", "strategies.calendar.calendar_spreads"),
        ("packages/data/tests/test_store_cutover.py", "strategies.pair_screener.screener"),
        ("packages/data/tests/test_store_cutover.py", "strategies.pair_screener.pair_screener"),
        (
            "packages/accounting/tests/test_execution_accounting.py",
            "strategies.pair_mean_reversion.analytics",
        ),
        ("packages/accounting/tests/test_position_sizing.py", "proof.proof_batch"),
        ("packages/accounting/tests/test_position_sizing.py", "proof.proof_search"),
        ("packages/accounting/tests/test_position_sizing.py", "proof.service"),
        (
            "packages/accounting/tests/test_run_artifacts.py",
            "strategies.pair_mean_reversion.analytics",
        ),
        (
            "packages/accounting/tests/test_validation.py",
            "strategies.pair_mean_reversion.analytics",
        ),
        (
            "packages/proof/tests/test_calendar_proof.py",
            "web.server",
        ),
        (
            "packages/proof/tests/test_proof_search.py",
            "web.server",
        ),
        (
            "packages/proof/tests/test_strategy_search.py",
            "web.server",
        ),
        ("packages/proof/tests/test_audit.py", "web.server"),
        ("packages/proof/tests/test_catalog_cache.py", "web.server"),
        ("packages/proof/tests/test_global_capital.py", "web.server"),
        ("packages/proof/tests/test_perf_equivalence_stream.py", "web.server"),
        ("packages/proof/tests/test_proof_batch.py", "web.server"),
        ("packages/proof/tests/test_proof_plateau.py", "web.server"),
        ("packages/proof/tests/test_proof_windows.py", "web.server"),
        ("packages/proof/tests/test_screener_search.py", "web.server"),
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


def is_legacy_test_import_exception(rel: str, target: str) -> bool:
    parts = target.split(".")
    return any(
        (rel, ".".join(parts[:i])) in LEGACY_UPWARD_TEST_IMPORTS for i in range(1, len(parts) + 1)
    )


def check_test_file_layers(
    path: Path, repo_root: Path, pkg_layer: str, allowed: frozenset[str]
) -> list[str]:
    rel = str(path.relative_to(repo_root))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 1)
        for target in extract_msl_import_targets(node):
            layer = resolve_target_layer(target)
            if (
                layer is not None
                and layer not in allowed
                and not is_legacy_test_import_exception(rel, target)
            ):
                violations.append(
                    f"{rel}:{lineno} ({pkg_layer}) -> moex_spread_lab.{target} ({layer})"
                )
    return violations


def check_package_test_layers(
    pkg_dir: Path, repo_root: Path, allowed_targets: dict[str, frozenset[str]]
) -> list[str]:
    tests_dir = pkg_dir / "tests"
    pkg_layer = PACKAGE_TEST_LAYERS.get(pkg_dir.name)
    if not tests_dir.is_dir() or not pkg_layer:
        return []
    allowed = allowed_targets.get(pkg_layer, frozenset()) | {"cli"}
    return [
        v
        for path in sorted(tests_dir.rglob("*.py"))
        for v in check_test_file_layers(path, repo_root, pkg_layer, allowed)
    ]
