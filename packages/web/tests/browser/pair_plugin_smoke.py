"""Drop-in pair plugin: real catalog, proof, form and cohort controls.

Run with: uv run --with playwright python tests/browser/pair_plugin_smoke.py
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if not os.environ.get("MSL_PLUGIN_SMOKE_CHILD"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "demo_pair_ui.py").write_text('''
from dataclasses import replace
from moex_spread_lab.core.contracts import ParameterSpec
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
class Demo(PairMeanReversionPlugin):
    strategy_id = "demo_ui_pair"
    family_id = "demo_ui_pair"
    label_ru = "Демо плагин"
    proof_adapter = "daily_pair_price_intents.v1"
    requires_daily_bars = True
    def parameter_schema(self, context=None):
        return super().parameter_schema(context) + (ParameterSpec("custom", "Особый параметр", "int", 3, 1, 9, 1),)
    def evaluate_intents(self, window, params, fill):
        base = {k:v for k,v in params.items() if k != "custom"}
        run = PairMeanReversionPlugin().evaluate_intents(window, base, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(run, strategy_id=self.strategy_id, parameters=params,
            intents=tuple({**item, "signal": {"kind":"z", "date":days[days.index(item["entry_date"][:10])-delay], "value":item["entry_signal_z"]}} for item in run.intents))
PLUGIN = Demo()
from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin
class DemoCarry(FundingCarryPlugin):
    strategy_id = "demo_ui_carry"
    family_id = "demo_ui_carry"
    label_ru = "Демо carry"
    proof_adapter = "carry_settlement_intents.v1"
CARRY = DemoCarry()
''')
        dist = path / "demo_ui-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-ui\nVersion: 1.0\n")
        (dist / "entry_points.txt").write_text("[moex_spread_lab.strategies]\ndemo_ui = demo_pair_ui:PLUGIN\ndemo_ui_carry = demo_pair_ui:CARRY\n")
        env = {**os.environ, "MSL_PLUGIN_SMOKE_CHILD": "1", "MOEX_PROOF_WORKERS": "1", "MOEX_OFFLINE_ONLY": "1",
               "PYTHONPATH": os.pathsep.join([str(path), str(Path(__file__).resolve().parents[1]),
                    str(Path(__file__).resolve().parents[2] / "packages/proof/tests"), os.environ.get("PYTHONPATH", "")])}
        raise SystemExit(subprocess.call([sys.executable, __file__], env=env))

import threading
from http.server import ThreadingHTTPServer
from playwright.sync_api import expect, sync_playwright
from period_settings_smoke import ready, install_stream_spy, expect_api_json
from proof_fixtures import funding_winner
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler

with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
    root = Path(tmp)
    funding_winner(root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root))))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = pw.chromium.launch()
    try:
        page = browser.new_page(viewport={"width":1450,"height":950})
        install_stream_spy(page)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server.server_port}/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
        ready(page, "training")
        expect(page.locator('#strategy option[value="demo_ui_pair"]')).to_be_enabled()
        page.locator("#strategy").select_option("demo_ui_pair")
        page.wait_for_function("() => payload?.selection?.strategy === 'demo_ui_pair'", timeout=30000)
        expect(page.locator("#plugin-custom")).to_have_value("3")
        expect(page.locator("#lookback")).to_be_hidden()
        page.locator("#plugin-custom").fill("5")
        page.locator("#plugin-custom").press("Enter")
        page.wait_for_function("() => payload?.selection?.parameters?.custom === 5", timeout=30000)
        page.locator('tr[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
        expect(page.locator("#group-strategy")).to_have_value("demo_ui_pair")
        expect(page.locator("#group-custom")).to_have_value("5")
        expect(page.locator("#group-custom")).to_have_attribute("min", "1")
        expect(page.locator("#group-custom")).to_have_attribute("max", "9")
        page.locator('tr[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').uncheck()
        expect(page.locator("#plugin-custom")).to_have_value("5")
        page.locator("#strategy").select_option("mean_reversion")
        expect(page.locator("#lookback")).to_be_visible()
        page.locator("#strategy").select_option("demo_ui_pair")
        expect(page.locator("#plugin-custom")).to_have_value("5")
        with expect_api_json(page, "/api/proof/optimize", timeout=120000) as done:
            page.locator("#auto-tune").click()
        alternatives = done["data"]["strategy_search"]["alternatives"]
        assert next(row for row in alternatives if row["strategy"] == "demo_ui_pair")["trial_count"] == 80
        page.locator("#strategy").select_option("demo_ui_carry")
        page.wait_for_function("() => payload?.selection?.strategy === 'demo_ui_carry'", timeout=30000)
        assert page.evaluate("payload.selection.fill") == "same_day_settlement"
        expect(page.locator("#plugin-lookback")).to_have_value("20")
        page.locator('tr[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
        expect(page.locator("#group-strategy")).to_have_value("demo_ui_carry")
        assert page.evaluate("sharedRequest().fill") == "same_day_settlement"
        expect(page.locator("#group-lookback")).to_be_visible()
        assert not errors, errors
        print("Drop-in pair plugin catalog, actual proof, parameter editing and cohort controls: OK")
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
