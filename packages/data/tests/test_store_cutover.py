import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import load_contracts, sha256_file
from moex_spread_lab.data.lake.data_lake import (
    BarSeries,
    LegacyJsonBarStore,
    MarketDataStore,
    ParquetBarStore,
)


def _pair_bars_and_provenance(
    near_series: BarSeries,
    far_series: BarSeries,
    benchmark_path: Path,
    contracts_path: Path,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    benchmark_sha = sha256_file(benchmark_path)
    _, contracts_sha = load_contracts(contracts_path, "NEAR", "FAR")
    rows = []
    for near_row, far_row in zip(near_series.rows, far_series.rows, strict=True):
        assert near_row["begin"] == far_row["begin"]
        rows.append(
            {
                "begin": near_row["begin"],
                "near_open": near_row["open"],
                "near_close": near_row["close"],
                "near_volume": near_row["volume"],
                "far_open": far_row["open"],
                "far_close": far_row["close"],
                "far_volume": far_row["volume"],
                "price_proxy_kind": near_row["price_proxy_kind"],
            }
        )
    provenance = {
        "near_sha256": near_series.source_sha256,
        "far_sha256": far_series.source_sha256,
        "benchmark_sha256": benchmark_sha,
        "contracts_sha256": contracts_sha,
    }
    return rows, provenance


def _instrument(secid: str) -> dict[str, object]:
    return {
        "secid": secid,
        "lot_size": 1,
        "tick_size": 0.1,
        "tick_value": 1.0,
        "initial_margin": 100.0,
        "source_vintage": "current_snapshot",
    }


class StoreCutoverTests(unittest.TestCase):
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
            day = f"2026-01-0{index}"
            hour = "10" if timeframe == "1h" else "00"
            rows.append(
                {
                    "begin": f"{day} {hour}:00:00",
                    "end": f"{day} 10:59:59" if timeframe == "1h" else f"{day} 23:59:59",
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
                        }
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
                    "instruments": [_instrument("NEAR"), _instrument("FAR")],
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
            expected = _pair_bars_and_provenance(
                legacy.read(near), legacy.read(far), benchmark, contracts
            )

            parquet = ParquetBarStore(root)
            parquet.import_legacy(near)
            parquet.import_legacy(far)
            store = MarketDataStore(root)
            near_parquet, far_parquet = store.read(near), store.read(far)
            actual = _pair_bars_and_provenance(near_parquet, far_parquet, benchmark, contracts)

            self.assertEqual(near_parquet.backend, "parquet")
            self.assertEqual(far_parquet.backend, "parquet")
            self.assertEqual(near_parquet.source_sha256, legacy.read(near).source_sha256)
            self.assertEqual(far_parquet.source_sha256, legacy.read(far).source_sha256)
            projected = store.read(near, columns=("begin", "open", "close", "volume"))
            self.assertEqual(projected.backend, "parquet")
            self.assertEqual(
                set(projected.rows[0]), {"begin", "open", "close", "volume", "price_proxy_kind"}
            )
            self.assertEqual(
                projected.rows,
                legacy.read(near, columns=("begin", "open", "close", "volume")).rows,
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
            legacy_store = MarketDataStore(root)
            selected_legacy = legacy_store.select_h1("H1")
            assert selected_legacy is not None
            legacy_series = legacy_store.read(
                selected_legacy, columns=("begin", "open", "close", "volume")
            )
            ParquetBarStore(root).import_legacy(path)
            parquet_store = MarketDataStore(root)
            selected_parquet = parquet_store.select_h1("H1")
            assert selected_parquet is not None
            parquet_series = parquet_store.read(
                selected_parquet, columns=("begin", "open", "close", "volume")
            )

            self.assertEqual(selected_legacy, path)
            self.assertEqual(selected_parquet, path)
            self.assertEqual(legacy_series.backend, "legacy_json")
            self.assertEqual(parquet_series.backend, "parquet")
            self.assertEqual(parquet_series.source_sha256, legacy_series.source_sha256)
            self.assertEqual(parquet_series.rows, legacy_series.rows)


if __name__ == "__main__":
    unittest.main()
