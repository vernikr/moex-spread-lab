"""Isolated regressions for the strict-typed legacy candle parser (P7.2 leaf #62).

Pins export/facade identity plus the input, error and payload contracts of the
parser and of the compatibility reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.data.lake import data_lake, legacy_series
from moex_spread_lab.data.lake.bar_types import DataLakeError

_SUPPORTED_HOOKS = (
    "_component",
    "_integer",
    "_number",
    "_read_legacy_series",
    "_source_key",
    "_timestamp",
)


def _payload(rows: list[Any] | None = None) -> dict[str, Any]:
    source = {
        "url": "https://iss.moex.example/page-0",
        "received_at": "2026-11-20T10:00:00+00:00",
        "raw_response_sha256": "a" * 64,
    }
    if rows is None:
        rows = [
            {
                "begin": f"2026-01-01 {10 + index:02d}:00:00",
                "end": f"2026-01-01 {10 + index:02d}:59:59",
                "open": 10.0 + index,
                "high": 11.0 + index,
                "low": 9.5 + index,
                "close": 10.5 + index,
                "value": 12.0,
                "volume": 20 + index,
                "price_proxy": str(10.5 + index),
                "price_proxy_kind": "candle_close",
                "price_proxy_assumption": "test_proxy",
                "source": source,
            }
            for index in range(3)
        ]
    return {
        "schema_version": "iss-candles.v1",
        "secid": "CNYRUBF",
        "board": "RFUD",
        "interval": 60,
        "timeframe": "1h",
        "from": "2026-01-01",
        "till": "2026-01-02",
        "coverage_status": "available",
        "rows": rows,
    }


def _write(root: Path, payload: Any, name: str = "CNYRUBF_1h.json") -> Path:
    path = root / "data" / "w1" / "candles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_export_identity() -> None:
    # One object per supported hook, reachable through both facades.
    assert legacy_series.__all__[:2] == ["LegacyCandleSeries", "LegacyJsonBarStore"]
    assert set(_SUPPORTED_HOOKS) <= set(legacy_series.__all__)
    for name in _SUPPORTED_HOOKS:
        assert getattr(legacy_series, name) is getattr(data_lake, name)
    assert data_lake.LegacyJsonBarStore is legacy_series.LegacyJsonBarStore
    assert data_lake.LegacyCandleSeries is legacy_series.LegacyCandleSeries


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_reader_payload(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _payload())
    series = legacy_series._read_legacy_series(path)
    assert series.dataset_id == "1h/CNYRUBF"
    assert (series.secid, series.board, series.interval, series.timeframe) == (
        "CNYRUBF",
        "RFUD",
        60,
        "1h",
    )
    assert (series.from_date, series.till_date) == ("2026-01-01", "2026-01-02")
    assert series.path == path.resolve()
    assert len(series.sha256) == 64
    assert [bar["begin"] for bar in series.bars] == [
        "2026-01-01 10:00:00",
        "2026-01-01 11:00:00",
        "2026-01-01 12:00:00",
    ]
    assert series.bars[0] == {
        "begin": "2026-01-01 10:00:00",
        "end": "2026-01-01 10:59:59",
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "value": 12.0,
        "volume": 20,
        "source_page_id": 0,
    }
    assert series.sources == [
        {
            "source_page_id": 0,
            "url": "https://iss.moex.example/page-0",
            "received_at": "2026-11-20T10:00:00+00:00",
            "raw_response_sha256": "a" * 64,
            "first_begin": "2026-01-01 10:00:00",
            "last_begin": "2026-01-01 12:00:00",
            "row_count": 3,
        }
    ]
    assert series.row_semantics == {
        "price_proxy_kind": "candle_close",
        "price_proxy_assumption": "test_proxy",
        "price_proxy_matches_close": True,
    }
    assert "rows" not in series.legacy_metadata
    assert series.legacy_metadata["coverage_status"] == "available"


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_source_dedup(
    tmp_path: Path,
) -> None:
    payload = _payload()
    second = {
        "url": "https://iss.moex.example/page-1",
        "received_at": "2026-11-20T10:05:00+00:00",
        "raw_response_sha256": "b" * 64,
    }
    payload["rows"][2]["source"] = second
    payload["rows"][2]["price_proxy"] = 12.5
    series = legacy_series._read_legacy_series(_write(tmp_path, payload))
    assert [bar["source_page_id"] for bar in series.bars] == [0, 0, 1]
    assert [page["row_count"] for page in series.sources] == [2, 1]
    assert (
        series.sources[1]["first_begin"] == series.sources[1]["last_begin"] == "2026-01-01 12:00:00"
    )


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_reader_errors(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _payload())
    broken = _write(tmp_path, _payload(), name="Broken_1h.json")
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(DataLakeError, match="cannot read legacy candle JSON"):
        legacy_series._read_legacy_series(broken)
    with pytest.raises(DataLakeError, match="cannot read legacy candle JSON"):
        legacy_series._read_legacy_series(tmp_path / "missing.json")

    wrong_schema = _write(tmp_path, _payload(), name="Schema_1h.json")
    payload = _payload()
    payload["schema_version"] = "iss-candles.v0"
    wrong_schema.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataLakeError, match=r"legacy file is not iss-candles\.v1"):
        legacy_series._read_legacy_series(wrong_schema)

    empty_rows = _write(tmp_path, _payload(rows=[]), name="Empty_1h.json")
    with pytest.raises(DataLakeError, match="legacy candle series has no rows"):
        legacy_series._read_legacy_series(empty_rows)

    unsorted = _payload()
    unsorted["rows"][0]["begin"] = "2026-01-01 13:00:00"
    with pytest.raises(DataLakeError, match="timestamps are not strictly increasing"):
        legacy_series._read_legacy_series(_write(tmp_path, unsorted, name="Unsorted_1h.json"))

    bad_proxy = _payload()
    bad_proxy["rows"][1]["price_proxy"] = "999"
    with pytest.raises(DataLakeError, match="price_proxy differs from close"):
        legacy_series._read_legacy_series(_write(tmp_path, bad_proxy, name="Proxy_1h.json"))

    bad_volume = _payload()
    bad_volume["rows"][1]["volume"] = 1.5
    with pytest.raises(DataLakeError, match="volume must be an integer or null"):
        legacy_series._read_legacy_series(_write(tmp_path, bad_volume, name="Volume_1h.json"))

    bad_hash = _payload()
    bad_hash["rows"][0]["source"]["raw_response_sha256"] = "xyz"
    with pytest.raises(DataLakeError, match="raw_response_sha256 is invalid"):
        legacy_series._read_legacy_series(_write(tmp_path, bad_hash, name="Hash_1h.json"))
    assert path.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("CNYRUBF", "CNYRUBF"), ("usd_rub-9M", "usd_rub-9M"), ("1h", "1h")],
)
def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_component_contract(
    value: str, expected: str
) -> None:
    assert legacy_series._component(value, "SECID") == expected
    for invalid in (None, "", "sp ace", "../etc", "sec/id", 7):
        with pytest.raises(DataLakeError, match="invalid SECID for data-lake path"):
            legacy_series._component(invalid, "SECID")


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_scalar_helpers() -> None:
    number = legacy_series._number
    integer = legacy_series._integer
    assert number(None, "open") is None
    assert number(3, "open") == 3.0
    assert isinstance(number(3, "open"), float)
    with pytest.raises(DataLakeError, match="legacy candle close is missing"):
        number(None, "close", required=True)
    for invalid in (True, "3", [], object()):
        with pytest.raises(DataLakeError, match="must be numeric or null"):
            number(invalid, "open")

    assert integer(None, "volume") is None
    assert integer(5.0, "volume") == 5
    assert isinstance(integer(5, "volume"), int)
    for invalid in (5.5, "5", False, []):
        with pytest.raises(DataLakeError, match="must be an integer or null"):
            integer(invalid, "volume")

    assert legacy_series._timestamp("2026-01-01 10:00:00", "begin") == "2026-01-01 10:00:00"
    for invalid in ("", None, 20260101):
        with pytest.raises(DataLakeError, match="begin must be a non-empty string"):
            legacy_series._timestamp(invalid, "begin")


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_source_key() -> None:
    source = {
        "url": "https://iss.moex.example/page-0",
        "received_at": "2026-11-20T10:00:00+00:00",
        "raw_response_sha256": "a" * 64,
        "ignored_extra": "not in the key",
    }
    reordered = {
        "raw_response_sha256": "a" * 64,
        "received_at": "2026-11-20T10:00:00+00:00",
        "url": "https://iss.moex.example/page-0",
    }
    assert legacy_series._source_key(source) == legacy_series._source_key(reordered)
    assert isinstance(legacy_series._source_key(source), bytes)
    other = dict(reordered, received_at="2026-11-20T10:05:00+00:00")
    assert legacy_series._source_key(other) != legacy_series._source_key(reordered)
    assert legacy_series._source_key({}) == legacy_series._source_key(
        {"url": None, "received_at": None, "raw_response_sha256": None}
    )


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_compat_reader(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _payload())
    series = legacy_series.LegacyJsonBarStore().read(path)
    assert series.schema_version == "iss-candles.v1"
    assert (series.secid, series.interval, series.timeframe) == ("CNYRUBF", 60, "1h")
    assert series.backend == "legacy_json"
    assert series.source_path == str(path)
    assert len(series.rows) == 3
    assert series.rows[0]["price_proxy"] == "10.5"
    assert series.cache_key[0] == "legacy_json"
    assert series.cache_key[1] == str(path.resolve())
    assert series.metadata["coverage_status"] == "available"

    projected = legacy_series.LegacyJsonBarStore().read(path, columns=("begin", "close"))
    # Projection keeps the row-level price-proxy marker.
    assert set(projected.rows[0]) == {"begin", "close", "price_proxy_kind"}
    with_proxy = legacy_series.LegacyJsonBarStore().read(
        path, columns=("begin", "price_proxy_kind")
    )
    assert with_proxy.rows[0] == {
        "begin": "2026-01-01 10:00:00",
        "price_proxy_kind": "candle_close",
    }


def test_p72_packages_data_src_moex_spread_lab_data_lake_legacy_series_compat_reader_errors(
    tmp_path: Path,
) -> None:
    reader = legacy_series.LegacyJsonBarStore()
    with pytest.raises(DataLakeError, match="cannot read legacy candle JSON"):
        reader.read(tmp_path / "missing.json")
    not_object = _write(tmp_path, [], name="List_1h.json")
    with pytest.raises(DataLakeError, match=r"legacy file is not iss-candles\.v1"):
        reader.read(not_object)
    bad_rows = _write(
        tmp_path, {"schema_version": "iss-candles.v1", "rows": {}}, name="Rows_1h.json"
    )
    with pytest.raises(DataLakeError, match="legacy candle rows must be a list"):
        reader.read(bad_rows)
    bad_row = _write(tmp_path, _payload(rows=["nope"]), name="Row_1h.json")
    with pytest.raises(DataLakeError, match="legacy candle row is not an object"):
        reader.read(bad_row)
    tolerant = _write(
        tmp_path,
        {
            "schema_version": "iss-candles.v1",
            "interval": True,
            "secid": 7,
            "rows": [{"begin": "b"}],
        },
        name="Tolerant_1h.json",
    )
    relaxed = reader.read(tolerant)
    assert (relaxed.secid, relaxed.interval, relaxed.timeframe) == (None, None, None)
