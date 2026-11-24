"""Actual registered carry plugin against native proof and Optuna."""

import os
import subprocess
import sys
from pathlib import Path


def test_drop_in_carry_proof_and_search(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "demo_carry.py").write_text("""
from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin
class Demo(FundingCarryPlugin):
    strategy_id = "demo_carry"
    family_id = "demo_carry"
    proof_adapter = "carry_settlement_intents.v1"
PLUGIN = Demo()
from dataclasses import replace
class Mutating(Demo):
    strategy_id = "mutating_carry"
    family_id = "mutating_carry"
    def check_compatibility(self, window, settings=None):
        verdict = super().check_compatibility(window, settings)
        window.carry_study["rows"][0]["near_settle"] = 999999
        return verdict
    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        window.carry_study["rows"][0]["near_settle"] = 999999
        return replace(run, intents=tuple({**t, "gross_pnl": 99999999, "funding_rub":99999999} for t in run.intents))
MUTATING = Mutating()
""")
    dist = plugin / "demo_carry-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-carry\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[moex_spread_lab.strategies]\ndemo_carry = demo_carry:PLUGIN\nmutating_carry = demo_carry:MUTATING\n"
    )
    root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "MOEX_OFFLINE_ONLY": "1",
        "MOEX_PROOF_WORKERS": "1",
        "PYTHONPATH": os.pathsep.join(
            [
                str(plugin),
                str(root / "tests"),
                str(root / "packages/proof/tests"),
                str(root / "packages/accounting/tests"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    }
    code = """
import sys
from pathlib import Path
from funding_fixtures import fixture, change, request, PAIR
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.core.w0 import W0Error
root = Path(sys.argv[1]); days = fixture(root)
change(root, "CNYRUBF", {d: -0.04 for d in days[20:32]})
service = ProofService(root)
meta = next(p for p in service.catalog()["pair_screen"]["proof_plugins"] if p["id"] == "demo_carry")
assert meta["adapter"] == "carry_settlement_intents.v1" and meta["timeframe_supported"]
for validation in (0,20):
    for auto in (False,True):
        for slip in (0.0,2.0):
            settings = ResearchSettings(validation_percent=validation, random_seed=41, slippage_ticks=slip)
            native = {**request("funding_only"), "fill":"same_day_settlement", "slippage_ticks":slip}
            if auto:
                native.pop("quantity"); native["sizing"] = "max_affordable"
            plugin = {**native, "strategy":"demo_carry"}
            n = service.prepare(native, settings=settings); p = service.prepare(plugin, settings=settings)
            for through in (None, days[38]):
                a, b = p(plugin, through=through), n(native, through=through)
                assert a["selection"]["strategy"] == "demo_carry"
                assert a["trades"] == b["trades"], (auto, slip, through, a["trades"], b["trades"])
                assert a["ledger"] == b["ledger"], (auto, slip, through)
                assert a["summary"] == b["summary"], (auto, slip, through, a["summary"], b["summary"])
                assert a["series"] == b["series"], (auto, slip, through)
                fast_a, fast_b = p(plugin, through=through, metrics_only=True), n(native, through=through, metrics_only=True)
                assert fast_a["trades"] == fast_b["trades"], (auto, slip, through, fast_a["trades"], fast_b["trades"])
                assert fast_a["ledger"] == fast_b["ledger"], (auto, slip, through, fast_a["ledger"], fast_b["ledger"])
                if auto:
                    from unittest.mock import patch
                    with patch("moex_spread_lab.core._fastcore_loader.load_fastcore", return_value=None):
                        python_a = p(plugin, through=through, metrics_only=True)
                        python_b = n(native, through=through, metrics_only=True)
                    assert python_a["ledger"] == python_b["ledger"]
                    assert python_a["ledger"] == fast_a["ledger"]
            invalid_requests = [{**plugin, "pair":"GLZ6/GLH7"}, {**plugin, "universe":"calendar_native"}]
            if auto:
                invalid_requests += [{**plugin, "quantity":2}, {**plugin, "sizing":None}]
            else:
                invalid_requests += [{**plugin, "sizing":"max_affordable"}]
            for invalid in invalid_requests:
                try:
                    p(invalid)
                except W0Error:
                    pass
                else:
                    raise AssertionError("prepared carry replay must reject a different identity or sizing mode")
try:
    service.prepare({**request("funding_only"), "strategy":"demo_carry"}, settings=ResearchSettings(timeframe="1h"))
except W0Error:
    pass
else:
    raise AssertionError("carry plugins must not run with hourly data")
# A signal rejected for insufficient capital must not earn funding.
low = ResearchSettings(initial_capital_rub=1000, allow_failed_capital=True)
small_native = {**request("funding_only"), "sizing":"max_affordable"}; small_native.pop("quantity")
small_plugin = {**small_native, "strategy":"demo_carry"}
sa = service.prepare(small_plugin, settings=low)(small_plugin)
sb = service.prepare(small_native, settings=low)(small_native)
assert sa["skipped_entries"] and sa["skipped_entries"] == sb["skipped_entries"]
assert not sa["trades"] and all(not row["funding_rub"] for row in sa["ledger"])
native = request("funding_only")
mutating = {**native, "strategy":"mutating_carry"}
a, b = service.prepare(mutating, settings=ResearchSettings())(mutating), service.prepare(native, settings=ResearchSettings())(native)
assert a["trades"] == b["trades"] and a["ledger"] == b["ledger"] and a["series"] == b["series"]
# An estimated midpoint may be used for retrospective cash only after its right anchor.
gaproot = root.parent / "estimated"
gapdays = fixture(gaproot)
change(gaproot, "CNYRUBF", {gapdays[19]:0.005, gapdays[20]:None, gapdays[21]:None, gapdays[22]:None, gapdays[23]:0.035})
gapsvc = ProofService(gaproot)
ng = request("funding_only"); pg = {**ng, "strategy":"demo_carry"}
np = gapsvc.prepare(ng, settings=ResearchSettings()); pp = gapsvc.prepare(pg, settings=ResearchSettings())
na, pa = np(ng), pp(pg)
assert pa["trades"] == na["trades"] and pa["ledger"] == na["ledger"]
assert pa["provenance"]["funding_estimated_dates"] == gapdays[20:23]
for replay, req0 in ((np, ng), (pp, pg)):
    try:
        replay(req0, through=gapdays[21])
    except W0Error:
        pass
    else:
        raise AssertionError("a prefix cannot borrow the future midpoint anchor")
save_settings(root, {"random_seed":41, "validation_percent":20})
req = {**request("funding_only"), "strategy":"demo_carry", "sizing":"max_affordable", "fill":"same_day_settlement"}
req.pop("quantity")
search = research_run(service, {**req, "optimize_strategy":True}, optimize=True)
row = next(x for x in search["strategy_search"]["alternatives"] if x["strategy"] == "demo_carry")
assert row["trial_count"] == 80, row
from funding_fixtures import weekend, WEEKEND_PAIR
import os
weekend(root)
# Add a second launch-perpetual window, using the same synthetic observations.
import json
from funding_fixtures import page
from moex_spread_lab.data.universe import HISTORY_WINDOWS
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.strategies.funding_carry.carry import _history_url
src = ScreenerInputs(root)
third = "SBERF/SRZ6"
contracts = json.loads(src.contracts.read_text())
for old, new in zip(WEEKEND_PAIR.split("/"), third.split("/")):
    contract = next(r for r in contracts["instruments"] if r["secid"] == old)
    contracts["instruments"].append({**contract, "secid":new, "stable_id":new})
    candles = json.loads(src.candles(WEEKEND_PAIR, old).read_text()); candles["secid"] = new
    src.candles(third, new).write_text(json.dumps(candles))
    doc = json.loads(page(root, old, WEEKEND_PAIR).read_text())
    block = doc["payload"]["history"]; idx = block["columns"].index("SECID")
    for row in block["data"]: row[idx] = new
    doc["url"] = _history_url(new, *HISTORY_WINDOWS[third], 0)
    out = page(root, new, third); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(doc))
src.contracts.write_text(json.dumps(contracts))
cohort = {**req, "targets":[{"universe":"pair_screen", "pair":PAIR},
                           {"universe":"pair_screen", "pair":WEEKEND_PAIR},
                           {"universe":"pair_screen", "pair":third}]}
os.environ["MOEX_PROOF_WORKERS"] = "1"
serial = research_run(ProofService(root), cohort, optimize=True)
os.environ["MOEX_PROOF_WORKERS"] = "2"
os.environ["MOEX_PROOF_FORCE_POOL"] = "1"
parallel_service = ProofService(root)
parallel = research_run(parallel_service, cohort, optimize=True)
assert len(parallel_service._pool._worker_slots) == 2, (serial.get("status"),parallel.get("status"),parallel.get("excluded"))
assert serial["selected_parameters"] == parallel["selected_parameters"], (serial.get("status"),parallel.get("status"))
assert serial["run_id"] == parallel["run_id"]
parallel_service.close()
service.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "root")],
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
