"""Real browser: the global random seed is shown, validated, saved and reported.

Run: uv run --with playwright python tests/browser/seed_settings_smoke.py
"""

from _harness import scenario_isolation, serve_fixture, temporary_root, ui_only_trials
from period_settings_smoke import expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect

from proof_fixtures import two_calendar_windows


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 980})
    page = ctx.new_page()
    errors = []
    install_stream_spy(page)
    page.on("pageerror", lambda e: errors.append(str(e)))

    def post(route):
        return page.expect_response(lambda r: r.url.endswith("/api/" + route), timeout=20000)

    page.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion")
    ready(page, "training")

    # The default is 0: no fixed seed, a fresh one per run.
    page.locator("#open-settings").click()
    expect(page.locator("#global-seed")).to_have_value("0")
    assert "случайный" in page.locator("#settings-badge").inner_text()

    # A non-zero seed is accepted and persisted; the badge shows it verbatim.
    page.locator("#global-seed").fill("41")
    with post("settings") as saved:
        page.locator("#apply-settings").click()
    assert saved.value.json()["random_seed"] == 41
    expect(page.locator("#settings-badge")).to_contain_text("41")
    page.locator("#open-settings").click()
    expect(page.locator("#global-seed")).to_have_value("41")
    # The save must release the form before the background catalog refresh,
    # otherwise Escape/backdrop stay blocked for the whole refresh.
    expect(page.locator("#apply-settings")).to_be_enabled(timeout=20000)
    page.keyboard.press("Escape")
    expect(page.locator("#global-settings")).not_to_have_attribute("open", "")

    # The saved seed reaches the search and is reported with the drawn base seed.
    with expect_api_json(page, "/api/proof/optimize", timeout=120000) as live:
        page.locator("#auto-tune").click()
    data = live["data"]
    assert data["seed"] == 41, data["seed"]
    assert data["seed_used"] > 0, data["seed_used"]

    # Invalid seeds are rejected by the input and never saved.
    page.locator("#open-settings").click()
    for invalid in ("-1", "2147483648", "1.5"):
        page.locator("#global-seed").fill(invalid)
        assert not page.locator("#global-seed").evaluate("n=>n.validity.valid")
    page.locator("#global-seed").fill("0")
    with post("settings") as saved:
        page.locator("#apply-settings").click()
    assert saved.value.json()["random_seed"] == 0
    assert not errors, errors
    ctx.close()


MESSAGE = "Global seed: default 0, validated save, reported base seed, badge, rejection of invalid values: OK"


def scenario(browser):
    # UI-only assertions: the seed echo and badge are read; the chosen parameters are not.
    with (
        ui_only_trials(),
        temporary_root() as root,
        serve_fixture(root, two_calendar_windows) as url,
        scenario_isolation(browser),
    ):
        run(browser, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
