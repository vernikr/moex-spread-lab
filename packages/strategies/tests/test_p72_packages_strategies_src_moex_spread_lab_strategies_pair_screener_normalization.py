"""Isolated regression coverage for the strict-typed pair-screener normalization.

``normalization`` is a private facade: ``screener`` re-exports ``_normalized_series``
and friends, so these tests pin the observable contracts only — source dispatch,
normalized payload shape and benchmark alignment.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.lake.data_lake import BarSeries, DataLakeError
from moex_spread_lab.strategies.pair_screener import normalization
from moex_spread_lab.strategies.pair_screener.normalization import (
    ScreenError,
    _bar_series,
    _normalized_series,
    _round,
)

FACADE = {"ScreenError", "_bar_series", "_normalized_series", "_read_json", "_round"}
DATES = ("2026-03-02", "2026-03-03", "2026-03-04")


def _candle_rows(offset: float, dates: tuple[str, ...] = DATES) -> list[dict[str, Any]]:
    return [
        {
            "begin": f"{date} 00:00:00",
            "open": float(index) + offset,
            "close": float(index + 1) + offset,
            "price_proxy_kind": "candle_close",
        }
        for index, date in enumerate(dates)
    ]


def _write(root: Path, name: str, payload: dict[str, Any]) -> Path:
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _candles(root: Path, secid: str, interval: int, rows: list[dict[str, Any]]) -> Path:
    payload = {"schema_version": "iss-candles.v1", "secid": secid, "interval": interval}
    return _write(root, f"{secid}_{interval}.json", {**payload, "rows": rows})


def _benchmark(root: Path, dates: tuple[str, ...]) -> Path:
    rows = [
        {"date": date, "price": 1.5 + index, "source_kind": "observed", "source_ticker": "LQDT"}
        for index, date in enumerate(dates)
    ]
    return _write(root, "benchmark.json", {"schema_version": "lqdt-lineage.v1", "rows": rows})


def _contracts(root: Path) -> Path:
    instruments = (("NEAR", "n", 0.5, 5.0, 900.0), ("FAR", "f", 0.05, 0.5, 1000.0))
    rows = [
        {
            "secid": secid,
            "stable_id": stable,
            "board": "RFUD",
            "expiry": "2100-01-01",
            "lot_size": 1,
            "tick_size": tick_size,
            "tick_value": tick_value,
            "initial_margin": margin,
            "source_vintage": "current_snapshot",
        }
        for secid, stable, tick_size, tick_value, margin in instruments
    ]
    return _write(root, "contracts.json", {"schema_version": "iss-intake.v1", "instruments": rows})


class BarSeriesDispatchTests(unittest.TestCase):
    def test_module_all_declares_screener_facade(self):
        self.assertTrue(set(normalization.__all__) >= FACADE)
        for name in FACADE:
            self.assertTrue(hasattr(normalization, name), name)
        self.assertEqual(_round(1.0 / 3.0), 0.33333333)
        self.assertTrue(issubclass(ScreenError, W0Error))
        self.assertTrue(issubclass(DataLakeError, W0Error))

    def test_bar_series_passes_through_dataclass_instance(self):
        series = BarSeries(
            schema_version="iss-candles.v1",
            secid="NEAR",
            interval=24,
            timeframe=None,
            rows=({"begin": "2026-03-02 00:00:00", "close": 100.0},),
            source_path="inline",
            source_sha256="0" * 64,
            backend="legacy_json",
            cache_key=("inline",),
            metadata={},
        )

        self.assertIs(_bar_series(series), series)

    def test_bar_series_reads_legacy_json_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _candles(Path(directory), "NEAR", 24, _candle_rows(0.0))

            series = _bar_series(path)

        self.assertIsInstance(series, BarSeries)
        self.assertEqual((series.schema_version, series.interval), ("iss-candles.v1", 24))
        self.assertEqual(len(series.rows), len(DATES))

    def test_bar_series_rejections_are_screen_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = _write(Path(directory), "broken.json", {"schema_version": "other.v1"})
            with self.assertRaises(ScreenError) as raised:
                _bar_series(broken)

        self.assertIn("iss-candles.v1", str(raised.exception))
        with self.assertRaises(ScreenError) as raised:
            _bar_series(cast(Any, object()))
        self.assertEqual(str(raised.exception), "screen candle source must be a BarSeries or Path")


class NormalizedSeriesContractTests(unittest.TestCase):
    def _daily_payload(self, root: Path) -> dict[str, Any]:
        return _normalized_series(
            _candles(root, "NEAR", 24, _candle_rows(0.0)),
            _candles(root, "FAR", 24, _candle_rows(0.5)),
            _benchmark(root, DATES),
            _contracts(root),
            near="NEAR",
            far="FAR",
        )

    def test_daily_payload_shape_and_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self._daily_payload(Path(directory))

        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "near",
                "far",
                "interval",
                "timeframe",
                "direction",
                "price_unit",
                "spread_tick_value_rub",
                "rows",
                "contracts",
                "provenance",
            },
        )
        self.assertEqual(payload["schema_version"], "analytical-series.v1")
        self.assertEqual((payload["near"], payload["far"]), ("NEAR", "FAR"))
        self.assertEqual((payload["interval"], payload["timeframe"]), (24, "1d"))
        self.assertEqual(payload["direction"], "near_minus_far_normalized_rub")
        self.assertEqual(payload["price_unit"], "rub_normalized_per_pair")
        self.assertEqual(payload["spread_tick_value_rub"], 5.0)
        self.assertEqual(set(payload["contracts"]), {"NEAR", "FAR"})

        provenance = payload["provenance"]
        self.assertIn("near_candles_sha256", provenance)
        self.assertIn("contracts_sha256", provenance)
        self.assertEqual(provenance["normalization"], "close * (tick_value / tick_size) per leg")
        self.assertEqual(
            provenance["benchmark_alignment"],
            "same-day close; documented non-clearing fallback only",
        )

        rows = payload["rows"]
        self.assertEqual([row["timestamp"] for row in rows], [f"{date}T00:00:00" for date in DATES])
        first = rows[0]
        self.assertEqual(first["date"], "2026-03-02")
        self.assertEqual(first["near_close"], 1.0)
        self.assertEqual(first["far_close"], 1.5)
        self.assertEqual(first["spread_value"], _round(1.0 * 10.0 - 1.5 * 10.0))
        self.assertEqual(first["spread_ticks"], -1.0)
        self.assertEqual(first["benchmark_price"], 1.5)
        self.assertEqual(first["benchmark_mark_kind"], "daily_close_same_date")
        self.assertEqual(first["near_price_proxy_kind"], "candle_close")
        self.assertIsNone(first["near_volume"])

    def test_intraday_uses_previous_observed_benchmark_close(self):
        rows = [
            {"begin": f"{date} {clock}:00:00", "close": price, "price_proxy_kind": "candle_close"}
            for date, clock, price in (
                ("2026-03-02", "10", 10.0),
                ("2026-03-02", "11", 11.0),
                ("2026-03-03", "10", 12.0),
                ("2026-03-03", "11", 13.0),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _normalized_series(
                _candles(root, "NEAR", 60, rows),
                _candles(root, "FAR", 60, rows),
                _benchmark(root, ("2026-02-27", "2026-03-02", "2026-03-03")),
                _contracts(root),
                near="NEAR",
                far="FAR",
                prior_benchmark_dates=("2026-03-03",),
                timeframe="1h",
            )

        self.assertEqual((payload["interval"], payload["timeframe"]), (60, "1h"))
        self.assertEqual(
            payload["provenance"]["benchmark_alignment"],
            "previous observed daily close for intraday bars",
        )
        normalized = payload["rows"]
        self.assertEqual(len(normalized), len(rows))
        self.assertEqual(normalized[0]["benchmark_mark_kind"], "prior_daily_close")
        self.assertEqual(normalized[0]["benchmark_mark_date"], "2026-02-27")
        self.assertEqual(normalized[-1]["date"], "2026-03-03T11:00:00")
        self.assertEqual(normalized[-1]["session_date"], "2026-03-03")

    def test_unknown_option_and_thin_overlap_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            near_path = _candles(root, "NEAR", 24, _candle_rows(0.0))
            far_path = _candles(root, "FAR", 24, _candle_rows(0.5)[:1])
            benchmark_path = _benchmark(root, DATES)
            contracts_path = _contracts(root)

            with self.assertRaises(TypeError) as raised:
                _normalized_series(
                    near_path,
                    far_path,
                    benchmark_path,
                    contracts_path,
                    near="NEAR",
                    far="FAR",
                    unexpected=True,
                )
            self.assertIn("unexpected keyword argument(s)", str(raised.exception))

            with self.assertRaises(ScreenError) as raised:
                _normalized_series(
                    near_path, far_path, benchmark_path, contracts_path, near="NEAR", far="FAR"
                )
            self.assertIn("fewer than two normalized common rows", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
