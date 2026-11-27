"""Run the repository-wide, dependency-light architecture gate.

This script intentionally reuses the checked-in DAG/conformance tests and invokes
the governance CLI check in-process. It is designed for the fast CI gate;
full behavioral and numerical suites run in the package test matrix.

Package-manifest conformance (Issue #140) is validated in-process with the
standard library only (``json`` + ``tomllib``), so ``packages/*/manifest.json``
drift from ``pyproject.toml`` or the Stage-5 required keys fails loudly before
any heavy import is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import unittest
from pathlib import Path
from typing import Any, TypeGuard

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

# Make the script usable both from an installed uv workspace and from a plain
# repository checkout, without adding a runtime dependency or build step.
for source in sorted((ROOT / "packages").glob("*/src"), reverse=True):
    sys.path.insert(0, str(source))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "core" / "tests"))
# The conformance module lives in ``tests/contract`` and imports its shared fixture
# builders from ``tests/``, so both directories must be importable here.
sys.path.insert(0, str(ROOT / "packages" / "strategies" / "tests"))
sys.path.insert(0, str(ROOT / "packages" / "strategies" / "tests" / "contract"))

# NOTE: the DAG/conformance test modules are imported inside ``main()`` so that
# importing this module (and running the stdlib-only manifest check below)
# never requires the heavy package dependencies.

# Stage-5 required keys for every packages/<name>/manifest.json
# (docs/stages/05-architecture.md, "Module contract").
REQUIRED_MANIFEST_KEYS = (
    "module_id",
    "release",
    "contract_version",
    "kind",
    "capabilities",
    "inputs",
    "outputs",
    "dependencies",
    "owner",
    "determinism",
)

_STRING_MANIFEST_KEYS = ("module_id", "release", "contract_version", "kind", "owner")
_NONEMPTY_LIST_MANIFEST_KEYS = ("capabilities", "inputs", "outputs")
_DEP_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _workspace_module_id(dist_name: str) -> str | None:
    normalized = _normalize_dist_name(dist_name)
    if normalized.startswith("msl-"):
        return f"moex-spread-lab.{normalized[4:]}"
    return None


def _package_dirs(root: Path) -> list[Path]:
    packages_dir = root / "packages"
    if not packages_dir.is_dir():
        return []
    return sorted(
        path
        for path in packages_dir.iterdir()
        if path.is_dir()
        and ((path / "manifest.json").is_file() or (path / "pyproject.toml").is_file())
    )


def _is_nonempty_str_list(value: Any) -> TypeGuard[list[str]]:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def _is_str_list(value: Any) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_manifest_shape(manifest: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        errors.append(f"{prefix}: missing required manifest keys: {', '.join(missing)}")
    for key in _STRING_MANIFEST_KEYS:
        value = manifest.get(key)
        if key in manifest and (not isinstance(value, str) or not value):
            errors.append(f"{prefix}: manifest key {key!r} must be a non-empty string")
    for key in _NONEMPTY_LIST_MANIFEST_KEYS:
        if key in manifest and not _is_nonempty_str_list(manifest.get(key)):
            errors.append(f"{prefix}: manifest key {key!r} must be a non-empty list of strings")
    if "dependencies" in manifest and not _is_str_list(manifest.get("dependencies")):
        errors.append(f"{prefix}: manifest key 'dependencies' must be a list of strings")
    if "determinism" in manifest and not isinstance(manifest.get("determinism"), dict):
        errors.append(f"{prefix}: manifest key 'determinism' must be an object")
    return errors


def _check_version(manifest: dict[str, Any], project: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    version = project.get("version")
    if not isinstance(version, str) or not version:
        errors.append(f"{prefix}: pyproject.toml [project].version missing")
        return errors
    release = manifest.get("release")
    if isinstance(release, str) and release != version:
        errors.append(f"{prefix}: release {release!r} != pyproject version {version!r}")
    if "version" in manifest and manifest["version"] != version:
        errors.append(f"{prefix}: version {manifest['version']!r} != pyproject version {version!r}")
    return errors


def _expected_workspace_deps(project: dict[str, Any], prefix: str) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    raw = project.get("dependencies", [])
    if not isinstance(raw, list) or not all(isinstance(dep, str) for dep in raw):
        errors.append(f"{prefix}: pyproject [project].dependencies must be a list of strings")
        return set(), errors
    expected: set[str] = set()
    for dep in raw:
        match = _DEP_NAME_RE.match(dep)
        if match is None:
            errors.append(f"{prefix}: unparseable pyproject dependency {dep!r}")
            continue
        module_id = _workspace_module_id(match.group(1))
        if module_id is not None:
            expected.add(module_id)
    return expected, errors


def _check_dependencies(
    manifest: dict[str, Any],
    project: dict[str, Any],
    known: set[str],
    prefix: str,
) -> list[str]:
    errors: list[str] = []
    expected, dep_errors = _expected_workspace_deps(project, prefix)
    errors.extend(dep_errors)
    actual = manifest.get("dependencies")
    if _is_str_list(actual):
        if len(set(actual)) != len(actual):
            errors.append(f"{prefix}: dependencies contain duplicates: {actual}")
        if set(actual) != expected:
            errors.append(
                f"{prefix}: dependencies {sorted(actual)} do not match "
                f"pyproject msl-* edges {sorted(expected)}"
            )
    for module_id in sorted(expected):
        package = module_id.removeprefix("moex-spread-lab.")
        if package not in known:
            errors.append(f"{prefix}: depends on unknown workspace package {package!r}")
    return errors


def _load_manifest(package_dir: Path, prefix: str) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        return None, [f"{prefix}: missing manifest.json (pyproject.toml present)"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{prefix}: manifest.json does not parse: {exc}"]
    if not isinstance(manifest, dict):
        return None, [f"{prefix}: manifest.json must be a JSON object"]
    return manifest, []


def _load_project(package_dir: Path, prefix: str) -> tuple[dict[str, Any] | None, list[str]]:
    pyproject_path = package_dir / "pyproject.toml"
    if not pyproject_path.is_file():
        return None, [f"{prefix}: missing pyproject.toml (manifest.json present)"]
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, [f"{prefix}: pyproject.toml does not parse: {exc}"]
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return None, [f"{prefix}: pyproject.toml missing [project] table"]
    return project, []


def _validate_package(package_dir: Path, known: set[str]) -> list[str]:
    name = package_dir.name
    prefix = f"packages/{name}"
    manifest, manifest_errors = _load_manifest(package_dir, prefix)
    project, project_errors = _load_project(package_dir, prefix)
    errors = [*manifest_errors, *project_errors]
    if manifest is None or project is None:
        return errors
    errors.extend(_check_manifest_shape(manifest, prefix))
    module_id = manifest.get("module_id")
    if isinstance(module_id, str) and module_id != f"moex-spread-lab.{name}":
        errors.append(f"{prefix}: module_id {module_id!r} != expected 'moex-spread-lab.{name}'")
    errors.extend(_check_version(manifest, project, prefix))
    errors.extend(_check_dependencies(manifest, project, known, prefix))
    return errors


def validate_package_manifests(root: Path = ROOT) -> list[str]:
    package_dirs = _package_dirs(root)
    known = {path.name for path in package_dirs}
    errors: list[str] = []
    for package_dir in package_dirs:
        errors.extend(_validate_package(package_dir, known))
    return errors


def _run_manifest_check() -> bool:
    errors = validate_package_manifests()
    if errors:
        for error in errors:
            print(f"architecture-gate: {error}", file=sys.stderr)
        return False
    print("architecture-gate: package manifests conform")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="import-graph tests only; skip plugin-conformance discovery, the "
        "package-manifest conformance check and the secret-scan CLI check "
        "(for `check_health.sh --changed`)",
    )
    args = parser.parse_args()
    started = time.perf_counter()

    if not args.fast and not _run_manifest_check():
        return 1

    from test_architecture_dag import ArchitectureDagTests

    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ArchitectureDagTests))
    if not args.fast:
        from test_strategy_conformance import StrategyPluginConformanceTests

        suite.addTests(
            StrategyPluginConformanceTests(name)
            for name in (
                "test_auto_discovers_all_builtin_strategy_plugins",
                "test_synthetic_drop_in_strategy_folder_registers_without_core_edits",
            )
        )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1

    if not args.fast:
        from moex_spread_lab import cli

        commands = (["secret-scan", "--tracked-only"],)
        original_argv = sys.argv
        try:
            for command_args in commands:
                sys.argv = ["moex-spread-lab", *command_args]
                if cli.main() != 0:
                    return 1
        finally:
            sys.argv = original_argv
        print(f"architecture-gate: passed in {time.perf_counter() - started:.2f}s")
    else:
        print(
            f"architecture-gate: fast passed in {time.perf_counter() - started:.2f}s "
            "(plugin conformance and CLI checks skipped)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
