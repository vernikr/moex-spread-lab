import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from moex_spread_lab.strategies.s3_reference.engine import REFERENCE_ROUND_TRIP_COST, run_s3


def _reference(root: Path, *, artifact_volume: int) -> Path:
    processed = root / "data" / "processed"
    processed.mkdir(parents=True)
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(45)]
    spreads = [2000 if i == 3 else 1000 for i in range(45)]
    with (processed / "spreads_sbrf.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "near", "far", "spread", "dev_ruonia", "days_to_near_exp", "div_window", "go_sum"])
        for index, day in enumerate(days):
            writer.writerow([day.isoformat(), "SRA", "SRB", spreads[index], 5.0 if index == 3 else 0.0, 46 - index, 0, 10000])
    for secid, offset in (("SRA", 0), ("SRB", 1)):
        with (processed / f"futures_{secid}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["begin", "open", "close", "volume"])
            for index, day in enumerate(days):
                price = 30000 + (spreads[index] if offset else 0)
                writer.writerow([f"{day.isoformat()} 00:00:00", price, price, artifact_volume if index == 3 else 5000])
    return root


def _benchmark(root: Path) -> Path:
    path = root / "benchmark.json"
    rows = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "price": 1.0} for i in range(45)]
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


class S3ReferenceTests(unittest.TestCase):
    def test_stale_print_profit_vanishes_with_a_later_fill_and_thin_pair_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_s3(_reference(root / "ref", artifact_volume=3), _benchmark(root))
        pick = lambda variant, model: next(
            v for v in result["variants"]
            if (v["strategy"], v["variant"], v["cost_model"]) == ("convergence_to_ruonia", variant, model)
        )
        same_bar = pick("reference_same_bar", "reference_flat_round_trip")
        self.assertEqual(same_bar["gross_rub"], 1000)
        self.assertEqual(same_bar["net_rub"], 1000 - REFERENCE_ROUND_TRIP_COST)
        for variant in ("next_close", "next_open"):
            self.assertEqual(pick(variant, "published_tariff")["gross_rub"], 0)
        tariff = pick("next_close", "published_tariff")
        self.assertGreater(tariff["costs_rub"], 0)
        self.assertEqual(tariff["net_rub"], round(-tariff["costs_rub"], 2))
        self.assertFalse(result["pairs"]["SRA/SRB"]["tradable_every_day"])
        self.assertEqual(result["pairs"]["SRA/SRB"]["min_leg_daily_volume"], 3)


if __name__ == "__main__":
    unittest.main()
