"""Drop-in pair plugin: real catalog, proof, form and cohort controls.

Run with: uv run --with playwright python tests/browser/pair_plugin_smoke.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("MSL_PLUGIN_SMOKE_CHILD"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "demo_pair_ui.py").write_text("""
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
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin
class DemoCalendar(CalendarZscorePlugin):
    strategy_id = "demo_ui_calendar"
    family_id = "demo_ui_calendar"
    label_ru = "Демо календарь"
    proof_adapter = "calendar_price_intents.v1"
    def parameter_schema(self, context=None):
        return super().parameter_schema(context) + (ParameterSpec("cal_extra", "Шаг календаря", "int", 4, 1, 8, 1),)
    def evaluate_intents(self, window, params, fill):
        base = {k:v for k,v in params.items() if k != "cal_extra"}
        run = CalendarZscorePlugin().evaluate_intents(window, base, fill)
        return replace(run, strategy_id=self.strategy_id, parameters=params)
CALENDAR = DemoCalendar()
""")
        dist = path / "demo_ui-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-ui\nVersion: 1.0\n")
        (dist / "entry_points.txt").write_text(
            "[moex_spread_lab.strategies]\ndemo_ui = demo_pair_ui:PLUGIN\ndemo_ui_carry = demo_pair_ui:CARRY\ndemo_ui_cal = demo_pair_ui:CALENDAR\n"
        )
        env = {
            **os.environ,
            "MSL_PLUGIN_SMOKE_CHILD": "1",
            "MOEX_PROOF_WORKERS": "1",
            "MOEX_OFFLINE_ONLY": "1",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(path),
                    str(Path(__file__).resolve().parents[4] / "tests"),
                    str(Path(__file__).resolve().parents[4] / "packages/proof/tests"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        }
        raise SystemExit(subprocess.call([sys.executable, __file__], env=env))

from _harness import exact_trials, scenario_isolation, serve_fixture, temporary_root
from period_settings_smoke import expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect

from proof_fixtures import funding_winner

MESSAGE = "Drop-in pair, carry, and calendar plugin catalog, actual proof, parameter editing and cohort controls: OK"


def scenario(browser):
    # Exact-trial opt-out: the search must report 80 trials for the demo family.
    with (
        exact_trials(),
        temporary_root() as root,
        serve_fixture(root, funding_winner) as url,
        scenario_isolation(browser),
    ):
        page = browser.new_page(viewport={"width": 1450, "height": 950})
        install_stream_spy(page)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
        ready(page, "training")
        expect(page.locator('#strategy option[value="demo_ui_pair"]')).to_be_enabled()
        page.locator("#strategy").select_option("demo_ui_pair")
        page.wait_for_function(
            "() => payload?.selection?.strategy === 'demo_ui_pair'", timeout=30000
        )
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
        assert (
            next(row for row in alternatives if row["strategy"] == "demo_ui_pair")["trial_count"]
            == 80
        )
        page.locator("#strategy").select_option("demo_ui_carry")
        page.wait_for_function(
            "() => payload?.selection?.strategy === 'demo_ui_carry'", timeout=30000
        )
        assert page.evaluate("payload.selection.fill") == "same_day_settlement"
        expect(page.locator("#plugin-lookback")).to_have_value("20")
        page.locator('tr[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
        expect(page.locator("#group-strategy")).to_have_value("demo_ui_carry")
        assert page.evaluate("sharedRequest().fill") == "same_day_settlement"
        expect(page.locator("#group-lookback")).to_be_visible()

        cal_page = browser.new_page(viewport={"width": 1450, "height": 950})
        install_stream_spy(cal_page)
        cal_page.on("pageerror", lambda e: errors.append(str(e)))
        cal_page.goto(url + "/?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3")
        ready(cal_page, "training")
        expect(cal_page.locator('#cstrategy option[value="demo_ui_calendar"]')).to_be_enabled()
        cal_page.locator("#cstrategy").select_option("demo_ui_calendar")
        cal_page.wait_for_function(
            "() => payload?.selection?.strategy === 'demo_ui_calendar'", timeout=30000
        )
        expect(cal_page.locator("#cplugin-cal_extra")).to_have_value("4")
        expect(cal_page.locator("#centry")).to_have_count(0)
        cal_page.locator("#cplugin-cal_extra").fill("6")
        cal_page.locator("#cplugin-cal_extra").press("Enter")
        cal_page.wait_for_function(
            "() => payload?.selection?.parameters?.cal_extra === 6", timeout=30000
        )
        cal_page.locator('tr[data-leaf="calendar_native|SR|SRH3/SRM3"] input').check()
        expect(cal_page.locator("#group-strategy")).to_have_value("demo_ui_calendar")
        expect(cal_page.locator("#group-cal_extra")).to_have_value("6")
        assert not errors, errors


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
