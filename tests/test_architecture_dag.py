"""Architectural DAG and 6-layer dependency boundary tests (Phase 0 guardrails).

Enforces two static invariants across every Python module in ``src/moex_spread_lab/``
using AST inspection (including function-level / lazy imports):

1. Zero internal import cycles (Tarjan max strongly-connected-component size == 1).
2. Strict 6-layer dependency DAG compliance matching ``docs/development/modular-architecture-plan.md``:
   - L0 ``core``       -> (stdlib / C extension only; no higher layers)
   - L1a ``data``      -> ``core``, ``data``
   - L1b ``accounting``-> ``core``, ``accounting``  (disjoint sibling of ``data``)
   - L2 ``strategies`` -> ``core``, ``data``, ``accounting``, ``strategies``
   - L3 ``proof``      -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``
   - L4 ``web``        -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``, ``web``
   - L5 ``cli``        -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``, ``web``, ``cli``
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "moex_spread_lab"

LAYER_ASSIGNMENTS: dict[str, str] = {
    # L0: core
    "_fastcore_loader": "core",
    "w0": "core",
    "timeframes": "core",
    "evaluation_periods": "core",
    "universe": "core",
    # L1a: data
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
    "run_artifacts": "data",
    "storage_policy": "data",
    # L1b: accounting
    "costs": "accounting",
    "portfolio": "accounting",
    "account_risk": "accounting",
    "position_sizing": "accounting",
    # L2: strategies
    "analytics": "strategies",
    "ledger": "strategies",
    "validation": "strategies",
    "run_artifact_import": "strategies",
    "carry": "strategies",
    "funding_screen": "strategies",
    "funding_recheck": "strategies",
    "proof_funding": "strategies",
    "proof_signals": "strategies",
    "calendar_spreads": "strategies",
    "s3_reference": "strategies",
    "screener": "strategies",
    "pair_screener": "strategies",
    "research": "strategies",
    # L3: proof
    "audit": "proof",
    "screener_search": "proof",
    "proof": "proof",
    "proof_kernel": "proof",
    "proof_pool": "proof",
    "proof_windows": "proof",
    "proof_batch": "proof",
    "proof_strategy_search": "proof",
    "proof_search": "proof",
    # L4: web
    "screener_app": "web",
    # L5: cli
    "cli": "cli",
}

ALLOWED_LAYER_TARGETS: dict[str, frozenset[str]] = {
    "core": frozenset({"core"}),
    "data": frozenset({"core", "data"}),
    "accounting": frozenset({"core", "accounting"}),
    "strategies": frozenset({"core", "data", "accounting", "strategies"}),
    "proof": frozenset({"core", "data", "accounting", "strategies", "proof"}),
    "web": frozenset({"core", "data", "accounting", "strategies", "proof", "web"}),
    "cli": frozenset({"core", "data", "accounting", "strategies", "proof", "web", "cli"}),
}


def _discover_modules() -> dict[str, Path]:
    return {
        path.stem: path
        for path in sorted(PACKAGE_DIR.glob("*.py"))
        if path.stem != "__init__"
    }


def _build_import_graph(modules: dict[str, Path]) -> dict[str, set[tuple[str, int]]]:
    """Return ``{module_name: {(imported_module, lineno), ...}}`` including lazy imports."""
    edges: dict[str, set[tuple[str, int]]] = {name: set() for name in modules}
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    if node.module:
                        target = node.module.split(".", 1)[0]
                        if target in modules and target != name:
                            edges[name].add((target, node.lineno))
                    else:
                        for alias in node.names:
                            if alias.name in modules and alias.name != name:
                                edges[name].add((alias.name, node.lineno))
                elif (
                    node.level == 0
                    and node.module
                    and node.module.startswith("moex_spread_lab.")
                ):
                    target = node.module.split(".", 2)[1]
                    if target in modules and target != name:
                        edges[name].add((target, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("moex_spread_lab."):
                        target = alias.name.split(".", 2)[1]
                        if target in modules and target != name:
                            edges[name].add((target, node.lineno))
    return edges


def _tarjan_sccs(adjacency: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in sorted(adjacency[node]):
            if neighbour not in indices:
                strongconnect(neighbour)
                lowlink[node] = min(lowlink[node], lowlink[neighbour])
            elif neighbour in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbour])
        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            sccs.append(sorted(component))

    for module_name in sorted(adjacency):
        if module_name not in indices:
            strongconnect(module_name)
    return sccs


class ArchitectureDagTests(unittest.TestCase):
    def test_every_package_module_has_explicit_layer_assignment(self) -> None:
        modules = _discover_modules()
        unclassified = sorted(set(modules) - set(LAYER_ASSIGNMENTS))
        stale = sorted(set(LAYER_ASSIGNMENTS) - set(modules))
        self.assertEqual(
            unclassified,
            [],
            f"New modules in src/moex_spread_lab/ must declare a layer in LAYER_ASSIGNMENTS: {unclassified}",
        )
        self.assertEqual(
            stale,
            [],
            f"Removed modules still listed in LAYER_ASSIGNMENTS: {stale}",
        )

    def test_zero_import_cycles_including_lazy_imports(self) -> None:
        modules = _discover_modules()
        detailed_edges = _build_import_graph(modules)
        adjacency = {
            src: {dst for dst, _ in targets}
            for src, targets in detailed_edges.items()
        }
        cycles = [scc for scc in _tarjan_sccs(adjacency) if len(scc) > 1]
        self.assertEqual(
            cycles,
            [],
            f"Internal import cycles detected (even lazy imports must remain acyclic): {cycles}",
        )

    def test_strict_six_layer_dependency_dag(self) -> None:
        modules = _discover_modules()
        detailed_edges = _build_import_graph(modules)
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            src_layer = LAYER_ASSIGNMENTS[src]
            allowed = ALLOWED_LAYER_TARGETS[src_layer]
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                dst_layer = LAYER_ASSIGNMENTS[dst]
                if dst_layer not in allowed:
                    violations.append(
                        f"{src}.py:{lineno} ({src_layer}) -> {dst}.py ({dst_layer})"
                    )
        self.assertEqual(
            violations,
            [],
            "Cross-layer dependency DAG violations:\n  " + "\n  ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
