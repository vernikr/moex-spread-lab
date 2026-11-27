"""Fixture templates are cached by builder identity and cloned per smoke scenario."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BROWSER_DIR = Path(__file__).resolve().parent / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import _templates  # noqa: E402 - the browser directory must be importable first


def _counting_builder(calls: list[int], marker: str):
    def build(root: Path) -> None:
        calls.append(1)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "data" / "inputs.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")

    return build


def test_template_is_built_once_and_cloned_per_root(tmp_path, monkeypatch):
    monkeypatch.setattr(_templates, "_CACHE", {})
    calls: list[int] = []
    build = _counting_builder(calls, "first")
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()

    _templates.materialize(first, build)
    _templates.materialize(second, build)

    assert len(calls) == 1, "the builder must run once per process"
    assert (first / "data/inputs.json").read_text(encoding="utf-8") == json.dumps(
        {"marker": "first"}
    )
    assert (second / "data/inputs.json").read_text(encoding="utf-8") == json.dumps(
        {"marker": "first"}
    )


def test_a_scenario_mutation_never_reaches_the_template(tmp_path, monkeypatch):
    monkeypatch.setattr(_templates, "_CACHE", {})
    calls: list[int] = []
    build = _counting_builder(calls, "shared")
    writer, reader = tmp_path / "writer", tmp_path / "reader"
    writer.mkdir()
    reader.mkdir()

    _templates.materialize(writer, build)
    (writer / "data/inputs.json").write_text("mutated", encoding="utf-8")
    (writer / "data/extra.json").write_text("{}", encoding="utf-8")
    _templates.materialize(reader, build)

    assert (reader / "data/inputs.json").read_text(encoding="utf-8") == json.dumps(
        {"marker": "shared"}
    )
    assert not (reader / "data/extra.json").exists()
    assert len(calls) == 1


def test_distinct_builders_never_share_a_template(tmp_path, monkeypatch):
    monkeypatch.setattr(_templates, "_CACHE", {})
    calls: list[int] = []
    one = _counting_builder(calls, "one")
    two = _counting_builder(calls, "two")
    assert _templates.identity(one) != _templates.identity(two)

    for index, build in enumerate((one, two)):
        root = tmp_path / str(index)
        root.mkdir()
        _templates.materialize(root, build)
        assert json.loads((root / "data/inputs.json").read_text(encoding="utf-8"))["marker"] == (
            "one" if index == 0 else "two"
        )
    assert len(calls) == 2

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _templates.materialize(explicit, one, key="explicit-key")
    assert _templates.cached() == tuple(
        sorted({_templates.identity(one), _templates.identity(two), "explicit-key"})
    )


def test_materialize_reports_build_and_clone_phases(tmp_path, monkeypatch):
    monkeypatch.setattr(_templates, "_CACHE", {})
    calls: list[int] = []
    build = _counting_builder(calls, "timed")
    root = tmp_path / "root"
    root.mkdir()

    cold = _templates.materialize(root, build)
    warm_root = tmp_path / "warm"
    warm_root.mkdir()
    warm = _templates.materialize(warm_root, build)

    assert set(cold) == {"template_build", "fixture_clone"}
    assert cold["template_build"] > 0 and warm["template_build"] == 0
    assert cold["fixture_clone"] >= 0 and warm["fixture_clone"] >= 0


def test_identity_of_local_builders_distinguishes_their_code(tmp_path):
    silent = _templates.identity(lambda root: None)
    writing = _templates.identity(lambda root: root.joinpath("x.json").write_text("{}"))
    assert silent and writing and silent != writing
    with pytest.raises(FileNotFoundError):
        _templates.materialize(tmp_path, lambda root: root.joinpath("missing").read_text())
