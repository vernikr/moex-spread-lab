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
    package_head_layers,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
UMBRELLA_DIR = REPO_ROOT / "src" / "moex_spread_lab"
PACKAGES_DIR = REPO_ROOT / "packages"


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


def _discover_modules(repo_root: Path = REPO_ROOT) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    umbrella_dir = repo_root / "src" / "moex_spread_lab"
    packages_dir = repo_root / "packages"
    roots = [
        *([umbrella_dir] if umbrella_dir.is_dir() else []),
        *(sorted(packages_dir.glob("*/src/moex_spread_lab")) if packages_dir.is_dir() else ()),
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


# Live mapping: filesystem-derived (see architecture_layers.derive_layer). Splitting
# a module never requires editing this test.
LAYER_ASSIGNMENTS: dict[str, str] = _layer_assignments(_discover_modules(), REPO_ROOT)


# A new strategy is a new folder: it is classified and isolation-checked from disk,
# so two branches adding strategies never edit this file.
STRATEGIES_SRC = PACKAGES_DIR / "strategies/src/moex_spread_lab/strategies"
SHARED_STRATEGY_INFRA = frozenset({"calendar", "pair_screener"})
STRATEGY_SUBPACKAGES = frozenset(
    path.name for path in STRATEGIES_SRC.glob("*") if path.is_dir() and path.name != "__pycache__"
)


def _resolve_internal_target(
    current_key: str,
    is_package_init: bool,
    level: int,
    module: str | None,
    alias_name: str | None,
    known_modules: set[str],
    *,
    alias_roots: dict[str, str] | None = None,
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

    for probe in expand_compat_candidates(candidates, alias_roots or {}):
        while probe:
            if probe in known_modules and probe != current_key:
                results.add(probe)
                break
            if "." not in probe:
                break
            probe = probe.rsplit(".", 1)[0]
    return results


def _build_import_graph(
    modules: dict[str, Path], *, repo_root: Path = REPO_ROOT
) -> dict[str, set[tuple[str, int]]]:
    known = set(modules)
    alias_roots = _optional_compat_aliases(repo_root)
    edges: dict[str, set[tuple[str, int]]] = {name: set() for name in modules}
    for name, path in modules.items():
        is_init = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    for tgt in _resolve_internal_target(
                        name,
                        is_init,
                        node.level,
                        node.module,
                        alias.name,
                        known,
                        alias_roots=alias_roots,
                    ):
                        edges[name].add((tgt, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for tgt in _resolve_internal_target(
                        name,
                        is_init,
                        0,
                        None,
                        alias.name,
                        known,
                        alias_roots=alias_roots,
                    ):
                        edges[name].add((tgt, node.lineno))
    return edges


@lru_cache(maxsize=1)
def _workspace_analysis() -> tuple[dict[str, Path], dict[str, set[tuple[str, int]]]]:
    modules = _discover_modules(REPO_ROOT)
    return modules, _build_import_graph(modules, repo_root=REPO_ROOT)


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
        roots = set(package_head_layers(REPO_ROOT))
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
        plugins = STRATEGY_SUBPACKAGES - SHARED_STRATEGY_INFRA
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
        disk_shims = {
            p.stem for p in UMBRELLA_DIR.glob("*.py") if p.name not in {"__init__.py", "cli.py"}
        }
        shim_names = disk_shims | (set(_ROOT_SHIM_LAYERS) - set(package_head_layers(REPO_ROOT)))
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

    def test_package_tests_do_not_import_upward_layers(self) -> None:
        violations = [
            v
            for pkg_dir in sorted(PACKAGES_DIR.glob("*"))
            for v in check_package_test_layers(pkg_dir, REPO_ROOT, ALLOWED_LAYER_TARGETS)
        ]
        self.assertFalse(
            violations,
            "Package test-layer upward import violations:\n  " + "\n  ".join(violations),
        )

    def test_upward_test_import_gate_rejects_new_and_stale_allowlist_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "packages/data/tests/test_sample.py"
            test_file.parent.mkdir(parents=True)
            test_file.write_text(
                "from moex_spread_lab.web import server\n",
                encoding="utf-8",
            )
            allowed = ALLOWED_LAYER_TARGETS["data"] | {"cli"}
            violations = check_test_file_layers(
                test_file, root, "data", allowed, allowlist=frozenset()
            )
            self.assertEqual(len(violations), 1)
            self.assertIn("packages/data/tests/test_sample.py:1 (data)", violations[0])
            self.assertIn("moex_spread_lab.web (web)", violations[0])

            # Stale allowlist entry on an existing clean file fails the upper-bound check.
            test_file.write_text("from moex_spread_lab.core import w0\n", encoding="utf-8")
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
            core_mod.parent.mkdir(parents=True)
            proof_mod.parent.mkdir(parents=True)
            core_mod.write_text(
                "from moex_spread_lab.proof_runtime import service\n",
                encoding="utf-8",
            )
            proof_mod.write_text("X = 1\n", encoding="utf-8")

            # When proof_runtime is absent on disk, discovery/classification succeeds cleanly.
            self.assertNotIn("proof_runtime", package_head_layers(root))
            modules_absent = _discover_modules(root)
            layers_absent = _layer_assignments(modules_absent, root)
            self.assertEqual(set(modules_absent), set(layers_absent))

            # When proof_runtime is present on disk, upward imports through the alias fail DAG.
            pr_init = root / "packages/proof/src/moex_spread_lab/proof_runtime/__init__.py"
            pr_init.parent.mkdir(parents=True)
            pr_init.write_text("", encoding="utf-8")
            self.assertEqual(package_head_layers(root).get("proof_runtime"), "proof")
            modules_present = _discover_modules(root)
            layers_present = _layer_assignments(modules_present, root)
            edges = _build_import_graph(modules_present, repo_root=root)
            dag_violations = [
                f"{src} -> {dst}"
                for src, targets in edges.items()
                for dst, _ in targets
                if layers_present[dst] not in ALLOWED_LAYER_TARGETS[layers_present[src]]
            ]
            self.assertIn("core.sample -> proof.service", dag_violations)

            # Package test upward-import check also catches proof_runtime imports.
            core_test = root / "packages/core/tests/test_upward_alias.py"
            core_test.parent.mkdir(parents=True)
            core_test.write_text(
                "from moex_spread_lab.proof_runtime import service\n",
                encoding="utf-8",
            )
            test_violations = check_package_test_layers(
                root / "packages/core", root, ALLOWED_LAYER_TARGETS, allowlist=frozenset()
            )
            self.assertEqual(len(test_violations), 1)
            self.assertIn("moex_spread_lab.proof_runtime (proof)", test_violations[0])


if __name__ == "__main__":
    unittest.main()
