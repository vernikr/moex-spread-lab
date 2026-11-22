"""Regression tests for the dependency-free frontend module gate."""

from __future__ import annotations

import shutil

import pytest
from scripts import check_js_modules as gate


@pytest.mark.parametrize(
    ("filename", "symbol"),
    [
        ("picker.js", "$"),
        ("ledger-cells.js", "state"),
        ("strategy-plans.js", "activeStrategyName"),
        ("ledger.js", "td"),
    ],
)
def test_missing_import_regressions(tmp_path, filename, symbol):
    shutil.copytree(gate.JS_DIR, tmp_path / "js")
    directory = tmp_path / "js"
    source = directory / filename
    text = source.read_text(encoding="utf-8")
    for match in gate.IMPORT.finditer(text):
        items = gate.bindings(match.group(1))
        if any(alias == symbol for _, alias in items):
            kept = [
                f"{name} as {alias}" if name != alias else name
                for name, alias in items
                if alias != symbol
            ]
            replacement = f"import {{ {', '.join(kept)} }} from '{match.group(2)}';" if kept else ""
            source.write_text(
                text[: match.start()] + replacement + text[match.end() :], encoding="utf-8"
            )
            break
    else:
        pytest.fail(f"{symbol} not imported by {filename}")
    assert any(
        filename in error and symbol in error
        for error in gate.check_modules(directory, syntax=False)
    )


def test_transitive_aliases_and_missing_exports(tmp_path):
    (tmp_path / "base.js").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "middle.js").write_text(
        "export { value as renamed } from './base.js';\n", encoding="utf-8"
    )
    (tmp_path / "use.js").write_text(
        "import { renamed as local } from './middle.js';\nlocal();\n", encoding="utf-8"
    )
    assert gate.check_modules(tmp_path, syntax=False) == []
    (tmp_path / "base.js").write_text("export const other = 1;\n", encoding="utf-8")
    errors = gate.check_modules(tmp_path, syntax=False)
    assert any("middle.js" in error and "value" in error for error in errors)
    assert any("use.js" in error and "renamed" in error for error in errors)


def test_duplicate_imports_and_missing_module(tmp_path):
    (tmp_path / "base.js").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "use.js").write_text(
        "import { x } from './base.js';\nimport { x as y } from './base.js';\n"
        "import { z } from './missing.js';\n",
        encoding="utf-8",
    )
    errors = gate.check_modules(tmp_path, syntax=False)
    assert any("duplicate import" in error for error in errors)
    assert any("missing.js" in error for error in errors)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_syntax_check_covers_every_module(tmp_path):
    (tmp_path / "a.js").write_text("export const ok = 1;\n", encoding="utf-8")
    (tmp_path / "z.js").write_text("const bad = ;\n", encoding="utf-8")
    assert any(
        "z.js" in error and "syntax" in error.lower() for error in gate.check_modules(tmp_path)
    )
