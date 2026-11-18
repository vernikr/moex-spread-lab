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
