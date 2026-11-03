import unittest
from datetime import datetime
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from openpyxl import Workbook

from moex_spread_lab.benchmark import (
    adjust_vtbm_split,
    build_lineage,
    fetch_cbr_ruonia_rates,
    fetch_moex_history,
    rate_rows_to_price,
)


class BenchmarkTests(unittest.TestCase):
    def test_rate_proxy_compounds_over_elapsed_days(self):
        rows = rate_rows_to_price(
            [{"date": "2020-01-01", "rate": 10}, {"date": "2020-01-02", "rate": 10}],
            "CBR_RUONIA",
        )
        self.assertGreater(rows[-1]["price"], rows[0]["price"])
        self.assertEqual(rows[0]["source_kind"], "synthetic_proxy")

    def test_vtbm_split_adjustment_removes_large_break(self):
        adjusted = adjust_vtbm_split([
            {"date": "2020-03-23", "price": 1008.8, "source_ticker": "VTBM", "source_kind": "observed"},
            {"date": "2020-03-24", "price": 1.01, "source_ticker": "VTBM", "source_kind": "observed"},
        ])
        self.assertAlmostEqual(adjusted[0]["price"], adjusted[1]["price"], places=4)
        self.assertAlmostEqual(adjusted[1]["price"], 1.01, places=4)

    def test_lineage_marks_proxy_and_observed_periods(self):
        result = build_lineage({
            "RUSFAR": rate_rows_to_price([{"date": "2020-01-01", "rate": 10}, {"date": "2020-01-02", "rate": 10}], "RUSFAR"),
            "VTBM": [{"date": "2020-01-02", "price": 1.01, "source_ticker": "VTBM", "source_kind": "observed"}, {"date": "2020-01-03", "price": 1.02, "source_ticker": "VTBM", "source_kind": "observed"}],
            "LQDT": [{"date": "2020-01-03", "price": 1.02, "source_ticker": "LQDT", "source_kind": "observed"}],
        })
        self.assertEqual(result["rows"][-1]["source_kind"], "observed")
        self.assertIn("synthetic_proxy", {row["source_kind"] for row in result["rows"]})

    def test_moex_history_paginates_and_preserves_board(self):
        calls = []
        columns = ["BOARDID", "TRADEDATE", "LEGALCLOSEPRICE", "CLOSE", "WAPRICE"]

        def response(url):
            start = int(parse_qs(urlparse(url).query)["start"][0])
            calls.append(start)
            rows = [["TQTF", f"2020-01-{index + 1:02d}", 1.0 + index, 1.0 + index, 1.0 + index] for index in range(100)] if start == 0 else []
            return {"history": {"columns": columns, "data": rows}}, f"hash-{start}"

        with patch("moex_spread_lab.benchmark._request_json", side_effect=response):
            result = fetch_moex_history("VTBM", "2020-01-01", "2020-12-31", delay_seconds=0)

        self.assertEqual(calls, [0, 100])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["rows"][0]["board"], "TQTF")

    def test_cbr_ruonia_html_rows_are_normalized(self):
        html = "<td>11.01.2010</td><td class='x'>6,25</td>"
        with patch("moex_spread_lab.benchmark._request_text", return_value=(html, "cbr-hash")):
            result = fetch_cbr_ruonia_rates("2010-01-01", "2010-12-31", delay_seconds=0)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["rows"][0]["date"], "2010-01-11")
        self.assertEqual(result["rows"][0]["rate"], 6.25)

    def test_cbr_ruonia_falls_back_to_current_xlsx_export(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["DT", "ruo"])
        sheet.append([datetime(2010, 1, 11), 2.98])
        stream = BytesIO()
        workbook.save(stream)
        with patch("moex_spread_lab.benchmark._request_text", return_value=("", "html-hash")), patch(
            "moex_spread_lab.benchmark._request_bytes",
            return_value=(stream.getvalue(), "xlsx-hash", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ):
            result = fetch_cbr_ruonia_rates("2010-01-01", "2010-12-31", delay_seconds=0)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["methods"], ["xlsx"])
        self.assertEqual(result["rows"][0]["date"], "2010-01-11")


if __name__ == "__main__":
    unittest.main()
