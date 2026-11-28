import http.client
import json
import sys
from datetime import datetime
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from openpyxl import Workbook

from moex_spread_lab.cli import main
from moex_spread_lab.data.benchmark import lineage
from moex_spread_lab.data.benchmark.lineage import (
    BenchmarkError,
    adjust_vtbm_split,
    build_lineage,
    fetch_cbr_ruonia_rates,
    fetch_moex_history,
    rate_rows_to_price,
)

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RATES = [{"date": "2020-01-01", "rate": 10}, {"date": "2020-01-02", "rate": 10}]


def test_benchmark_fetch_wraps_incomplete_read(monkeypatch):
    class Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"{", 100)

    monkeypatch.setattr(lineage.urllib.request, "urlopen", lambda *args, **kwargs: Truncated())
    with pytest.raises(BenchmarkError, match="benchmark request failed") as exc:
        lineage._fetch("https://example.test", "application/json")
    assert isinstance(exc.value.__cause__, http.client.IncompleteRead)


def observed(ticker, day, price):
    return {"date": day, "price": price, "source_ticker": ticker, "source_kind": "observed"}


def transport(responses):
    """Fake ``_fetch``: ``responses`` maps a URL path fragment to ``(body, content_type)``."""

    def fetch(url, accept):
        for fragment, (body, content_type) in responses.items():
            if fragment in urlparse(url).path:
                return body, f"hash:{fragment}", content_type
        raise BenchmarkError(f"unexpected request: {url}")

    return fetch


def ruonia_xlsx():
    workbook = Workbook()
    sheet = workbook.worksheets[0]
    sheet.append(["DT", "ruo"])
    sheet.append([datetime(2010, 1, 11), 2.98])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_lineage_cli_reuses_cache_but_refreshes_unavailable_ruonia(tmp_path, monkeypatch, capsys):
    first, last = "2020-01-01", "2020-01-03"
    common = {"from": first, "till": last, "raw_response_hashes": ["fixture"], "page_count": 1}
    for ticker in ("FXMM", "VTBM", "LQDT"):
        rows = [{**observed(ticker, day, 1.01), "board": "TQTF"} for day in (first, last)]
        source = {**common, "source": ticker, "board": "TQTF", "rows": rows}
        (tmp_path / f"{ticker}.json").write_text(json.dumps(source))
    rates = [{"date": day, "rate": 6} for day in (first, last)]
    (tmp_path / "RUSFAR.json").write_text(json.dumps({**common, "source": "RUSFAR", "rows": rates}))
    ruonia = {**common, "source": "CBR", "request_count": 1, "methods": ["fixture"]}
    cached = {**ruonia, "status": "unavailable", "rows": []}
    (tmp_path / "CBR_RUONIA.json").write_text(json.dumps(cached))
    output = tmp_path / "out" / "lineage.json"
    calls = []

    def fetch_cbr(*args, **kwargs):
        calls.append((args, kwargs))
        return {**ruonia, "status": "available", "rows": rates}

    monkeypatch.setattr(lineage, "fetch_moex_history", lambda *_, **__: calls.append("moex"))
    monkeypatch.setattr(lineage, "fetch_rusfar_rates", lambda *_, **__: calls.append("rusfar"))
    monkeypatch.setattr(lineage, "fetch_cbr_ruonia_rates", fetch_cbr)
    argv = ["cli", "benchmark-lineage", "--from-date", first, "--till-date", last]
    monkeypatch.setattr(sys, "argv", [*argv, "--cache-dir", str(tmp_path), "--output", str(output)])
    assert main() == 0
    original = output.read_bytes()
    assert main() == 0
    assert output.read_bytes() == original
    assert calls == [((first, last), {"delay_seconds": 0.1})]
    result = json.loads(original)
    assert result["missing_sources"] == []
    assert result["source_reports"]["LQDT"]["board_row_counts"] == {"TQTF": 2}
    assert result["source_reports"]["CBR_RUONIA"]["methods"] == ["fixture"]


def test_rate_proxy_compounds_over_elapsed_days():
    rows = rate_rows_to_price(RATES, "CBR_RUONIA")
    assert rows[-1]["price"] > rows[0]["price"]
    assert rows[0]["source_kind"] == "synthetic_proxy"


def test_vtbm_split_adjustment_preserves_break_day_return_and_applied_factors():
    original = [
        observed("VTBM", f"2024-01-0{i}", price)
        for i, price in enumerate((1000, 1010, 1.02, 1.03), 1)
    ]
    adjusted = adjust_vtbm_split(original)
    assert [r["price"] for r in adjusted] == pytest.approx([1, 1.01, 1.02, 1.03])
    assert [r["split_adjustment"] for r in adjusted] == pytest.approx([0.001, 0.001, 1, 1])
    assert [r["price"] for r in original] == [1000, 1010, 1.02, 1.03]
    assert adjusted[2]["price"] / adjusted[1]["price"] - 1 == pytest.approx(1.02 / 1.01 - 1)


def test_vtbm_split_adjustment_matches_observed_break():
    rows = [observed("VTBM", "2020-03-23", 1008.8), observed("VTBM", "2020-03-24", 1.0084)]
    adjusted = adjust_vtbm_split(rows)
    assert [r["price"] for r in adjusted] == pytest.approx([1.0088, 1.0084])
    assert [r["split_adjustment"] for r in adjusted] == pytest.approx([0.001, 1])


def test_vtbm_multiple_inverse_splits_apply_each_factor_to_earlier_rows():
    rows = [
        observed("VTBM", f"2024-01-0{i}", price)
        for i, price in enumerate((1000, 1.01, 1020, 1030), 1)
    ]
    adjusted = adjust_vtbm_split(rows)
    assert [r["price"] for r in adjusted] == pytest.approx([1000, 1010, 1020, 1030])
    assert [r["split_adjustment"] for r in adjusted] == pytest.approx([1, 1000, 1, 1])


def test_vtbm_rejects_break_inconsistent_with_documented_split():
    rows = [observed("VTBM", "2024-01-01", 100), observed("VTBM", "2024-01-02", 10)]
    with pytest.raises(BenchmarkError, match=r"not consistent with.*1000"):
        adjust_vtbm_split(rows)


def test_lineage_marks_proxy_and_observed_periods():
    result = build_lineage(
        {
            "RUSFAR": rate_rows_to_price(RATES, "RUSFAR"),
            "VTBM": [observed("VTBM", "2020-01-02", 1.01), observed("VTBM", "2020-01-03", 1.02)],
            "LQDT": [observed("LQDT", "2020-01-03", 1.02)],
        }
    )
    assert result["rows"][-1]["source_kind"] == "observed"
    assert "synthetic_proxy" in {row["source_kind"] for row in result["rows"]}


def test_lineage_compounds_each_source_span_once_and_does_not_invent_middle_mark():
    sources = {
        "LQDT": [observed("LQDT", "2024-01-01", 100), observed("LQDT", "2024-01-03", 110)],
        "FXMM": [
            observed("FXMM", "2024-01-01", 100),
            observed("FXMM", "2024-01-02", 105),
            observed("FXMM", "2024-01-03", 110),
        ],
    }
    result = build_lineage(sources)
    assert [(row["date"], row["price"]) for row in result["rows"]] == [
        ("2024-01-01", pytest.approx(100.0)),
        ("2024-01-03", pytest.approx(110.0)),
    ]
    assert result["omitted_interior_dates"] == ["2024-01-02"]


def test_lineage_uses_nonoverlapping_returns_across_source_transitions():
    sources = {
        "LQDT": [observed("LQDT", "2024-01-02", 100), observed("LQDT", "2024-01-04", 110)],
        "FXMM": [
            observed("FXMM", f"2024-01-0{i}", value)
            for i, value in enumerate((95, 100, 105, 110), 1)
        ],
    }
    rows = build_lineage(sources)["rows"]
    assert [row["date"] for row in rows] == ["2024-01-01", "2024-01-02", "2024-01-04"]
    assert [row["price"] for row in rows] == pytest.approx([95, 100, 110])
    assert rows[0]["source_kind"] == "synthetic_proxy"
    assert rows[1]["source_kind"] == "observed"


def test_lineage_falls_back_to_valid_lower_ranked_interval():
    sources = {
        "LQDT": [observed("LQDT", "2024-01-01", 100), observed("LQDT", "2024-01-02", 150)],
        "FXMM": [observed("FXMM", "2024-01-01", 100), observed("FXMM", "2024-01-02", 110)],
    }
    result = build_lineage(sources)
    assert result["rows"][0]["price"] == pytest.approx(150 / 1.1)
    assert result["rows"][0]["source_kind"] == "synthetic_proxy"
    assert result["omitted_interior_dates"] == []


def test_lineage_refuses_disconnected_or_invalid_return_spans():
    sources = {
        "LQDT": [observed("LQDT", "2024-01-01", 100), observed("LQDT", "2024-01-03", 150)],
        "FXMM": [observed("FXMM", "2024-01-01", 100), observed("FXMM", "2024-01-02", 105)],
    }
    with pytest.raises(BenchmarkError, match="cannot link benchmark"):
        build_lineage(sources)


def test_lineage_refuses_to_skip_higher_priority_observation():
    sources = {
        "LQDT": [observed("LQDT", "2024-01-02", 105)],
        "FXMM": [observed("FXMM", "2024-01-01", 100), observed("FXMM", "2024-01-03", 110)],
    }
    with pytest.raises(BenchmarkError, match="higher-ranked"):
        build_lineage(sources)


def test_moex_history_paginates_and_preserves_board(monkeypatch):
    calls = []
    columns = ["BOARDID", "TRADEDATE", "LEGALCLOSEPRICE", "CLOSE", "WAPRICE"]

    def fetch(url, accept):
        start = int(parse_qs(urlparse(url).query)["start"][0])
        calls.append(start)
        rows = [["TQTF", f"2020-01-{index + 1:02d}", 1.0 + index, 1.0, 1.0] for index in range(100)]
        payload = {"history": {"columns": columns, "data": rows if start == 0 else []}}
        return json.dumps(payload).encode(), f"hash-{start}", "application/json"

    monkeypatch.setattr(lineage, "_fetch", fetch)
    result = fetch_moex_history("VTBM", "2020-01-01", "2020-12-31", delay_seconds=0)
    assert calls == [0, 100]
    assert result["page_count"] == 2
    assert result["raw_response_hashes"] == ["hash-0", "hash-100"]
    assert result["rows"][0]["board"] == "TQTF"
    assert result["rows"][0]["price_role"] == "legal_close"


def test_cbr_ruonia_html_rows_are_normalized(monkeypatch):
    html = "<td>11.01.2010</td><td class='x'>6,25</td>".encode("cp1251")
    monkeypatch.setattr(lineage, "_fetch", transport({"dynamics": (html, "text/html")}))
    result = fetch_cbr_ruonia_rates("2010-01-01", "2010-12-31", delay_seconds=0)
    assert result["status"] == "available"
    assert result["methods"] == ["html"]
    assert result["rows"][0] == {
        "date": "2010-01-11",
        "rate": 6.25,
        "source_ticker": "CBR_RUONIA",
        "source_kind": "synthetic_proxy",
    }


def test_cbr_ruonia_falls_back_to_current_xlsx_export(monkeypatch):
    responses = {"dynamics": (b"", "text/html"), "DownloadExcel": (ruonia_xlsx(), XLSX_TYPE)}
    monkeypatch.setattr(lineage, "_fetch", transport(responses))
    result = fetch_cbr_ruonia_rates("2010-01-01", "2010-12-31", delay_seconds=0)
    assert result["status"] == "available"
    assert result["methods"] == ["xlsx"]
    assert result["request_count"] == 2
    assert result["rows"][0]["date"] == "2010-01-11"


def test_cbr_ruonia_failures_stay_truthfully_empty(monkeypatch):
    responses = {"dynamics": (b"<td>oops</td>", "text/html"), "DownloadExcel": (b"oops", XLSX_TYPE)}
    monkeypatch.setattr(lineage, "_fetch", transport(responses))
    unparsed = fetch_cbr_ruonia_rates("2010-01-01", "2011-06-30", delay_seconds=0)
    assert unparsed["status"] == "unavailable_or_unparsed"
    assert unparsed["methods"] == ["unavailable", "unavailable"]
    assert unparsed["rows"] == []

    def refuse(url, accept):
        raise BenchmarkError("down") from OSError("refused")

    monkeypatch.setattr(lineage, "_fetch", refuse)
    offline = fetch_cbr_ruonia_rates("2010-01-01", "2010-12-31", delay_seconds=0)
    assert offline["status"] == "unavailable_or_unparsed"
    assert offline["raw_response_hashes"] == ["request_error:OSError"] * 2
