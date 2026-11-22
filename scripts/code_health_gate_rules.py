"""Pure ratchet and policy checks used by the code-health gate."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

if __package__:
    from .code_health_gate_common import (
        ALLOW_MARKER,
        POLICY,
        PROTECTED_EXCEPTION_ROOTS,
        ROOT,
        GateError,
        _file_metrics,
        _relative,
    )
else:
    from code_health_gate_common import (
        ALLOW_MARKER,
        POLICY,
        PROTECTED_EXCEPTION_ROOTS,
        ROOT,
        GateError,
        _file_metrics,
        _relative,
    )


def _fastcore_exports(c_source: str) -> tuple[list[str], str | None]:
    table_start = c_source.find("FastCoreMethods[]")
    body_start = c_source.find("{", table_start)
    table_end = c_source.find("};", body_start)
    if min(table_start, body_start, table_end) < 0:
        return [], "_fastcore.c: FastCoreMethods table was not found"
    names = re.findall(r'\{\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,', c_source[body_start:table_end])
    if not names:
        return [], "_fastcore.c: FastCoreMethods table contains no exported methods"
    return names, None


def _parity_inventory(
    test_tree: ast.Module, test_path: Path
) -> tuple[dict[str, str] | None, str | None]:
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
        return (
            None,
            f"{test_path.relative_to(ROOT)}: FASTCORE_EXPORT_PARITY_TESTS inventory is missing",
        )
    try:
        inventory = ast.literal_eval(inventory_node.value)
    except (ValueError, TypeError):
        return None, f"{test_path.relative_to(ROOT)}: parity inventory must be a literal mapping"
    if not isinstance(inventory, dict) or not all(
        isinstance(name, str) and isinstance(target, str) for name, target in inventory.items()
    ):
        return (
            None,
            f"{test_path.relative_to(ROOT)}: parity inventory must map export names to test targets",
        )
    return inventory, None


def _parity_target_exists(relative: str, selector: str) -> bool:
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
    class_name, function_name = selector.rsplit(".", 1)
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == class_name
        and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == function_name
            for member in node.body
        )
        for node in tree.body
    )


def _fastcore_parity_failures() -> list[str]:
    c_path = ROOT / "packages/core/src/moex_spread_lab/core/_fastcore.c"
    test_path = ROOT / "packages/proof/tests/test_fastcore_parity.py"
    try:
        c_source = c_path.read_text(encoding="utf-8")
        test_tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise GateError(f"cannot read or parse fastcore parity sources: {exc}") from exc
    exports, error = _fastcore_exports(c_source)
    if error:
        return [error]
    inventory, error = _parity_inventory(test_tree, test_path)
    if error or inventory is None:
        return [error or "fastcore parity inventory is malformed"]

    failures = []
    missing = sorted(set(exports) - inventory.keys())
    extra = sorted(inventory.keys() - set(exports))
    if missing:
        failures.append(
            f"{test_path.relative_to(ROOT)}: add parity tests for C exports {', '.join(missing)}"
        )
    if extra:
        failures.append(
            f"{test_path.relative_to(ROOT)}: remove stale C exports {', '.join(extra)} from parity inventory"
        )
    for export in sorted(set(exports) & inventory.keys()):
        target = inventory[export]
        if "::" not in target:
            failures.append(
                f"{test_path.relative_to(ROOT)}: invalid parity test target for {export}: {target}"
            )
            continue
        relative, selector = target.split("::", 1)
        if not _parity_target_exists(relative, selector):
            failures.append(f"{export}: parity test target does not exist: {target}")
    return failures


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


def _source_exception_failures(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise GateError(f"cannot parse protected Python source {path}: {exc}") from exc
    lines = text.splitlines()
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _broad_exception(node.type):
            continue
        line = lines[node.lineno - 1]
        previous = lines[node.lineno - 2] if node.lineno > 1 else ""
        if ALLOW_MARKER not in line and ALLOW_MARKER not in previous:
            failures.append(
                f"{_relative(path)}:{node.lineno}: broad exception needs an inline "
                f"justification containing {ALLOW_MARKER!r}"
            )
    return failures


def _exception_failures() -> list[str]:
    return [
        failure
        for root in PROTECTED_EXCEPTION_ROOTS
        for path in sorted(root.rglob("*.py"))
        for failure in _source_exception_failures(path)
    ]


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


def _new_file_failures(path: Path, relative: str, metrics: dict[str, int]) -> list[str]:
    failures = []
    is_c = path.suffix.lower() == ".c"
    limit = POLICY["new_c_file_max_loc"] if is_c else POLICY["new_file_max_loc"]
    if metrics["loc"] > limit:
        failures.append(f"{relative}: new file has {metrics['loc']} LOC (limit {limit})")
    if not is_c and metrics["tokens"] > POLICY["new_file_max_tokens"]:
        failures.append(
            f"{relative}: new file has about {metrics['tokens']} tokens "
            f"(limit {POLICY['new_file_max_tokens']})"
        )
    return failures


def _check_files(
    files: list[Path], baseline: dict[str, Any]
) -> tuple[list[str], dict[str, dict[str, int]]]:
    failures = []
    current = {}
    base_files = baseline["files"]
    guard_files = set(baseline["guard_files"])
    for path in files:
        relative = _relative(path)
        metrics = _file_metrics(path)
        current[relative] = metrics
        old = base_files.get(relative)
        if old is None:
            failures.extend(_new_file_failures(path, relative, metrics))
        else:
            failures.extend(_existing_file_failures(relative, metrics, old))
    failures.extend(
        f"guarded compatibility file is missing: {relative}"
        for relative in sorted(guard_files - current.keys())
    )
    return failures, current


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


def _updated_baseline(
    baseline: dict[str, Any],
    file_metrics: dict[str, dict[str, int]],
    complexity: dict[str, int],
    c_functions: dict[str, dict[str, int]],
) -> dict[str, Any]:
    updated = dict(baseline)
    updated["files"] = {}
    for path, old in baseline["files"].items():
        if path not in file_metrics:
            continue
        is_c = path.lower().endswith(".c")
        loc_limit = POLICY["new_c_file_max_loc"] if is_c else POLICY["new_file_max_loc"]
        new_loc = min(int(old["baseline_loc"]), file_metrics[path]["loc"])
        new_tokens = min(int(old["baseline_tokens"]), file_metrics[path]["tokens"])
        if new_loc <= loc_limit and (is_c or new_tokens <= POLICY["new_file_max_tokens"]):
            continue
        updated["files"][path] = {
            "baseline_loc": new_loc,
            "baseline_tokens": new_tokens,
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
    return updated
