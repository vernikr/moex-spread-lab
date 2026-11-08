import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from moex_spread_lab.s3_reference import ROUND_TRIP_COST, run_s3


def _reference(root: Path, *, artifact_volume: int) -> Path:
    processed = root / "data" / "processed"
    processed.mkdir(parents=True)
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(45)]
    with (processed / "spreads_sbrf.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "near", "far", "spread", "dev_ruonia", "days_to_near_exp", "div_window", "go_sum"])
        for index, day in enumerate(days):
            artifact = index == 3
            writer.writerow([day.isoformat(), "SRA", "SRB", 2000 if artifact else 1000, 5.0 if artifact else 0.0, 46 - index, 0, 10000])
    for secid in ("SRA", "SRB"):
        with (processed / f"futures_{secid}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["begin", "volume"])
            for index, day in enumerate(days):
                writer.writerow([f"{day.isoformat()} 00:00:00", artifact_volume if index == 3 else 5000])
    return root


def _benchmark(root: Path) -> Path:
    path = root / "benchmark.json"
    rows = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "price": 1.0} for i in range(45)]
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


class S3ReferenceTests(unittest.TestCase):
    def test_stale_print_profit_vanishes_with_delay_and_liquidity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_s3(_reference(root / "ref", artifact_volume=3), _benchmark(root))
        by_variant = {(v["strategy"], v["variant"]): v for v in result["variants"]}
        same_bar = by_variant[("convergence_to_ruonia", "reference_same_bar")]
        delayed = by_variant[("convergence_to_ruonia", "delay_1")]
        self.assertEqual(same_bar["gross_rub"], 1000)
        self.assertEqual(same_bar["net_rub"], 1000 - ROUND_TRIP_COST)
        self.assertEqual(delayed["gross_rub"], 0)
        self.assertEqual(by_variant[("convergence_to_ruonia", "delay_1_min_volume_100")]["trades"], 0)
        self.assertEqual(delayed["overlay_excess_rub"], round(-ROUND_TRIP_COST, 2))


if __name__ == "__main__":
    unittest.main()
