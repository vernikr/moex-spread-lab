"""Two synthetic proof universes with deliberately disjoint histories."""

import json

from test_calendar_proof import _build_root as calendar_root
from test_execution_proof import _build_root as pair_root

# Canonical replay rows over the synthetic `fixture` workspace: the pair-screen
# mean-reversion winner, the native SR calendar and its flat SRM3/SRU3 sibling.
PAIR = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    "sizing": "max_affordable",
}
CAL = {
    "universe": "calendar_native",
    "asset": "SR",
    "pair": "SRH3/SRM3",
    "strategy": "zscore_mean_reversion",
    "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    "sizing": "max_affordable",
}
FLAT = {**CAL, "pair": "SRM3/SRU3"}


def fixture(root):
    pair_root(root)
    paths = [
        (root / "data/w1/current-intake.json", "instruments"),
        (root / "data/benchmark/lqdt-lineage.json", "rows"),
    ]
    previous = [json.loads(p.read_text()) for p, _ in paths]
    calendar_root(root)
    for (path, key), data in zip(paths, previous, strict=True):
        if key == "instruments":
            for contract in data[key]:
                contract["settlement"] = (
                    13.0  # synthetic CNY reference for the combined calendar catalog
                )
        data[key] = json.loads(path.read_text())[key] + data[key]
        path.write_text(json.dumps(data))


def two_calendar_windows(root):
    """A second, deliberately flat spread in the *same* asset's lower picker list."""
    from test_calendar_proof import _write_history

    fixture(root)
    path = next((root / "data/spread-history-cache").glob("SRM3-*.json"))
    data = json.loads(path.read_text())["payload"]["history"]
    cols = data["columns"]
    rows = data["data"]
    # The next near must expire before the last cached observation to form a complete window.
    _write_history(root, "SRM3", rows[:-1])
    far = []
    spread = []
    for r in rows:
        f = r.copy()
        s = r.copy()
        f[cols.index("SECID")] = "SRU3"
        s[cols.index("SECID")] = "SRM3SRU3"
        for key in ("OPEN", "HIGH", "LOW", "CLOSE", "SETTLEPRICE"):
            i = cols.index(key)
            f[i] += 50
            s[i] = 50
        far.append(f)
        spread.append(s)
    _write_history(root, "SRU3", far)
    _write_history(root, "SRM3SRU3", spread)


def screen_failures(root, *, thin_calendar=False):
    """Independent gates: one liquid but stress-failing window, one thin but funded."""
    fixture(root)
    if thin_calendar:
        path = next((root / "data/spread-history-cache").glob("SRH3SRM3-*.json"))
        doc = json.loads(path.read_text())
        block = doc["payload"]["history"]
        block["data"][4][block["columns"].index("VOLUME")] = 1
    else:
        path = root / "data/w1/candles/CRZ6_1d.json"
        doc = json.loads(path.read_text())
        doc["rows"][4]["volume"] = 1
    path.write_text(json.dumps(doc))
    path = root / "data/w1/current-intake.json"
    doc = json.loads(path.read_text())
    for row in doc["instruments"]:
        if row["secid"] in (("CNYRUBF", "CRZ6") if thin_calendar else ("SRZ6",)):
            row["initial_margin"] = 6000  # ~12k actual margin fits; 3× margin plus buffers fails.
    path.write_text(json.dumps(doc))


def funding_winner(root):
    """Flat leg prices and positive published funding: MR idle, funded carry profitable.

    Deliberately synthetic choice-of-strategy fixture, never market evidence.
    """
    two_calendar_windows(root)
    for secid in ("CNYRUBF", "CRZ6"):
        path = root / f"data/w1/candles/{secid}_1d.json"
        data = json.loads(path.read_text())
        for row in data["rows"]:
            for key in ("open", "high", "low", "close"):
                row[key] = 13.0
        path.write_text(json.dumps(data))
        for path in (root / "data/carry-history-cache").glob(f"{secid}-*.json"):
            data = json.loads(path.read_text())
            block = data["payload"]["history"]
            for row in block["data"]:
                for key in ("SETTLEPRICE", "PREVSETTLEPRICE", "LASTTRADEPRICE"):
                    if key in block["columns"]:
                        row[block["columns"].index(key)] = 13.0
            path.write_text(json.dumps(data))


def rising_calendar(root):
    """Synthetic dividend-free rising spread: delayed convergence can beat idle MR."""
    paths = {
        s: next((root / "data/spread-history-cache").glob(f"{s}-*.json"))
        for s in ("SRM3", "SRU3", "SRM3SRU3")
    }
    docs = {s: json.loads(p.read_text()) for s, p in paths.items()}
    near = docs["SRM3"]["payload"]["history"]
    reference = {
        r[near["columns"].index("TRADEDATE")]: r[near["columns"].index("SETTLEPRICE")]
        for r in near["data"]
    }
    for secid in ("SRU3", "SRM3SRU3"):
        block = docs[secid]["payload"]["history"]
        columns = block["columns"]
        i = 0
        for row in block["data"]:
            day = row[columns.index("TRADEDATE")]
            if day not in reference:
                continue
            if day >= "2023-03-17":
                i += 1
            spread = 50 + i * 10
            for field in ("OPEN", "HIGH", "LOW", "CLOSE", "SETTLEPRICE"):
                row[columns.index(field)] = (reference[day] + spread) if secid == "SRU3" else spread
        paths[secid].write_text(json.dumps(docs[secid]))
