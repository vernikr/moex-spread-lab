"""Positive and negative tests for the repository-wide architecture gate.

Covers the two checks the gate runs before the DAG suite on a full run, plus the
legacy W0 run-manifest contract the gate still exercises through the supported
``validate-manifest`` CLI command:

* package-manifest conformance (``scripts/architecture_gate.py``, stdlib-only);
* the tracked-file secret scan (``secret-scan`` / ``scan_secrets``);
* the run-manifest contract pinned by ``config/run-manifest.json`` (hash pins,
  path-escape and offline invariants).

Negative cases use synthetic trees under ``tmp_path`` so the real repository
configs/fixtures stay untouched, and the pinned legacy contracts remain the
compatibility baseline until every live consumer migrates.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import architecture_gate

from moex_spread_lab.core.w0 import (
    W0Error,
    manifest_config_hash,
    scan_secrets,
    sha256_file,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[3]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _valid_manifest(name: str = "core") -> dict[str, Any]:
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
    }


def _valid_pyproject(
    name: str = "core",
    *,
    version: str = "0.1.0",
    dependencies: list[str] | None = None,
) -> str:
    deps = dependencies if dependencies is not None else []
    rendered = ", ".join(json.dumps(dep) for dep in deps)
    return f'[project]\nname = "msl-{name}"\nversion = "{version}"\ndependencies = [{rendered}]\n'


def _make_package(
    root: Path,
    name: str = "core",
    *,
    manifest: object | None = "default",
    pyproject: str | None = "default",
    manifest_raw: str | None = None,
) -> Path:
    package = root / "packages" / name
    package.mkdir(parents=True)
    if manifest_raw is not None:
        (package / "manifest.json").write_text(manifest_raw, encoding="utf-8")
    elif manifest == "default":
        _write_json(package / "manifest.json", _valid_manifest(name))
    elif manifest is not None:
        _write_json(package / "manifest.json", manifest)
    if pyproject == "default":
        (package / "pyproject.toml").write_text(_valid_pyproject(name), encoding="utf-8")
    elif pyproject is not None:
        (package / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return package


def _package_errors(root: Path) -> list[str]:
    return architecture_gate.validate_package_manifests(root)


def test_repository_package_manifests_conform() -> None:
    assert _package_errors(ROOT) == []


def test_synthetic_package_tree_conforms(tmp_path: Path) -> None:
    _make_package(tmp_path)
    assert _package_errors(tmp_path) == []


def test_workspace_dependency_edge_conforms(tmp_path: Path) -> None:
    _make_package(tmp_path, "core")
    _make_package(tmp_path, "data")
    manifest = _valid_manifest("core")
    manifest["dependencies"] = ["moex-spread-lab.data"]
    _write_json(tmp_path / "packages" / "core" / "manifest.json", manifest)
    (tmp_path / "packages" / "core" / "pyproject.toml").write_text(
        _valid_pyproject("core", dependencies=["msl-data>=0.1.0"]), encoding="utf-8"
    )
    assert _package_errors(tmp_path) == []


def test_workspace_module_id_maps_msl_distribution_names() -> None:
    assert architecture_gate._workspace_module_id("msl-core") == "moex-spread-lab.core"
    assert architecture_gate._workspace_module_id("MSL_Data") == "moex-spread-lab.data"
    assert architecture_gate._workspace_module_id("requests") is None


def test_missing_manifest_json_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, manifest=None)
    assert any("missing manifest.json" in error for error in _package_errors(tmp_path))


def test_unparseable_manifest_json_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, manifest_raw="{not json")
    assert any("does not parse" in error for error in _package_errors(tmp_path))


def test_non_object_manifest_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, manifest_raw="[1, 2]")
    assert any("must be a JSON object" in error for error in _package_errors(tmp_path))


def test_missing_pyproject_toml_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, pyproject=None)
    assert any("missing pyproject.toml" in error for error in _package_errors(tmp_path))


def test_unparseable_pyproject_toml_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, pyproject="this is = = not toml")
    assert any("pyproject.toml does not parse" in error for error in _package_errors(tmp_path))


def test_missing_project_table_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, pyproject="[tool.other]\nx = 1\n")
    assert any("missing [project] table" in error for error in _package_errors(tmp_path))


def test_missing_project_version_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, pyproject='[project]\nname = "msl-core"\n')
    assert any("[project].version missing" in error for error in _package_errors(tmp_path))


def test_missing_required_manifest_keys_fail(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    del manifest["owner"]
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "missing required manifest keys" in error and "owner" in error
        for error in _package_errors(tmp_path)
    )


def test_empty_string_manifest_key_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["owner"] = ""
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "key 'owner' must be a non-empty string" in error for error in _package_errors(tmp_path)
    )


def test_empty_capabilities_fail(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["capabilities"] = []
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "'capabilities' must be a non-empty list of strings" in error
        for error in _package_errors(tmp_path)
    )


def test_non_string_dependencies_fail(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["dependencies"] = [1]
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "'dependencies' must be a list of strings" in error for error in _package_errors(tmp_path)
    )


def test_non_object_determinism_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["determinism"] = ["required"]
    _make_package(tmp_path, manifest=manifest)
    assert any("'determinism' must be an object" in error for error in _package_errors(tmp_path))


def test_module_id_mismatch_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["module_id"] = "moex-spread-lab.other"
    _make_package(tmp_path, manifest=manifest)
    assert any("module_id 'moex-spread-lab.other'" in error for error in _package_errors(tmp_path))


def test_release_mismatch_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["release"] = "9.9.9"
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "release '9.9.9' != pyproject version '0.1.0'" in error
        for error in _package_errors(tmp_path)
    )


def test_manifest_version_mismatch_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["version"] = "9.9.9"
    _make_package(tmp_path, manifest=manifest)
    assert any(
        "version '9.9.9' != pyproject version" in error for error in _package_errors(tmp_path)
    )


def test_duplicate_dependencies_fail(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["dependencies"] = ["moex-spread-lab.data", "moex-spread-lab.data"]
    _make_package(tmp_path, manifest=manifest)
    assert any("dependencies contain duplicates" in error for error in _package_errors(tmp_path))


def test_dependency_mismatch_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, "core")
    _make_package(tmp_path, "data")
    (tmp_path / "packages" / "core" / "pyproject.toml").write_text(
        _valid_pyproject("core", dependencies=["msl-data"]), encoding="utf-8"
    )
    assert any("do not match" in error for error in _package_errors(tmp_path))


def test_unknown_workspace_dependency_fails(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["dependencies"] = ["moex-spread-lab.ghost"]
    _make_package(
        tmp_path,
        manifest=manifest,
        pyproject=_valid_pyproject("core", dependencies=["msl-ghost"]),
    )
    assert any(
        "depends on unknown workspace package 'ghost'" in error
        for error in _package_errors(tmp_path)
    )


def test_unparseable_pyproject_dependency_fails(tmp_path: Path) -> None:
    _make_package(tmp_path, pyproject=_valid_pyproject("core", dependencies=["!!!"]))
    assert any("unparseable pyproject dependency" in error for error in _package_errors(tmp_path))


def test_non_list_pyproject_dependencies_fail(tmp_path: Path) -> None:
    pyproject = '[project]\nname = "msl-core"\nversion = "0.1.0"\ndependencies = 5\n'
    _make_package(tmp_path, pyproject=pyproject)
    assert any(
        "dependencies must be a list of strings" in error for error in _package_errors(tmp_path)
    )


_SECRET_SAMPLES = [
    pytest.param(b"token = ghp_" + b"a" * 36, id="github-pat"),
    pytest.param(b"github_pat_" + b"b" * 22, id="github-fine-grained-pat"),
    pytest.param(b"AKIA" + b"A" * 16, id="aws-access-key"),
    pytest.param(b"-----BEGIN OPENSSH" + b" PRIVATE KEY-----\n", id="private-key-block"),
    pytest.param(b"https://user:" + b"pass@internal.example.com/x\n", id="credentials-in-url"),
    pytest.param(b"x-access-token:" + b"c" * 24, id="x-access-token"),
]


@pytest.mark.parametrize("payload", _SECRET_SAMPLES)
def test_secret_scan_detects_each_pattern(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / "sample.txt").write_bytes(payload)
    assert scan_secrets(tmp_path)


def test_secret_scan_clean_file_passes(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("nothing sensitive here\n", encoding="utf-8")
    assert scan_secrets(tmp_path) == []


def test_secret_scan_skips_files_over_5mb(tmp_path: Path) -> None:
    payload = b"ghp_" + b"a" * 36 + b"\n" + b"x" * (5 * 1024 * 1024)
    (tmp_path / "large.bin").write_bytes(payload)
    assert scan_secrets(tmp_path) == []


def test_tracked_secret_scan_ignores_untracked_but_finds_staged(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "sample.txt").write_bytes(b"ghp_" + b"a" * 36)
    assert scan_secrets(tmp_path, tracked_only=True) == []
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    assert scan_secrets(tmp_path, tracked_only=True)


def test_tracked_secret_scan_outside_a_repo_finds_nothing(tmp_path: Path) -> None:
    assert scan_secrets(tmp_path, tracked_only=True) == []


def _make_run_tree(root: Path, mutate: Any = None) -> Path:
    module_path = root / "config" / "modules" / "w0.json"
    _write_json(
        module_path,
        {"module_id": "moex-spread-lab.w0", "release": "0.1.0", "contract_version": "w0.v1"},
    )
    artifact_path = root / "fixtures" / "replay.json"
    _write_json(
        artifact_path,
        {
            "schema_version": "w0.fixture.v1",
            "fixture_id": "contract-test-fixture",
            "source": {"rights_status": "synthetic_test_data"},
            "records": [],
        },
    )
    manifest: dict[str, Any] = {
        "schema_version": "run-manifest.v1",
        "run_id": "contract-test",
        "project": "moex-spread-lab",
        "configuration": {
            "locale": "en-US",
            "mode": "offline",
            "network_access": False,
            "timezone": "Europe/Moscow",
        },
        "trial_counter": 0,
        "modules": [
            {
                "contract_version": "w0.v1",
                "manifest_path": "config/modules/w0.json",
                "manifest_sha256": sha256_file(module_path),
                "module_id": "moex-spread-lab.w0",
                "release": "0.1.0",
            }
        ],
        "artifacts": [
            {
                "artifact_id": "w0-replay-fixture",
                "effective_at": None,
                "path": "fixtures/replay.json",
                "producer": "project-core",
                "publication_at": None,
                "receipt_at": "2026-10-27T00:00:00Z",
                "rights_status": "synthetic_test_data",
                "schema_version": "w0.fixture.v1",
                "sha256": sha256_file(artifact_path),
            }
        ],
        "source_requests": [
            {
                "endpoint": None,
                "fixture_ref": "w0-replay-fixture",
                "network_required": False,
                "parameters": {},
                "provider": "synthetic",
                "request_id": "w0-synthetic-fixture",
                "rights_status": "synthetic_test_data",
            }
        ],
        "config_hash": "",
    }
    if mutate is not None:
        mutate(manifest)
    manifest["config_hash"] = manifest_config_hash(manifest)
    manifest_path = root / "config" / "run-manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def test_run_manifest_contract_accepts_consistent_tree(tmp_path: Path) -> None:
    manifest_path = _make_run_tree(tmp_path)
    assert validate_manifest(manifest_path)["run_id"] == "contract-test"


def test_run_manifest_rejects_unsupported_schema(tmp_path: Path) -> None:
    manifest_path = _make_run_tree(tmp_path, lambda m: m.update(schema_version="run-manifest.v9"))
    with pytest.raises(W0Error, match="unsupported run manifest schema"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_config_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = _make_run_tree(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config_hash"] = "0" * 64
    _write_json(manifest_path, manifest)
    with pytest.raises(W0Error, match="config_hash does not match"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_module_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = _make_run_tree(tmp_path)
    _write_json(
        tmp_path / "config" / "modules" / "w0.json",
        {"module_id": "moex-spread-lab.w0", "release": "0.2.0"},
    )
    with pytest.raises(W0Error, match="module manifest hash mismatch"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = _make_run_tree(tmp_path)
    _write_json(
        tmp_path / "fixtures" / "replay.json",
        {"schema_version": "w0.fixture.v1", "fixture_id": "tampered", "records": []},
    )
    with pytest.raises(W0Error, match="artifact hash mismatch"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_artifact_path_escape(tmp_path: Path) -> None:
    def escape(manifest: dict[str, Any]) -> None:
        manifest["artifacts"][0]["path"] = "../outside.json"

    manifest_path = _make_run_tree(tmp_path, escape)
    with pytest.raises(W0Error, match="path escapes repository root"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_network_access_true(tmp_path: Path) -> None:
    def enable_network(manifest: dict[str, Any]) -> None:
        manifest["configuration"]["network_access"] = True

    manifest_path = _make_run_tree(tmp_path, enable_network)
    with pytest.raises(W0Error, match=r"network_access=false"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_source_request_network_true(tmp_path: Path) -> None:
    def enable_network(manifest: dict[str, Any]) -> None:
        manifest["source_requests"][0]["network_required"] = True

    manifest_path = _make_run_tree(tmp_path, enable_network)
    with pytest.raises(W0Error, match="network_required=false"):
        validate_manifest(manifest_path)


def test_run_manifest_rejects_empty_artifact_rights_status(tmp_path: Path) -> None:
    def drop_rights(manifest: dict[str, Any]) -> None:
        manifest["artifacts"][0]["rights_status"] = ""

    manifest_path = _make_run_tree(tmp_path, drop_rights)
    with pytest.raises(W0Error, match="rights_status is empty"):
        validate_manifest(manifest_path)


def test_gate_manifest_check_passes_on_repository() -> None:
    assert architecture_gate._run_manifest_check() is True


def test_gate_returns_one_when_manifest_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(architecture_gate, "_run_manifest_check", lambda: False)
    monkeypatch.setattr(sys, "argv", ["architecture_gate.py"])
    assert architecture_gate.main() == 1


def test_gate_fast_skips_cli_contract_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["architecture_gate.py", "--fast"])
    assert architecture_gate.main() == 0
