"""Regression coverage for the ISS catalog facade (P7.2 leaf #53).

The facade must preserve identity for every re-exported object, including
the historical private entry points, so downstream patch points and the
shared history JSON cache keep working unchanged.
"""

import unittest

from moex_spread_lab.data.iss import (
    catalog,
    iss_candles,
    iss_coverage,
    iss_history,
    iss_http,
    iss_snapshot,
)


class CatalogFacadeTests(unittest.TestCase):
    def test_all_exports_exist_and_match_origin_identity(self):
        origins = {
            "RawSinkCallback": iss_candles,
            "analyze_candle_pair": iss_candles,
            "fetch_candle_series": iss_candles,
            "normalize_candles": iss_candles,
            "CSV_COLUMNS": iss_coverage,
            "fetch_candle_coverage": iss_coverage,
            "normalize_candle_borders": iss_coverage,
            "write_timeframe_inventory": iss_coverage,
            "HISTORY_FIELDS": iss_history,
            "HISTORY_PAGE_SIZE": iss_history,
            "HISTORY_SCHEMA": iss_history,
            "IssHistoryError": iss_history,
            "fetch_history": iss_history,
            "CANDLE_FIELDS": iss_http,
            "CANDLE_INTERVAL_LABELS": iss_http,
            "CANDLE_PAGE_SIZE": iss_http,
            "DEFAULT_BOARD": iss_http,
            "DEFAULT_FAR": iss_http,
            "DEFAULT_ISS_BASE_URL": iss_http,
            "DEFAULT_NEAR": iss_http,
            "HISTORICAL_SPECIFICATION_STATUS": iss_http,
            "ISS_MODULE_SCHEMA": iss_http,
            "MARKETDATA_FIELDS": iss_http,
            "PROJECT_RIGHTS_STATUS": iss_http,
            "SECURITY_FIELDS": iss_http,
            "IssIntakeError": iss_http,
            "fetch_json_with_raw": iss_http,
            "fetch_iss_catalog": iss_snapshot,
            "normalize_iss_intake": iss_snapshot,
            "normalize_iss_payload": iss_snapshot,
        }
        private_origins = {
            "_HISTORY_JSON_CACHE": iss_history,
            "_fetch_history_json": iss_history,
            "_history_rows": iss_history,
            "_history_url": iss_history,
            "_candleborders_url": iss_http,
            "_candles_url": iss_http,
            "_fetch_json": iss_http,
            "_rows": iss_http,
            "_url": iss_http,
            "_candidate_status": iss_snapshot,
            "_quote_quality": iss_snapshot,
        }
        self.assertEqual(set(catalog.__all__), set(origins) | set(private_origins))
        for name, origin in {**origins, **private_origins}.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(catalog, name))
                self.assertIs(getattr(catalog, name), getattr(origin, name))

    def test_public_aliases_in_origin_modules_are_identical(self):
        self.assertIs(iss_http.candleborders_url, iss_http._candleborders_url)
        self.assertIs(iss_http.fetch_json, iss_http._fetch_json)
        self.assertIs(iss_http.candles_url, iss_http._candles_url)
        self.assertIs(iss_http.rows, iss_http._rows)
        self.assertIs(iss_http.url, iss_http._url)
        self.assertIs(iss_history.HISTORY_JSON_CACHE, iss_history._HISTORY_JSON_CACHE)
        self.assertIs(iss_history.fetch_history_json, iss_history._fetch_history_json)
        self.assertIs(iss_history.history_rows, iss_history._history_rows)
        self.assertIs(iss_history.history_url, iss_history._history_url)
        self.assertIs(iss_snapshot.candidate_status, iss_snapshot._candidate_status)
        self.assertIs(iss_snapshot.quote_quality, iss_snapshot._quote_quality)

    def test_private_facade_behaviour_is_unchanged_offline(self):
        self.assertEqual(catalog._quote_quality(None, 1.0), "missing_quote")
        self.assertEqual(catalog._quote_quality(2.0, 1.0), "crossed_quote")
        self.assertEqual(catalog._quote_quality(1.0, 2.0), "two_sided_quote")
        url = catalog._history_url("CRZ6", "2026-01-01", "2026-01-02", 0)
        self.assertIn("securities/CRZ6.json", url)
        self.assertIn("from=2026-01-01", url)
        self.assertIn("till=2026-01-02", url)
        rows = catalog._history_rows({"columns": ["A", "B"], "data": [[1, 2], [3, 4]]}, "history")
        self.assertEqual(rows, [{"A": 1, "B": 2}, {"A": 3, "B": 4}])
        status = catalog._candidate_status([], "CNYRUBF", "CRZ6")
        self.assertEqual(status["status"], "not_admitted")
        self.assertEqual(status["candidate_id"], "CNYRUBF/CRZ6")

    def test_package_init_reexports_history_facade(self):
        from moex_spread_lab.data.iss import _HISTORY_JSON_CACHE, _history_url

        self.assertIs(_HISTORY_JSON_CACHE, catalog._HISTORY_JSON_CACHE)
        self.assertIs(_history_url, catalog._history_url)


if __name__ == "__main__":
    unittest.main()
