"""Journal, CSV, zero-valued estimates, documented non-clearing, and no silent pruning."""

import csv
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler


def download(page):
    if not page.locator("#download-csv").is_visible():
        page.locator("#toggle-summary").click()
    with page.expect_download() as data:
        page.locator("#download-csv").click()
    text = Path(data.value.path()).read_text().splitlines()
    return {
        r["date"]: r
        for r in csv.DictReader(line for line in text if not line.startswith("#"))
    }


def run(page, url, root, days):
    from funding_fixtures import WEEKEND_PAIR, change

    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    req = "/?universe=pair_screen&strategy=mean_reversion&pair="
    page.goto(url + req + "CNYRUBF%2FCRZ6")
    ready(page, "training")
    page.locator("#tab-table").click()
    row = page.locator("tr[data-period]").filter(has_text=days[18])
    expect(row).to_contain_text("Расчётная ставка")
    expect(row).to_contain_text(days[17])
    expect(row).to_contain_text(days[20])
    rows = download(page)
    for d in days[18:20]:
        r = rows[d]
        assert (
            r["funding_rate_kind"] == "midpoint"
            and float(r["funding_swaprate_rub_per_unit"]) == 0
            and float(r["funding_rub"]) == 0
        )
        assert (r["funding_left_date"], r["funding_right_date"]) == (days[17], days[20])
        assert float(r["funding_left_rate"]) == -float(r["funding_right_rate"])
    assert rows[days[16]]["funding_rate_kind"] == "published"
    expect(page.locator("tr[data-period]").filter(has_text=days[16])).to_contain_text(
        "Опубликованная ставка: 0"
    )
    page.goto(url + req + WEEKEND_PAIR.replace("/", "%2F"))
    ready(page, "training")
    page.locator("#tab-table").click()
    rows = download(page)
    for d in ("2025-08-16", "2025-08-17"):
        expect(page.locator("tr[data-period]").filter(has_text=d)).to_contain_text(
            "не было отдельного клиринга"
        )
        r = rows[d]
        assert (
            r["funding_rate_kind"] == "no_clearing"
            and not r["funding_swaprate_rub_per_unit"]
            and float(r["funding_rub"]) == 0
        )
    change(root, "CNYRUBF", dict.fromkeys(days[17:21]))
    page.reload()
    ready(page, "training")
    page.locator('[data-branch="pair_screen|CNYRUBF"] input').check()
    page.locator('[data-branch="pair_screen|GAZPF"] input').check()
    with page.expect_response(lambda r: r.url.endswith("/api/proof/optimize")) as rx:
        page.locator("#auto-tune").click()
    r = rx.value.json()
    assert r["trial_count"] == 0
    expect(page.locator("#compatibility-list")).to_contain_text("без ставки: 4")
    page.keyboard.press("Escape")
    expect(page.locator('[data-branch="pair_screen|GAZPF"] input')).to_be_checked()
    assert not errors, errors


if __name__ == "__main__":
    from funding_fixtures import change, fixture, weekend

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        ds = fixture(root)
        weekend(root)
        change(
            root,
            "CNYRUBF",
            {ds[16]: 0, ds[17]: -0.04, ds[18]: None, ds[19]: None, ds[20]: 0.04},
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = pw.chromium.launch()
        try:
            run(
                browser.new_page(viewport={"width": 1440, "height": 900}),
                f"http://127.0.0.1:{server.server_port}",
                root,
                ds,
            )
            print("Funding journal, CSV, dated reasons, unchanged checked cohort: OK")
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
