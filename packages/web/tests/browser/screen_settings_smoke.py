"""Actual controls and research API: allow or require each screen without changing the checked cohort."""

import re

from _harness import exact_trials, scenario_isolation, serve_fixture, temporary_root
from period_settings_smoke import expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect

from proof_fixtures import screen_failures


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1380, "height": 900})
    pg = ctx.new_page()
    other = ctx.new_page()
    errors = []
    for page in (pg, other):
        install_stream_spy(page)
        page.on("pageerror", lambda e: errors.append(str(e)))

    def wait(page, route):
        return page.expect_response(lambda r: r.url.endswith("/api/" + route), timeout=16000)

    def open_dialog(page):
        page.locator("#open-settings").click()
        return page.get_by_role(
            "group",
            name="При подборе параметров позволять учитывать спреды, не прошедшие проверку:",
        )

    def edit(volume, capital, share=0):
        group = open_dialog(pg)
        group.get_by_role("checkbox", name="на объём").set_checked(volume)
        group.get_by_role("checkbox", name="на капитал").set_checked(capital)
        pg.locator("#validation-percent").fill(str(share))
        with expect_api_json(pg, "/api/proof/plateau") as live, wait(pg, "settings") as saved:
            pg.locator("#apply-settings").click()
        # The whole settings payload is a contract of its own
        # (packages/proof/tests/test_global_capital.py::test_default_migration_and_partial_saves_preserve_all_other_preferences,
        # packages/proof/tests/test_tuning_screens.py::test_all_four_combinations_in_preflight_search_report_and_live_lamps);
        # the browser only has to show that this dialog writes the three fields it edits.
        written = {
            "allow_failed_capital": capital,
            "allow_failed_volume": volume,
            "validation_percent": share,
        }
        assert {name: saved.value.json()[name] for name in written} == written
        return live["data"]

    pg.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion")
    other.goto(url + "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3")
    ready(pg, "training")
    ready(other, "training")
    assert "volume" in pg.evaluate("payload.evaluation.failed_screens")
    assert "capital" in other.evaluate("payload.evaluation.failed_screens")
    expect(pg.locator("#period-result")).not_to_contain_text("Не пройдена проверка")
    expect(other.locator("#period-result")).not_to_contain_text("Не пройдена проверка")
    controls = open_dialog(pg)
    expect(controls.get_by_role("checkbox", name="на объём")).to_be_checked()
    expect(controls.get_by_role("checkbox", name="на капитал")).to_be_checked()
    controls.get_by_role("checkbox", name="на капитал").uncheck()
    pg.locator("#cancel-settings").click()
    expect(open_dialog(pg).get_by_role("checkbox", name="на капитал")).to_be_checked()
    pg.keyboard.press("Escape")
    pg.locator('[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
    with expect_api_json(pg, "/api/proof/plateau") as combined:
        pg.locator('[data-branch="calendar_native|SR"] input').check()
    assert combined["data"]["status"] == "measured"  # premise: both tabs were profiled live
    with expect_api_json(pg, "/api/proof/optimize"):
        pg.locator("#auto-tune").click()
    expect(pg.locator("#tune-state")).to_be_empty()
    pg.locator("#batch-result summary").click()
    expect(pg.locator("#batch-result")).not_to_contain_text("допуск разрешён")
    # Excluded pairs, their reasons and the search status are asserted on the payload in
    # packages/proof/tests/test_tuning_screens.py::test_all_four_combinations_in_preflight_search_report_and_live_lamps;
    # the browser keeps the reason list, the lamps and the cohort.
    r = edit(True, False)
    assert len(r["excluded"]) == 1, r["excluded"]
    expect(pg.locator("#batch-result")).to_be_hidden()
    expect(pg.locator("#group-profile-state")).to_contain_text("1 из 2")
    expect(other.locator("#window-list tr.current [data-metric=rate]")).to_have_attribute(
        "title", re.compile("Не пройден стресс-тест капитала")
    )
    expect(other.locator("#period-result")).not_to_contain_text("Не пройден стресс-тест капитала")
    with expect_api_json(pg, "/api/proof/optimize"):
        pg.locator("#auto-tune").click()
    expect(pg.locator("#compatibility-list")).to_contain_text("SRH3/SRM3")
    pg.keyboard.press("Escape")
    assert pg.evaluate("checked.size") == 2
    r = edit(False, True)
    assert len(r["excluded"]) == 1, r["excluded"]
    expect(pg.locator("#charts-empty")).to_be_visible()
    expect(pg.locator("#status")).to_contain_text("на объём")
    expect(other.locator("#period-result")).not_to_contain_text("допуск разрешён")
    other.reload()
    ready(other, "training")
    g = open_dialog(other)
    expect(g.get_by_role("checkbox", name="на объём")).not_to_be_checked()
    expect(g.get_by_role("checkbox", name="на капитал")).to_be_checked()
    other.keyboard.press("Escape")
    r = edit(False, False, 25)
    assert len(r["excluded"]) == 2 and pg.evaluate("checked.size") == 2
    expect(pg.locator("#group-profile-state")).to_contain_text("2 из 2")
    r = edit(True, True, 25)
    assert r["validation_percent"] == 25
    ready(pg, "validation")
    expect(pg.locator("#period-result")).not_to_contain_text("допуск разрешён")
    assert not errors, errors
    ctx.close()


MESSAGE = "Default checks, cancel/save, four combinations, modal and group lamps, two tabs, reload and reports: OK"


def scenario(browser):
    # Exact trials: the assertions read search-derived UI state (selection, lamps, ledger).
    with (
        exact_trials(),
        temporary_root() as root,
        serve_fixture(root, screen_failures) as url,
        scenario_isolation(browser),
    ):
        run(browser, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
