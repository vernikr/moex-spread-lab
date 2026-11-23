from __future__ import annotations

import json
from pathlib import Path

import pytest

from moex_spread_lab.core.json_types import JSONBoundaryError, as_str_dict, load_object


class DomainError(ValueError):
    pass


def test_as_str_dict_narrows_json_object_and_keeps_nested_values() -> None:
    value = {"name": "demo", "values": [1, None, {"enabled": True}]}

    assert as_str_dict(value) == value


@pytest.mark.parametrize("value", [None, [], "text", {1: "not a JSON object"}])
def test_as_str_dict_rejects_non_object_or_non_string_keys(value: object) -> None:
    with pytest.raises(JSONBoundaryError, match="string keys"):
        as_str_dict(value, label="payload")


def test_load_object_reads_utf8_and_requires_object_root(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"ключ": [1, "текст"]}, ensure_ascii=False), encoding="utf-8")

    assert load_object(path) == {"ключ": [1, "текст"]}

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(JSONBoundaryError, match="JSON object"):
        load_object(path)


def test_load_object_preserves_the_callers_domain_exception(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(DomainError, match="cannot read settings"):
        load_object(path, label="settings", error=DomainError)
