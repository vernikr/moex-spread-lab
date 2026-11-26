"""Real family winners, checkbox cohorts, explicit compatibility and per-tab editor state."""

import json

from _harness import (
    PAIR_URL,
    apply_settings,
    edit_parameters,
    exact_trials,
    expect,
    hold_response,
    page_session,
    post,
    ready,
    release_response,
    serve_fixture,
    temporary_root,
    tune,
)

from proof_fixtures import fixture as base_fixture
from proof_fixtures import funding_winner, rising_calendar, two_calendar_windows

PAIR, RISING = "pair_screen||CNYRUBF/CRZ6", "calendar_native|SR|SRM3/SRU3"
PAIR_B, CAL_B = "pair_screen|CNYRUBF", "calendar_native|SR"


def branch(page, key):
    return page.locator(f'[data-branch="{key}"]')


def leaf(page, key):
    return page.locator(f'[data-leaf="{key}"]')


def open_branch(page, key):
    with post(page):
        branch(page, key).locator("button").click()
    ready(page)


def strategy(page, selector, value):
    with post(page):
        page.locator(selector).select_option(value)
    ready(page)


def clear_checks(page):
    page.locator("#all-assets").check()
    page.locator("#all-assets").uncheck()


def review(page):
    page.locator("#auto-tune").click()
    expect(page.locator("#compatibility")).to_be_visible()


def run_pair(page, url):
    result = tune(page)
    assert result["selected_strategy"] == "funding_only"
    assert result["group"]["member_count"] == 1
    assert {
        (r["strategy"], r["fill"], r["trial_count"])
        for r in result["strategy_search"]["alternatives"]
    } == {("mean_reversion", "reference_same_bar", 80), ("funding_only", "same_day_settlement", 80)}
    expect(page.locator("#strategy")).to_have_value("funding_only")
    expect(page.locator("#entry")).to_be_hidden()
    assert (
        result["selected_fill"] == page.evaluate("payload.selection.fill") == "same_day_settlement"
    )
    assert not page.evaluate("payload.execution_reference")
    expect(page.locator("#chart-h-spread-raw")).to_have_attribute("data-signal-lines", "0")
    leaf(page, PAIR).locator("input").check()
    expect(page.locator("#group-strategy")).to_have_value("funding_only")
    expect(page.locator("#group-params input")).to_have_count(1)
    expect(page.locator("#focus-editor")).to_be_hidden()
    strategy(page, "#group-strategy", "mr")
    assert page.evaluate("payload.selection.fill") == "reference_same_bar"
    expect(page.locator("#group-params input")).to_have_count(3)


def stale_search_steps(page):
    checkbox = leaf(page, RISING).locator("input")
    original = page.locator("#group-threshold_pp").input_value()
    hold_response(page, "/optimize")
    with post(page, "optimize"):
        page.locator("#auto-tune").click()
    page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
    with post(page):
        checkbox.uncheck()
    release_response(page)
    ready(page, "validation")
    expect(page.locator("#batch-result")).to_be_hidden()
    expect(page.locator("#cthreshold")).to_be_visible()
    expect(page.locator("#cthreshold")).to_have_value(original)
    expect(page.locator("#centry")).to_be_hidden()
    checkbox.check()
    assert page.evaluate("checked.size") == 1


def run_calendar(page, url):
    apply_settings(page, global_fill="next_close", validation_percent=25, global_seed=41)
    edit_parameters(page, lookback=33)
    open_branch(page, CAL_B)
    leaf(page, RISING).locator("input").check()
    open_branch(page, PAIR_B)
    result = tune(page)
    assert result["selected_strategy"] == "convergence_to_ruonia"
    assert result["group"]["result_basis"] == "validation"
    assert result["group"]["members"][0]["target"]["pair"] == "SRM3/SRU3"
    expect(page.locator("#lookback")).to_have_value("33")
    assert page.evaluate("payload.selection.pair") == "CNYRUBF/CRZ6"
    assert page.evaluate("payload.selection.parameters.lookback") == 33
    open_branch(page, CAL_B)
    # Reuse the real fit to exercise focused adoption without another Optuna run.
    page.route(
        "**/api/proof/optimize",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(result)
        ),
    )
    tune(page)
    expect(page.locator("#cstrategy")).to_have_value("convergence_to_ruonia")
    expect(page.locator("#group-strategy")).to_have_value("convergence_to_ruonia")
    expect(page.locator("#group-params input")).to_have_count(1)
    expect(page.locator("#group-threshold_pp")).to_have_value(
        str(page.evaluate("payload.selection.parameters.threshold_pp"))
    )
    assert page.evaluate("payload.selection.fill") == "next_close"
    assert page.evaluate("payload.summary.ledger_reconciliation_rub") == 0
    assert page.evaluate("payload.trades.length") > 0
    expect(page.locator("#group-profile-state")).not_to_have_attribute("data-state", "loading")
    stale_search_steps(page)


def hierarchy_steps(page, runs):
    for selector in ("#main-asset", "#window-list"):
        expect(page.locator(selector + " .picker-row input[type=checkbox]")).to_have_count(
            page.locator(selector + " .picker-row").count()
        )
    initial, count = page.evaluate("payload.run_id"), len(runs)
    leaf(page, PAIR).locator("input").check()
    for control in (
        branch(page, PAIR_B).locator("input"),
        page.locator("#all-assets"),
        page.locator("#all-windows"),
    ):
        expect(control).to_have_attribute("aria-checked", "mixed")
    assert page.locator("#all-windows").evaluate("n=>n.indeterminate")
    assert len(runs) == count and page.evaluate("payload.run_id") == initial
    page.locator("#all-assets").check()
    assert page.evaluate("checked.size===leaves.size")
    page.locator("#all-windows").uncheck()
    expect(branch(page, CAL_B).locator("input")).to_be_checked()
    expect(branch(page, PAIR_B).locator("input")).not_to_be_checked()
    expect(page.locator("#all-assets")).to_have_attribute("aria-checked", "mixed")
    clear_checks(page)
    leaf(page, PAIR).locator("input").check()
    open_branch(page, CAL_B)
    expect(page.locator("#all-windows")).not_to_be_checked()
    page.locator("#all-windows").check()
    open_branch(page, PAIR_B)
    expect(leaf(page, PAIR).locator("input")).to_be_checked()
    expect(branch(page, CAL_B).locator("input")).to_be_checked()


def compatibility_steps(page, searches):
    strategy(page, "#group-strategy", "funding_only")
    missing = page.evaluate("branches.get('pair_screen|CNYRUBF').children.find(l=>!l.enabled).key")
    missing_pair = page.evaluate("k=>leaves.get(k).target.pair", missing)
    leaf(page, missing).locator("input").check()
    review(page)
    expect(page.locator("#compatibility-list")).to_contain_text(missing_pair)
    expect(page.locator("#compatibility-message")).to_contain_text("останется 2")
    attempts = len(searches)
    page.keyboard.press("Escape")
    expect(page.locator("#tune-state")).to_contain_text("Отметки не изменены")
    assert len(searches) == attempts and page.evaluate("checked.size") == 3
    review(page)
    result = tune(page, "#compatibility-yes")
    assert result["selected_strategy"] == "mean_reversion", result["selected_strategy"]
    assert len(searches[-1]["targets"]) == result["group"]["member_count"] == 2
    expect(leaf(page, missing).locator("input")).not_to_be_checked()
    expect(leaf(page, PAIR).locator("input")).to_be_checked()
    expect(page.locator("#group-strategy")).to_have_value("mr")
    expect(page.locator("#lookback")).to_have_value(str(result["selected_parameters"]["lookback"]))
    expect(page.locator("#batch-result summary")).to_contain_text("справочно")
    page.locator("#batch-result summary").click()
    for text in (
        "Общие параметры",
        "SRH3/SRM3",
        "Все оценки группы — ориентир, не проверка исполнимой доходности",
    ):
        expect(page.locator("#batch-result")).to_contain_text(text)
    page.locator("#group-lookback + .plateau-dot").hover()
    expect(page.locator("#plateau-tip")).to_contain_text("Справочный same-bar расчёт")
    assert all(r["selection"]["fill"] == "reference_same_bar" for r in result["group"]["members"])
    open_branch(page, CAL_B)
    expect(page.locator("#clookback")).to_have_value(str(result["selected_parameters"]["lookback"]))
    assert float(page.locator("#centry").input_value()) == result["selected_parameters"]["entry_z"]
    open_branch(page, PAIR_B)
    clear_checks(page)
    leaf(page, missing).locator("input").check()
    review(page)
    expect(page.locator("#compatibility-message")).to_contain_text("не останется")
    attempts = len(searches)
    page.locator("#compatibility-yes").click()
    expect(page.locator("#tune-state")).to_contain_text("не запускался")
    assert page.evaluate("checked.size") == 0 and len(searches) == attempts


def run_checks(page, url):
    apply_settings(page, global_seed=41)
    searches, runs = [], []

    def capture(request):
        if request.url.endswith("/api/proof/optimize"):
            searches.append(request.post_data_json)
        if request.url.endswith("/api/proof/run"):
            runs.append(request.post_data_json)

    page.on("request", capture)
    other = page.context.new_page()
    other.goto(url + PAIR_URL)
    ready(other)
    original = other.locator("#lookback").input_value()
    hierarchy_steps(page, runs)
    compatibility_steps(page, searches)
    assert other.evaluate("checked.size") == 0
    expect(other.locator("#lookback")).to_have_value(original)


def calendar_fixture(root):
    two_calendar_windows(root)
    rising_calendar(root)


def scenario(browser):
    with exact_trials(), temporary_root() as root:
        for name, builder, run in (
            ("funding", funding_winner, run_pair),
            ("convergence", calendar_fixture, run_calendar),
            ("checks", base_fixture, run_checks),
        ):
            workspace = root / name
            workspace.mkdir()
            with serve_fixture(workspace, builder) as url, page_session(browser, url) as page:
                run(page, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario)
