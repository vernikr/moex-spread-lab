"""UI normalization regressions executed against the shipped ES module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from string import Template

import pytest

_JS_DIR = Path(__file__).resolve().parents[1] / "src/moex_spread_lab/web/static/js"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_ols_normalization_evaluates_fit_at_the_current_gap_preserving_x() -> None:
    script = f"""
const mod = await import({json.dumps((_JS_DIR / "normalizations.js").as_uri())});
const series = Array.from({{ length: 12 }}, (_, i) => ({{
  spread_rel: i === 4 || i === 5 ? null : i * 0.1,
}}));
const out = mod.spreadDisplayValues(series, 'ols20');
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "--no-warnings", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(proc.stdout)

    assert values[4:6] == [None, None]
    assert abs(values[1] - 0.05) < 1e-12
    assert all(abs(value) < 1e-12 for value in values[6:] if value is not None)


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "--no-warnings", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _node_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "--no-warnings", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


_CR = chr(13)
_EVENT = "line" + _CR + "two"
_ROW = {
    "near": 1,
    "far": 2,
    "spread": 3,
    "spread_volume": 4,
    "spread_rel": 0.1,
    "spread_open_rel": None,
    "spread_last_trade_rel": None,
}
_PAYLOAD = {
    "schema_version": "1",
    "run_id": "r",
    "price_role": "settlement",
    "provenance": {},
    "notes": ["n"],
    "evaluation": {"periods": {}},
    "selection": {"timeframe": "1d"},
    "window": {},
    "summary": {},
    "series": [{**_ROW, "date": "2025-07-02"}, {**_ROW, "date": "2025-07-05"}],
    "ledger": [
        {
            "date": "2025-07-05",
            "costs_rub": 1,
            "cumulative_net_rub": 2,
            "mark_pnl_rub": 3,
            "event": _EVENT,
        }
    ],
    "skipped_entries": [{"date": "2025-07-02", "reason": "spread < -2.0"}],
    "trades": [
        {
            "number": 1,
            "quantity": 3,
            "entry": {"date": "2025-07-01"},
            "exit": {"date": "2025-07-05"},
        },
        {
            "number": 2,
            "quantity": 5,
            "entry": {"date": "2025-07-05"},
            "exit": {"date": "2025-07-05"},
        },
    ],
}


def _ledger_script(render_table: bool) -> str:
    return Template(_LEDGER_SCRIPT).substitute(
        charts=json.dumps((_JS_DIR / "charts.js").as_uri()),
        dom=json.dumps((_JS_DIR / "dom.js").as_uri()),
        ledger=json.dumps((_JS_DIR / "ledger.js").as_uri()),
        state=json.dumps((_JS_DIR / "state.js").as_uri()),
        payload=json.dumps(_PAYLOAD),
        render_table="true" if render_table else "false",
    )


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_ledger_csv_sums_same_bar_contracts_and_quotes_carriage_returns() -> None:
    """#333: same-bar close+open must sum contracts (not keep the last trade) and `\\r` needs quoting."""
    result = _node_json(_ledger_script(render_table=False))
    assert result["entry"] == "5", result
    assert result["exit"] == "8", result
    assert f'"line{_CR}two"' in result["csv"], result["csv"]


_LEDGER_SCRIPT = """
const charts = await import($charts);
const dom = await import($dom);
const ledger = await import($ledger);
const stateModule = await import($state);
let csv = null;
globalThis.Blob = class { constructor(parts) { this.parts = parts; } };
globalThis.URL = { createObjectURL: (b) => { csv = b; return "blob:x"; }, revokeObjectURL: () => {} };
globalThis.setTimeout = () => 0;
const mkCell = (tag) => ({
  tag, innerHTML: "", textContent: "", children: [], dataset: {}, colSpan: 1,
  className: "", classList: { contains: () => false, add: () => {}, remove: () => {} },
  style: {}, click: () => {},
  append(...kids) { this.children.push(...kids); },
  querySelectorAll: () => [],
  addEventListener: () => {},
  replaceChildren() {},
});
const tableRoot = mkCell("div");
globalThis.document = {
  getElementById: (id) => (id === "ledger-table" ? tableRoot : null),
  createElement: (tag) => mkCell(tag),
  createDocumentFragment: () => mkCell("fragment"),
  dispatchEvent: () => {},
};
globalThis.window = {};
const state = stateModule.state;
state.payload = $payload;
state.universe = "pair_screen";
ledger.downloadCsv();
if ($render_table) ledger.renderTable();
const text = csv.parts.join("");
const lines = text.split(String.fromCharCode(10));
const head = lines.filter(l => l.startsWith("date,"))[0].split(",");
const picked = lines.filter(l => l.startsWith("2025-07-05")).pop().split(",");
const collect = (node, out = []) => {
  for (const k of (node && node.children) || []) {
    out.push(String((k && k.innerHTML) || ""));
    collect(k, out);
  }
  return out;
};
console.log(JSON.stringify({
  entry: picked[head.indexOf("entry_contracts")],
  exit: picked[head.indexOf("exit_contracts")],
  csv: text,
  tableHtml: collect(tableRoot).join("|"),
}));
"""
