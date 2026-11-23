"""Browser check for fixed-size trade-rounding disclosure and evidence rendering."""

from _harness import scenario_isolation, serve_fixture, temporary_root
from period_settings_smoke import ready
from playwright.sync_api import expect

from proof_fixtures import fixture

MESSAGE = "Rounding disclosure and evidence: OK"


def scenario(browser):
    with temporary_root() as root, serve_fixture(root, fixture) as url, scenario_isolation(browser):
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


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
