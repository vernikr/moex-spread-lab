"""Actual table, causal chart bands and same-bar CNY/calendar compatibility.

Run with Chromium installed: uv run --with playwright python tests/browser/window_table_smoke.py
"""

import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import apply, expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect, sync_playwright
from proof_fixtures import two_calendar_windows

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 980})
    pg = ctx.new_page()
    install_stream_spy(pg)
    pg.set_default_timeout(12000)
    errors, searches = [], []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on(
        "request",
        lambda r: (
            searches.append(r.post_data_json)
            if r.url.endswith("/api/proof/optimize")
            else None
        ),
    )

    def row(key):
        return pg.locator(f'#window-list tr[data-leaf="{key}"]')

    def table_ready():
        expect(pg.locator("#window-list [data-reading=pending]")).to_have_count(
            0, timeout=20000
        )

    def matches_chart():
        table_ready()
        key = pg.evaluate("targetKey(payload.selection)")
        r = row(key)
        expect(r).to_have_attribute("data-reading", "measured")
        expect(r).to_have_attribute("data-run-id", pg.evaluate("payload.run_id"))
        expect(r.locator('[data-metric="trades"]')).to_have_text(
            str(pg.evaluate("payload.summary.trades"))
        )
        expect(r.locator('[data-metric="rate"]')).to_have_text(
            pg.evaluate(
                "payload.evaluation.result.annualized_excess_pp==null?'—':signed(payload.evaluation.result.annualized_excess_pp)"
            )
        )

    def post(route):
        return pg.expect_response(
            lambda r: r.url.endswith("/api/proof/" + route), timeout=20000
        )

    def bands():
        return pg.evaluate(
            "Object.fromEntries(Object.entries(document.getElementById('chart-h-spread-raw')._signalLevels).map(([k,s])=>[k,s.data()]))"
        )

    a, b = "calendar_native|SR|SRH3/SRM3", "calendar_native|SR|SRM3/SRU3"
    pg.goto(
        url
        + "/?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3&strategy=zscore_mean_reversion"
    )
    ready(pg, "training")
    matches_chart()
    assert not pg.locator("#tune-help").count()
    assert "Нажмите название для просмотра" not in pg.locator("body").inner_text()
    assert pg.locator("#window-list th").all_text_contents() == [
        "",
        "Период",
        "V",
        "C",
        "Сд.",
        "п.п./г",
    ]
    text = pg.locator("#window-list").inner_text()
    assert "SRH3" not in text and "SRM3" not in text and "мин. объём" not in text
    assert "объём календарного спреда" in row(a).locator(
        "[data-gate=volume] span"
    ).get_attribute("title")
    assert "3×ГО" in row(a).locator("[data-gate=capital] span").get_attribute("title")
    expect(row(b).locator("[data-metric=trades]")).to_have_text("0")
    expect(row(b).locator("[data-metric=rate]")).to_have_text("0,00")
    assert not searches  # Viewing table rows is not an optimization.
    raw = pg.locator("#chart-h-spread-raw")
    expect(raw).to_be_visible()
    expect(raw).to_have_attribute("data-signal-lines", "2")
    assert pg.evaluate(
        "Object.values(document.getElementById('chart-h-spread-raw')._signalLevels).every(s=>s.options().lineWidth===1)"
    )
    before = bands()
    pg.locator("#norm").select_option("raw")
    expect(raw).to_be_visible()
    assert bands() == before
    pg.locator("#norm").select_option("garch")
    assert bands() == before
    with post("run"):
        pg.locator("#clookback").fill("10")
        pg.locator("#clookback").press("Enter")
    ready(pg, "training")
    matches_chart()
    assert bands() != before
    with post("run"):
        row(b).locator("button").click()
    ready(pg, "training")
    matches_chart()
    with post("run"):
        row(a).locator("button").click()
    ready(pg, "training")
    matches_chart()
    expect(pg.locator("#clookback")).to_have_value("10")

    # The existing reference mode is the global default and works in standalone ordinary CNY pair.
    pg.goto(
        url + "/proof?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion"
    )
    ready(pg, "training")
    pg.locator("#entry").fill("0.8")
    pg.locator("#exit").fill("0.1")
    pg.locator("#lookback").fill("6")
    with post("run"):
        pg.locator("#lookback").press("Enter")
    ready(pg, "training")
    matches_chart()
    assert pg.evaluate("payload.execution_reference")
    expect(pg.locator("#period-result .result-warning")).to_have_count(0)
    assert pg.evaluate("payload.execution_reference")
    assert pg.evaluate("payload.trades.every(t=>t.signal.date===t.entry.date)")
    cny = "pair_screen||CNYRUBF/CRZ6"
    row(cny).locator("input").check()
    expect(pg.locator("#group-fill,#fill,#cfill")).to_have_count(0)
    with post("run"):
        pg.locator('[data-branch="calendar_native|SR"] button').click()
    ready(pg, "training")
    with expect_api_json(pg, "/api/proof/plateau") as profile:
        row(a).locator("input").check()
    assert profile["data"]["status"] == "measured"
    assert profile["data"]["execution_reference"]
    assert not pg.locator("#compatibility").is_visible()
    with expect_api_json(pg, "/api/proof/optimize") as result:
        pg.locator("#auto-tune").click()
    assert result["data"]["status"] == "maximum_found"
    expect(pg.locator("#tune-state")).to_be_empty()
    ready(pg, "training")
    matches_chart()
    assert pg.evaluate("checked.size") == 2
    assert all(
        r["selection"]["fill"] == "reference_same_bar"
        for r in result["data"]["group"]["members"]
    )
    assert pg.evaluate("leafId") == b
    assert pg.evaluate("payload.execution_reference")
    expect(pg.locator("#batch-result summary")).to_contain_text("справочно")
    expect(pg.locator("#batch-result")).to_contain_text(
        "Все оценки группы — ориентир, не проверка исполнимой доходности"
    )
    pg.locator("#group-lookback + .plateau-dot").hover()
    expect(pg.locator("#plateau-tip")).to_contain_text("Справочный same-bar расчёт")
    expect(row(a)).to_have_attribute("data-reference", "true")
    with post("run"):
        row(a).locator("button").click()
    ready(pg, "training")
    matches_chart()
    assert pg.evaluate("payload.execution_reference")
    pg.screenshot(path=str(Path(tempfile.gettempdir()) / "moex-window-preview.png"))

    # Hold a stale table response across navigation; it cannot repaint another asset.
    pg.evaluate(
        """() => {windowReadings.clear();const old=window.fetch;let once=true;window.fetch=async(...args)=>{const r=await old(...args);if(once&&String(args[0]).endsWith('/windows')){once=false;await new Promise(resolve=>window.releaseTable=resolve)}return r};refreshWindowReadings(0)}"""
    )
    pg.wait_for_function("() => typeof window.releaseTable==='function'")
    with post("run"):
        pg.locator('[data-branch="pair_screen|CNYRUBF"] button').click()
    ready(pg, "training")
    pg.evaluate("window.releaseTable()")
    matches_chart()
    assert pg.locator('#window-list tr[data-leaf^="calendar_native"]').count() == 0
    apply(pg, 25)
    matches_chart()
    assert pg.evaluate("payload.evaluation.result_basis") == "validation"
    assert "проверка:" in row(cny).locator("[data-metric=rate]").get_attribute("title")

    # Unloaded history has a distinct icon and unknown metrics, never a fictional zero.
    pg.locator('[data-branch="pair_screen|BTCUSDF"] button').click()
    missing = row("pair_screen||BTCUSDF/BTZ6")
    expect(missing.locator("[data-icon=download]")).to_be_visible()
    expect(missing.locator("[data-metric=trades]")).to_have_text("—")
    expect(missing.locator("[data-metric=rate]")).to_have_text("—")
    assert (
        "нужна загрузка данных" not in pg.locator("#window-list").inner_text().lower()
    )
    pg.set_viewport_size({"width": 400, "height": 850})
    assert pg.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert not errors, errors
    ctx.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        two_calendar_windows(root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = pw.chromium.launch()
        try:
            run(browser, "http://127.0.0.1:" + str(server.server_port))
            print(
                "Window table, V/C tooltips, exact metrics, raw bands, reference CNY/cohort, races and validation: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
