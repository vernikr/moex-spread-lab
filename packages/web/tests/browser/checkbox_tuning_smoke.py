"""Optional Chromium regression: focus, tri-state selection, compatibility, application and fallback."""

import json

from _harness import exact_trials, scenario_isolation, serve_fixture, temporary_root
from period_settings_smoke import (
    apply,
    expect_api_json,
    fixture,
    install_stream_spy,
    ready,
)
from playwright.sync_api import expect

PAIR = "pair_screen||CNYRUBF/CRZ6"
CAL = "calendar_native|SR|SRH3/SRM3"
PAIR_B = "pair_screen|CNYRUBF"
CAL_B = "calendar_native|SR"


def run(browser, url):
    page = browser.new_page(viewport={"width": 1400, "height": 850})
    install_stream_spy(page)
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
        with expect_api_json(page, "/api/proof/optimize") as done:
            button.click()
        r = done["data"]
        ready(page, page.evaluate("payload.evaluation.result_basis"))
        expect(page.locator("#tune-state")).to_be_empty()
        return r

    def review():
        page.locator("#auto-tune").click()
        expect(page.locator("#compatibility")).to_be_visible(timeout=20000)

    all_top, all_bottom = page.locator("#all-assets"), page.locator("#all-windows")
    for ls in ("#main-asset", "#window-list"):
        rows = page.locator(ls + " .picker-row")
        expect(page.locator(ls + " .picker-row input[type=checkbox]")).to_have_count(rows.count())
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
    assert page.evaluate("checked.size") == n
    expect(page.locator("#selection-count")).to_have_count(0)
    expect(branch(CAL_B).locator("input")).to_be_checked()
    all_bottom.uncheck()
    expect(branch(CAL_B).locator("input")).to_be_checked()
    expect(branch(PAIR_B).locator("input")).not_to_be_checked()
    expect(all_top).to_have_attribute("aria-checked", "mixed")
    all_top.check()
    all_top.uncheck()
    assert page.evaluate("checked.size") == 0
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
    expect(page.locator("#lookback")).to_have_value(str(group["selected_parameters"]["lookback"]))
    open_branch(CAL_B)
    expect(page.locator("#clookback")).to_have_value(str(group["selected_parameters"]["lookback"]))
    assert float(page.locator("#centry").input_value()) == group["selected_parameters"]["entry_z"]
    # Strategy search can replace an incompatible editor strategy with common MR;
    # it must not discard a valid calendar just to keep funding carry.
    open_branch(PAIR_B)
    change_strategy("#group-strategy", "funding_only")
    common = tuned(page.locator("#auto-tune"))
    assert common["selected_strategy"] == "mean_reversion"
    assert page.evaluate("checked.size") == 2
    expect(page.locator("#group-strategy")).to_have_value("mr")
    # A universally data-missing window still requires explicit cancel/confirm.
    missing = page.evaluate("branches.get('pair_screen|CNYRUBF').children.find(l=>!l.enabled).key")
    missing_pair = page.evaluate("key=>leaves.get(key).target.pair", missing)
    leaf(missing).locator("input").check()
    review()
    expect(page.locator("#compatibility-list")).to_contain_text(missing_pair)
    expect(page.locator("#compatibility-message")).to_contain_text("останется 2")
    expect(branch(CAL_B).locator("input")).to_be_checked()
    attempts = len(searches)
    page.keyboard.press("Escape")
    expect(page.locator("#compatibility")).not_to_be_visible()
    expect(page.locator("#tune-state")).to_contain_text("Отметки не изменены")
    assert len(searches) == attempts and page.evaluate("checked.size") == 3
    review()
    result = tuned(page.locator("#compatibility-yes"))
    assert result["group"]["member_count"] == 2
    expect(leaf(missing).locator("input")).not_to_be_checked()
    expect(leaf(PAIR).locator("input")).to_be_checked()
    # A partial tri-state is not checked; normalize it before Playwright unchecks.
    # Confirming an all-unavailable checked set must not then tune the unchecked focus.
    all_top.check()
    all_top.uncheck()
    leaf(missing).locator("input").check()
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
    assert runs[-1]["pair"] == "CNYRUBF/CRZ6" and runs[-1]["parameters"]["lookback"] == 33
    with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        branch(CAL_B).locator("button").click()
    ready(page, "validation")
    expect(page.locator("#clookback")).to_have_value(str(off["selected_parameters"]["window"]))
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
    assert page.evaluate("checked.size") == 1
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth") and not errors, errors
    page.close()


MESSAGE = "Checkbox hierarchy, focus, scoped All, modal/cancel/confirm, cross-universe application, holdout and fallback: OK"


def scenario(browser):
    # Exact-trial opt-out: the common winning strategy is asserted by name.
    with (
        exact_trials(),
        temporary_root() as root,
        serve_fixture(root, fixture) as url,
        scenario_isolation(browser),
    ):
        run(browser, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
