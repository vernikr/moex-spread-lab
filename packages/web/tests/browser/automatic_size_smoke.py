"""Real browser: automatic per-trade size and one persistent slippage setting.

Run: uv run --with playwright python tests/browser/automatic_size_smoke.py
Synthetic data only; no workstation or market caches are changed.
"""

import csv
import io
from pathlib import Path

from _harness import serve_fixture, temporary_root
from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright

from proof_fixtures import fixture


def run(browser, url):
    context = browser.new_context(viewport={"width": 1440, "height": 950})
    pair, cal = context.new_page(), context.new_page()
    errors, requests = [], []
    for page in (pair, cal):
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: requests.append(r) if "/api/proof/" in r.url and r.post_data else None,
        )
    pair.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
    cal.goto(
        url + "/?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3&strategy=zscore_mean_reversion"
    )
    ready(pair, "training")
    ready(cal, "training")
    for page in (pair, cal):
        assert (
            page.locator(
                "#quantity,#cquantity,#slippage,#cslippage,#group-quantity,#group-slippage_ticks"
            ).count()
            == 0
        )
        expect(page.locator("#sizing-hint")).to_have_count(0)
        assert page.evaluate("payload.selection.sizing") == "max_affordable"
        assert not page.locator(
            "#col-norm input[data-axis=quantity],#col-norm input[data-axis=slippage_ticks]"
        ).count()
    # Invalid values cannot save; cancel cannot change the server or a proof.
    original = pair.evaluate("payload.run_id")
    pair.locator("#open-settings").click()
    expect(pair.locator("label[for=global-slippage]")).to_have_text("Проскальзывание · тики")
    pair.locator("#global-slippage").fill("-1")
    pair.locator("#apply-settings").click()
    expect(pair.locator("#global-settings")).to_be_visible()
    pair.locator("#global-slippage").fill("5")
    pair.locator("#cancel-settings").click()
    assert pair.evaluate("payload.run_id") == original
    pair.locator("#open-settings").click()
    expect(pair.locator("#global-slippage")).to_have_value("1")
    pair.locator("#global-slippage").fill("2.5")
    with pair.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        pair.locator("#apply-settings").click()
    ready(pair, "training")
    ready(cal, "training")
    expect(cal.locator("#settings-badge")).to_contain_text("2,5")
    assert pair.evaluate("payload.selection.slippage_ticks") == 2.5
    assert cal.evaluate("payload.selection.slippage_ticks") == 2.5
    # It survives reload, asset switches, a checked editor and optimization.
    cal.reload()
    ready(cal, "training")
    cal.locator("#clookback").fill("10")
    with cal.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        cal.locator("#clookback").press("Enter")
    ready(cal, "training")
    assert cal.evaluate("payload.summary.max_quantity") > 1
    for trade in cal.evaluate("payload.trades"):
        assert trade["entry"]["costs"]["slippage"] == 2.5 * trade["quantity"]
    cal.locator("#all-windows").check()
    expect(cal.locator("#group-window")).to_be_visible()
    assert cal.locator("#cohort-editor input[data-axis]").count() == 3
    with cal.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        cal.locator("#auto-tune").click()
    ready(cal, "training")
    expect(cal.locator("#tune-state")).to_be_empty()
    assert cal.evaluate("payload.selection.slippage_ticks") == 2.5
    assert set(cal.evaluate("Object.keys(payload.selection.parameters)")) == {
        "window",
        "entry_z",
        "exit_z",
    }
    cal.locator("#tab-table").click()
    expect(cal.locator("#ledger-table .badge.entry").first).to_contain_text("контр.")
    cal.locator("#ledger-table .badge.entry").first.click()
    cal.locator("tr.fill.entry").first.click()
    expect(cal.locator("tr.card")).to_contain_text("контрактов:")
    cal.locator("#toggle-summary").click()
    expect(cal.locator("#ev-selection")).to_contain_text("размер: авто")
    with cal.expect_download() as download:
        cal.locator("#download-csv").click()
    text = Path(download.value.path()).read_text()
    table = list(
        csv.DictReader(
            io.StringIO("\n".join(l for l in text.splitlines() if not l.startswith("#")))
        )
    )
    assert all(k in table[0] for k in ("contracts_held", "entry_contracts", "exit_contracts"))
    entries = {r["date"]: int(r["entry_contracts"]) for r in table if int(r["entry_contracts"])}
    assert entries == {t["entry"]["date"]: t["quantity"] for t in cal.evaluate("payload.trades")}
    # All browser requests use the same implicit size/cost policy, including cohort profiles.
    for request in requests:
        body = request.post_data_json
        assert body["sizing"] == "max_affordable", body
        assert "quantity" not in body and "slippage_ticks" not in body, body
    cal.set_viewport_size({"width": 400, "height": 850})
    cal.locator("#open-settings").click()
    expect(cal.locator("#global-slippage")).to_have_value("2.5")
    assert cal.evaluate("document.documentElement.scrollWidth <= innerWidth"), cal.evaluate(
        "[...document.body.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).map(n=>[n.tagName,n.id,n.className,n.getBoundingClientRect().right]).slice(0,15)"
    )
    assert not errors, errors
    context.close()


if __name__ == "__main__":
    with sync_playwright() as pw, temporary_root() as root, serve_fixture(root, fixture) as url:
        browser = pw.chromium.launch()
        try:
            run(browser, url)
            print(
                "Automatic size, shared slippage, migration, two tabs, group search, fills and CSV: OK"
            )
        finally:
            browser.close()
