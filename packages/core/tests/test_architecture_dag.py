"""Architectural DAG and 6-layer dependency boundary tests (Phase 0 & Phase 1+ guardrails).

Enforces static invariants across every Python module in both ``packages/*/src/moex_spread_lab/``
and ``src/moex_spread_lab/`` using AST inspection (including function-level / lazy imports):

1. Explicit layer assignment for every module across the workspace.
2. Zero internal import cycles (Tarjan max strongly-connected-component size == 1).
3. Strict 6-layer dependency DAG compliance matching ``docs/development/modular-architecture-plan.md``:
   - L0 ``core``       -> ``core`` only (stdlib / C extension only; never imports umbrella shims)
   - L1a ``data``      -> ``core``, ``data``
   - L1b ``accounting``-> ``core``, ``accounting``  (disjoint sibling of ``data``)
   - L2 ``strategies`` -> ``core``, ``data``, ``accounting``, ``strategies``
   - L3 ``proof``      -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``
   - L4 ``web``        -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``, ``web``
   - L5 ``cli``        -> ``core``, ``data``, ``accounting``, ``strategies``, ``proof``, ``web``, ``cli``
"""

from __future__ import annotations

import ast
import re
import unittest
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
UMBRELLA_DIR = REPO_ROOT / "src" / "moex_spread_lab"
PACKAGES_DIR = REPO_ROOT / "packages"

LAYER_ASSIGNMENTS: dict[str, str] = {
    # L0: packages/core (`msl-core`)
    "core": "core",
    "core._fastcore_loader": "core",
    "core.contracts": "core",
    "core.evaluation_periods": "core",
    "core.settings": "core",
    "core.timeframes": "core",
    "core.universe": "core",
    "core.w0": "core",
    # L0 backward-compatibility shims in src/moex_spread_lab/
    "_fastcore_loader": "core",
    "w0": "core",
    "timeframes": "core",
    "evaluation_periods": "core",
    "universe": "core",
    # L1a: packages/data (`msl-data`)
    "data": "data",
    "data.iss": "data",
    "data.iss.catalog": "data",
    "data.lake": "data",
    "data.lake.bar_types": "data",
    "data.lake.data_lake": "data",
    "data.lake.data_refresh": "data",
    "data.lake.h1_migration": "data",
    "data.lake.h1_seal": "data",
    "data.retention": "data",
    "data.retention.data_inventory": "data",
    "data.retention.data_retention": "data",
    "data.retention.data_archive_candidates": "data",
    "data.retention.h1_archive": "data",
    "data.retention.storage_policy": "data",
    "data.benchmark": "data",
    "data.benchmark.lineage": "data",
    "data.universe": "data",
    # L1a backward-compatibility shims in src/moex_spread_lab/
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
    # L1b: packages/accounting (`msl-accounting`)
    "accounting": "accounting",
    "accounting.costs": "accounting",
    "accounting.portfolio": "accounting",
    "accounting.account_risk": "accounting",
    "accounting.position_sizing": "accounting",
    "accounting.ledger": "accounting",
    "accounting.run_artifacts": "accounting",
    "accounting.run_artifact_import": "accounting",
    "accounting.validation": "accounting",
    "accounting.research": "accounting",
    # L1b backward-compatibility shims in src/moex_spread_lab/
    "costs": "accounting",
    "portfolio": "accounting",
    "account_risk": "accounting",
    "position_sizing": "accounting",
    "ledger": "accounting",
    "run_artifacts": "accounting",
    "run_artifact_import": "accounting",
    "validation": "accounting",
    "research": "accounting",
    # L2: packages/strategies (`msl-strategies`)
    "strategies": "strategies",
    "strategies.base": "strategies",
    "strategies.conformance": "strategies",
    "strategies.signals": "strategies",
    "strategies.pair_mean_reversion": "strategies",
    "strategies.pair_mean_reversion.analytics": "strategies",
    "strategies.pair_mean_reversion.engine": "strategies",
    "strategies.pair_mean_reversion.plugin": "strategies",
    "strategies.funding_carry": "strategies",
    "strategies.funding_carry.carry": "strategies",
    "strategies.funding_carry.proof_funding": "strategies",
    "strategies.funding_carry.funding_recheck": "strategies",
    "strategies.funding_carry.funding_screen": "strategies",
    "strategies.funding_carry.engine": "strategies",
    "strategies.funding_carry.plugin": "strategies",
    "strategies.funding_carry.intents": "strategies",
    "strategies.calendar_zscore": "strategies",
    "strategies.calendar_zscore.engine": "strategies",
    "strategies.calendar_zscore.plugin": "strategies",
    "strategies.calendar_ruonia": "strategies",
    "strategies.calendar_ruonia.engine": "strategies",
    "strategies.calendar_ruonia.plugin": "strategies",
    "strategies.s3_reference": "strategies",
    "strategies.s3_reference.engine": "strategies",
    "strategies.calendar": "strategies",
    "strategies.calendar.calendar_spreads": "strategies",
    "strategies.calendar.intents": "strategies",
    "strategies.pair_screener": "strategies",
    "strategies.pair_screener.screener": "strategies",
    "strategies.pair_screener.pair_screener": "strategies",
    "strategies.pair_screener.intents": "strategies",
    # L2 backward-compatibility shims in src/moex_spread_lab/
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
    # L3: proof
    "proof_runtime": "proof",
    "proof_runtime.service": "proof",
    "proof_runtime.pair_funding_replay": "proof",
    "proof_runtime.pair_plugin_accounting": "proof",
    "proof_runtime.carry_intent_accounting": "proof",
    "proof_runtime.proof_kernel": "proof",
    "proof_runtime.proof_windows": "proof",
    "proof_runtime.proof_search": "proof",
    "proof_runtime.proof_batch": "proof",
    "proof_runtime.proof_strategy_search": "proof",
    "proof_runtime.proof_pool": "proof",
    "proof_runtime.screener_search": "proof",
    "proof_runtime.audit": "proof",
    "audit": "proof",
    "screener_search": "proof",
    "proof": "proof",
    "proof_kernel": "proof",
    "proof_pool": "proof",
    "proof_windows": "proof",
    "proof_batch": "proof",
    "proof_strategy_search": "proof",
    "proof_search": "proof",
    # L4: packages/web (`msl-web`) + umbrella shim
    "web": "web",
    "web.store": "web",
    "web.server": "web",
    "web.routes_proof": "web",
    "web.routes_settings": "web",
    "web.routes_legacy": "web",
    "screener_app": "web",
    # L5: cli
    "core.cli": "cli",
    "data.cli": "cli",
    "accounting.cli": "cli",
    "strategies.cli": "cli",
    "proof_runtime.cli": "cli",
    "web.cli": "cli",
    "cli_web": "cli",
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


def _module_key(pkg_root: Path, file_path: Path) -> str | None:
    rel = file_path.relative_to(pkg_root)
    parts = list(rel.with_suffix("").parts)
    if parts == ["__init__"]:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _discover_modules() -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    roots = [UMBRELLA_DIR]
    if PACKAGES_DIR.is_dir():
        roots.extend(sorted(PACKAGES_DIR.glob("*/src/moex_spread_lab")))
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            key = _module_key(root, path)
            if key is not None:
                discovered[key] = path
    return discovered


def _resolve_internal_target(
    current_key: str,
    is_package_init: bool,
    level: int,
    module: str | None,
    alias_name: str | None,
    known_modules: set[str],
) -> set[str]:
    results: set[str] = set()
    if level == 0:
        full = module or alias_name or ""
        if not full.startswith("moex_spread_lab."):
            return results
        rel = full.removeprefix("moex_spread_lab.")
        candidates = [rel]
        if alias_name and module:
            candidates.insert(0, f"{rel}.{alias_name}")
    else:
        parts = current_key.split(".") if is_package_init else current_key.split(".")[:-1]
        up = level - 1
        if up > len(parts):
            return results
        base_parts = parts[: len(parts) - up] if up else parts
        prefix = ".".join(base_parts)
        if module:
            rel = f"{prefix}.{module}" if prefix else module
            candidates = [rel]
            if alias_name and alias_name != "*":
                candidates.insert(0, f"{rel}.{alias_name}")
        elif alias_name and alias_name != "*":
            rel = f"{prefix}.{alias_name}" if prefix else alias_name
            candidates = [rel]
        else:
            candidates = [prefix] if prefix else []

    for cand in candidates:
        probe = cand
        while probe:
            if probe in known_modules and probe != current_key:
                results.add(probe)
                break
            if "." not in probe:
                break
            probe = probe.rsplit(".", 1)[0]
    return results


def _build_import_graph(modules: dict[str, Path]) -> dict[str, set[tuple[str, int]]]:
    """Return ``{module_key: {(imported_key, lineno), ...}}`` including lazy imports."""
    known = set(modules)
    edges: dict[str, set[tuple[str, int]]] = {name: set() for name in modules}
    for name, path in modules.items():
        is_pkg_init = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    for target in _resolve_internal_target(
                        name, is_pkg_init, node.level, node.module, alias.name, known
                    ):
                        edges[name].add((target, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for target in _resolve_internal_target(
                        name, is_pkg_init, 0, None, alias.name, known
                    ):
                        edges[name].add((target, node.lineno))
    return edges


@lru_cache(maxsize=1)
def _workspace_analysis() -> tuple[dict[str, Path], dict[str, set[tuple[str, int]]]]:
    modules = _discover_modules()
    return modules, _build_import_graph(modules)


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
        modules, _ = _workspace_analysis()
        unclassified = sorted(set(modules) - set(LAYER_ASSIGNMENTS))
        stale = sorted(set(LAYER_ASSIGNMENTS) - set(modules))
        self.assertEqual(
            unclassified,
            [],
            f"New modules in workspace must declare a layer in LAYER_ASSIGNMENTS: {unclassified}",
        )
        self.assertEqual(
            stale,
            [],
            f"Removed modules still listed in LAYER_ASSIGNMENTS: {stale}",
        )

    def test_zero_import_cycles_including_lazy_imports(self) -> None:
        modules, detailed_edges = _workspace_analysis()
        adjacency = {src: {dst for dst, _ in targets} for src, targets in detailed_edges.items()}
        cycles = [scc for scc in _tarjan_sccs(adjacency) if len(scc) > 1]
        self.assertEqual(
            cycles,
            [],
            f"Internal import cycles detected (even lazy imports must remain acyclic): {cycles}",
        )

    def test_strict_six_layer_dependency_dag(self) -> None:
        modules, detailed_edges = _workspace_analysis()
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            src_layer = LAYER_ASSIGNMENTS[src]
            allowed = ALLOWED_LAYER_TARGETS[src_layer]
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                dst_layer = LAYER_ASSIGNMENTS[dst]
                if dst_layer not in allowed:
                    violations.append(f"{src}:{lineno} ({src_layer}) -> {dst} ({dst_layer})")
        self.assertEqual(
            violations,
            [],
            "Cross-layer dependency DAG violations:\n  " + "\n  ".join(violations),
        )

    def test_extracted_packages_do_not_import_umbrella_modules(self) -> None:
        modules, detailed_edges = _workspace_analysis()
        package_prefixes = (
            "core.",
            "data.",
            "accounting.",
            "strategies.",
            "proof_runtime.",
            "web.",
        )
        package_roots = {"core", "data", "accounting", "strategies", "proof_runtime", "web"}
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            if src in package_roots or src.startswith(package_prefixes):
                for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                    if dst not in package_roots and not dst.startswith(package_prefixes):
                        violations.append(f"{src}:{lineno} -> {dst} (umbrella import forbidden)")
        self.assertEqual(
            violations,
            [],
            "Extracted workspace packages must not import root umbrella modules:\n  "
            + "\n  ".join(violations),
        )

    def test_frontend_es_module_import_dag_is_acyclic(self) -> None:
        import re

        js_dir = PACKAGES_DIR / "web" / "src" / "moex_spread_lab" / "web" / "static" / "js"
        js_files = sorted(js_dir.glob("*.js"))
        self.assertGreaterEqual(len(js_files), 9)
        known = {p.name for p in js_files}
        adjacency: dict[str, set[str]] = {name: set() for name in known}
        import_re = re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""")
        for path in js_files:
            src = path.read_text(encoding="utf-8")
            for target in import_re.findall(src):
                self.assertTrue(
                    target.startswith("./") and target.endswith(".js"),
                    f"{path.name} must use relative ./foo.js imports, got {target!r}",
                )
                dep = target.removeprefix("./")
                self.assertIn(dep, known, f"{path.name} imports missing ES module {dep!r}")
                adjacency[path.name].add(dep)
        cycles = [scc for scc in _tarjan_sccs(adjacency) if len(scc) > 1]
        self.assertEqual(cycles, [], f"Frontend ES module import cycles detected: {cycles}")

    def test_strategy_plugin_subfolders_are_strictly_isolated(self) -> None:
        """Each strategy plugin subfolder may import only core/data/accounting, strategies.base, and itself."""
        modules, detailed_edges = _workspace_analysis()
        plugin_folders = {
            "pair_mean_reversion",
            "funding_carry",
            "calendar_zscore",
            "calendar_ruonia",
            "s3_reference",
        }
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            if not src.startswith("strategies."):
                continue
            parts = src.split(".")
            if len(parts) < 2 or parts[1] not in plugin_folders:
                continue
            own_subpkg = f"strategies.{parts[1]}"
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                if dst.startswith(("core", "data", "accounting")):
                    continue
                if dst in {"strategies", "strategies.base"}:
                    continue
                if dst == own_subpkg or dst.startswith(f"{own_subpkg}."):
                    continue
                violations.append(
                    f"{src}:{lineno} -> {dst} (cross-strategy subfolder import forbidden)"
                )
        self.assertEqual(
            violations,
            [],
            "Strategy plugin subfolders must never import sibling strategy subfolders:\n  "
            + "\n  ".join(violations),
        )

    def test_data_and_accounting_siblings_are_strictly_isolated(self) -> None:
        modules, detailed_edges = _workspace_analysis()
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            if src == "data" or src.startswith("data."):
                for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                    dst_layer = LAYER_ASSIGNMENTS[dst]
                    if dst_layer not in {"core", "data"}:
                        violations.append(f"{src}:{lineno} (data) -> {dst} ({dst_layer})")
            elif src == "accounting" or src.startswith("accounting."):
                for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                    dst_layer = LAYER_ASSIGNMENTS[dst]
                    if dst_layer not in {"core", "accounting"}:
                        violations.append(f"{src}:{lineno} (accounting) -> {dst} ({dst_layer})")
        self.assertEqual(
            violations,
            [],
            "Layer 1 siblings msl-data and msl-accounting must never import each other or higher layers:\n  "
            + "\n  ".join(violations),
        )

    def test_workspace_code_and_tests_do_not_import_compatibility_shims(self) -> None:
        """Internal packages/tests use canonical package paths; root modules are public shims only."""
        shim_names = {
            path.stem
            for path in UMBRELLA_DIR.glob("*.py")
            if path.name not in {"__init__.py", "cli.py"}
        }
        candidates = [
            *sorted(PACKAGES_DIR.glob("*/src/moex_spread_lab/**/*.py")),
            *sorted(PACKAGES_DIR.glob("*/tests/**/*.py")),
            *sorted((REPO_ROOT / "tests").glob("*.py")),
        ]
        violations: list[str] = []
        legacy_path = re.compile(r"\bmoex_spread_lab\.([A-Za-z_]\w*)\b")
        for path in candidates:
            source = path.read_text(encoding="utf-8")
            for match in legacy_path.finditer(source):
                if match.group(1) in shim_names:
                    line = source[: match.start()].count(chr(10)) + 1
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{line} -> {match.group(0)}")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "moex_spread_lab":
                    for alias in node.names:
                        if alias.name in shim_names:
                            violations.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno} -> "
                                f"from moex_spread_lab import {alias.name}"
                            )
        self.assertEqual(
            violations,
            [],
            "Workspace internals must import canonical package modules, not root compatibility shims:"
            + chr(10)
            + "  "
            + chr(10).join(violations),
        )


if __name__ == "__main__":
    unittest.main()
