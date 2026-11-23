"""Filesystem-derived layer classification for the architecture DAG tests.

A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
or from the explicit lists in this module — splitting a module never requires editing
test_architecture_dag.py (plan S1.4). `cli` surfaces that HEAD classified as "cli"
keep it; root-level compatibility shims stay explicit. See also
architecture_frozen_head.py for the frozen HEAD table used by the equivalence test.
"""

# A module's layer follows from its package directory (packages/<pkg>/src/moex_spread_lab/<top>/...)
# or from the explicit lists below — splitting a module never requires editing
# test_architecture_dag.py. `cli` surfaces whose HEAD classification was "cli" keep it;
# root-level compatibility shims stay explicit; everything else under a package tree
# takes the package's layer.
_CLI_LAYER_KEYS = frozenset({"cli", "cli_web", "core.cli", "data.cli", "accounting.cli"})
_PACKAGE_HEAD_LAYERS = {
    "core": "core",
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
