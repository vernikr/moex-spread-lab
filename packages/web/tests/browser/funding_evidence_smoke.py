"""Journal, CSV, zero-valued estimates, documented non-clearing, and no silent pruning."""

import csv
from pathlib import Path

from _harness import scenario_isolation, serve, temporary_root
from period_settings_smoke import expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect


def download(page):
    if not page.locator("#download-csv").is_visible():
        page.locator("#toggle-summary").click()
    with page.expect_download() as data:
        page.locator("#download-csv").click()
    text = Path(data.value.path()).read_text().splitlines()
    return {r["date"]: r for r in csv.DictReader(line for line in text if not line.startswith("#"))}


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
    # The rates, kinds, dates and symmetry of the export are asserted on the proof payload in
    # packages/proof/tests/test_proof_funding.py::test_constant_midpoint_1_2_3_and_observed_zero_both_signs;
    # here only the client-side column mapping has to survive the browser.
    assert set(days[16:21]) <= set(download(page))
    expect(page.locator("tr[data-period]").filter(has_text=days[16])).to_contain_text(
        "Опубликованная ставка: 0"
    )
    page.goto(url + req + WEEKEND_PAIR.replace("/", "%2F"))
    ready(page, "training")
    page.locator("#tab-table").click()
    # Dated no-clearing rows and their zero funding are asserted on the payload in
    # packages/proof/tests/test_proof_funding.py::test_weekend_candles_stay_in_price_journal_but_no_monday_double_booking_or_synthetic_settles;
    # the rendered reason and the exported columns stay here.
    rows = download(page)
    assert {"2025-08-16", "2025-08-17"} <= set(rows)
    for d in ("2025-08-16", "2025-08-17"):
        expect(page.locator("tr[data-period]").filter(has_text=d)).to_contain_text(
            "не было отдельного клиринга"
        )
    change(root, "CNYRUBF", dict.fromkeys(days[17:21]))
    page.reload()
    ready(page, "training")
    page.locator('[data-branch="pair_screen|CNYRUBF"] input').check()
    page.locator('[data-branch="pair_screen|GAZPF"] input').check()
    with expect_api_json(page, "/api/proof/optimize") as rx:
        page.locator("#auto-tune").click()
    # An inadmissible cohort spends no trial and names the excluded pair at the payload level
    # (packages/proof/tests/test_strategy_search.py::test_universally_bad_members_need_confirmation_before_any_fit,
    # packages/proof/tests/test_proof_funding.py::test_cutoff_and_training_tail_never_use_right_anchor_from_holdout);
    # the UI check is the reason list and the survival of the checked cohort.
    assert rx["data"]["status"] == "selection_requires_confirmation", rx["data"]["status"]
    expect(page.locator("#compatibility-list")).to_contain_text("без ставки: 4")
    page.keyboard.press("Escape")
    expect(page.locator('[data-branch="pair_screen|GAZPF"] input')).to_be_checked()
    assert not errors, errors


MESSAGE = "Funding journal, CSV, dated reasons, unchanged checked cohort: OK"


def build(root):
    from funding_fixtures import change, fixture, weekend

    days = fixture(root)
    weekend(root)
    change(
        root,
        "CNYRUBF",
        {days[16]: 0, days[17]: -0.04, days[18]: None, days[19]: None, days[20]: 0.04},
    )
    return days


def scenario(browser):
    # The suite no longer asserts a trial count (that is a payload contract now), so the reduced
    # UI budget applies as for every other suite; the cohort is inadmissible either way.
    with temporary_root() as root:
        days = build(root)  # unique builder: a template copy would only add a copy
        with serve(root) as url, scenario_isolation(browser):
            pg = browser.new_page(viewport={"width": 1440, "height": 900})
            install_stream_spy(pg)
            run(pg, url, root, days)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
