"""Regression tests for the dependency-free frontend module gate."""

from __future__ import annotations

import shutil

import pytest
from scripts import check_js_modules as gate

_ALIAS_CHAIN = {
    "base.js": "export const value = 1;\n",
    "middle.js": "export { value as renamed } from './base.js';\n",
    "use.js": "import { renamed as local } from './middle.js';\nlocal();\n",
}
# (module files, fragment pairs that must co-occur in one reported error each)
BROKEN_TREES = [
    (
        {"provider.js": "export function helper() {}\n", "consumer.js": "helper();\n"},
        (("consumer.js", "helper"),),
    ),
    (
        {**_ALIAS_CHAIN, "base.js": "export const other = 1;\n"},
        (("middle.js", "value"), ("use.js", "renamed")),
    ),
    (
        {
            "base.js": "export const x = 1;\n",
            "use.js": "import { x } from './base.js';\nimport { x as y } from './base.js';\n"
            "import { z } from './missing.js';\n",
        },
        (("duplicate import",), ("missing.js",)),
    ),
]


def _write_tree(directory, files):
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")


def test_current_modules_and_a_transitive_alias_chain_pass(tmp_path):
    assert gate.check_modules(gate.JS_DIR) == []
    _write_tree(tmp_path, _ALIAS_CHAIN)
    assert gate.check_modules(tmp_path, syntax=False) == []


@pytest.mark.parametrize(("files", "pairs"), BROKEN_TREES)
def test_broken_trees_report_each_binding_error(tmp_path, files, pairs):
    _write_tree(tmp_path, files)
    errors = gate.check_modules(tmp_path, syntax=False)
    for pair in pairs:
        assert any(all(fragment in error for fragment in pair) for error in errors), errors


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_syntax_check_covers_every_module(tmp_path):
    _write_tree(tmp_path, {"a.js": "export const ok = 1;\n", "z.js": "const bad = ;\n"})
    errors = gate.check_modules(tmp_path)
    assert any("z.js" in error and "syntax" in error.lower() for error in errors)
