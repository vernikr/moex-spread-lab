"""Compact, synthetic exchange pages for signed cash and session-evidence tests."""

import json

from moex_spread_lab.data import history_url
from moex_spread_lab.data.universe import HISTORY_WINDOWS
from moex_spread_lab.strategies.pair_screener import ScreenerInputs
from test_execution_proof import _build_root

PAIR = "CNYRUBF/CRZ6"
WEEKEND_PAIR = "GAZPF/GZH6"


def request(strategy="mean_reversion", pair=PAIR):
    return {
        "universe": "pair_screen",
        "pair": pair,
        "strategy": strategy,
        "quantity": 2,
        "parameters": (
            {"lookback": 5, "entry_z": 1.5, "exit_z": 0.3}
            if strategy == "mean_reversion"
            else {"lookback": 5}
        ),
    }


def page(root, secid, pair=PAIR):
    a, b = HISTORY_WINDOWS[pair]
    return ScreenerInputs(root).history_cache(pair) / f"{secid}-{a}-{b}-0.json"


def change(root, secid, values, *, pair=PAIR, field="SWAPRATE", remove=False):
    p = page(root, secid, pair)
    doc = json.loads(p.read_text())
    block = doc["payload"]["history"]
    cols = block["columns"]
    d = cols.index("TRADEDATE")
    i = cols.index(field)
    for r in block["data"]:
        if r[d] in values:
            r[i] = values[r[d]]
    if remove:
        block["data"] = [r for r in block["data"] if r[d] not in values]
        doc["payload"]["history.cursor"]["data"][0][1] = len(block["data"])
    p.write_text(json.dumps(doc))


def fixture(root):
    return [d.isoformat() for d in _build_root(root)]


def weekend(root):
    """Clone CNY-shaped synthetic prices under a launch-weekend perpetual; no weekend EOD or LQDT."""
    from moex_spread_lab.strategies.funding_carry.proof_funding import NO_CLEARING

    src = ScreenerInputs(root)
    near, far = WEEKEND_PAIR.split("/")
    contracts = json.loads(src.contracts.read_text())
    for old, new in zip(PAIR.split("/"), (near, far), strict=True):
        contract = next(r for r in contracts["instruments"] if r["secid"] == old)
        contracts["instruments"].append(
            dict(
                contract,
                secid=new,
                stable_id=new,
                tariff_group="perpetual_stock" if new == near else "stock",
            )
        )
        candles = json.loads(src.candles(PAIR, old).read_text())
        candles["secid"] = new
        friday = next(r for r in candles["rows"] if r["begin"][:10] == "2025-08-15")
        candles["rows"] += [dict(friday, begin=f"{d} 00:00:00") for d in sorted(NO_CLEARING)]
        candles["rows"].sort(key=lambda r: r["begin"])
        src.candles(WEEKEND_PAIR, new).write_text(json.dumps(candles))
        doc = json.loads(page(root, old).read_text())
        block = doc["payload"]["history"]
        idx = block["columns"].index("SECID")
        for row in block["data"]:
            row[idx] = new
        doc["url"] = history_url(new, *HISTORY_WINDOWS[WEEKEND_PAIR], 0)
        p = page(root, new, WEEKEND_PAIR)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))
    src.contracts.write_text(json.dumps(contracts))
