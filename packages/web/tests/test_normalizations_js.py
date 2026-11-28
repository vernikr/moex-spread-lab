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


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_ledger_table_escapes_the_skipped_entry_reason() -> None:
    """#340: a skip reason with `<` must be escaped before it reaches `innerHTML`."""
    result = _node_json(_ledger_script(render_table=True))
    assert "spread &lt; -2.0" in result["tableHtml"], result["tableHtml"]
    assert "spread < -2.0" not in result["tableHtml"], result["tableHtml"]


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_escape_html_covers_angle_brackets_quotes_and_ampersands() -> None:
    script = f"""
const dom = await import({json.dumps((_JS_DIR / "dom.js").as_uri())});
const quote = String.fromCharCode(34);
const apos = String.fromCharCode(39);
console.log(JSON.stringify({{
  out: dom.escapeHtml("a<b & " + quote + "c" + quote + " " + apos + "d"),
}}));
"""
    assert _node_json(script)["out"] == "a&lt;b &amp; &quot;c&quot; &#39;d"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_garch_normalization_does_not_explode_after_flat_opening_bars() -> None:
    """#331: a zero opening difference must not divide by the 1e-10 omega floor (5000-sigma spikes)."""
    script = f"""
const mod = await import({json.dumps((_JS_DIR / "normalizations.js").as_uri())});
const flat = [1.0, 1.0, 1.05, 1.03].map(v => ({{ spread_rel: v }}));
console.log(JSON.stringify(mod.spreadDisplayValues(flat, "garch")));
"""
    values = _node_json(script)
    assert values[0] is None and values[1] == 0, values
    assert abs(values[2]) <= 1.0 + 1e-9, values
    assert max(abs(v) for v in values if v is not None) < 10, values


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_garch_normalization_spans_a_missing_bar_without_freezing_variance() -> None:
    """#331: a null bar keeps the next valid bar (lag-aware) and must not reset the recursion."""
    script = f"""
const mod = await import({json.dumps((_JS_DIR / "normalizations.js").as_uri())});
const gapped = [1.0, 1.1, null, 1.2, 1.15].map(v => ({{ spread_rel: v }}));
const clean = [1.0, 1.1, 1.2, 1.15].map(v => ({{ spread_rel: v }}));
console.log(JSON.stringify({{
  gapped: mod.spreadDisplayValues(gapped, "garch"),
  clean: mod.spreadDisplayValues(clean, "garch"),
}}));
"""
    result = _node_json(script)
    gapped, clean = result["gapped"], result["clean"]
    assert gapped[2] is None, gapped
    assert gapped[3] is not None and gapped[4] is not None, gapped
    # Two-bar gap: the innovation is standardized by the 2-step scale, so it stays finite and signed.
    assert 0 < abs(gapped[3]) < 10, gapped
    assert abs(gapped[4]) < 10, gapped
    assert all(abs(v) < 10 for v in clean if v is not None), clean


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_ou60_normalization_pairs_only_adjacent_bars_across_gaps() -> None:
    """#328: an AR(1) fit must not treat `raw[i-1] -> raw[i+g]` across a gap as a 1-step lag."""
    script = f"""
const mod = await import({json.dumps((_JS_DIR / "normalizations.js").as_uri())});
const clean = [];
let y = 5.0;
for (let t = 0; t < 60; t++) {{
  clean.push({{ spread_rel: y }});
  y = 2.0 + 0.8 * (y - 2.0);
}}
const gapped = clean.map((r, i) => ([3, 17, 41].includes(i) ? {{ spread_rel: null }} : r));
const negative = [];
let v = 1.0;
for (let t = 0; t < 40; t++) {{
  negative.push({{ spread_rel: v }});
  v = 0.5 - 0.5 * (v - 0.5);
}}
const negativeGapped = negative.map((r, i) => (i === 37 ? {{ spread_rel: null }} : r));
console.log(JSON.stringify({{
  truth: clean[59].spread_rel - 2.0,
  gapFree: mod.spreadDisplayValues(clean, "ou60")[59],
  gapped: mod.spreadDisplayValues(gapped, "ou60")[59],
  negGapFree: mod.spreadDisplayValues(negative, "ou60")[39],
  negGapped: mod.spreadDisplayValues(negativeGapped, "ou60")[39],
}}));
"""
    out = _node_json(script)
    # A gap must not move the equilibrium residual of a stationary series by an order of magnitude.
    assert abs(out["gapFree"] - out["truth"]) < 1e-6, out
    assert abs(out["gapped"] - out["truth"]) < 1e-3, out
    # A negative-autocorrelation window keeps its sign and stays close to the gap-free fit.
    assert out["negGapped"] < 0 and abs(out["negGapped"] - out["negGapFree"]) < 1e-2, out


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
