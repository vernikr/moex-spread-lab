"""Real UI strategy replacement, native fills, newest-first periods and cash-shortfall dashes.

Synthetic offline inputs exercise the actual optimizer and funded account, not mocked winners.
"""

import json
import re
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from period_settings_smoke import ready
from playwright.sync_api import expect, sync_playwright
from proof_fixtures import funding_winner, rising_calendar, two_calendar_windows
from test_global_capital import PAIR

from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_search import research_run
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler

PAIR_URL = "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion"
CAL_URL = "/proof?universe=calendar_native&asset=SR&pair=SRM3%2FSRU3&strategy=zscore_mean_reversion"
PAIR_KEY = "pair_screen||CNYRUBF/CRZ6"
CAL_KEY = "calendar_native|SR|SRM3/SRU3"
REMOVED = (
    "Справочный режим: цена закрытия сигнального бара уже известна. Это ориентир, не проверка исполнимой доходности.",
    "только график спреда · причинные окна",
)


def post(page, path):
    return page.expect_response(
        lambda r: r.url.endswith("/api/proof/" + path), timeout=30000
    )


def tune(page, expected):
    with post(page, "optimize") as done:
        page.locator("#auto-tune").click()
    result = done.value.json()
    assert result["status"] == "maximum_found", result.get("reason")
    assert result["selected_strategy"] == expected
    assert result["trial_count"] == len(result["candidates"]) == 160
    expect(page.locator("#tune-state")).to_contain_text("Применён", timeout=20000)
    ready(page, "training")
    assert page.evaluate("payload.selection.strategy") == expected
    assert page.evaluate("payload.run_id") == result["group"]["members"][0]["run_id"]
    expect(page.locator("#batch-result summary")).to_contain_text("окон: 1")
    return result


def presentation(page):
    assert all(text not in page.locator("body").inner_text() for text in REMOVED)
    assert (
        page.locator("#strategy-form").evaluate("n=>getComputedStyle(n).borderTopWidth")
        == "1px"
    )
    assert (
        page.locator("#strategy-form").evaluate("n=>getComputedStyle(n).borderTopStyle")
        == "solid"
    )
    rows = page.locator("#window-list tr[data-leaf]").evaluate_all(
        "ns=>ns.map(n=>({from:n.dataset.from,to:n.dataset.to,text:n.querySelector('button').textContent,wrap:getComputedStyle(n.querySelector('button')).whiteSpace}))"
    )
    dated = [r for r in rows if r["to"]]
    assert [(r["to"], r["from"]) for r in dated] == sorted(
        ((r["to"], r["from"]) for r in dated), reverse=True
    )
    assert all(
        re.fullmatch(r"\d{4}\.\d{2}-\d{4}\.\d{2}", r["text"]) and r["wrap"] == "nowrap"
        for r in dated
    )
    assert page.locator("#window-list .window-period small").count() == 0


def run_pair(browser, url):
    page = browser.new_page(viewport={"width": 1450, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + PAIR_URL)
    ready(page, "training")
    presentation(page)
    with post(page, "run"):
        page.locator("#fill").select_option("reference_same_bar")
    ready(page, "training")
    assert page.evaluate("payload.execution_reference")
    expect(page.locator("#period-result .result-warning")).to_have_count(0)
    r = tune(page, "funding_only")
    assert {
        (a["strategy"], a["fill"]) for a in r["strategy_search"]["alternatives"]
    } == {
        ("mean_reversion", "reference_same_bar"),
        ("funding_only", "same_day_settlement"),
    }
    expect(page.locator("#strategy")).to_have_value("funding_only")
    expect(page.locator("#fill")).to_have_value("same_day_settlement")
    expect(page.locator("#fill")).to_be_disabled()
    expect(page.locator("#entry")).to_be_hidden()
    assert not page.evaluate("payload.execution_reference")
    assert (
        r["selected_fill"]
        == page.evaluate("payload.selection.fill")
        == "same_day_settlement"
    )
    expect(page.locator("#chart-h-spread-raw")).to_have_attribute(
        "data-signal-lines", "0"
    )
    # A same-day native winner must not overwrite the user's price-fill draft.
    with post(page, "run"):
        page.locator("#strategy").select_option("mean_reversion")
    ready(page, "training")
    expect(page.locator("#fill")).to_have_value("reference_same_bar")
    page.locator(f'tr[data-leaf="{PAIR_KEY}"] input').check()
    expect(page.locator("#cohort-editor")).to_be_visible()
    expect(page.locator("#focus-editor")).to_be_hidden()
    presentation(page)
    tune(page, "funding_only")
    expect(page.locator("#group-strategy")).to_have_value("funding_only")
    expect(page.locator("#group-params input")).to_have_count(1)
    expect(page.locator("#group-fill")).to_have_value("same_day_settlement")
    expect(page.locator("#group-fill")).to_be_disabled()
    assert page.evaluate("checked.size") == 1
    with post(page, "run"):
        page.locator("#group-strategy").select_option("mr")
    ready(page, "training")
    expect(page.locator("#group-fill")).to_have_value("reference_same_bar")
    expect(page.locator("#group-params input")).to_have_count(3)
    assert not errors, errors
    page.close()


def run_calendar(browser, url):
    page = browser.new_page(viewport={"width": 1450, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + CAL_URL)
    ready(page, "training")
    presentation(page)
    with post(page, "run"):
        page.locator("#cfill").select_option("next_close")
    ready(page, "training")
    result = tune(page, "convergence_to_ruonia")
    assert {c["fill"] for c in result["candidates"]} == {"next_close"}
    expect(page.locator("#cstrategy")).to_have_value("convergence_to_ruonia")
    expect(page.locator("#cthreshold")).to_be_visible()
    expect(page.locator("#centry")).to_be_hidden()
    expect(page.locator("#cfill")).to_have_value("next_close")
    assert page.evaluate("payload.summary.ledger_reconciliation_rub") == 0
    assert len(page.evaluate("payload.trades")) > 0
    page.locator(f'tr[data-leaf="{CAL_KEY}"] input').check()
    with post(page, "run"):
        page.locator("#group-strategy").select_option("mr")
    ready(page, "training")
    tune(page, "convergence_to_ruonia")
    expect(page.locator("#group-strategy")).to_have_value("convergence_to_ruonia")
    expect(page.locator("#group-params input")).to_have_count(1)
    expect(page.locator("#group-threshold_pp")).to_have_value(
        str(page.evaluate("payload.selection.parameters.threshold_pp"))
    )
    expect(page.locator("#group-fill")).to_have_value("next_close")
    expect(page.locator("#group-profile-state")).not_to_have_attribute(
        "data-state", "loading"
    )
    presentation(page)
    page.set_viewport_size({"width": 400, "height": 850})
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    assert not errors, errors
    page.close()


def shortfall_inputs(root):
    two_calendar_windows(root)
    # Identify a causal entry in the synthetic unshocked fixture, then shock the
    # next date and later candle prices adversely. No funding/risk permission changes.
    p = research_run(ProofService(root), PAIR, optimize=False, include_plateau=False)
    trade = p["trades"][0]
    shock_after = trade["entry"]["date"]
    delta = -1 if trade["side"] == "long_spread" else 1
    path = root / "data/w1/candles/CNYRUBF_1d.json"
    data = json.loads(path.read_text())
    for row in data["rows"]:
        if row["begin"][:10] > shock_after:
            for field in ("open", "close", "high", "low"):
                row[field] += delta
    path.write_text(json.dumps(data))
    p = research_run(ProofService(root), PAIR, optimize=False, include_plateau=False)
    assert p["summary"]["trades"] > 0
    assert p["evaluation"]["account_checks"]["cash_shortfall_rows"] > 0
    assert p["evaluation"]["result"].get("annualized_excess_pp") is None
    return p


def run_dash(browser, url, expected):
    page = browser.new_page(viewport={"width": 1450, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + PAIR_URL)
    ready(page, "training")
    for name, axis in (
        ("lookback", "lookback"),
        ("entry", "entry_z"),
        ("exit", "exit_z"),
    ):
        value = PAIR["parameters"][axis]
        page.locator("#" + name).fill(str(value))
        with post(page, "run"):
            page.locator("#" + name).press("Enter")
        ready(page, "training")
    row = page.locator(f'#window-list tr[data-leaf="{PAIR_KEY}"]')
    expect(row).to_have_attribute("data-reading", "measured")
    expect(row).to_have_attribute("data-rate-state", "unavailable")
    expect(row).to_have_attribute("data-run-id", expected["run_id"])
    expect(row.locator("[data-metric=trades]")).to_have_text(
        str(expected["summary"]["trades"])
    )
    rate = row.locator("[data-metric=rate]")
    expect(rate).to_have_text("—")
    checks = expected["evaluation"]["account_checks"]
    title = rate.get_attribute("title")
    assert "Годовая оценка недоступна" in title
    assert (
        "Дефицит свободных средств" in title
        and checks["first_cash_shortfall_date"] in title
    )
    assert page.evaluate("x=>fmt(x,2)", checks["minimum_free_cash_rub"]) in title
    assert "минимум" in title and "Входы могли пройти раньше" in title
    expect(rate).to_have_attribute("tabindex", "0")
    expect(rate).to_have_attribute("aria-label", title)
    presentation(page)
    assert not errors, errors
    page.close()


if __name__ == "__main__":
    servers = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for label, build, run in (
                ("funding", funding_winner, run_pair),
                ("convergence", two_calendar_windows, run_calendar),
                ("shortfall", shortfall_inputs, run_dash),
            ):
                root = Path(tmp) / label
                root.mkdir()
                result = build(root)
                if label == "convergence":
                    rising_calendar(root)
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
                )
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                args = (result,) if label == "shortfall" else ()
                run(browser, f"http://127.0.0.1:{server.server_port}", *args)
            print(
                "Strategy/fill replacement in solo/shared forms, 80 trials per family, newest periods, borders, removed hints and real funded-shortfall dash evidence: OK"
            )
        finally:
            browser.close()
            for server in servers:
                server.shutdown()
                server.server_close()
