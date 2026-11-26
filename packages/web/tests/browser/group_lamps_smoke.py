"""Cohort diagnostics stay independent of chart focus and reject stale responses."""

from _harness import (
    CAL_URL,
    apply_settings,
    edit_parameters,
    expect,
    expect_api_json,
    hold_response,
    page_session,
    post,
    ready,
    release_response,
    serve_fixture,
    temporary_root,
    ui_only_trials,
)

from proof_fixtures import two_calendar_windows


def leaf(page, pair):
    return page.locator(f'[data-leaf="calendar_native|SR|{pair}"]')


def open_row(page, row):
    with post(page):
        row.locator("button").click()
    ready(page)


def snapshot(page):
    return page.locator("#cohort-editor .number-shell").evaluate_all(
        "ns=>ns.map(n=>[n.firstElementChild.value,n.lastElementChild.dataset.level,n.lastElementChild.dataset.score,n.lastElementChild.dataset.detail])"
    )


def validate_ui(page, response):
    expect(page.locator("#group-profile-state")).to_have_attribute("data-state", "ready")
    for axis, field in response["plateau"]["fields"].items():
        lamp = page.locator(f"#group-{axis} + .plateau-dot")
        expect(lamp).to_be_visible()
        score = lamp.get_attribute("data-score")
        assert (float(score) if score else None) == field["score"]


def focus_steps(page, profiles, runs, searches):
    edit_parameters(page, clookback=10)
    assert float(page.locator("#centry + .plateau-dot").get_attribute("data-score")) > 70
    open_row(page, leaf(page, "SRM3/SRU3"))
    expect(page.locator("#centry + .plateau-dot")).to_have_attribute("data-level", "unknown")
    edit_parameters(page, clookback=7)
    open_row(page, leaf(page, "SRH3/SRM3"))
    expect(page.locator("#clookback")).to_have_value("10")
    with expect_api_json(page, "/api/proof/plateau") as result:
        page.locator("#all-windows").check()
    validate_ui(page, result["data"])
    assert not searches and page.evaluate("checked.size") == 2
    expect(page.locator("#focus-editor")).to_be_hidden()
    both, count = snapshot(page), len(profiles)
    for row in (leaf(page, "SRM3/SRU3"), page.locator('[data-branch="pair_screen|CNYRUBF"]')):
        open_row(page, row)
        assert "plateau" not in page.evaluate("payload")
        assert snapshot(page) == both and len(profiles) == count
    expect(page.locator("#group-window")).to_have_value("10")
    expect(page.locator("#chart-selection-scope")).to_contain_text("параметры окна")
    page.locator("#group-entry_z + .plateau-dot").hover()
    for text in ("Отмеченные окна: 2", "Среднее в центре"):
        expect(page.locator("#plateau-tip")).to_contain_text(text)
    page.locator('[data-branch="pair_screen|BTCUSDF"] button').click()
    expect(page.locator("#charts-empty")).to_be_visible()
    assert snapshot(page) == both and len(profiles) == count
    open_row(page, page.locator('[data-branch="pair_screen|CNYRUBF"]'))
    count = len(runs)
    with expect_api_json(page, "/api/proof/plateau") as changed:
        page.locator("#group-window").fill("12")
    validate_ui(page, changed["data"])
    assert profiles[-1]["parameters"]["window"] == 12 and len(runs) == count and not searches
    assert all(not ({"pair", "asset", "universe"} & r.keys()) for r in profiles)


def stale_profile_steps(page):
    with expect_api_json(page, "/api/proof/plateau") as split:
        apply_settings(page, validation_percent=25)
    validate_ui(page, split["data"])
    open_row(page, page.locator('[data-branch="calendar_native|SR"]'))
    open_row(page, leaf(page, "SRH3/SRM3"))
    hold_response(page, "/plateau")
    with post(page, "plateau"):
        leaf(page, "SRM3/SRU3").locator("input").uncheck()
    page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
    with expect_api_json(page, "/api/proof/plateau") as current:
        leaf(page, "SRM3/SRU3").locator("input").check()
    validate_ui(page, current["data"])
    latest = snapshot(page)
    release_response(page)
    assert snapshot(page) == latest and page.evaluate("checked.size") == 2


def tuning_steps(page, profiles):
    latest = snapshot(page)
    hold_response(page, "/optimize")
    with expect_api_json(page, "/api/proof/optimize") as optimized:
        page.locator("#auto-tune").click()
        with post(page):
            leaf(page, "SRM3/SRU3").locator("button").click()
        expect(page.locator("#auto-tune")).to_be_disabled()
        assert snapshot(page) == latest
        page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
        with post(page):
            release_response(page)
        ready(page, "validation")
    expect(page.locator("#tune-state")).to_be_empty()
    validate_ui(page, optimized["data"])
    adopted, count = snapshot(page), len(profiles)
    open_row(page, leaf(page, "SRH3/SRM3"))
    assert snapshot(page) == adopted and len(profiles) == count
    with expect_api_json(page, "/api/proof/plateau"):
        page.locator("#group-strategy").select_option("funding_only")
    expect(page.locator("#group-profile-state")).to_contain_text("2 из 2")
    expect(page.locator("#compatibility")).not_to_be_visible()
    assert page.evaluate("checked.size") == 2
    expect(page.locator("#cohort-editor .plateau-dot[data-level=unknown]")).to_have_count(1)
    with post(page):
        page.locator("#all-windows").uncheck()
    ready(page, "validation")
    expect(page.locator("#cohort-editor")).to_be_hidden()
    expect(page.locator("#centry + .plateau-dot")).to_be_visible()
    assert page.evaluate("payload.selection.pair") == "SRH3/SRM3"
    assert "plateau" in page.evaluate("payload")


def run(page):
    profiles, runs, searches = [], [], []

    def record(request):
        for path, log in (("plateau", profiles), ("run", runs), ("optimize", searches)):
            if request.url.endswith("/api/proof/" + path):
                log.append(request.post_data_json)

    page.on("request", record)
    apply_settings(page, global_fill="next_open")
    focus_steps(page, profiles, runs, searches)
    stale_profile_steps(page)
    tuning_steps(page, profiles)


def scenario(browser):
    with (
        ui_only_trials(),
        temporary_root() as root,
        serve_fixture(root, two_calendar_windows) as url,
        page_session(browser, url, CAL_URL) as page,
    ):
        run(page)


MESSAGE = "Plateau lamps, hover scope, unchecked/unavailable focus, profile races, in-flight adoption and solo fallback: OK"

if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
