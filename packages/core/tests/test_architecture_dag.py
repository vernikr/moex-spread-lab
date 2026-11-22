"""Architecture dependency tests."""

from __future__ import annotations

import ast
import re
import unittest
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
UMBRELLA_DIR = REPO_ROOT / "src" / "moex_spread_lab"
PACKAGES_DIR = REPO_ROOT / "packages"


def _expand(spec: str) -> list[str]:
    out: list[str] = []
    for token in spec.split():
        if "{" in token:
            prefix, rest = token.split("{", 1)
            body = rest.rstrip("}")
            out.extend(
                f"{prefix}{item}" if item else prefix.rstrip(".") for item in body.split(",")
            )
        else:
            out.append(token)
    return out


_LAYER_SPECS: dict[str, str] = {
    "core": (
        "core.{,_fastcore_loader,cache_metrics,contracts,evaluation_periods,hashed_json_cache,remote_cli,settings,timeframes,universe,w0,workstation_bench,workstation_remote} "
        "_fastcore_loader w0 timeframes evaluation_periods universe"
    ),
    "data": (
        "data.{,cli_parsers,cli_storage_handlers,universe} "
        "data.iss.{,catalog,iss_candles,iss_coverage,iss_history,iss_http,iss_snapshot} "
        "data.lake.{,bar_types,data_lake,data_refresh,h1_migration,h1_seal,legacy_series,market_store,parquet_catalog,parquet_store,raw_archive,refresh_merge} "
        "data.retention.{,data_inventory,data_retention,data_archive_candidates,h1_archive,inventory_policy,storage_policy} "
        "data.benchmark.{,lineage,lineage_math,ruonia_xlsx} "
        "bar_types data_lake h1_migration h1_seal h1_archive iss benchmark data_inventory data_retention data_archive_candidates data_refresh storage_policy"
    ),
    "accounting": (
        "accounting.{,costs,portfolio,account_risk,position_sizing,sizing_fast,sizing_replay,sizing_report,sizing_terms,ledger,run_artifacts,run_artifact_import,run_artifact_store,validation,research,research_intake,research_simulation} "
        "costs portfolio account_risk position_sizing ledger run_artifacts run_artifact_import validation research"
    ),
    "strategies": (
        "strategies.{,base,registry,calendar_eval,cli_parsers,conformance,signals} "
        "strategies.pair_mean_reversion.{,analytics,costs_helper,series_builder,fast_scan,baseline_runner,report_builder,engine,plugin} "
        "strategies.funding_carry.{,carry,study,strategy_costs,strategy_report,strategy_runner,timeline,proof_funding,funding_recheck,funding_screen,engine,plugin,intents} "
        "strategies.calendar_zscore.{,engine,plugin} strategies.calendar_ruonia.{,engine,plugin} "
        "strategies.s3_reference.{,engine} "
        "strategies.calendar.{,calendar_spreads,adjacent,contracts,data_cache,loaders,study,h1_candles,h1_windows,proof_ledger,proof_payload,catalog_builder,proof_runner,intents} "
        "strategies.pair_screener.{,normalization,screener,pair_screener,inputs,catalog,result_builder,runner,intents} "
        "analytics carry funding_screen funding_recheck proof_funding proof_signals calendar_spreads s3_reference screener pair_screener"
    ),
    "proof": "proof_runtime audit screener_search proof proof_kernel proof_pool proof_windows proof_batch proof_strategy_search proof_search",
    "web": "web screener_app",
    "cli": "core.cli data.cli accounting.cli strategies.cli proof_runtime.cli web.cli cli_web cli",
}
LAYER_ASSIGNMENTS: dict[str, str] = {
    module: layer for layer, spec in _LAYER_SPECS.items() for module in _expand(spec)
}
for _layer, _prefix in (("proof", "proof_runtime"), ("web", "web")):
    _dir = f"{_prefix.split('_')[0]}/src/moex_spread_lab/{_prefix}"
    _mods = (PACKAGES_DIR / _dir).glob("*.py")
    LAYER_ASSIGNMENTS |= {f"{_prefix}.{m.stem}": _layer for m in _mods if m.stem != "__init__"}

_LAYERS_ORDER = ("core", "data", "accounting", "strategies", "proof", "web", "cli")
ALLOWED_LAYER_TARGETS: dict[str, frozenset[str]] = {
    "core": frozenset({"core"}),
    "data": frozenset({"core", "data"}),
    "accounting": frozenset({"core", "accounting"}),
    "strategies": frozenset(_LAYERS_ORDER[:4]),
    "proof": frozenset(_LAYERS_ORDER[:5]),
    "web": frozenset(_LAYERS_ORDER[:6]),
    "cli": frozenset(_LAYERS_ORDER),
}


def _module_key(pkg_root: Path, file_path: Path) -> str | None:
    parts = list(file_path.relative_to(pkg_root).with_suffix("").parts)
    if parts == ["__init__"]:
        return None
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _discover_modules() -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    roots = [
        UMBRELLA_DIR,
        *(sorted(PACKAGES_DIR.glob("*/src/moex_spread_lab")) if PACKAGES_DIR.is_dir() else ()),
    ]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if (key := _module_key(root, path)) is not None:
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
        candidates = [f"{rel}.{alias_name}", rel] if (alias_name and module) else [rel]
    else:
        parts = current_key.split(".") if is_package_init else current_key.split(".")[:-1]
        up = level - 1
        if up > len(parts):
            return results
        prefix = ".".join(parts[: len(parts) - up] if up else parts)
        if module:
            rel = f"{prefix}.{module}" if prefix else module
            candidates = (
                [f"{rel}.{alias_name}", rel] if (alias_name and alias_name != "*") else [rel]
            )
        elif alias_name and alias_name != "*":
            candidates = [f"{prefix}.{alias_name}" if prefix else alias_name]
        else:
            candidates = [prefix] if prefix else []

    for probe in candidates:
        while probe:
            if probe in known_modules and probe != current_key:
                results.add(probe)
                break
            if "." not in probe:
                break
            probe = probe.rsplit(".", 1)[0]
    return results


def _build_import_graph(modules: dict[str, Path]) -> dict[str, set[tuple[str, int]]]:
    known = set(modules)
    edges: dict[str, set[tuple[str, int]]] = {name: set() for name in modules}
    for name, path in modules.items():
        is_init = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    for tgt in _resolve_internal_target(
                        name, is_init, node.level, node.module, alias.name, known
                    ):
                        edges[name].add((tgt, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for tgt in _resolve_internal_target(name, is_init, 0, None, alias.name, known):
                        edges[name].add((tgt, node.lineno))
    return edges


@lru_cache(maxsize=1)
def _workspace_analysis() -> tuple[dict[str, Path], dict[str, set[tuple[str, int]]]]:
    modules = _discover_modules()
    return modules, _build_import_graph(modules)


def _tarjan_sccs(adjacency: dict[str, set[str]]) -> list[list[str]]:
    idx = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal idx
        indices[node] = lowlink[node] = idx
        idx += 1
        stack.append(node)
        on_stack.add(node)
        for nb in sorted(adjacency[node]):
            if nb not in indices:
                strongconnect(nb)
                lowlink[node] = min(lowlink[node], lowlink[nb])
            elif nb in on_stack:
                lowlink[node] = min(lowlink[node], indices[nb])
        if lowlink[node] == indices[node]:
            comp: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                comp.append(member)
                if member == node:
                    break
            sccs.append(sorted(comp))

    for mod in sorted(adjacency):
        if mod not in indices:
            strongconnect(mod)
    return sccs


class ArchitectureDagTests(unittest.TestCase):
    def test_every_package_module_has_explicit_layer_assignment(self) -> None:
        modules, _ = _workspace_analysis()
        unclassified = sorted(set(modules) - set(LAYER_ASSIGNMENTS))
        stale = sorted(set(LAYER_ASSIGNMENTS) - set(modules))
        self.assertEqual(unclassified, [], f"Unclassified modules: {unclassified}")
        self.assertEqual(stale, [])

    def test_zero_import_cycles_including_lazy_imports(self) -> None:
        _, detailed_edges = _workspace_analysis()
        adjacency = {src: {dst for dst, _ in targets} for src, targets in detailed_edges.items()}
        cycles = [scc for scc in _tarjan_sccs(adjacency) if len(scc) > 1]
        self.assertEqual(cycles, [], f"Internal import cycles detected: {cycles}")

    def test_strict_six_layer_dependency_dag(self) -> None:
        _, detailed_edges = _workspace_analysis()
        violations = [
            f"{src}:{lineno} ({LAYER_ASSIGNMENTS[src]}) -> {dst} ({LAYER_ASSIGNMENTS[dst]})"
            for src, targets in sorted(detailed_edges.items())
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0]))
            if LAYER_ASSIGNMENTS[dst] not in ALLOWED_LAYER_TARGETS[LAYER_ASSIGNMENTS[src]]
        ]
        self.assertFalse(violations, "Cross-layer DAG violations:\n  " + "\n  ".join(violations))

    def test_extracted_packages_do_not_import_umbrella_modules(self) -> None:
        _, detailed_edges = _workspace_analysis()
        roots = {"core", "data", "accounting", "strategies", "proof_runtime", "web"}
        prefixes = tuple(f"{r}." for r in roots)
        violations = [
            f"{src}:{lineno} -> {dst} (umbrella import forbidden)"
            for src, targets in sorted(detailed_edges.items())
            if src in roots or src.startswith(prefixes)
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0]))
            if dst not in roots and not dst.startswith(prefixes)
        ]
        self.assertFalse(violations, "Umbrella imports forbidden:\n  " + "\n  ".join(violations))

    def test_frontend_es_module_import_dag_is_acyclic(self) -> None:
        js_dir = PACKAGES_DIR / "web" / "src" / "moex_spread_lab" / "web" / "static" / "js"
        js_files = sorted(js_dir.glob("*.js"))
        self.assertGreaterEqual(len(js_files), 9)
        known = {p.name for p in js_files}
        adjacency: dict[str, set[str]] = {name: set() for name in known}
        import_re = re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""")
        for path in js_files:
            for target in import_re.findall(path.read_text(encoding="utf-8")):
                self.assertTrue(target.startswith("./") and target.endswith(".js"))
                dep = target.removeprefix("./")
                self.assertIn(dep, known)
                adjacency[path.name].add(dep)
        cycles = [scc for scc in _tarjan_sccs(adjacency) if len(scc) > 1]
        self.assertEqual(cycles, [], f"Frontend ES module import cycles: {cycles}")

    def test_strategy_plugin_subfolders_are_strictly_isolated(self) -> None:
        _, detailed_edges = _workspace_analysis()
        plugins = {
            "pair_mean_reversion",
            "funding_carry",
            "calendar_zscore",
            "calendar_ruonia",
            "s3_reference",
        }
        shared = {"strategies", "strategies.base", "strategies.calendar_eval"}
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            parts = src.split(".")
            if parts[0] != "strategies" or len(parts) < 2 or parts[1] not in plugins:
                continue
            own = f"strategies.{parts[1]}"
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                if (
                    dst.startswith(("core", "data", "accounting", f"{own}."))
                    or dst in shared
                    or dst == own
                ):
                    continue
                violations.append(f"{src}:{lineno} -> {dst}")
        self.assertFalse(
            violations, "Cross-strategy subfolder imports:\n  " + "\n  ".join(violations)
        )

    def test_data_and_accounting_siblings_are_strictly_isolated(self) -> None:
        _, detailed_edges = _workspace_analysis()
        violations: list[str] = []
        for src, targets in sorted(detailed_edges.items()):
            for pkg in ("data", "accounting"):
                if src == pkg or src.startswith(f"{pkg}."):
                    for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
                        if LAYER_ASSIGNMENTS[dst] not in {"core", pkg}:
                            violations.append(
                                f"{src}:{lineno} ({pkg}) -> {dst} ({LAYER_ASSIGNMENTS[dst]})"
                            )
        self.assertFalse(
            violations, "Sibling Layer 1 isolation broken:\n  " + "\n  ".join(violations)
        )

    def test_workspace_code_and_tests_do_not_import_compatibility_shims(self) -> None:
        shim_names = {
            p.stem for p in UMBRELLA_DIR.glob("*.py") if p.name not in {"__init__.py", "cli.py"}
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
            rel = path.relative_to(REPO_ROOT)
            for match in legacy_path.finditer(source):
                if match.group(1) in shim_names:
                    line = source[: match.start()].count("\n") + 1
                    violations.append(f"{rel}:{line} -> {match.group(0)}")
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if isinstance(node, ast.ImportFrom) and node.module == "moex_spread_lab":
                    for alias in node.names:
                        if alias.name in shim_names:
                            violations.append(
                                f"{rel}:{node.lineno} -> from moex_spread_lab import {alias.name}"
                            )
        self.assertFalse(violations, "Root shim imports forbidden:\n  " + "\n  ".join(violations))


if __name__ == "__main__":
    unittest.main()
