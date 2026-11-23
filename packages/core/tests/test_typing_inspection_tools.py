from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import code_inspect, codemod_imports, typecheck_workspace


def _namespace_fixture(root: Path) -> None:
    for relative in (
        "packages/core/src/moex_spread_lab/core",
        "packages/data/src/moex_spread_lab/data",
        "packages/accounting/src/moex_spread_lab/accounting",
        "packages/strategies/src/moex_spread_lab/strategies",
        "packages/proof/src/moex_spread_lab/proof_runtime",
        "packages/web/src/moex_spread_lab/web",
        "src/moex_spread_lab",
    ):
        directory = root / relative
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("\n", encoding="utf-8")


def test_typecheck_view_unifies_namespace_roots_and_is_reproducible(tmp_path: Path) -> None:
    _namespace_fixture(tmp_path)

    view = typecheck_workspace.build_view(tmp_path)
    first = sorted((view / "moex_spread_lab").iterdir())
    assert {path.name for path in first} == {
        "__init__.py",
        "accounting",
        "core",
        "data",
        "proof_runtime",
        "strategies",
        "web",
    }
    assert (view / "moex_spread_lab/core").resolve() == (
        tmp_path / "packages/core/src/moex_spread_lab/core"
    )

    stale = view / "moex_spread_lab/stale.py"
    stale.symlink_to(tmp_path / "src/moex_spread_lab/__init__.py")
    typecheck_workspace.build_view(tmp_path)
    assert not stale.exists()


def test_typecheck_view_refuses_to_delete_unmanaged_directory(tmp_path: Path) -> None:
    _namespace_fixture(tmp_path)
    generated = tmp_path / ".typecheck_typings"
    generated.mkdir()
    marker = generated / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(typecheck_workspace.ViewError, match="unmarked"):
        typecheck_workspace.build_view(tmp_path)
    assert marker.read_text(encoding="utf-8") == "user data"


def test_typecheck_view_reports_namespace_collisions(tmp_path: Path) -> None:
    _namespace_fixture(tmp_path)
    duplicate = tmp_path / "packages/data/src/moex_spread_lab/core"
    duplicate.mkdir()
    with pytest.raises(typecheck_workspace.ViewError, match="duplicate namespace"):
        typecheck_workspace.build_view(tmp_path)


def test_inspector_counts_function_complexity_without_nested_functions() -> None:
    tree = ast.parse("""
def outer(value):
    if value and value > 0:
        def inner(other):
            if other:
                return 1
        return inner(value)
    return 0
""")
    outer, inner = code_inspect._function_nodes(tree)
    assert code_inspect._complexity(outer) == 3
    assert code_inspect._complexity(inner) == 2


def test_inspector_strict_limit_detects_a_ceiling_breach(tmp_path: Path) -> None:
    path = tmp_path / "too_complex.py"
    branches = "\n".join(f"    if value == {index}: return {index}" for index in range(17))
    path.write_text(f"def choose(value):\n{branches}\n    return -1\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/code_inspect.py",
            "check",
            str(path),
            "--metrics-only",
            "--strict-limits",
        ],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "complexity ceiling 15" in result.stdout


def test_import_codemod_is_ast_scoped_and_idempotent() -> None:
    source = (
        'from old.package import Item\ntext = "from old.package import Item"\nfrom . import local\n'
    )
    rewritten = codemod_imports.rewrite_imports(source, {"old.package": "new.package"})

    assert (
        rewritten
        == 'from new.package import Item\ntext = "from old.package import Item"\nfrom . import local\n'
    )
    assert codemod_imports.rewrite_imports(rewritten, {"old.package": "new.package"}) == rewritten


def test_reexport_shim_rejects_invalid_module_and_keyword_symbols() -> None:
    with pytest.raises(ValueError, match="dotted Python identifier"):
        codemod_imports.render_shim("safe.module\nimport os", ["Thing"])
    with pytest.raises(ValueError, match="Python identifiers"):
        codemod_imports.render_shim("safe.module", ["class"])


def test_reexport_shim_is_deterministic_and_idempotent() -> None:
    first = codemod_imports.render_shim("new.module", ["Thing", "helper", "Thing"])
    second = codemod_imports.render_shim("new.module", ["Thing", "helper"])

    assert first == second
    assert "from new.module import (" in first
    assert first.count("    Thing,") == 1
