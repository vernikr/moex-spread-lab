"""Browser check for fixed-size trade-rounding disclosure and evidence rendering."""

from _harness import serve_fixture, temporary_root
from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright

from proof_fixtures import fixture

if __name__ == "__main__":
    with sync_playwright() as pw, temporary_root() as root, serve_fixture(root, fixture) as url:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
            ready(page, "training")
            page.evaluate("""async () => {
                const { state } = await import('/static/js/state.js');
                const { renderSummary, renderEvidence } = await import('/static/js/ledger.js');
                state.payload.summary.displayed_trade_net_rounding_delta_rub = -0.01;
                renderSummary();
                renderEvidence();
            }""")
            tile = page.locator("#tiles .tile").filter(has_text="Округление сделок")
            expect(tile).to_have_count(1)
            expect(tile.locator("small")).to_contain_text("сумма показанных сделок")
            assert not errors, errors
            print("Rounding disclosure and evidence: OK")
        finally:
            browser.close()
