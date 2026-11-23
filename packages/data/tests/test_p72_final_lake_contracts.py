"""Preserve pre-refactor wildcard and legacy facade object identities."""

import pytest

from moex_spread_lab.data.lake import data_lake, legacy_series, raw_archive


def test_legacy_wildcard_exports_are_the_original_set():
    namespace = {}
    exec("from moex_spread_lab.data.lake.legacy_series import *", namespace)
    assert set(namespace) - {"__builtins__"} == {
        "Any",
        "BarSeries",
        "DataLakeError",
        "Iterable",
        "LEGACY_CANDLE_SCHEMA",
        "LegacyCandleSeries",
        "LegacyJsonBarStore",
        "Path",
        "annotations",
        "canonical_bytes",
        "dataclass",
        "hashlib",
        "itertools",
        "json",
        "re",
    }


def test_legacy_and_raw_private_facade_identities():
    for name in ("component", "integer", "number", "read_legacy_series", "source_key", "timestamp"):
        original = getattr(legacy_series, "_" + name)
        assert getattr(legacy_series, name) is original
        assert getattr(data_lake, "_" + name) is original
    assert raw_archive.atomic_write_bytes is raw_archive._atomic_write_bytes
    assert data_lake._atomic_write_bytes is raw_archive._atomic_write_bytes
    namespace = {}
    exec("from moex_spread_lab.data.lake.raw_archive import *", namespace)
    assert set(namespace) - {"__builtins__"} == {
        "RAW_ARCHIVE_SCHEMA",
        "UTC",
        "Any",
        "DataLakeError",
        "Path",
        "RawResponseArchive",
        "annotations",
        "cast",
        "datetime",
        "hashlib",
        "json",
        "os",
        "pa",
        "tempfile",
        "timedelta",
    }


def test_price_proxy_validator_uses_late_bound_error(monkeypatch, tmp_path):
    class FacadeError(ValueError):
        pass

    monkeypatch.setattr(legacy_series, "DataLakeError", FacadeError)
    with pytest.raises(FacadeError, match="price_proxy differs from close"):
        legacy_series._validate_price_proxy({"price_proxy": 2}, 1, tmp_path / "x", 0)
