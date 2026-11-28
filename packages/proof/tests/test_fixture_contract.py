"""Synthetic workspace snapshots and fixture import-direction contracts."""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from funding_fixtures import fixture as funding_fixture
from funding_fixtures import weekend
from proof_fixtures import fixture, two_calendar_windows
from test_calendar_proof import _build_root as calendar_root
from test_execution_proof import _build_root as pair_root


def _snapshot(root: Path) -> tuple[int, str]:
    files = []
    for path in sorted(root.rglob("*.json")):
        document = json.loads(path.read_text())
        document.pop("cached_at", None)
        files.append((str(path.relative_to(root)), document))
    content = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return len(files), hashlib.sha256(content).hexdigest()


def _funding_weekend(root: Path) -> None:
    funding_fixture(root)
    weekend(root)


@pytest.mark.parametrize(
    ("builder", "count", "digest"),
    [
        (pair_root, 6, "d9214649bfe00267c2a78e8077649a1db2bb99dff0ad9a76f193d0098191acdd"),
        (calendar_root, 6, "f4bd59c80d7523b98a0cc82d88c838b270cd06221ff96b6d20086b495a0aa96b"),
        (fixture, 10, "f0019ab45d19d2a44bee34c99ab659442404fe6f26e0e28ab06c4a9cb6dc9621"),
        (
            two_calendar_windows,
            12,
            "ce25f9d88a12ab704cc7649a9a6194043070227e43886fdf417112136083ba94",
        ),
        (_funding_weekend, 10, "ad54bd622fc9a79be2758d63c204c7db70126482189883bfbfa42c86a538c79d"),
    ],
)
def test_synthetic_workspace_content_contract(tmp_path, builder, count, digest):
    builder(tmp_path)
    assert _snapshot(tmp_path) == (count, digest)


def _test_imports(source: str) -> list[str]:
    violations = []
    for node in ast.walk(ast.parse(source)):
        names = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        violations.extend(name for name in names if name.split(".")[-1].startswith("test_"))
    return violations


def test_fixture_import_rule_detects_test_modules():
    assert _test_imports("from test_execution_proof import _build_root") == ["test_execution_proof"]
    assert _test_imports("import package.test_calendar_proof") == ["package.test_calendar_proof"]
    assert _test_imports("from workspace_fixtures import build_pair_root") == []
