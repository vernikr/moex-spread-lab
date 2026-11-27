"""Pure ratchet and policy checks used by the code-health gate."""

import ast
import re
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.code_health_gate_common import (
    ALLOW_MARKER,
    EXCLUDED_PARTS,
    POLICY,
    PROTECTED_EXCEPTION_ROOTS,
    ROOT,
    GateError,
    _budget_for,
    _file_metrics,
    _fragment_name_problem,
    _is_compatibility_shim,
    _package_group,
    _package_root,
    _relative,
)


def _fastcore_parity_failures() -> list[str]:
    c_path = ROOT / "packages/core/src/moex_spread_lab/core/_fastcore.c"
    test_path = ROOT / "packages/proof/tests/test_fastcore_parity.py"
    try:
        c_source = c_path.read_text(encoding="utf-8")
        test_tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise GateError(f"cannot read or parse fastcore parity sources: {exc}") from exc
    table_start = c_source.find("FastCoreMethods[]")
    body_start = c_source.find("{", table_start)
    table_end = c_source.find("};", body_start)
    if min(table_start, body_start, table_end) < 0:
        return ["_fastcore.c: FastCoreMethods table was not found"]
    exports = re.findall(r'\{\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,', c_source[body_start:table_end])
    if not exports:
        return ["_fastcore.c: FastCoreMethods table contains no exported methods"]
    rel = test_path.relative_to(ROOT)
    inventory_node = next(
        (
            node
            for node in test_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FASTCORE_EXPORT_PARITY_TESTS"
                for target in node.targets
            )
        ),
        None,
    )
    if inventory_node is None:
        return [f"{rel}: FASTCORE_EXPORT_PARITY_TESTS inventory is missing"]
    try:
        inventory = ast.literal_eval(inventory_node.value)
    except (ValueError, TypeError):
        return [f"{rel}: parity inventory must be a literal mapping"]
    if not isinstance(inventory, dict) or not all(
        isinstance(name, str) and isinstance(target, str) for name, target in inventory.items()
    ):
        return [f"{rel}: parity inventory must map export names to test targets"]
    failures = []
    missing = sorted(set(exports) - set(inventory))
    extra = sorted(set(inventory) - set(exports))
    if missing:
        failures.append(f"{rel}: add parity tests for C exports {', '.join(missing)}")
    if extra:
        failures.append(f"{rel}: remove stale C exports {', '.join(extra)} from parity inventory")
    for export in sorted(set(exports) & set(inventory)):
        target = inventory[export]
        if "::" not in target:
            failures.append(f"{rel}: invalid parity test target for {export}: {target}")
            continue
        relative, selector = target.split("::", 1)
        if not _target_exists(relative, selector):
            failures.append(f"{export}: parity test target does not exist: {target}")
    return failures


def _target_exists(relative: str, selector: str) -> bool:
    if not selector or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return False
    path = ROOT / relative
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return False
    if "." not in selector:
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == selector
            for node in tree.body
        )
    cls, fn = selector.rsplit(".", 1)
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == cls
        and any(
            isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == fn
            for m in node.body
        )
        for node in tree.body
    )


def _broad_exception(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"Exception", "BaseException"}
    if isinstance(node, ast.Tuple):
        return any(_broad_exception(item) for item in node.elts)
    return False


def _exception_failures(paths: list[Path] | None = None) -> list[str]:
    if paths is None:
        candidates = [
            path for root in PROTECTED_EXCEPTION_ROOTS for path in sorted(root.rglob("*.py"))
        ]
    else:
        candidates = [
            path
            for path in sorted(paths)
            if any(path == root or root in path.parents for root in PROTECTED_EXCEPTION_ROOTS)
        ]
    failures: list[str] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise GateError(f"cannot parse protected Python source {path}: {exc}") from exc
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not _broad_exception(node.type):
                continue
            nearby = "\n".join(lines[max(0, node.lineno - 2) : node.lineno])
            if ALLOW_MARKER not in nearby:
                failures.append(
                    f"{_relative(path)}:{node.lineno}: broad exception needs an inline "
                    f"justification containing {ALLOW_MARKER!r}"
                )
    return failures


def _existing_file_failures(
    relative: str, metrics: dict[str, int], old: dict[str, Any]
) -> list[str]:
    failures = []
    for name in ("loc", "tokens"):
        ceiling = int(old[f"baseline_{name}"])
        if metrics[name] > ceiling:
            failures.append(
                f"{relative}: {name} grew from {ceiling} to {metrics[name]} (ratchet allows no growth)"
            )
        elif name == "loc" and metrics[name] <= ceiling * 0.9:
            print(f"baseline improvement available: {relative} ({name} fell by >=10%)")
    return failures


def _budget_outcome(relative: str, metrics: dict[str, int]) -> tuple[list[str], list[str]]:
    archetype, budget = _budget_for(relative)
    failures: list[str] = []
    advisories: list[str] = []
    for measure in ("loc", "tokens"):
        hard = budget.get(f"hard_{measure}")
        if hard is None:
            continue
        value = metrics[measure]
        soft = budget[f"soft_{measure}"]
        if value > hard:
            failures.append(
                f"{relative}: {value} {measure} exceed the {archetype} hard ceiling {hard}; "
                "split it along a real domain seam, never into pass-through shards"
            )
        elif value > soft:
            advisories.append(
                f"{relative}: {value} {measure} in the {archetype} advisory band "
                f"(soft {soft}, hard {hard})"
            )
    return failures, advisories


def _fragment_name_failures(files: list[Path], baseline: dict[str, Any]) -> list[str]:
    legacy = {str(path) for path in baseline["legacy_fragment_files"]}
    failures = []
    for path in files:
        problem = _fragment_name_problem(path.name)
        relative = _relative(path)
        if problem is None or relative in legacy:
            continue
        failures.append(
            f"{relative}: forbidden overflow filename ({problem}); keep the code in its "
            "cohesive module instead of sharding it by issue or path"
        )
    return failures


def _intra_package_callers(path: Path, relative: str) -> int:
    package_root = _package_root(relative)
    if package_root is None or not package_root.is_dir():
        return 0
    pattern = re.compile(rf"^\s*(?:from|import)\s+.*\b{re.escape(path.stem)}\b")
    callers = 0
    for candidate in package_root.rglob("*.py"):
        if candidate == path or EXCLUDED_PARTS.intersection(candidate.parts):
            continue
        try:
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(pattern.match(line) for line in source.splitlines()):
            callers += 1
    return callers


def _shallow_shard_paths(files: list[Path], file_metrics: dict[str, dict[str, int]]) -> list[str]:
    max_loc = int(POLICY["shallow_shard_max_loc"])
    max_callers = int(POLICY["shallow_shard_max_callers"])
    flagged = []
    for path in sorted(files):
        relative = _relative(path)
        if (
            file_metrics[relative]["loc"] >= max_loc
            or path.suffix.lower() != ".py"
            or path.name == "__init__.py"
            or "tests" in PurePosixPath(relative).parts
            or _is_compatibility_shim(relative)
        ):
            continue
        if _intra_package_callers(path, relative) <= max_callers:
            flagged.append(relative)
    return flagged


def _shallow_shard_advisories(
    files: list[Path], file_metrics: dict[str, dict[str, int]], baseline: dict[str, Any]
) -> list[str]:
    legacy = {str(path) for path in baseline["legacy_shallow_shards"]}
    return [
        f"{relative}: {file_metrics[relative]['loc']} LOC module with at most "
        f"{POLICY['shallow_shard_max_callers']} intra-package caller(s); fold it back into a "
        "deep module unless it is a real seam"
        for relative in _shallow_shard_paths(files, file_metrics)
        if relative not in legacy
    ]


def _package_totals(file_metrics: dict[str, dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for relative, metrics in file_metrics.items():
        if (group := _package_group(relative)) is not None:
            totals[group] = totals.get(group, 0) + metrics["tokens"]
    return totals


def _package_token_advisories(totals: dict[str, int], baseline: dict[str, Any]) -> list[str]:
    recorded = baseline["packages"]
    tolerance = int(POLICY["package_token_tolerance_pct"])
    advisories = []
    for group, total in sorted(totals.items()):
        old = recorded.get(group)
        if old is None:
            advisories.append(f"{group}: {total} tokens with no recorded package total")
            continue
        ceiling = int(old["baseline_tokens"])
        if total > ceiling * (100 + tolerance) // 100:
            advisories.append(
                f"{group}: source tokens grew from {ceiling} to {total} "
                f"(+{total - ceiling}); a split must lower the package total"
            )
    return advisories


def _check_files(
    files: list[Path], baseline: dict[str, Any], check_guard: bool = True
) -> tuple[list[str], dict[str, dict[str, int]], list[str]]:
    failures = []
    advisories = []
    current = {}
    base_files = baseline["files"]
    guard_files = set(baseline["guard_files"])
    for path in files:
        relative = _relative(path)
        metrics = _file_metrics(path)
        current[relative] = metrics
        old = base_files.get(relative)
        if old is not None:
            failures.extend(_existing_file_failures(relative, metrics, old))
            continue
        file_failures, file_advisories = _budget_outcome(relative, metrics)
        failures.extend(file_failures)
        advisories.extend(file_advisories)
    if check_guard:
        failures.extend(
            f"guarded compatibility file is missing: {relative}"
            for relative in sorted(guard_files - current.keys())
        )
    return failures, current, advisories


def _complexity_failures(current: dict[str, int], baseline: dict[str, Any]) -> list[str]:
    failures = []
    ceilings = baseline["complexity"]
    limit = POLICY["max_cognitive_complexity"]
    for key, score in sorted(current.items()):
        ceiling = ceilings.get(key)
        if ceiling is None and score > limit:
            failures.append(f"{key}: new cognitive complexity {score} exceeds {limit}")
        elif ceiling is not None and score > int(ceiling):
            failures.append(f"{key}: cognitive complexity grew from {ceiling} to {score}")
    return failures


def _c_function_failures(current: dict[str, dict[str, int]], baseline: dict[str, Any]) -> list[str]:
    failures = []
    ceilings = baseline["c_functions"]
    limits = {
        "nloc": POLICY["max_c_function_nloc"],
        "ccn": POLICY["max_c_function_ccn"],
        "parameters": POLICY["max_c_function_parameters"],
    }
    for key, metrics in sorted(current.items()):
        ceiling = ceilings.get(key)
        if ceiling is None:
            breaches = [
                f"{name}={value}>{limits[name]}"
                for name, value in metrics.items()
                if value > limits[name]
            ]
            if breaches:
                failures.append(f"{key}: new C function exceeds limits ({', '.join(breaches)})")
            continue
        for name, value in metrics.items():
            old = int(ceiling[f"baseline_{name}"])
            if value > old:
                failures.append(f"{key}: C {name} grew from {old} to {value}")
    return failures


def _baseline_entry_is_obsolete(relative: str, metrics: dict[str, int]) -> bool:
    archetype, budget = _budget_for(relative)
    if archetype == "kernel":
        # The C99 kernel is one translation unit: keep its LOC ratchet above the soft target.
        return metrics["loc"] <= budget["soft_loc"]
    return metrics["loc"] <= budget["hard_loc"] and metrics["tokens"] <= budget["hard_tokens"]


def _updated_baseline(
    baseline: dict[str, Any],
    file_metrics: dict[str, dict[str, int]],
    complexity: dict[str, int],
    c_functions: dict[str, dict[str, int]],
    files: list[Path] | None = None,
) -> dict[str, Any]:
    updated = dict(baseline)
    updated["files"] = {}
    for path, old in baseline["files"].items():
        metrics = file_metrics.get(path)
        if metrics is None or _baseline_entry_is_obsolete(path, metrics):
            continue
        updated["files"][path] = {
            "baseline_loc": min(int(old["baseline_loc"]), metrics["loc"]),
            "baseline_tokens": min(int(old["baseline_tokens"]), metrics["tokens"]),
        }
    limit = POLICY["max_cognitive_complexity"]
    updated["complexity"] = {
        key: min(int(old), complexity[key])
        for key, old in baseline["complexity"].items()
        if key in complexity and complexity[key] > limit
    }
    limits = {
        "nloc": POLICY["max_c_function_nloc"],
        "ccn": POLICY["max_c_function_ccn"],
        "parameters": POLICY["max_c_function_parameters"],
    }
    updated["c_functions"] = {}
    for key, old in baseline["c_functions"].items():
        metrics = c_functions.get(key)
        if metrics is None or all(metrics[name] <= limits[name] for name in limits):
            continue
        updated["c_functions"][key] = {
            f"baseline_{name}": min(int(old[f"baseline_{name}"]), metrics[name]) for name in limits
        }
    offenders = {
        relative
        for relative in file_metrics
        if _fragment_name_problem(PurePosixPath(relative).name) is not None
    }
    updated["legacy_fragment_files"] = sorted(set(baseline["legacy_fragment_files"]) & offenders)
    shards: set[str] | None = None
    if files is not None:
        shards = set(_shallow_shard_paths(files, file_metrics))
        updated["legacy_shallow_shards"] = sorted(set(baseline["legacy_shallow_shards"]) & shards)
    updated["packages"] = {
        group: {
            "baseline_tokens": total
            if (old := baseline["packages"].get(group)) is None
            else min(int(old["baseline_tokens"]), total)
        }
        for group, total in _package_totals(file_metrics).items()
    }
    return updated
