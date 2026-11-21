"""A registered daily pair plugin enters proof and Optuna with validated accounting."""
import os
from pathlib import Path
import subprocess
import sys


def test_drop_in_pair_plugin_proof_and_search(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "demo_pair.py").write_text('''
from dataclasses import replace
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
class Demo(PairMeanReversionPlugin):
    strategy_id = "demo_pair"
    family_id = "demo_pair"
    proof_adapter = "daily_pair_price_intents.v1"
    requires_daily_bars = True
    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(run, intents=tuple({**item, "signal": {"kind": "z",
            "date": days[days.index(item["entry_date"][:10]) - delay],
            "value": item["entry_signal_z"]}} for item in run.intents))
PLUGIN = Demo()
class HourlyDemo(Demo):
    strategy_id = "demo_pair_hourly"
    family_id = "demo_pair_hourly"
    proof_adapter = "pair_price_intents.v1"
    requires_daily_bars = False
    def evaluate_intents(self, window, params, fill):
        run = PairMeanReversionPlugin.evaluate_intents(self, window, params, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(run, intents=tuple({**item, "signal": {"kind": "z",
            "date": days[days.index(item["entry_date"][:10] if window.timeframe == "1d" else item["entry_date"][:19].replace(" ", "T")) - delay],
            "value": item["entry_signal_z"]}} for item in run.intents))
HOURLY = HourlyDemo()
''')
    dist = plugin_dir / "demo_pair-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-pair\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text("[moex_spread_lab.strategies]\ndemo_pair = demo_pair:PLUGIN\ndemo_pair_hourly = demo_pair:HOURLY\n")
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(plugin_dir), str(root / "tests"), str(root / "packages/proof/tests"), str(root / "packages/accounting/tests"), env.get("PYTHONPATH", "")])
    env["MOEX_PROOF_WORKERS"] = "1"
    code = '''
import sys
from pathlib import Path
from funding_fixtures import PAIR, WEEKEND_PAIR, fixture, request, weekend
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_kernel import STRATEGY_AXES
root = Path(sys.argv[1])
fixture(root)
weekend(root)
# Add one registered dated pair, with synthetic copies of the same bars.
import json
path = root / "data/w1/current-intake.json"
contracts = json.loads(path.read_text())
for old, new in (("CNYRUBF", "GLZ6"), ("CRZ6", "GLH7")):
    original = next(c for c in contracts["instruments"] if c["secid"] == old)
    contracts["instruments"].append({**original, "secid": new, "stable_id": new})
    source = root / "data/w1/candles" / (old + "_1d.json")
    bars = json.loads(source.read_text())
    bars["secid"] = new
    (source.parent / (new + "_1d.json")).write_text(json.dumps(bars))
path.write_text(json.dumps(contracts))
save_settings(root, {"random_seed": 41, "validation_percent": 20})
assert "demo_pair" in STRATEGY_AXES
service = ProofService(root)
from moex_spread_lab.core.settings import ResearchSettings
for fill in ("next_open", "next_close", "reference_same_bar"):
    base = {**request(), "sizing": "max_affordable", "fill": fill}
    base.pop("quantity")
    candidate = {**base, "strategy": "demo_pair"}
    actual = research_run(service, candidate, optimize=False, include_plateau=False)
    expected = research_run(service, base, optimize=False, include_plateau=False)
    assert actual["selection"]["strategy"] == "demo_pair"
    assert actual["trades"] == expected["trades"], (fill, actual["trades"], expected["trades"])
    assert actual["ledger"] == expected["ledger"], (fill, next(((a,e) for a,e in zip(actual["ledger"],expected["ledger"]) if a != e), None))
    assert actual["summary"]["ledger_reconciliation_rub"] == 0
    assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
for fill in ("next_open", "next_close", "reference_same_bar"):
    base = {**request(), "fill": fill}
    fixed = {**base, "strategy": "demo_pair"}
    actual = research_run(service, fixed, optimize=False, include_plateau=False)
    expected = research_run(service, base, optimize=False, include_plateau=False)
    assert actual["trades"] == expected["trades"], (fill, actual["trades"], expected["trades"])
    assert actual["ledger"] == expected["ledger"], (fill, actual["ledger"], expected["ledger"])
    assert actual["summary"] == expected["summary"], (fill, actual["summary"], expected["summary"])
    assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
    cutoff = actual["evaluation"]["periods"]["training"][-1]
    actual_prefix = service.prepare(fixed, settings=ResearchSettings(validation_percent=20))(fixed, through=cutoff)
    expected_prefix = service.prepare(base, settings=ResearchSettings(validation_percent=20))(base, through=cutoff)
    assert actual_prefix["trades"] == expected_prefix["trades"]
    assert actual_prefix["ledger"] == expected_prefix["ledger"]
    assert actual_prefix["summary"] == expected_prefix["summary"]
for fill in ("next_open", "next_close", "reference_same_bar"):
    weekend_request = {**request(pair=WEEKEND_PAIR), "fill": fill}
    actual_weekend = research_run(service, {**weekend_request, "strategy": "demo_pair"}, optimize=False, include_plateau=False)
    expected_weekend = research_run(service, weekend_request, optimize=False, include_plateau=False)
    assert actual_weekend["trades"] == expected_weekend["trades"]
    assert actual_weekend["ledger"] == expected_weekend["ledger"]
    assert actual_weekend["summary"] == expected_weekend["summary"]
    dated = {**request(pair="GLZ6/GLH7"), "fill": fill}
    actual = research_run(service, {**dated, "strategy": "demo_pair"}, optimize=False, include_plateau=False)
    expected = research_run(service, dated, optimize=False, include_plateau=False)
    assert actual["trades"] == expected["trades"]
    assert actual["ledger"] == expected["ledger"]
    assert actual["summary"] == expected["summary"]
for qty in (1, 3):
    for slip in (0.0, 2.5):
        base = {**request(), "fill": "next_open", "quantity": qty, "slippage_ticks": slip}
        actual = research_run(service, {**base, "strategy": "demo_pair"}, optimize=False, include_plateau=False)
        expected = research_run(service, base, optimize=False, include_plateau=False)
        assert actual["trades"] == expected["trades"]
        assert actual["ledger"] == expected["ledger"]
        assert actual["summary"] == expected["summary"]
try:
    service.prepare({**candidate, "quantity": 2})
except W0Error:
    pass
else:
    raise AssertionError("auto sizing must reject an explicit quantity")
from moex_spread_lab.core.settings import ResearchSettings
try:
    service.prepare(candidate, settings=ResearchSettings(timeframe="1h"))
except W0Error:
    pass
else:
    raise AssertionError("hourly plugin accounting must fail closed")
prepared = service.prepare(candidate, settings=ResearchSettings())
try:
    prepared({**candidate, "pair": "GLZ6/GLH7"})
except W0Error:
    pass
else:
    raise AssertionError("prepared replay must reject another pair")
search = research_run(service, {**candidate, "optimize_strategy": True}, optimize=True)
row = next(item for item in search["strategy_search"]["alternatives"] if item["strategy"] == "demo_pair")
assert row["trial_count"] == 80, row

# Force the same two-window cohort through local and worker-process paths.
import os
cohort = {**candidate, "targets": [{"universe": "pair_screen", "pair": PAIR},
                                   {"universe": "pair_screen", "pair": WEEKEND_PAIR},
                                   {"universe": "pair_screen", "pair": "GLZ6/GLH7"}]}
os.environ["MOEX_PROOF_WORKERS"] = "1"
serial = research_run(ProofService(root), cohort, optimize=True)
os.environ["MOEX_PROOF_WORKERS"] = "2"
os.environ["MOEX_PROOF_FORCE_POOL"] = "1"
parallel_service = ProofService(root)
parallel = research_run(parallel_service, cohort, optimize=True)
assert len(parallel_service._pool._worker_slots) == 2
assert serial["selected_parameters"] is not None, serial
assert serial["selected_parameters"] == parallel["selected_parameters"], (serial, parallel)
assert serial["run_id"] == parallel["run_id"], (serial["run_id"], parallel["run_id"])
parallel_service.close()

# Hourly bars use prior daily LQDT and exclude daily funding, as native proof does.
import math
from test_execution_proof import _near_close, _far_close
for secid, close_fn in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
    path = root / "data/w1/candles" / (secid + "_1d.json")
    daily = json.loads(path.read_text())
    hourly = []
    for i, day_row in enumerate(daily["rows"]):
        for j, hour in enumerate((9, 10, 11, 12)):
            close = close_fn(i) + .28 * math.sin(i * .65 + j * 1.1 + (secid == "CRZ6"))
            hourly.append({"begin": day_row["begin"][:10] + f" {hour:02}:00:00", "open": close-.025,
                "high": close+.04, "low": close-.04, "close": close, "volume": 200,
                "price_proxy_kind": "candle_close"})
    (path.parent / (secid + "_1h.json")).write_text(json.dumps({"schema_version": "iss-candles.v1",
        "secid": secid, "interval": 60, "timeframe": "1h", "rows": hourly}))
save_settings(root, {"timeframe": "1h", "validation_percent": 20, "random_seed": 41})
h1 = ProofService(root)
for fill in ("next_open", "next_close", "reference_same_bar"):
    for auto in (False, True):
        base = {**request(), "fill": fill}
        if auto:
            base.pop("quantity")
            base["sizing"] = "max_affordable"
        custom = {**base, "strategy": "demo_pair_hourly"}
        actual = research_run(h1, custom, optimize=False, include_plateau=False)
        expected = research_run(h1, base, optimize=False, include_plateau=False)
        assert actual["trades"] == expected["trades"], (fill, auto, actual["trades"], expected["trades"])
        assert actual["ledger"] == expected["ledger"], (fill, auto, next(((a,e) for a,e in zip(actual["ledger"], expected["ledger"]) if a != e), None))
        assert actual["summary"] == expected["summary"], (fill, auto, actual["summary"], expected["summary"])
        assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
        assert actual["provenance"]["funding_applicability"] == "intraday_not_included"
        cutoff = actual["evaluation"]["periods"]["training"][-1]
        settings = ResearchSettings(timeframe="1h", validation_percent=20)
        a = h1.prepare(custom, settings=settings)(custom, through=cutoff)
        e = h1.prepare(base, settings=settings)(base, through=cutoff)
        assert a["trades"] == e["trades"] and a["ledger"] == e["ledger"]
        day_cutoff = cutoff[:10]
        a_day = h1.prepare(custom, settings=settings)(custom, through=day_cutoff)
        e_day = h1.prepare(base, settings=settings)(base, through=day_cutoff)
        assert a_day["trades"] == e_day["trades"] and a_day["ledger"] == e_day["ledger"]
h1_search = research_run(h1, {**custom, "optimize_strategy": True}, optimize=True)
h1_row = next(row for row in h1_search["strategy_search"]["alternatives"] if row["strategy"] == "demo_pair_hourly")
assert h1_row["trial_count"] == 80, h1_row
assert all(row["strategy"] != "demo_pair" for row in h1_search["strategy_search"]["alternatives"])
h1.close()
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "root")],
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
