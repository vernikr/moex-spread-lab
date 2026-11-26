"""Window metrics, chart bands, journal/CSV evidence and unavailable-return diagnostics."""

import csv
import json
import re
from pathlib import Path

from _harness import (
    CAL_URL,
    PAIR_URL,
    apply_settings,
    edit_parameters,
    expect,
    expect_api_json,
    hold_response,
    page_session,
    post,
    ready,
    release_response,
    serve_fixture,
    table_matches_chart,
    temporary_root,
    ui_only_trials,
)

from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import PAIR, two_calendar_windows


def row(page, key):
    return page.locator(f'#window-list tr[data-leaf="{key}"]')


def open_row(page, target):
    with post(page):
        target.locator("button").click()
    ready(page)
    table_matches_chart(page)


def download(page):
    if not page.locator("#download-csv").is_visible():
        page.locator("#toggle-summary").click()
    with page.expect_download() as data:
        page.locator("#download-csv").click()
    text = Path(data.value.path()).read_text()
    return text, list(
        csv.DictReader(line for line in text.splitlines() if not line.startswith("#"))
    )


def presentation(page):
    assert page.locator("#strategy-form").evaluate("n=>getComputedStyle(n).borderTopWidth") == "1px"
    assert (
        page.locator("#strategy-form").evaluate("n=>getComputedStyle(n).borderTopStyle") == "solid"
    )
    rows = page.locator("#window-list tr[data-leaf]").evaluate_all(
        "ns=>ns.filter(n=>n.dataset.to).map(n=>({from:n.dataset.from,to:n.dataset.to,text:n.querySelector('button').textContent,wrap:getComputedStyle(n.querySelector('button')).whiteSpace}))"
    )
    dates = [(r["to"], r["from"]) for r in rows]
    assert dates == sorted(dates, reverse=True)
    assert all(
        re.fullmatch(r"\d{4}\.\d{2}-\d{4}\.\d{2}", r["text"]) and r["wrap"] == "nowrap"
        for r in rows
    )


def bands(page):
    return page.evaluate(
        "Object.fromEntries(Object.entries(document.getElementById('chart-h-spread-raw')._signalLevels).map(([k,s])=>[k,s.data()]))"
    )


def calendar_steps(page):
    a, b = "calendar_native|SR|SRH3/SRM3", "calendar_native|SR|SRM3/SRU3"
    table_matches_chart(page)
    presentation(page)
    assert page.locator("#window-list th").all_text_contents() == [
        "",
        "Период",
        "V",
        "C",
        "Сд.",
        "п.п./г",
    ]
    assert "объём календарного спреда" in row(page, a).locator(
        "[data-gate=volume] span"
    ).get_attribute("title")
    assert "3×ГО" in row(page, a).locator("[data-gate=capital] span").get_attribute("title")
    expect(row(page, b).locator("[data-metric=trades]")).to_have_text("0")
    expect(row(page, b).locator("[data-metric=rate]")).to_have_text("0,00")
    raw = page.locator("#chart-h-spread-raw")
    expect(raw).to_be_visible()
    expect(raw).to_have_attribute("data-signal-lines", "2")
    assert page.evaluate(
        "Object.values(document.getElementById('chart-h-spread-raw')._signalLevels).every(s=>s.options().lineWidth===1)"
    )
    before = bands(page)
    for mode in ("raw", "garch"):
        page.locator("#norm").select_option(mode)
        assert bands(page) == before
    edit_parameters(page, clookback=10)
    assert bands(page) != before
    open_row(page, row(page, b))
    open_row(page, row(page, a))
    expect(page.locator("#clookback")).to_have_value("10")


def ledger_steps(page):
    page.locator("#tab-table").click()
    entry = page.locator("#ledger-table .badge.entry").first
    expect(entry).to_contain_text("контр.")
    entry.click()
    page.locator("tr.fill.entry").first.click()
    expect(page.locator("tr.card")).to_contain_text("контрактов:")
    text, rows = download(page)
    assert {"contracts_held", "entry_contracts", "exit_contracts"} <= rows[0].keys()
    for metadata in (
        "# scope: full_history",
        '"validation_percent":25',
        '"initial_capital_rub":60000',
        '"fill":"reference_same_bar"',
    ):
        assert metadata in text
    assert any(r["period"] == "validation" for r in rows)
    expect(page.locator("#ev-selection")).to_contain_text("размер: авто")
    assert {r["date"]: int(r["entry_contracts"]) for r in rows if int(r["entry_contracts"])} == {
        t["entry"]["date"]: t["quantity"] for t in page.evaluate("payload.trades")
    }
    assert {r["date"]: int(r["contracts_held"]) for r in rows} == {
        r["date"]: r["quantity"] for r in page.evaluate("payload.ledger")
    }
    page.evaluate("""async () => {
      const {state} = await import('/static/js/state.js');
      const {renderSummary, renderEvidence} = await import('/static/js/ledger.js');
      state.payload.summary.displayed_trade_net_rounding_delta_rub = -0.01;
      renderSummary(); renderEvidence();
    }""")
    tile = page.locator("#tiles .tile").filter(has_text="Округление сделок")
    expect(tile).to_have_count(1)
    expect(tile.locator("small")).to_contain_text("сумма показанных сделок")


def run_windows(page, url, root):
    calendar_steps(page)
    page.goto(url + PAIR_URL)
    ready(page)
    edit_parameters(page, entry=0.8, exit=0.1, lookback=6)
    assert page.evaluate(
        "payload.execution_reference && payload.trades.every(t=>t.signal.date===t.entry.date)"
    )
    cny, cal = "pair_screen||CNYRUBF/CRZ6", "calendar_native|SR|SRH3/SRM3"
    row(page, cny).locator("input").check()
    open_row(page, page.locator('[data-branch="calendar_native|SR"]'))
    with expect_api_json(page, "/api/proof/plateau") as profile:
        row(page, cal).locator("input").check()
    assert profile["data"]["status"] == "measured" and profile["data"]["execution_reference"]
    expect(page.locator("#compatibility")).not_to_be_visible()
    expect(row(page, cal)).to_have_attribute("data-reference", "true")
    open_row(page, row(page, cal))
    hold_response(page, "/windows")
    page.evaluate("windowReadings.clear();refreshWindowReadings(0)")
    page.wait_for_function("() => typeof window.releaseSmokeResponse==='function'")
    open_row(page, page.locator('[data-branch="pair_screen|CNYRUBF"]'))
    release_response(page)
    table_matches_chart(page)
    expect(page.locator('#window-list tr[data-leaf^="calendar_native"]')).to_have_count(0)
    apply_settings(page, global_capital=60000, global_slippage=2.5, validation_percent=25)
    table_matches_chart(page)
    assert page.evaluate("payload.summary.max_quantity") > 1
    ledger_steps(page)
    assert "проверка:" in row(page, cny).locator("[data-metric=rate]").get_attribute("title")
    assert page.evaluate(
        "performance.getEntriesByType('resource').every(r=>!r.name.endsWith('/optimize'))"
    )
    page.locator('[data-branch="pair_screen|BTCUSDF"] button').click()
    missing = row(page, "pair_screen||BTCUSDF/BTZ6")
    expect(missing.locator("[data-icon=download]")).to_be_visible()
    for metric in ("trades", "rate"):
        expect(missing.locator("[data-metric=" + metric + "]")).to_have_text("—")


def funding_fixture(root):
    from funding_fixtures import change, fixture, weekend

    days = fixture(root)
    weekend(root)
    change(
        root,
        "CNYRUBF",
        {days[16]: 0, days[17]: -0.04, days[18]: None, days[19]: None, days[20]: 0.04},
    )


def run_funding(page, url, root):
    from funding_fixtures import WEEKEND_PAIR, change

    days = [
        r["begin"][:10]
        for r in json.loads((root / "data/w1/candles/CNYRUBF_1d.json").read_text())["rows"]
    ]
    page.locator("#tab-table").click()
    estimated = page.locator("tr[data-period]").filter(has_text=days[18])
    for text in ("Расчётная ставка", days[17], days[20]):
        expect(estimated).to_contain_text(text)
    _, rows = download(page)
    exported = {r["date"]: r for r in rows}
    assert set(days[16:21]) <= exported.keys()
    assert exported[days[18]]["funding_rate_kind"] == "midpoint"
    assert exported[days[18]]["funding_left_date"] == days[17]
    assert exported[days[18]]["funding_right_date"] == days[20]
    expect(page.locator("tr[data-period]").filter(has_text=days[16])).to_contain_text(
        "Опубликованная ставка: 0"
    )
    page.goto(url + PAIR_URL.replace("CNYRUBF%2FCRZ6", WEEKEND_PAIR.replace("/", "%2F")))
    ready(page)
    page.locator("#tab-table").click()
    _, rows = download(page)
    assert {"2025-08-16", "2025-08-17"} <= {r["date"] for r in rows}
    for day in ("2025-08-16", "2025-08-17"):
        expect(page.locator("tr[data-period]").filter(has_text=day)).to_contain_text(
            "не было отдельного клиринга"
        )
    change(root, "CNYRUBF", dict.fromkeys(days[17:21]))
    page.reload()
    ready(page)
    for key in ("pair_screen|CNYRUBF", "pair_screen|GAZPF"):
        page.locator(f'[data-branch="{key}"] input').check()
    with expect_api_json(page, "/api/proof/optimize") as result:
        page.locator("#auto-tune").click()
    assert result["data"]["status"] == "selection_requires_confirmation"
    expect(page.locator("#compatibility-list")).to_contain_text("без ставки: 4")
    page.keyboard.press("Escape")
    expect(page.locator('[data-branch="pair_screen|GAZPF"] input')).to_be_checked()


def shortfall_fixture(root):
    two_calendar_windows(root)
    trade = research_run(ProofService(root), PAIR, optimize=False, include_plateau=False)["trades"][
        0
    ]
    path = root / "data/w1/candles/CNYRUBF_1d.json"
    data = json.loads(path.read_text())
    delta = -1 if trade["side"] == "long_spread" else 1
    for candle in data["rows"]:
        if candle["begin"][:10] > trade["entry"]["date"]:
            for field in ("open", "close", "high", "low"):
                candle[field] += delta
    path.write_text(json.dumps(data))


def run_shortfall(page, url, root):
    expected = research_run(ProofService(root), PAIR, optimize=False, include_plateau=False)
    edit_parameters(page, entry=0.8, exit=0.1, lookback=6)
    current = row(page, "pair_screen||CNYRUBF/CRZ6")
    expect(current).to_have_attribute("data-reading", "measured")
    expect(current).to_have_attribute("data-rate-state", "unavailable")
    expect(current).to_have_attribute("data-run-id", expected["run_id"])
    expect(current.locator("[data-metric=trades]")).to_have_text(str(expected["summary"]["trades"]))
    rate = current.locator("[data-metric=rate]")
    expect(rate).to_have_text("—")
    title = rate.get_attribute("title")
    checks = expected["evaluation"]["account_checks"]
    for text in (
        "Годовая оценка недоступна",
        "Дефицит свободных средств",
        checks["first_cash_shortfall_date"],
        page.evaluate("x=>fmt(x,2)", checks["minimum_free_cash_rub"]),
        "минимум",
        "Входы могли пройти раньше",
    ):
        assert text in title
    expect(rate).to_have_attribute("tabindex", "0")
    expect(rate).to_have_attribute("aria-label", title)
    apply_settings(page, validation_percent=25)
    page.locator("#lookback + .plateau-dot").hover()
    expect(page.locator("#plateau-tip")).to_contain_text("Проверка:")
    expect(page.locator("#plateau-tip")).to_contain_text("Соседи на обучении")
    reason = page.evaluate("payload.evaluation.result.reason")
    assert reason
    expect(page.locator("#plateau-tip")).to_contain_text(reason)


def scenario(browser):
    with ui_only_trials(), temporary_root() as root:
        for name, builder, run, route in (
            ("windows", two_calendar_windows, run_windows, CAL_URL),
            ("funding", funding_fixture, run_funding, PAIR_URL),
            ("shortfall", shortfall_fixture, run_shortfall, PAIR_URL),
        ):
            workspace = root / name
            workspace.mkdir()
            with (
                serve_fixture(workspace, builder) as url,
                page_session(browser, url, route) as page,
            ):
                run(page, url, workspace)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario)
