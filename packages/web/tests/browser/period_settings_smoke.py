"""Global preferences, validation split, automatic sizing and independent research screens."""

import re

from _harness import (
    CAL_URL,
    apply_settings,
    edit_parameters,
    expect,
    expect_api_json,
    hold_response,
    open_settings_modal,
    page_session,
    ready,
    release_response,
    serve_fixture,
    table_matches_chart,
    temporary_root,
)

from proof_fixtures import screen_failures, two_calendar_windows


def validate_dialog(page):
    original = page.evaluate("payload.run_id")
    dialog = open_settings_modal(page)
    defaults = {
        "validation-percent": "0",
        "global-capital": "30000",
        "global-slippage": "1",
        "global-seed": "0",
    }
    for name, value in defaults.items():
        expect(page.locator("#" + name)).to_have_value(value)
    expect(page.locator("#global-fill")).to_have_value("reference_same_bar")
    expect(page.locator('#global-timeframe[role="radiogroup"]')).to_be_visible()
    expect(page.locator('#global-timeframe [role=radio][data-value="1d"]')).to_have_attribute(
        "aria-checked", "true"
    )
    assert (
        dialog.locator(".settings-footer").evaluate("n=>getComputedStyle(n).position") == "sticky"
    )
    page.mouse.click(5, 5)
    expect(dialog).not_to_be_visible()
    open_settings_modal(page)
    for name, invalid in {
        "validation-percent": ("51", "-1"),
        "global-capital": ("0", "-1", "0.001", "1000000001"),
        "global-slippage": ("-1",),
        "global-seed": ("-1", "2147483648", "1.5"),
    }.items():
        control = page.locator("#" + name)
        for value in invalid:
            control.fill(value)
            assert not control.evaluate("n=>n.validity.valid")
        page.locator("#apply-settings").click()
        expect(dialog).to_be_visible()
        control.fill(defaults[name])
    page.locator("#global-capital").fill("45000")
    page.locator("#cancel-settings").click()
    open_settings_modal(page)
    expect(page.locator("#global-capital")).to_have_value("30000")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    assert page.evaluate("payload.run_id") == original


def capital_ready(page, amount):
    page.wait_for_function("n=>payload?.evaluation.initial_capital_rub===n", arg=amount)
    ready(page)
    table_matches_chart(page)


def run_preferences(page, url):
    other = page.context.new_page()
    other.goto(url + CAL_URL)
    ready(other, "training")
    requests = []
    page.context.on(
        "request",
        lambda r: (
            requests.append(r.post_data_json) if "/api/proof/" in r.url and r.post_data else None
        ),
    )
    validate_dialog(page)
    edit_parameters(page, entry=0.8, exit=0.1, lookback=6)
    edit_parameters(other, clookback=10)
    sizes = [p.evaluate("payload.trades[0].quantity") for p in (page, other)]
    original = page.evaluate("payload.run_id")
    hold_response(page, "/api/proof/catalog")
    saved = apply_settings(
        page,
        rerun=False,
        global_capital=15000,
        global_slippage=2.5,
        global_seed=41,
        validation_percent=25,
        global_fill="next_close",
    )
    assert {
        k: saved[k]
        for k in ("initial_capital_rub", "slippage_ticks", "random_seed", "validation_percent")
    } == {
        "initial_capital_rub": 15000,
        "slippage_ticks": 2.5,
        "random_seed": 41,
        "validation_percent": 25,
    }
    open_settings_modal(page)
    expect(page.locator("#apply-settings")).to_be_enabled()
    page.keyboard.press("Escape")
    expect(page.locator("#global-settings")).not_to_be_visible()
    assert page.evaluate("payload.run_id") == original
    page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
    release_response(page)
    for tab, before in zip((page, other), sizes, strict=True):
        capital_ready(tab, 15000)
        ready(tab, "validation")
        assert tab.evaluate("payload.trades[0].quantity") < before
        assert tab.evaluate("payload.sizing.initial_capital_rub") == 15000
        assert tab.evaluate("payload.selection.slippage_ticks") == 2.5
        expect(tab.locator("#window-list tr.current [data-gate=capital] span")).to_have_attribute(
            "data-state", "fail"
        )
        expect(tab.locator("#settings-badge")).to_contain_text("41")
        assert tab.evaluate("payload.selection.sizing") == "max_affordable"
        expect(
            tab.locator(
                "#quantity,#cquantity,#slippage,#cslippage,#group-quantity,#group-slippage_ticks"
            )
        ).to_have_count(0)
        expect(
            tab.locator(
                "#col-norm input[data-axis=quantity],#col-norm input[data-axis=slippage_ticks]"
            )
        ).to_have_count(0)
    assert page.evaluate("payload.run_id") != original
    expect(page.locator("#lookback")).to_have_value("6")
    expect(other.locator("#clookback")).to_have_value("10")
    for trade in other.evaluate("payload.trades"):
        assert trade["entry"]["costs"]["slippage"] == 2.5 * trade["quantity"]
    expect(page.locator("#chart-h-spread .validation-line")).to_be_visible()
    expect(page.locator("#chart-scope")).to_contain_text("пунктир")
    page.locator("#tab-table").click()
    expect(page.locator("tr.validation-start")).to_contain_text(
        page.evaluate("payload.evaluation.periods.validation[0]")
    )
    expect(page.locator("tr[data-period=validation]")).to_have_count(13)
    expect(page.locator("tr[data-period=training]")).to_have_count(42)
    page.locator('[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
    page.locator('[data-branch="calendar_native|SR"] input').check()
    hold_response(page, "/windows")
    page.evaluate("windowReadings.clear();refreshWindowReadings(0)")
    page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
    apply_settings(other, global_capital=60000)
    capital_ready(page, 60000)
    capital_ready(other, 60000)
    release_response(page)
    table_matches_chart(page)
    assert page.evaluate(
        "[...windowReadings.values()].every(r=>!r.evaluation||r.evaluation.initial_capital_rub===60000)"
    )
    assert page.evaluate("checked.size") == 3
    expect(page.locator("#window-list tr.current [data-gate=capital] span")).to_have_attribute(
        "data-state", "pass"
    )
    other.reload()
    capital_ready(other, 60000)
    open_settings_modal(other)
    expect(other.locator("#global-slippage")).to_have_value("2.5")
    expect(other.locator("#global-seed")).to_have_value("41")
    expect(other.locator("#apply-settings")).to_be_enabled()
    other.keyboard.press("Escape")
    for share in (50, 0.5, 0):
        saved = apply_settings(
            other,
            validation_percent=share,
            global_fill="reference_same_bar",
            global_seed=41 if share else 0,
        )
        page.wait_for_function("v=>payload.evaluation.validation_percent===v", arg=share)
        ready(page, "validation" if share else "training")
        if share == 50:
            expect(page.locator("#period-result")).to_contain_text("Проверка 50%")
    expect(page.locator(".validation-line,tr.validation-start")).to_have_count(0)
    assert saved["random_seed"] == 0
    expect(other.locator("#settings-badge")).to_contain_text("случайный")
    bodies = [body for request in requests for body in request.get("requests", [request])]
    assert bodies and all(
        r["sizing"] == "max_affordable" and not ({"quantity", "slippage_ticks"} & r.keys())
        for r in bodies
    )


def edit_screens(page, volume, capital, share=0):
    with expect_api_json(page, "/api/proof/plateau") as profile:
        saved = apply_settings(
            page,
            rerun=False,
            allow_failed_volume=volume,
            allow_failed_capital=capital,
            validation_percent=share,
        )
    assert (
        saved["allow_failed_volume"],
        saved["allow_failed_capital"],
        saved["validation_percent"],
    ) == (volume, capital, share)
    assert page.evaluate("checked.size") == 2
    return profile["data"]


def run_screens(page, url):
    other = page.context.new_page()
    other.goto(url + CAL_URL)
    ready(other)
    assert "volume" in page.evaluate("payload.evaluation.failed_screens")
    assert "capital" in other.evaluate("payload.evaluation.failed_screens")
    open_settings_modal(page)
    for name in ("volume", "capital"):
        expect(page.locator("#allow-failed-" + name)).to_be_checked()
    page.locator("#allow-failed-capital").uncheck()
    page.locator("#cancel-settings").click()
    open_settings_modal(page)
    expect(page.locator("#allow-failed-capital")).to_be_checked()
    page.keyboard.press("Escape")
    page.locator('[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
    with expect_api_json(page, "/api/proof/plateau") as combined:
        page.locator('[data-branch="calendar_native|SR"] input').check()
    assert combined["data"]["status"] == "measured"
    assert len(edit_screens(page, True, False)["excluded"]) == 1
    expect(page.locator("#group-profile-state")).to_contain_text("1 из 2")
    expect(other.locator("#window-list tr.current [data-metric=rate]")).to_have_attribute(
        "title", re.compile("Не пройден стресс-тест капитала")
    )
    page.locator("#auto-tune").click()
    expect(page.locator("#compatibility-list")).to_contain_text("SRH3/SRM3")
    page.keyboard.press("Escape")
    assert len(edit_screens(page, False, True)["excluded"]) == 1
    expect(page.locator("#charts-empty")).to_be_visible()
    expect(page.locator("#status")).to_contain_text("на объём")
    other.reload()
    ready(other)
    open_settings_modal(other)
    expect(other.locator("#allow-failed-volume")).not_to_be_checked()
    expect(other.locator("#allow-failed-capital")).to_be_checked()
    other.keyboard.press("Escape")
    assert len(edit_screens(page, False, False, 25)["excluded"]) == 2
    expect(page.locator("#group-profile-state")).to_contain_text("2 из 2")
    assert edit_screens(page, True, True, 25)["validation_percent"] == 25
    ready(page, "validation")


def scenario(browser):
    with temporary_root() as root:
        for name, builder, run in (
            ("settings", two_calendar_windows, run_preferences),
            ("screens", screen_failures, run_screens),
        ):
            workspace = root / name
            workspace.mkdir()
            with serve_fixture(workspace, builder) as url, page_session(browser, url) as page:
                run(page, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario)
