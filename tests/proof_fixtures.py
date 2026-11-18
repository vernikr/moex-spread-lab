"""Two synthetic proof universes with deliberately disjoint histories."""

import json

from test_calendar_proof import _build_root as calendar_root
from test_execution_proof import _build_root as pair_root


def fixture(root):
    pair_root(root)
    paths = [
        (root / "data/w1/current-intake.json", "instruments"),
        (root / "data/benchmark/lqdt-lineage.json", "rows"),
    ]
    previous = [json.loads(p.read_text()) for p, _ in paths]
    calendar_root(root)
    for (path, key), data in zip(paths, previous):
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
            row["initial_margin"] = (
                6000  # ~12k actual margin fits; 3× margin plus buffers fails.
            )
    path.write_text(json.dumps(doc))
