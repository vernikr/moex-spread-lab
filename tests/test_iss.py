import json
import unittest

from moex_spread_lab.iss import normalize_candle_borders, normalize_iss_intake


def payload():
    return {
        "securities": {
            "columns": [
                "SECID", "SHORTNAME", "ASSETCODE", "FRSTTRADE", "LOTVOLUME",
                "MINSTEP", "STEPPRICE", "INITIALMARGIN", "LASTTRADEDATE", "LASTDELDATE",
            ],
            "data": [
                ["CNYRUBF", "CNYRUBF", "CNYRUBTOM", "2022-04-26", 1000, 0.001, 1, 1000, "2100-01-01", "2100-01-01"],
                ["CRZ6", "CNY-12.26", "CNY", "2026-06-01", 1000, 0.001, 1, 1200, "2026-12-17", "2026-12-17"],
            ],
        },
        "marketdata": {
            "columns": [
                "SECID", "BOARDID", "BID", "OFFER", "LAST", "SETTLEPRICE",
                "UPDATETIME", "TIME", "SYSTIME", "TRADEDATE",
            ],
            "data": [
                ["CNYRUBF", "RFUD", 12.12, 12.121, 12.121, 12.125, "09:00:00", "08:59:00", "2026-10-27 09:15:00", "2026-10-27"],
                ["CRZ6", "RFUD", 12.30, 12.302, 12.301, 12.306, "09:00:00", "08:59:00", "2026-10-27 09:15:00", "2026-10-27"],
            ],
        },
    }


class IssIntakeTests(unittest.TestCase):
    def test_candle_borders_select_smallest_available_interval(self):
        result = normalize_candle_borders(
            {
                "borders": {
                    "columns": ["begin", "end", "interval", "board_group_id"],
                    "data": [
                        ["2022-01-01", "2026-10-27", 24, 45],
                        ["2022-01-01", "2026-10-27", 10, 45],
                        ["2022-01-01", "2026-10-27", 1, 45],
                    ],
                },
            },
            secid="CNYRUBF",
            url="https://iss.moex.com/test/candleborders",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="borders-hash",
        )
        self.assertEqual(result["minimum_interval"], 1)
        self.assertEqual(result["minimum_timeframe"], "1m")
        self.assertEqual([item["timeframe"] for item in result["available_intervals"]], ["1m", "10m", "1d"])

    def test_current_snapshot_keeps_price_roles_separate(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        near = next(item for item in result["instruments"] if item["secid"] == "CNYRUBF")
        self.assertEqual(near["settlement"], 12.125)
        self.assertEqual(near["last_trade"], 12.121)
        self.assertEqual(near["last_trade_close"], None)
        self.assertEqual(near["analytical_close"], None)
        self.assertEqual(near["live_mark"], None)
        self.assertEqual(result["admission_status"], "conditionally_admitted_analytical_only")

    def test_candidate_blockers_are_explicit(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        reasons = result["candidate"]["reasons"]
        self.assertTrue(any("executable bid/ask" in reason for reason in reasons))
        self.assertEqual(result["source"]["rights_status"], "accepted_project_assumption_personal_use")
        self.assertTrue(result["candidate"]["assumptions"])

    def test_normalized_output_is_json_serializable(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        json.dumps(result)

    def test_crossed_quote_is_flagged_without_dropping_catalog(self):
        broken = payload()
        broken["marketdata"]["data"][0][2:4] = [12.13, 12.12]
        result = normalize_iss_intake(
            broken,
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        near = next(item for item in result["instruments"] if item["secid"] == "CNYRUBF")
        self.assertEqual(near["quote_quality"], "crossed_quote")
        self.assertEqual(result["instrument_count"], 2)


if __name__ == "__main__":
    unittest.main()
