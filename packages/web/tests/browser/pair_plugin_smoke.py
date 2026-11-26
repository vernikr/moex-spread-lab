"""Installed pair/carry/calendar plugins, chart modes and the mobile workbench."""

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

from _harness import (
    CAL_URL,
    edit_parameters,
    exact_trials,
    expect,
    page_session,
    post,
    ready,
    serve_fixture,
    temporary_root,
    tune,
)

from proof_fixtures import funding_winner


def select_plugin(page, selector, strategy):
    with post(page):
        page.locator(selector).select_option(strategy)
    ready(page)
    assert page.evaluate("payload.selection.strategy") == strategy


def run_pair(page):
    expect(page.locator('#strategy option[value="demo_ui_pair"]')).to_be_enabled()
    select_plugin(page, "#strategy", "demo_ui_pair")
    expect(page.locator("#plugin-custom")).to_have_value("3")
    expect(page.locator("#lookback")).to_be_hidden()
    edit_parameters(page, **{"plugin-custom": 5})
    checkbox = page.locator('tr[data-leaf="pair_screen||CNYRUBF/CRZ6"] input')
    checkbox.check()
    expect(page.locator("#group-strategy")).to_have_value("demo_ui_pair")
    expect(page.locator("#group-custom")).to_have_value("5")
    for name, value in (("min", "1"), ("max", "9")):
        expect(page.locator("#group-custom")).to_have_attribute(name, value)
    checkbox.uncheck()
    expect(page.locator("#plugin-custom")).to_have_value("5")
    select_plugin(page, "#strategy", "mean_reversion")
    expect(page.locator("#lookback")).to_be_visible()
    select_plugin(page, "#strategy", "demo_ui_pair")
    expect(page.locator("#plugin-custom")).to_have_value("5")
    for mode in ("raw", "garch", "kalman"):
        page.locator("#norm").select_option(mode)
        expect(page.locator("#chart-h-spread canvas").first).to_be_visible()
    result = tune(page)
    assert (
        next(
            r for r in result["strategy_search"]["alternatives"] if r["strategy"] == "demo_ui_pair"
        )["trial_count"]
        == 80
    )
    select_plugin(page, "#strategy", "demo_ui_carry")
    assert page.evaluate("payload.selection.fill") == "same_day_settlement"
    expect(page.locator("#plugin-lookback")).to_have_value("20")
    checkbox.check()
    expect(page.locator("#group-strategy")).to_have_value("demo_ui_carry")
    assert page.evaluate("sharedRequest().fill") == "same_day_settlement"
    expect(page.locator("#group-lookback")).to_be_visible()


def run_calendar(page):
    expect(page.locator('#cstrategy option[value="demo_ui_calendar"]')).to_be_enabled()
    select_plugin(page, "#cstrategy", "demo_ui_calendar")
    expect(page.locator("#cplugin-cal_extra")).to_have_value("4")
    edit_parameters(page, **{"cplugin-cal_extra": 6})
    page.locator('tr[data-leaf="calendar_native|SR|SRH3/SRM3"] input').check()
    expect(page.locator("#group-strategy")).to_have_value("demo_ui_calendar")
    expect(page.locator("#group-cal_extra")).to_have_value("6")


def scenario(browser):
    with exact_trials(), temporary_root() as root, serve_fixture(root, funding_winner) as url:
        with page_session(browser, url, mobile=True) as page:
            run_pair(page)
        with page_session(browser, url, CAL_URL) as page:
            run_calendar(page)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario)
