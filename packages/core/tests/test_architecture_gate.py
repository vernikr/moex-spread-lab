"""Positive and negative tests for the repository-wide architecture gate: package-manifest
conformance and the tracked-file secret scan. Negative cases use synthetic trees under
``tmp_path``."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import architecture_gate

from moex_spread_lab.core.w0 import W0Error, scan_secrets

ROOT = Path(__file__).resolve().parents[3]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(name: str = "core", **overrides: Any) -> dict[str, Any]:
    return {
        "module_id": f"moex-spread-lab.{name}",
        "release": "0.1.0",
        "contract_version": f"{name}.v1",
        "kind": "contracts",
        "capabilities": ["synthetic_capability"],
        "inputs": ["synthetic-input.v1"],
        "outputs": ["synthetic-output.v1"],
        "dependencies": [],
        "owner": "project-core",
        "determinism": {"required": True},
    } | overrides


def _pyproject(name: str = "core", version: str = "0.1.0", deps: tuple[str, ...] = ()) -> str:
    rendered = ", ".join(json.dumps(dep) for dep in deps)
    return f'[project]\nname = "msl-{name}"\nversion = "{version}"\ndependencies = [{rendered}]\n'


def _package(root: Path, name: str = "core", manifest: Any = None, pyproject: Any = "") -> Path:
    """Write ``packages/<name>``; ``manifest``/``pyproject`` of ``None`` omit the file,
    a ``str`` manifest is written raw, and the empty-string pyproject means the default."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    if manifest is not None:
        if isinstance(manifest, str):
            (package / "manifest.json").write_text(manifest, encoding="utf-8")
        else:
            _write_json(package / "manifest.json", manifest)
    if pyproject is not None:
        text = _pyproject(name) if pyproject == "" else pyproject
        (package / "pyproject.toml").write_text(text, encoding="utf-8")
    return package


def _errors(root: Path) -> list[str]:
    return architecture_gate.validate_package_manifests(root)


def test_conforming_trees_pass(tmp_path: Path) -> None:
    assert _errors(ROOT) == []
    _package(tmp_path, "data", _manifest("data"))
    _package(
        tmp_path,
        "core",
        _manifest(dependencies=["moex-spread-lab.data"]),
        _pyproject(deps=("msl-data>=0.1.0",)),
    )
    assert _errors(tmp_path) == []
    assert architecture_gate._workspace_module_id("MSL_Data") == "moex-spread-lab.data"
    assert architecture_gate._workspace_module_id("requests") is None


# (manifest, pyproject, expected error fragment): one row per gate rule. ``...`` keeps the
# default manifest; ``MISSING`` omits the file.
MISSING = None
MANIFEST_FAILURES = [
    (MISSING, "", "missing manifest.json"),
    ("{not json", "", "does not parse"),
    ("[1, 2]", "", "must be a JSON object"),
    (..., MISSING, "missing pyproject.toml"),
    (..., "this is = = not toml", "pyproject.toml does not parse"),
    (..., "[tool.other]\nx = 1\n", "missing [project] table"),
    (..., '[project]\nname = "msl-core"\n', "[project].version missing"),
    (..., '[project]\nname = "msl-core"\nversion = "0.1.0"\ndependencies = 5\n', "must be a list"),
    (..., _pyproject(deps=("!!!",)), "unparseable pyproject dependency"),
    (..., _pyproject(deps=("msl-data",)), "do not match"),
    ({"owner": None}, "", "missing required manifest keys: owner"),
    ({"owner": ""}, "", "key 'owner' must be a non-empty string"),
    ({"capabilities": []}, "", "'capabilities' must be a non-empty list of strings"),
    ({"dependencies": [1]}, "", "'dependencies' must be a list of strings"),
    ({"determinism": ["required"]}, "", "'determinism' must be an object"),
    ({"module_id": "moex-spread-lab.other"}, "", "module_id 'moex-spread-lab.other'"),
    ({"release": "9.9.9"}, "", "release '9.9.9' != pyproject version '0.1.0'"),
    ({"version": "9.9.9"}, "", "version '9.9.9' != pyproject version"),
    ({"dependencies": ["moex-spread-lab.data"] * 2}, "", "dependencies contain duplicates"),
    (
        {"dependencies": ["moex-spread-lab.ghost"]},
        _pyproject(deps=("msl-ghost",)),
        "depends on unknown workspace package 'ghost'",
    ),
]


@pytest.mark.parametrize(("manifest", "pyproject", "expected"), MANIFEST_FAILURES)
def test_each_manifest_rule_fails_once(
    tmp_path: Path, manifest: Any, pyproject: Any, expected: str
):
    if isinstance(manifest, dict):
        manifest = {k: v for k, v in _manifest(**manifest).items() if v is not None}
    elif manifest is ...:
        manifest = _manifest()
    _package(tmp_path, manifest=manifest, pyproject=pyproject)
    assert any(expected in error for error in _errors(tmp_path)), _errors(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        b"token = ghp_" + b"a" * 36,
        b"github_pat_" + b"b" * 22,
        b"AKIA" + b"A" * 16,
        b"-----BEGIN OPENSSH" + b" PRIVATE KEY-----\n",
        b"https://user:" + b"pass@internal.example.com/x\n",
        b"x-access-token:" + b"c" * 24,
    ],
    ids=["github-pat", "fine-grained-pat", "aws-key", "private-key", "url-credentials", "x-access"],
)
def test_secret_scan_detects_each_pattern(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / "sample.txt").write_bytes(payload)
    assert scan_secrets(tmp_path)


def test_secret_scan_skips_clean_large_and_untracked_files(tmp_path: Path) -> None:
    (tmp_path / "clean.txt").write_text("nothing sensitive here\n", encoding="utf-8")
    (tmp_path / "large.bin").write_bytes(b"ghp_" + b"a" * 36 + b"\n" + b"x" * (5 * 1024 * 1024))
    assert scan_secrets(tmp_path) == []
    with pytest.raises(W0Error):  # not a repository: the gate must not report passed
        scan_secrets(tmp_path, tracked_only=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "sample.txt").write_bytes(b"ghp_" + b"a" * 36)
    assert scan_secrets(tmp_path, tracked_only=True) == []  # untracked files are ignored
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    assert scan_secrets(tmp_path, tracked_only=True)


def test_gate_main_reports_manifest_failures_and_fast_mode_skips_cli_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert architecture_gate._run_manifest_check() is True
    monkeypatch.setattr(sys, "argv", ["architecture_gate.py", "--fast"])
    assert architecture_gate.main() == 0
    monkeypatch.setattr(architecture_gate, "_run_manifest_check", lambda: False)
    monkeypatch.setattr(sys, "argv", ["architecture_gate.py"])
    assert architecture_gate.main() == 1
