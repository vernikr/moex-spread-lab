import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.data.lake.data_lake import LegacyJsonBarStore, MarketDataStore, ParquetBarStore
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.strategies.pair_screener.screener import _normalized_series


class StoreCutoverTests(unittest.TestCase):
    def test_record_path_preserves_private_override(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ParquetBarStore(Path(directory))
            expected = Path(directory) / "override.parquet"
            with patch.object(store, "_record_path", return_value=expected) as resolve:
                self.assertEqual(store.record_path("bars.parquet"), expected)
                resolve.assert_called_once_with("bars.parquet")

    def _write_series(
        self,
        root: Path,
        secid: str,
        timeframe: str,
        interval: int,
        closes: list[float],
        *,
        folder: str = "data/w1/candles",
    ) -> Path:
        path = root / folder / f"{secid}_{timeframe}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        source = {
            "url": f"https://iss.example/{secid}/{interval}",
            "received_at": "2026-11-20T10:00:00+00:00",
            "raw_response_sha256": hashlib.sha256(secid.encode()).hexdigest(),
        }
        rows = []
        for index, close in enumerate(closes, start=1):
            stamp = (
                f"2026-01-0{index} 10:00:00" if timeframe == "1h" else f"2026-01-0{index} 00:00:00"
            )
            end = (
                f"2026-01-0{index} 10:59:59" if timeframe == "1h" else f"2026-01-0{index} 23:59:59"
            )
            rows.append(
                {
                    "begin": stamp,
                    "end": end,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "value": close * 10,
                    "volume": 100 + index,
                    "price_proxy": str(close),
                    "price_proxy_kind": "candle_close",
                    "price_proxy_assumption": "test_proxy",
                    "source": source,
                }
            )
        path.write_text(
            json.dumps(
                {
                    "schema_version": "iss-candles.v1",
                    "secid": secid,
                    "board": "RFUD",
                    "interval": interval,
                    "timeframe": timeframe,
                    "from": "2026-01-01",
                    "till": "2026-01-03",
                    "coverage_status": "available",
                    "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
                    "rows": rows,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _benchmark_and_contracts(self, root: Path) -> tuple[Path, Path]:
        benchmark = root / "data/benchmark/lqdt-lineage.json"
        benchmark.parent.mkdir(parents=True, exist_ok=True)
        benchmark.write_text(
            json.dumps(
                {
                    "schema_version": "lqdt-lineage.v1",
                    "rows": [
                        {
                            "date": "2026-01-01",
                            "price": 1.0,
                            "source_kind": "observed",
                            "source_ticker": "LQDT",
                        },
                        {
                            "date": "2026-01-02",
                            "price": 1.01,
                            "source_kind": "observed",
                            "source_ticker": "LQDT",
                        },
                        {
                            "date": "2026-01-03",
                            "price": 1.02,
                            "source_kind": "observed",
                            "source_ticker": "LQDT",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        contracts = root / "data/w1/current-intake.json"
        contracts.parent.mkdir(parents=True, exist_ok=True)
        contracts.write_text(
            json.dumps(
                {
                    "schema_version": "iss-intake.v1",
                    "instruments": [
                        {
                            "secid": "NEAR",
                            "stable_id": "near",
                            "board": "RFUD",
                            "expiry": "2100-01-01",
                            "lot_size": 1,
                            "tick_size": 0.1,
                            "tick_value": 1.0,
                            "initial_margin": 100.0,
                            "source_vintage": "current_snapshot",
                        },
                        {
                            "secid": "FAR",
                            "stable_id": "far",
                            "board": "RFUD",
                            "expiry": "2026-12-17",
                            "lot_size": 1,
                            "tick_size": 0.1,
                            "tick_value": 1.0,
                            "initial_margin": 100.0,
                            "source_vintage": "current_snapshot",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return benchmark, contracts

    def test_normalized_rows_and_provenance_are_identical_after_verified_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            near = self._write_series(root, "NEAR", "1d", 24, [10.0, 10.5, 11.0])
            far = self._write_series(root, "FAR", "1d", 24, [9.0, 9.2, 9.4])
            benchmark, contracts = self._benchmark_and_contracts(root)
            legacy = LegacyJsonBarStore()
            expected = _normalized_series(
                legacy.read(near), legacy.read(far), benchmark, contracts, near="NEAR", far="FAR"
            )

            parquet = ParquetBarStore(root)
            parquet.import_legacy(near)
            parquet.import_legacy(far)
            store = MarketDataStore(root)
            near_parquet, far_parquet = store.read(near), store.read(far)
            actual = _normalized_series(
                near_parquet, far_parquet, benchmark, contracts, near="NEAR", far="FAR"
            )

            self.assertEqual(near_parquet.backend, "parquet")
            self.assertEqual(far_parquet.backend, "parquet")
            self.assertEqual(ScreenerInputs(root).bars("NEAR/FAR", "NEAR", "1d").backend, "parquet")
            projected = store.read(near, columns=("begin", "open", "close", "volume"))
            self.assertEqual(projected.backend, "parquet")
            self.assertEqual(
                set(projected.rows[0]), {"begin", "open", "close", "volume", "price_proxy_kind"}
            )
            self.assertEqual(actual, expected)

    def test_unmatched_screen_copy_stays_on_legacy_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intake = self._write_series(root, "NEAR", "1d", 24, [10.0, 10.5, 11.0])
            screen = self._write_series(
                root, "NEAR", "1d", 24, [11.0, 11.5, 12.0], folder="data/w1/candles-screen"
            )
            ParquetBarStore(root).import_legacy(intake)
            store = MarketDataStore(root)

            self.assertEqual(store.read(intake).backend, "parquet")
            fallback = store.read(screen)
            self.assertEqual(fallback.backend, "legacy_json")
            self.assertEqual(fallback.rows[0]["close"], 11.0)
            projected = store.read(screen, columns=("begin", "open", "close", "volume"))
            self.assertEqual(projected.rows[0]["price_proxy_kind"], "candle_close")

    def test_changed_legacy_input_is_not_silently_read_from_parquet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intake = self._write_series(root, "NEAR", "1d", 24, [10.0, 10.5, 11.0])
            ParquetBarStore(root).import_legacy(intake)
            payload = json.loads(intake.read_text(encoding="utf-8"))
            payload["rows"][0]["close"] = 42.0
            intake.write_text(json.dumps(payload), encoding="utf-8")

            series = MarketDataStore(root).read(intake)
            self.assertEqual(series.backend, "legacy_json")
            self.assertEqual(series.rows[0]["close"], 42.0)

    def test_missing_parquet_file_falls_back_to_legacy_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intake = self._write_series(root, "NEAR", "1d", 24, [10.0, 10.5, 11.0])
            record = ParquetBarStore(root).import_legacy(intake)
            (root / record["bars_path"]).unlink()

            series = MarketDataStore(root).read(intake)
            self.assertEqual(series.backend, "legacy_json")
            self.assertEqual(series.rows[0]["close"], 10.0)

    def test_calendar_h1_reader_is_identical_for_legacy_and_parquet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_series(root, "H1", "1h", 60, [100.0, 101.0, 102.0])
            legacy_source, legacy_error = CalendarData(root)._read_h1_candles("H1")
            ParquetBarStore(root).import_legacy(path)
            parquet_source, parquet_error = CalendarData(root)._read_h1_candles("H1")

            self.assertIsNone(legacy_error)
            self.assertIsNone(parquet_error)
            self.assertEqual(parquet_source, legacy_source)


if __name__ == "__main__":
    unittest.main()
