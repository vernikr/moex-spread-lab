"""Optional Chromium regression: focus, tri-state selection, compatibility, application and fallback."""

import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import apply, fixture, ready
from playwright.sync_api import expect, sync_playwright

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler

PAIR = "pair_screen||CNYRUBF/CRZ6"
CAL = "calendar_native|SR|SRH3/SRM3"
PAIR_B = "pair_screen|CNYRUBF"
CAL_B = "calendar_native|SR"


def run(browser, url):
    page = browser.new_page(viewport={"width": 1400, "height": 850})
    errors = []
    searches = []
    runs = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def capture(request):
        if request.url.endswith("/api/proof/optimize"):
            searches.append(request.post_data_json)
        if request.url.endswith("/api/proof/run"):
            runs.append(request.post_data_json)

    page.on("request", capture)
    page.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
    ready(page, "training")
    branch = lambda k: page.locator(f'.picker-row[data-branch="{k}"]')
    leaf = lambda k: page.locator(f'.picker-row[data-leaf="{k}"]')

    def open_branch(k):
        with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
            branch(k).locator("button").click()
        ready(page, "training")

    def change_strategy(id, value):
        with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
            page.locator(id).select_option(value)
        ready(page, "training")

    def tuned(button):
        with page.expect_response(
            lambda r: r.url.endswith("/api/proof/optimize")
        ) as done:
            button.click()
        r = done.value.json()
        expect(page.locator("#tune-state")).to_contain_text("Применён", timeout=20000)
        return r

    def review():
        page.locator("#auto-tune").click()
        expect(page.locator("#compatibility")).to_be_visible(timeout=20000)

    all_top, all_bottom = page.locator("#all-assets"), page.locator("#all-windows")
    for ls in ("#main-asset", "#window-list"):
        rows = page.locator(ls + " .picker-row")
        expect(page.locator(ls + " .picker-row input[type=checkbox]")).to_have_count(
            rows.count()
        )
    initial = page.locator("#ev-runid").text_content()
    count = len(runs)
    leaf(PAIR).locator("input").check()
    expect(branch(PAIR_B).locator("input")).to_have_attribute("aria-checked", "mixed")
    expect(all_bottom).to_have_attribute("aria-checked", "mixed")
    expect(all_top).to_have_attribute("aria-checked", "mixed")
    assert (
        all_bottom.evaluate("n=>n.indeterminate")
        and len(runs) == count
        and page.locator("#ev-runid").text_content() == initial
    )
    # The upper toggle covers every leaf, including unavailable and offscreen windows. Lower is local only.
    all_top.check()
    n = page.evaluate("leaves.size")
    expect(page.locator("#selection-count")).to_contain_text(str(n))
    expect(branch(CAL_B).locator("input")).to_be_checked()
    all_bottom.uncheck()
    expect(branch(CAL_B).locator("input")).to_be_checked()
    expect(branch(PAIR_B).locator("input")).not_to_be_checked()
    expect(all_top).to_have_attribute("aria-checked", "mixed")
    all_top.check()
    all_top.uncheck()
    expect(page.locator("#selection-count")).to_contain_text("Нет отметок")
    leaf(PAIR).locator("input").check()
    open_branch(CAL_B)
    expect(all_bottom).not_to_be_checked()
    expect(branch(PAIR_B).locator("input")).to_have_attribute("aria-checked", "mixed")
    all_bottom.check()
    open_branch(PAIR_B)
    expect(leaf(PAIR).locator("input")).to_be_checked()
    expect(branch(CAL_B).locator("input")).to_be_checked()
    group = tuned(page.locator("#auto-tune"))
    g = group["group"]
    assert len(searches[-1]["targets"]) == g["member_count"] == 2
    expect(page.locator("#batch-result summary")).to_contain_text("окон: 2")
    page.locator("#batch-result summary").click()
    expect(page.locator("#batch-result")).to_contain_text("Общие параметры")
    expect(page.locator("#batch-result")).to_contain_text("SRH3/SRM3")
    expect(page.locator("#lookback")).to_have_value(
        str(group["selected_parameters"]["lookback"])
    )
    open_branch(CAL_B)
    expect(page.locator("#clookback")).to_have_value(
        str(group["selected_parameters"]["lookback"])
    )
    assert (
        float(page.locator("#centry").input_value())
        == group["selected_parameters"]["entry_z"]
    )
    # Funding on the pair is incompatible with the offscreen calendar. Cancel preserves both.
    open_branch(PAIR_B)
    change_strategy("#strategy", "funding_only")
    review()
    expect(page.locator("#compatibility-list")).to_contain_text("SRH3/SRM3")
    expect(page.locator("#compatibility-message")).to_contain_text("останется 1")
    expect(branch(CAL_B).locator("input")).to_be_checked()
    attempts = len(searches)
    page.keyboard.press("Escape")
    expect(page.locator("#compatibility")).not_to_be_visible()
    expect(page.locator("#tune-state")).to_contain_text("Отметки не изменены")
    assert len(searches) == attempts and page.evaluate("checked.size") == 2
    review()
    result = tuned(page.locator("#compatibility-yes"))
    assert result["group"]["member_count"] == 1
    expect(branch(CAL_B).locator("input")).not_to_be_checked()
    expect(leaf(PAIR).locator("input")).to_be_checked()
    assert all(t["universe"] == "pair_screen" for t in searches[-1]["targets"])
    # All checked rows incompatible. Confirming must not immediately tune the unchecked focus.
    leaf(PAIR).locator("input").uncheck()
    branch(CAL_B).locator("input").check()
    review()
    expect(page.locator("#compatibility-message")).to_contain_text("не останется")
    assert page.evaluate("checked.size") == 1
    attempts = len(searches)
    page.locator("#compatibility-yes").click()
    expect(page.locator("#tune-state")).to_contain_text("не запускался")
    assert page.evaluate("checked.size") == 0 and len(searches) == attempts
    solo = tuned(page.locator("#auto-tune"))
    assert (
        "targets" not in searches[-1]
        and solo["group"]["members"][0]["target"]["pair"] == "CNYRUBF/CRZ6"
    )
    # Checked calendar only; inspect/edit the pair. The unchecked focus keeps its custom lookback.
    change_strategy("#strategy", "mean_reversion")
    page.locator("#lookback").fill("33")
    with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        page.locator("#lookback").press("Enter")
    ready(page, "training")
    branch(CAL_B).locator("input").check()
    apply(page, 25)
    off = tuned(page.locator("#auto-tune"))
    expect(page.locator("#lookback")).to_have_value("33")
    assert (
        off["group"]["result_basis"] == "validation"
        and off["group"]["members"][0]["target"]["universe"] == "calendar_native"
    )
    assert (
        runs[-1]["pair"] == "CNYRUBF/CRZ6" and runs[-1]["parameters"]["lookback"] == 33
    )
    with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        branch(CAL_B).locator("button").click()
    ready(page, "validation")
    expect(page.locator("#clookback")).to_have_value(
        str(off["selected_parameters"]["lookback"])
    )
    # A late response from the previous cohort cannot change inputs or resurrect its summary.
    original = page.locator("#clookback").input_value()
    page.route(
        "**/api/proof/optimize",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(group)
        ),
    )
    page.evaluate("""() => { const normal=window.fetch;window.fetch=async (...args)=>{
      if (!String(args[0]).endsWith('/optimize')) return normal(...args);
      const reply=await normal(...args); await new Promise(r=>{window.releaseOldSearch=r});return reply;
    }; }""")
    with page.expect_response(lambda r: r.url.endswith("/api/proof/optimize")):
        page.locator("#auto-tune").click()
    with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        all_bottom.uncheck()
    page.evaluate("window.releaseOldSearch()")
    ready(page, "validation")
    expect(page.locator("#batch-result")).to_be_hidden()
    expect(page.locator("#clookback")).to_have_value(original)
    all_bottom.check()
    page.set_viewport_size({"width": 400, "height": 850})
    expect(page.locator("#selection-count")).to_contain_text("1")
    assert (
        page.evaluate("document.documentElement.scrollWidth<=innerWidth") and not errors
    ), errors
    page.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        fixture(root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = pw.chromium.launch()
        try:
            run(browser, "http://127.0.0.1:" + str(server.server_port))
            print(
                "Checkbox hierarchy, focus, scoped All, modal/cancel/confirm, cross-universe application, holdout and fallback: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
