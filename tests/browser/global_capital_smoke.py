"""Real browser: persisted capital, two tabs/cohort/cache and quieter result panels.

Run: uv run --with playwright python tests/browser/global_capital_smoke.py
"""

import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright
from proof_fixtures import two_calendar_windows

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 980})
    pair, cal = ctx.new_page(), ctx.new_page()
    errors = []
    for page in (pair, cal):
        page.on("pageerror", lambda e: errors.append(str(e)))

    def post(page, route):
        return page.expect_response(
            lambda r: r.url.endswith("/api/" + route), timeout=20000
        )

    def amount_ready(page, amount):
        page.wait_for_function(
            "amount => payload?.evaluation.initial_capital_rub===amount && document.getElementById('render-progress').getAttribute('aria-hidden')==='true'",
            arg=amount,
            timeout=20000,
        )
        ready(page, "training")

    def change_capital(page, amount):
        page.locator("#open-settings").click()
        page.locator("#global-capital").fill(str(amount))
        with post(page, "settings") as saved:
            page.locator("#apply-settings").click()
        assert saved.value.json()["initial_capital_rub"] == amount
        amount_ready(pair, amount)
        amount_ready(cal, amount)

    def current_table_matches(page):
        expect(page.locator("#window-list [data-reading=pending]")).to_have_count(
            0, timeout=20000
        )
        row = page.locator("#window-list tr.current")
        expect(row).to_have_attribute("data-run-id", page.evaluate("payload.run_id"))
        expect(row.locator("[data-metric=trades]")).to_have_text(
            str(page.evaluate("payload.summary.trades"))
        )
        expect(row.locator("[data-metric=rate]")).to_have_text(
            page.evaluate(
                "payload.evaluation.result.annualized_excess_pp==null?'—':signed(payload.evaluation.result.annualized_excess_pp)"
            )
        )

    pair.goto(
        url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion"
    )
    cal.goto(
        url
        + "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3&strategy=zscore_mean_reversion"
    )
    ready(pair, "training")
    ready(cal, "training")
    pair.locator("#entry").fill("0.8")
    pair.locator("#exit").fill("0.1")
    pair.locator("#lookback").fill("6")
    with post(pair, "proof/run"):
        pair.locator("#lookback").press("Enter")
    ready(pair, "training")
    cal.locator("#clookback").fill("10")
    with post(cal, "proof/run"):
        cal.locator("#clookback").press("Enter")
    ready(cal, "training")
    original_sizes = [p.evaluate("payload.trades[0].quantity") for p in (pair, cal)]
    original_run = pair.evaluate("payload.run_id")
    for page in (pair, cal):
        expect(page.locator("#global-capital")).to_have_count(1)
        expect(
            page.locator("#selection-count,#group-count,#sizing-hint")
        ).to_have_count(0)
        assert (
            "Нет отметок · подбор для открытого окна"
            not in page.locator("body").inner_text()
        )
        assert (
            "Размер каждой сделки — максимум" not in page.locator("body").inner_text()
        )

    # Drive both previously noisy result branches without erasing the audit metadata.
    reason = "Нарушен лимит просадки или отставания от LQDT. Свободные средства недостаточны для маржи."
    pair.evaluate(
        "reason => {payload.evaluation.failed_screens=['volume'];payload.evaluation.result.reason=reason;renderPeriodResult()}",
        reason,
    )
    assert pair.evaluate("payload.evaluation.result.reason") == reason
    expect(pair.locator("#period-result")).not_to_contain_text(reason)
    expect(pair.locator("#period-result")).not_to_contain_text(
        "допуск разрешён в настройках"
    )

    # Invalid input and cancel never save or rerun.
    pair.locator("#open-settings").click()
    expect(pair.locator("#global-capital")).to_have_value("30000")
    for invalid in ("0", "-1", "0.001", "1000000001"):
        pair.locator("#global-capital").fill(invalid)
        pair.locator("#apply-settings").click()
        expect(pair.locator("#global-settings")).to_be_visible()
        assert not pair.locator("#global-capital").evaluate("n=>n.validity.valid")
    pair.locator("#global-capital").fill("45000")
    pair.locator("#cancel-settings").click()
    assert pair.evaluate("payload.run_id") == original_run

    change_capital(pair, 15000)
    assert pair.evaluate("payload.run_id") != original_run
    for page, before in zip((pair, cal), original_sizes):
        assert page.evaluate("payload.trades[0].quantity") < before
        assert page.evaluate("payload.sizing.initial_capital_rub") == 15000
        expect(
            page.locator("#window-list tr.current [data-gate=capital] span")
        ).to_have_attribute("data-state", "fail")
        current_table_matches(page)
    expect(pair.locator("#lookback")).to_have_value("6")
    expect(cal.locator("#clookback")).to_have_value("10")

    cny, sr = "pair_screen||CNYRUBF/CRZ6", "calendar_native|SR|SRH3/SRM3"
    pair.locator(f'[data-leaf="{cny}"] input').check()
    with post(pair, "proof/run"):
        pair.locator('[data-branch="calendar_native|SR"] button').click()
    ready(pair, "training")
    with post(pair, "proof/plateau"):
        pair.locator(f'[data-leaf="{sr}"] input').check()
    with post(pair, "proof/run"):
        pair.locator(f'[data-leaf="{sr}"] button').click()
    ready(pair, "training")
    with post(pair, "proof/plateau"):
        pair.locator("#group-fill").select_option("next_close")
    pair.wait_for_function("() => payload?.selection.fill==='next_close'")
    current_table_matches(pair)
    assert pair.evaluate("checked.size") == 2
    assert "Отмечено окон:" not in pair.locator("body").inner_text()

    # A completed old-budget table fetch, held across the setting update, is stale.
    pair.evaluate(
        """() => {windowReadings.clear();const old=window.fetch;let once=true;window.fetch=async(...args)=>{const r=await old(...args);if(once&&String(args[0]).endsWith('/windows')){once=false;await new Promise(resolve=>window.releaseCapitalTable=resolve)}return r};refreshWindowReadings(0)}"""
    )
    pair.wait_for_function("() => typeof window.releaseCapitalTable==='function'")
    change_capital(cal, 60000)
    pair.evaluate("window.releaseCapitalTable()")
    current_table_matches(pair)
    assert pair.evaluate(
        "[...windowReadings.values()].every(r=>!r.evaluation||r.evaluation.initial_capital_rub===60000)"
    )
    assert pair.evaluate("checked.size") == 2
    expect(pair.locator("#group-fill")).to_have_value("next_close")
    expect(
        pair.locator("#window-list tr.current [data-gate=capital] span")
    ).to_have_attribute("data-state", "pass")

    with post(pair, "proof/optimize") as result:
        pair.locator("#auto-tune").click()
    r = result.value.json()
    assert r["status"] == "maximum_found"
    assert r["group"]["initial_capital_rub"] == 60000
    for member in r["group"]["members"]:
        assert member["selection"]["fill"] == "next_close"
        assert member["selection"]["initial_capital_rub"] == 60000
        assert member["evaluation"]["initial_capital_rub"] == 60000
    assert set(r["selected_parameters"]) == {"lookback", "entry_z", "exit_z"}
    expect(pair.locator("#tune-state")).to_contain_text("Применён", timeout=20000)
    current_table_matches(pair)
    expect(pair.locator("#batch-result")).not_to_contain_text("допуск разрешён")
    expect(pair.locator("#batch-result")).not_to_contain_text(
        "Свободные средства недостаточны"
    )

    with post(pair, "proof/plateau"):
        pair.locator("#group-fill").select_option("reference_same_bar")
    expect(pair.locator("#period-result .result-warning")).to_have_count(0)
    assert pair.evaluate("payload.execution_reference")
    pair.locator("#toggle-summary").click()
    with pair.expect_download() as download:
        pair.locator("#download-csv").click()
    text = Path(download.value.path()).read_text()
    assert '"initial_capital_rub":60000' in text
    assert '"fill":"reference_same_bar"' in text
    pair.set_viewport_size({"width": 400, "height": 850})
    pair.locator("#open-settings").click()
    expect(pair.locator("#global-capital")).to_have_value("60000")
    assert pair.evaluate("document.documentElement.scrollWidth<=innerWidth")
    pair.keyboard.press("Escape")
    pair.reload()
    amount_ready(pair, 60000)
    expect(pair.locator("#selection-count,#group-count,#sizing-hint")).to_have_count(0)
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
                "Global capital: validation/cancel, two tabs, sizing/screens, exact table, stale cache, cohort fill/search, quieter results, CSV, reload/mobile: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
