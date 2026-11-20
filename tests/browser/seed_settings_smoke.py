"""Real browser: the global random seed is shown, validated, saved and reported.

Run: uv run --with playwright python tests/browser/seed_settings_smoke.py
"""

import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect, sync_playwright
from proof_fixtures import two_calendar_windows

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler


def run(browser, url):
    ctx = browser.new_context(viewport={"width": 1500, "height": 980})
    page = ctx.new_page()
    errors = []
    install_stream_spy(page)
    page.on("pageerror", lambda e: errors.append(str(e)))

    def post(route):
        return page.expect_response(
            lambda r: r.url.endswith("/api/" + route), timeout=20000
        )

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
    page.keyboard.press("Escape")

    # The saved seed reaches the search and is reported with the drawn base seed.
    with expect_api_json(page, "/api/proof/optimize", timeout=30000) as live:
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
                "Global seed: default 0, validated save, reported base seed, badge, rejection of invalid values: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
