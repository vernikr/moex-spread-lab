"""Architecture dependency tests."""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path

from architecture_layers import (
    _ROOT_SHIM_LAYERS,
    _optional_compat_aliases,
    check_package_test_layers,
    check_test_file_layers,
    derive_layer,
    expand_compat_candidates,
    iter_import_nodes,
    package_head_layers,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
UMBRELLA_DIR = REPO_ROOT / "src" / "moex_spread_lab"
PACKAGES_DIR = REPO_ROOT / "packages"


_LAYERS_ORDER = ("core", "data", "accounting", "strategies", "proof", "web", "cli")
# Each layer may import itself and all lower layers, except accounting, which
# may not import its data sibling (sibling isolation is checked separately).
ALLOWED_LAYER_TARGETS: dict[str, frozenset[str]] = {
    layer: frozenset(_LAYERS_ORDER[: i + 1]) for i, layer in enumerate(_LAYERS_ORDER)
}
ALLOWED_LAYER_TARGETS["accounting"] = frozenset({"core", "accounting"})

# Single-pass parse cache: each workspace file is parsed once, then reused.
_PARSE_CACHE: dict[Path, tuple[str, ast.Module]] = {}
Edges = dict[str, set[tuple[str, int]]]
_LEGACY_PATH_RE = re.compile(r"\bmoex_spread_lab\.([A-Za-z_]\w*)\b")


def _parse_file(path: Path) -> tuple[str, ast.Module]:
    cached = _PARSE_CACHE.get(path)
    if cached is not None:
        return cached
    source = path.read_text(encoding="utf-8")
    parsed = (source, ast.parse(source, filename=str(path)))
    if path.is_relative_to(REPO_ROOT):
        _PARSE_CACHE[path] = parsed
    return parsed


def _module_key(pkg_root: Path, file_path: Path) -> str | None:
    parts = list(file_path.relative_to(pkg_root).with_suffix("").parts)
    if parts == ["__init__"]:
        return None
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _discover_modules(repo_root: Path = REPO_ROOT) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    roots = [
        repo_root / "src" / "moex_spread_lab",
        *sorted((repo_root / "packages").glob("*/src/moex_spread_lab")),
    ]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if (key := _module_key(root, path)) is not None:
                discovered[key] = path
    return discovered


def _layer_assignments(modules: dict[str, Path], repo_root: Path = REPO_ROOT) -> dict[str, str]:
    return {
        key: layer
        for key in modules
        if (layer := derive_layer(key, repo_root=repo_root)) is not None
    }


# Live mapping: filesystem-derived (see architecture_layers.derive_layer).
LAYER_ASSIGNMENTS: dict[str, str] = _layer_assignments(_discover_modules(), REPO_ROOT)


# A new strategy folder is classified and isolation-checked from disk.
STRATEGIES_SRC = PACKAGES_DIR / "strategies/src/moex_spread_lab/strategies"
SHARED_STRATEGY_INFRA = frozenset({"calendar", "pair_screener"})
STRATEGY_SUBPACKAGES = frozenset(
    path.name for path in STRATEGIES_SRC.glob("*") if path.is_dir() and path.name != "__pycache__"
)


def _absolute_candidates(module: str | None, alias: str | None) -> list[str] | None:
    full = module or alias or ""
    if not full.startswith("moex_spread_lab."):
        return None
    rel = full.removeprefix("moex_spread_lab.")
    if alias and module:
        return [f"{rel}.{alias}", rel]
    return [rel]


def _relative_prefix(key: str, is_init: bool, level: int) -> str | None:
    parts = key.split(".") if is_init else key.split(".")[:-1]
    up = level - 1
    if up > len(parts):
        return None
    return ".".join(parts[: len(parts) - up] if up else parts)


def _relative_candidates(
    key: str, is_init: bool, level: int, module: str | None, alias: str | None
) -> list[str] | None:
    prefix = _relative_prefix(key, is_init, level)
    if prefix is None:
        return None
    if module:
        rel = f"{prefix}.{module}" if prefix else module
        if alias and alias != "*":
            return [f"{rel}.{alias}", rel]
        return [rel]
    if alias and alias != "*":
        return [f"{prefix}.{alias}" if prefix else alias]
    return [prefix] if prefix else []


def _probe_known(
    candidates: list[str], known: set[str], key: str, aliases: dict[str, str]
) -> set[str]:
    results: set[str] = set()
    for probe in expand_compat_candidates(candidates, aliases):
        while probe:
            if probe in known and probe != key:
                results.add(probe)
                break
            if "." not in probe:
                break
            probe = probe.rsplit(".", 1)[0]
    return results


def _resolve_internal_target(
    key: str,
    is_init: bool,
    level: int,
    module: str | None,
    alias: str | None,
    known: set[str],
    *,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    if level == 0:
        candidates = _absolute_candidates(module, alias)
    else:
        candidates = _relative_candidates(key, is_init, level, module, alias)
    if candidates is None:
        return set()
    return _probe_known(candidates, known, key, aliases or {})


def _edges_for_tree(
    name: str, is_init: bool, tree: ast.Module, known: set[str], aliases: dict[str, str]
) -> set[tuple[str, int]]:
    edges: set[tuple[str, int]] = set()
    for node in iter_import_nodes(tree):
        level = node.level if isinstance(node, ast.ImportFrom) else 0
        module = node.module if isinstance(node, ast.ImportFrom) else None
        for alias in node.names:
            for tgt in _resolve_internal_target(
                name, is_init, level, module, alias.name, known, aliases=aliases
            ):
                edges.add((tgt, node.lineno))
    return edges


def _build_import_graph(modules: dict[str, Path], *, repo_root: Path = REPO_ROOT) -> Edges:
    known = set(modules)
    aliases = _optional_compat_aliases(repo_root)
    edges: Edges = {}
    for name, path in modules.items():
        _, tree = _parse_file(path)
        edges[name] = _edges_for_tree(name, path.name == "__init__.py", tree, known, aliases)
    return edges


@lru_cache(maxsize=1)
def _workspace_analysis() -> tuple[dict[str, Path], Edges]:
    modules = _discover_modules(REPO_ROOT)
    return modules, _build_import_graph(modules, repo_root=REPO_ROOT)


def _find_cycles(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Return import cycles as DFS stack slices (a back edge closes a cycle)."""
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nb in sorted(adjacency[node]):
            if state.get(nb) == 1:
                cycles.append(stack[stack.index(nb) :])
            elif nb not in state:
                visit(nb)
        stack.pop()
        state[node] = 2

    for mod in sorted(adjacency):
        if mod not in state:
            visit(mod)
    return cycles


def _strategy_isolation_violations(edges: Edges, plugins: frozenset[str]) -> list[str]:
    shared = {"strategies", "strategies.base", "strategies.calendar_eval", "strategies.pair_data"}
    violations: list[str] = []
    for src, targets in sorted(edges.items()):
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
    return violations


def _sibling_violations_for_pkg(edges: Edges, layers: dict[str, str], pkg: str) -> list[str]:
    violations: list[str] = []
    for src, targets in sorted(edges.items()):
        if src != pkg and not src.startswith(f"{pkg}."):
            continue
        for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0])):
            if layers[dst] not in {"core", pkg}:
                violations.append(f"{src}:{lineno} ({pkg}) -> {dst} ({layers[dst]})")
    return violations


def _sibling_violations(edges: Edges, layers: dict[str, str]) -> list[str]:
    return [
        v for pkg in ("data", "accounting") for v in _sibling_violations_for_pkg(edges, layers, pkg)
    ]


def _adjacency(edges: Edges) -> dict[str, set[str]]:
    return {src: {dst for dst, _ in targets} for src, targets in edges.items()}


def _layer_violations(edges: Edges, layers: dict[str, str]) -> list[str]:
    return [
        f"{src}:{lineno} ({layers[src]}) -> {dst} ({layers[dst]})"
        for src, targets in sorted(edges.items())
        for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0]))
        if layers[dst] not in ALLOWED_LAYER_TARGETS[layers[src]]
    ]


def _shim_names() -> set[str]:
    disk_shims = {
        p.stem for p in UMBRELLA_DIR.glob("*.py") if p.name not in {"__init__.py", "cli.py"}
    }
    return disk_shims | (set(_ROOT_SHIM_LAYERS) - set(package_head_layers(REPO_ROOT)))


def _shim_candidates() -> list[Path]:
    return [
        *sorted(PACKAGES_DIR.glob("*/src/moex_spread_lab/**/*.py")),
        *sorted(PACKAGES_DIR.glob("*/tests/**/*.py")),
        *sorted((REPO_ROOT / "tests").glob("*.py")),
    ]


def _shim_violations_for_file(path: Path, shim_names: set[str]) -> list[str]:
    source, tree = _parse_file(path)
    rel = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for match in _LEGACY_PATH_RE.finditer(source):
        if match.group(1) in shim_names:
            line = source[: match.start()].count("\n") + 1
            violations.append(f"{rel}:{line} -> {match.group(0)}")
    for node in iter_import_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "moex_spread_lab":
            for alias in node.names:
                if alias.name in shim_names:
                    violations.append(
                        f"{rel}:{node.lineno} -> from moex_spread_lab import {alias.name}"
                    )
    return violations


# In-memory drop-in probe: overlay on the cached graph, no copy/subprocess.
_PROBE_KEY = "strategies.probe_demo.plugin"
_PROBE_PATH = Path("strategies/probe_demo/plugin.py")
_PROBE_CLEAN = (
    "from moex_spread_lab.core.contracts import StrategyPlugin\n"
    "from moex_spread_lab.core.universe import CALENDAR_UNIVERSE\n"
)
_PROBE_CROSS = (
    _PROBE_CLEAN
    + "from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin\n"
)


def _probe_analysis(plugin_source: str) -> tuple[Edges, dict[str, str]]:
    modules, base_edges = _workspace_analysis()
    overlay = dict(modules)
    overlay[_PROBE_KEY] = _PROBE_PATH
    edges = {name: set(targets) for name, targets in base_edges.items()}
    tree = ast.parse(plugin_source, filename=str(_PROBE_PATH))
    aliases = _optional_compat_aliases(REPO_ROOT)
    edges[_PROBE_KEY] = _edges_for_tree(_PROBE_KEY, False, tree, set(overlay), aliases)
    return edges, _layer_assignments(overlay, REPO_ROOT)


def _probe_plugins() -> frozenset[str]:
    return (STRATEGY_SUBPACKAGES | {"probe_demo"}) - SHARED_STRATEGY_INFRA


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ArchitectureDagTests(unittest.TestCase):
    def _assert_clean(self, violations: list[str], label: str) -> None:
        self.assertFalse(violations, f"{label}:\n  " + "\n  ".join(violations))

    def test_every_package_module_has_explicit_layer_assignment(self) -> None:
        modules, _ = _workspace_analysis()
        unclassified = sorted(set(modules) - set(LAYER_ASSIGNMENTS))
        stale = sorted(set(LAYER_ASSIGNMENTS) - set(modules))
        self.assertEqual(unclassified, [], f"Unclassified modules: {unclassified}")
        self.assertEqual(stale, [])

    def test_zero_import_cycles_including_lazy_imports(self) -> None:
        _, edges = _workspace_analysis()
        cycles = _find_cycles(_adjacency(edges))
        self.assertEqual(cycles, [], f"Internal import cycles detected: {cycles}")

    def test_strict_six_layer_dependency_dag(self) -> None:
        _, edges = _workspace_analysis()
        self._assert_clean(_layer_violations(edges, LAYER_ASSIGNMENTS), "Cross-layer DAG")

    def test_extracted_packages_do_not_import_umbrella_modules(self) -> None:
        _, edges = _workspace_analysis()
        roots = set(package_head_layers(REPO_ROOT))
        prefixes = tuple(f"{r}." for r in roots)
        violations = [
            f"{src}:{lineno} -> {dst} (umbrella import forbidden)"
            for src, targets in sorted(edges.items())
            if src in roots or src.startswith(prefixes)
            for dst, lineno in sorted(targets, key=lambda item: (item[1], item[0]))
            if dst not in roots and not dst.startswith(prefixes)
        ]
        self._assert_clean(violations, "Umbrella imports forbidden")

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
        cycles = _find_cycles(adjacency)
        self.assertEqual(cycles, [], f"Frontend ES module import cycles: {cycles}")

    def test_strategy_plugin_subfolders_are_strictly_isolated(self) -> None:
        _, edges = _workspace_analysis()
        plugins = STRATEGY_SUBPACKAGES - SHARED_STRATEGY_INFRA
        self._assert_clean(
            _strategy_isolation_violations(edges, plugins), "Cross-strategy subfolder imports"
        )

    def test_data_and_accounting_siblings_are_strictly_isolated(self) -> None:
        _, edges = _workspace_analysis()
        self._assert_clean(
            _sibling_violations(edges, LAYER_ASSIGNMENTS), "Sibling Layer 1 isolation broken"
        )

    def test_workspace_code_and_tests_do_not_import_compatibility_shims(self) -> None:
        shim_names = _shim_names()
        violations = [
            v for path in _shim_candidates() for v in _shim_violations_for_file(path, shim_names)
        ]
        self._assert_clean(violations, "Root shim imports forbidden")

    def test_package_tests_do_not_import_upward_layers(self) -> None:
        parsed_map = {p: _parse_file(p) for p in sorted(PACKAGES_DIR.glob("*/tests/**/*.py"))}
        violations = [
            v
            for pkg_dir in sorted(PACKAGES_DIR.glob("*"))
            for v in check_package_test_layers(
                pkg_dir, REPO_ROOT, ALLOWED_LAYER_TARGETS, parsed_map=parsed_map
            )
        ]
        self._assert_clean(violations, "Package test-layer upward import violations")

    def test_upward_test_import_gate_rejects_new_and_stale_allowlist_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "packages/data/tests/test_sample.py"
            _write(test_file, "from moex_spread_lab.web import server\n")
            allowed = ALLOWED_LAYER_TARGETS["data"] | {"cli"}
            violations = check_test_file_layers(
                test_file, root, "data", allowed, allowlist=frozenset()
            )
            self.assertEqual(len(violations), 1)
            self.assertIn("packages/data/tests/test_sample.py:1 (data)", violations[0])
            self.assertIn("moex_spread_lab.web (web)", violations[0])

            # Stale allowlist entry on an existing clean file fails the upper-bound check.
            _write(test_file, "from moex_spread_lab.core import w0\n")
            stale = check_test_file_layers(
                test_file,
                root,
                "data",
                allowed,
                allowlist=frozenset({("packages/data/tests/test_sample.py", "web.server")}),
            )
            self.assertEqual(len(stale), 1)
            self.assertIn("stale LEGACY_UPWARD_TEST_IMPORTS entry", stale[0])

            # Deleted allowlisted file path does not block package-scoped PRs before #168.
            clean_pkg = check_package_test_layers(
                root / "packages/data",
                root,
                ALLOWED_LAYER_TARGETS,
                allowlist=frozenset({("packages/data/tests/deleted_legacy.py", "web.server")}),
            )
            self.assertEqual(clean_pkg, [])

    def test_proof_runtime_registration_handles_presence_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core_mod = root / "packages/core/src/moex_spread_lab/core/sample.py"
            proof_mod = root / "packages/proof/src/moex_spread_lab/proof/service.py"
            _write(core_mod, "from moex_spread_lab.proof_runtime import service\n")
            _write(proof_mod, "X = 1\n")

            # When proof_runtime is absent on disk, discovery/classification succeeds cleanly.
            self.assertNotIn("proof_runtime", package_head_layers(root))
            modules_absent = _discover_modules(root)
            self.assertEqual(set(modules_absent), set(_layer_assignments(modules_absent, root)))

            # When proof_runtime is present on disk, upward imports through the alias fail DAG.
            pr_init = root / "packages/proof/src/moex_spread_lab/proof_runtime/__init__.py"
            _write(pr_init, "")
            self.assertEqual(package_head_layers(root).get("proof_runtime"), "proof")
            modules_present = _discover_modules(root)
            layers_present = _layer_assignments(modules_present, root)
            edges = _build_import_graph(modules_present, repo_root=root)
            dag_violations = _layer_violations(edges, layers_present)
            self.assertTrue(
                any("core.sample" in v and "proof.service" in v for v in dag_violations)
            )

            # Package test upward-import check also catches proof_runtime imports.
            core_test = root / "packages/core/tests/test_upward_alias.py"
            _write(core_test, "from moex_spread_lab.proof_runtime import service\n")
            test_violations = check_package_test_layers(
                root / "packages/core", root, ALLOWED_LAYER_TARGETS, allowlist=frozenset()
            )
            self.assertEqual(len(test_violations), 1)
            self.assertIn("moex_spread_lab.proof_runtime (proof)", test_violations[0])

    def test_new_strategy_folder_passes_the_shared_dag_test_without_edits(self) -> None:
        _, baseline_edges = _workspace_analysis()
        baseline_plugins = STRATEGY_SUBPACKAGES - SHARED_STRATEGY_INFRA
        self.assertEqual(_strategy_isolation_violations(baseline_edges, baseline_plugins), [])
        edges, layers = _probe_analysis(_PROBE_CLEAN)
        self.assertIn(_PROBE_KEY, layers)
        self.assertEqual(_strategy_isolation_violations(edges, _probe_plugins()), [])
        self.assertEqual(_layer_violations(edges, layers), [])
        self.assertEqual(_find_cycles(_adjacency(edges)), [])

    def test_dynamic_discovery_still_rejects_cross_strategy_imports(self) -> None:
        edges, _ = _probe_analysis(_PROBE_CROSS)
        violations = _strategy_isolation_violations(edges, _probe_plugins())
        self.assertTrue(violations)
        self.assertIn("funding_carry.plugin", "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
