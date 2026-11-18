"""Actual controls and research API: allow or require each screen without changing the checked cohort."""

import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright
from proof_fixtures import screen_failures

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1380, "height": 900})
    pg = ctx.new_page()
    other = ctx.new_page()
    errors = []
    for page in (pg, other):
        page.on("pageerror", lambda e: errors.append(str(e)))

    def wait(page, route):
        return page.expect_response(
            lambda r: r.url.endswith("/api/" + route), timeout=16000
        )

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
        with wait(pg, "proof/plateau") as live, wait(pg, "settings") as saved:
            pg.locator("#apply-settings").click()
        assert saved.value.json() == {
            "allow_failed_capital": capital,
            "allow_failed_volume": volume,
            "validation_percent": share,
        }
        return live.value.json()

    pg.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion")
    other.goto(url + "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3")
    ready(pg, "training")
    ready(other, "training")
    expect(pg.locator("#period-result")).to_contain_text("объём")
    expect(other.locator("#period-result")).to_contain_text("капитал")
    controls = open_dialog(pg)
    expect(controls.get_by_role("checkbox", name="на объём")).to_be_checked()
    expect(controls.get_by_role("checkbox", name="на капитал")).to_be_checked()
    controls.get_by_role("checkbox", name="на капитал").uncheck()
    pg.locator("#cancel-settings").click()
    expect(open_dialog(pg).get_by_role("checkbox", name="на капитал")).to_be_checked()
    pg.keyboard.press("Escape")
    pg.locator('[data-leaf="pair_screen||CNYRUBF/CRZ6"] input').check()
    with wait(pg, "proof/plateau") as combined:
        pg.locator('[data-branch="calendar_native|SR"] input').check()
    assert combined.value.json()["status"] == "measured"
    with wait(pg, "proof/optimize") as opt:
        pg.locator("#auto-tune").click()
    assert opt.value.json()["status"] == "maximum_found"
    expect(pg.locator("#tune-state")).to_contain_text("Применён")
    pg.locator("#batch-result summary").click()
    expect(pg.locator("#batch-result")).to_contain_text("проверка (капитал)")
    expect(pg.locator("#batch-result")).to_contain_text("проверка (объём)")
    r = edit(True, False)
    assert [x["target"]["pair"] for x in r["excluded"]] == ["SRH3/SRM3"]
    expect(pg.locator("#batch-result")).to_be_hidden()
    expect(pg.locator("#group-profile-state")).to_contain_text("1 из 2")
    expect(other.locator("#period-result")).to_contain_text(
        "Не пройден стресс-тест капитала"
    )
    with wait(pg, "proof/optimize") as rejection:
        pg.locator("#auto-tune").click()
    assert rejection.value.json()["trial_count"] == 0
    expect(pg.locator("#compatibility-list")).to_contain_text("SRH3/SRM3")
    pg.keyboard.press("Escape")
    assert pg.evaluate("checked.size") == 2
    r = edit(False, True)
    assert [x["target"]["pair"] for x in r["excluded"]] == ["CNYRUBF/CRZ6"]
    expect(pg.locator("#charts-empty")).to_be_visible()
    expect(pg.locator("#status")).to_contain_text("на объём")
    expect(other.locator("#period-result")).to_contain_text("допуск разрешён")
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
    assert (
        r["status"] == "measured"
        and r["member_count"] == 2
        and r["validation_percent"] == 25
    )
    ready(pg, "validation")
    expect(pg.locator("#period-result")).to_contain_text("допуск разрешён")
    assert not errors, errors
    ctx.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        screen_failures(root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = pw.chromium.launch()
        try:
            run(browser, "http://127.0.0.1:" + str(server.server_port))
            print(
                "Default checks, cancel/save, four combinations, modal and group lamps, two tabs, reload and reports: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
