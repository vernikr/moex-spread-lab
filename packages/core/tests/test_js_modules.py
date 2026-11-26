"""Regression tests for the dependency-free frontend module gate."""

from __future__ import annotations

import shutil

import pytest
from scripts import check_js_modules as gate


def test_current_modules_pass_the_gate():
    assert gate.check_modules(gate.JS_DIR) == []


def test_missing_sibling_import_is_detected(tmp_path):
    (tmp_path / "provider.js").write_text("export function helper() {}\n", encoding="utf-8")
    (tmp_path / "consumer.js").write_text("helper();\n", encoding="utf-8")

    errors = gate.check_modules(tmp_path, syntax=False)

    assert any("consumer.js" in error and "helper" in error for error in errors)


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
