"""The client-side CSV export reads payload fields that ``/api/proof/run`` must keep.

``ledger.js`` builds the file in the browser: a fixed 38-column header, a comment
preamble, and one row per ``series`` entry merged with ``ledger`` and ``trades``.
No route serves that CSV, so a rename on either side would silently produce an
empty column and no Python test would notice. This test reads the JavaScript
source and asserts that every column and preamble field it touches still exists
in a live payload, without needing a JS test runner.
"""

from __future__ import annotations

import json
import re
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from funding_fixtures import change, fixture, request
from moex_spread_lab.strategies.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler

LEDGER_JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "moex_spread_lab"
    / "web"
    / "static"
    / "js"
    / "ledger.js"
)

# Columns the export fills from the ledger row, and the field each one reads.
FROM_LEDGER = {
    "funding_rub": "funding_rub",
    "position": "position",
    "contracts_held": "quantity",
    "event": "event",
    "day_pnl_rub": "mark_pnl_rub",
    "costs_rub": "costs_rub",
    "cumulative_net_rub": "cumulative_net_rub",
}
# Columns the export fills from the trade list rather than from a series row: the contract count
# of the trade, keyed by its entry or exit date.
FROM_TRADES = {"entry_contracts": "entry", "exit_contracts": "exit"}
# Columns the export computes from other series fields instead of copying one.
DERIVED = {"period", "signal"}


def ledger_source() -> str:
    return LEDGER_JS.read_text(encoding="utf-8")


def export_source() -> str:
    """The body of ``downloadCsv()``: the only place the export format is decided."""
    body = re.search(r"export function downloadCsv\(\) \{(.*?)\n\}\n", ledger_source(), re.S)
    assert body, "downloadCsv() must stay a named export"
    return body.group(1)


def csv_columns() -> list[str]:
    block = re.search(r"const head = \[(.*?)\];", export_source(), re.S)
    assert block, "downloadCsv() must keep a literal column list named head"
    return re.findall(r"'([a-z0-9_]+)'", block.group(1))


def _post_proof(port: int, body_obj: dict) -> dict:
    conn = HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(
        "POST",
        "/api/proof/run",
        json.dumps(body_obj),
        {"Content-Type": "application/json"},
    )
    response = conn.getresponse()
    body = response.read()
    conn.close()
    assert response.status == 200, body[:400]
    data = json.loads(body)
    assert data["series"] and data["ledger"] and data["trades"], sorted(data)
    return data


@pytest.fixture(scope="module")
def payloads() -> dict[str, dict]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        days = fixture(root)
        # Funding kinds, dates and endpoints only appear where the rate had to be explained, so
        # the export's funding columns need the same evidence the browser smoke asserts.
        change(
            root,
            "CNYRUBF",
            {days[16]: 0, days[17]: -0.04, days[18]: None, days[19]: None, days[20]: 0.04},
        )
        store = ScreenerStore(ScreenerInputs(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sized_mr = {**request("mean_reversion"), "sizing": "max_affordable"}
            sized_mr.pop("quantity")
            return {
                "fixed_mr": _post_proof(server.server_port, request("mean_reversion")),
                "fixed_carry": _post_proof(
                    server.server_port, {**request("funding_only"), "quantity": 2}
                ),
                "sized_mr": _post_proof(server.server_port, sized_mr),
            }
        finally:
            server.shutdown()
            server.server_close()
            store.close()
            thread.join()


@pytest.fixture(scope="module")
def payload(payloads: dict[str, dict]) -> dict:
    return payloads["fixed_mr"]


def test_every_exported_column_is_unique_and_accounted_for(payload: dict) -> None:
    columns = csv_columns()
    assert len(columns) == len(set(columns)) == 38, columns
    known = set(FROM_LEDGER) | set(FROM_TRADES) | DERIVED
    series_columns = [c for c in columns if c not in known]
    # A funding field only appears on the rows it applies to, so "somewhere in the series" is the
    # contract; the price columns must be on every row or the export is silently short.
    rows = payload["series"]
    missing = [c for c in series_columns if not any(c in row for row in rows)]
    assert not missing, f"columns read a series field the payload no longer has: {missing}"
    price = ["date", "near", "far", "spread", "spread_volume"]
    assert [c for c in price if any(c not in row for row in rows)] == []
    assert {"funding_rate_kind", "funding_left_rate", "funding_right_rate"} <= set(series_columns)


def test_ledger_and_trade_columns_still_read_their_fields(payloads: dict[str, dict]) -> None:
    assert "l?.quantity," in export_source()
    for label, data in payloads.items():
        ledger, trade = data["ledger"], data["trades"][0]
        absent = {f for f in FROM_LEDGER.values() if not any(f in row for row in ledger)}
        assert absent == set(), (label, absent)
        assert all(
            "quantity" in row
            and type(row["quantity"]) is int
            and row["quantity"] >= 0
            and (row["position"] == 0) == (row["quantity"] == 0)
            for row in ledger
        ), label
        held = {row["quantity"] for row in ledger if row["position"] != 0}
        assert held and held <= {t["quantity"] for t in data["trades"]}, (label, held)
        assert any(row["position"] == 0 and row["quantity"] == 0 for row in ledger), label
        broken = sorted(c for c, side in FROM_TRADES.items() if "date" not in trade[side])
        assert broken == [] and "quantity" in trade, (label, broken)


def test_preamble_only_interpolates_fields_the_payload_carries(payload: dict) -> None:
    fields = set(re.findall(r"state\.payload\.([a-z_]+)", export_source()))
    assert {"schema_version", "run_id", "price_role", "provenance", "notes"} <= fields
    assert sorted(fields - set(payload)) == [], sorted(fields - set(payload))
    assert isinstance(payload["notes"], list) and payload["notes"], "notes.join() needs a list"
