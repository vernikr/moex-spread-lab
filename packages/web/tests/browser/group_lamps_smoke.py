"""Real browser: diagnostic scope must survive lower-list, offscreen and unavailable focus changes."""

from _harness import exact_trials, scenario_isolation, serve_fixture, temporary_root
from period_settings_smoke import apply, expect_api_json, install_stream_spy, ready
from playwright.sync_api import expect

from proof_fixtures import two_calendar_windows


def run(browser, url):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    install_stream_spy(pg)
    pg.set_default_timeout(8000)
    errors = []
    profiles = []
    runs = []
    searches = []
    pg.on("pageerror", lambda e: errors.append(str(e)))

    def record(r):
        for path, log in [("plateau", profiles), ("run", runs), ("optimize", searches)]:
            if r.url.endswith("/api/proof/" + path):
                log.append(r.post_data_json)

    pg.on("request", record)

    def post(path):
        return pg.expect_response(lambda r: r.url.endswith("/api/proof/" + path), timeout=12000)

    def open_row(row, basis="training"):
        with post("run"):
            row.locator("button").click()
        ready(pg, basis)

    def validate_ui(response):
        expect(pg.locator("#group-profile-state")).to_have_attribute("data-state", "ready")
        for k, f in response["plateau"]["fields"].items():
            lamp = pg.locator(f"#group-{k} + .plateau-dot")
            expect(lamp).to_be_visible()
            s = lamp.get_attribute("data-score")
            assert (float(s) if s else None) == f["score"]

    def snapshot():
        return pg.locator("#cohort-editor .number-shell").evaluate_all(
            "ns=>ns.map(n=>[n.firstElementChild.value,n.lastElementChild.dataset.level,n.lastElementChild.dataset.score,n.lastElementChild.dataset.detail])"
        )

    pg.goto(
        url
        + "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3&strategy=zscore_mean_reversion"
    )
    ready(pg, "training")
    pg.locator("#open-settings").click()
    pg.locator("#global-fill").select_option("next_open")
    with post("run"):
        pg.locator("#apply-settings").click()
    ready(pg, "training")
    leaf = lambda pair: pg.locator(f'[data-leaf="calendar_native|SR|{pair}"]')
    branch = lambda name: pg.locator(f'[data-branch="{name}"]')
    a, b = "SRH3/SRM3", "SRM3/SRU3"
    pg.locator("#clookback").fill("10")
    with post("run"):
        pg.locator("#clookback").press("Enter")
    ready(pg, "training")
    a_score = float(pg.locator("#centry + .plateau-dot").get_attribute("data-score"))
    assert a_score > 70
    open_row(leaf(b))
    expect(pg.locator("#centry + .plateau-dot")).to_have_attribute("data-level", "unknown")
    pg.locator("#clookback").fill("7")
    with post("run"):
        pg.locator("#clookback").press("Enter")
    ready(pg, "training")
    open_row(leaf(a))
    expect(pg.locator("#clookback")).to_have_value("10")
    with expect_api_json(pg, "/api/proof/plateau") as result:
        pg.locator("#all-windows").check()
    together = result["data"]
    validate_ui(together)
    # Plateau arithmetic (mean at center, per-window zero, utility) is a payload contract in
    # packages/proof/tests/test_proof_plateau.py::test_mean_at_center_and_every_neighbour_precedes_the_stability_ratio;
    # the browser only binds the lamp to the score it was given (validate_ui) and must not start a search.
    assert not searches
    assert pg.evaluate("checked.size") == 2
    expect(pg.locator("#group-count")).to_have_count(0)
    expect(pg.locator("#focus-editor")).to_be_hidden()
    both = snapshot()
    calls = len(profiles)
    open_row(leaf(b))
    assert pg.evaluate("payload.selection.pair") == b and "plateau" not in pg.evaluate("payload")
    expect(pg.locator("#group-window")).to_have_value("10")
    assert snapshot() == both and len(profiles) == calls
    open_row(branch("pair_screen|CNYRUBF"))
    assert pg.evaluate("payload.selection.pair") == "CNYRUBF/CRZ6"
    assert snapshot() == both and len(profiles) == calls
    expect(pg.locator("#chart-selection-scope")).to_contain_text("параметры окна")
    pg.locator("#group-entry_z + .plateau-dot").hover()
    tip = pg.locator("#plateau-tip")
    expect(tip).to_contain_text("Отмеченные окна: 2")
    expect(tip).to_contain_text("Среднее в центре")
    expect(tip).not_to_contain_text("2025-06-17")
    # An undisplayable graph cannot blank or replace the group lights.
    branch("pair_screen|BTCUSDF").locator("button").click()
    expect(pg.locator("#charts-empty")).to_be_visible()
    assert snapshot() == both and len(profiles) == calls
    open_row(branch("pair_screen|CNYRUBF"))
    run_count = len(runs)
    with expect_api_json(pg, "/api/proof/plateau") as changed:
        pg.locator("#group-window").fill("12")
    validate_ui(changed["data"])
    assert profiles[-1]["parameters"]["window"] == 12 and len(runs) == run_count and not searches
    assert all(not ({"pair", "asset", "universe"} & r.keys()) for r in profiles)
    # Changes to the persistent split re-evaluate the entire fixed cohort, not its current chart.
    with expect_api_json(pg, "/api/proof/plateau") as res:
        apply(pg, 25)
    validate_ui(res["data"])  # apply() already asserted the split reached the payload
    open_row(branch("calendar_native|SR"), "validation")
    open_row(leaf(a), "validation")
    # Deliver a stale profile after its successor. A one-window result or its focus metadata
    # must not paint over the latest two-window vector.
    pg.evaluate(
        """() => {const old=window.fetch;let once=true;window.fetch=async(...args)=>{const res=await old(...args);if(once&&String(args[0]).endsWith('/plateau')){once=false;await new Promise(r=>window.releaseOld=r)}return res}}"""
    )
    with post("plateau"):
        leaf(b).locator("input").uncheck()
    with expect_api_json(pg, "/api/proof/plateau") as current:
        leaf(b).locator("input").check()
    validate_ui(current["data"])
    new_snapshot = snapshot()
    pg.evaluate("window.releaseOld()")
    assert pg.evaluate("checked.size") == 2
    expect(pg.locator("#group-count")).to_have_count(0)
    assert snapshot() == new_snapshot
    # Group optimization should continue if the user merely inspects the other row while it is running.
    pg.evaluate(
        """() => {const old=window.fetch;window.fetch=async(...args)=>{const res=await old(...args);if(String(args[0]).endsWith('/optimize'))await new Promise(r=>window.releaseTune=r);return res}}"""
    )
    with expect_api_json(pg, "/api/proof/optimize") as optimized:
        pg.locator("#auto-tune").click()
        with post("run"):
            leaf(b).locator("button").click()
        expect(pg.locator("#auto-tune")).to_be_disabled()
        assert snapshot() == new_snapshot
        with post("run"):
            pg.evaluate("window.releaseTune()")
        ready(pg, "validation")
    expect(pg.locator("#tune-state")).to_be_empty()
    # The search status itself is asserted in packages/proof/tests/test_strategy_search.py.
    validate_ui(optimized["data"])
    adopted = snapshot()
    count = len(profiles)
    open_row(leaf(a), "validation")
    assert snapshot() == adopted and count == len(profiles)
    # Incompatible fixed denominator -> unknown, no silent pruning, no unsolicited modal.
    with expect_api_json(pg, "/api/proof/plateau"):
        pg.locator("#group-strategy").select_option("funding_only")
    # Exclusion reasons for an incompatible denominator: packages/proof/tests/test_proof_plateau.py::
    # test_incompatible_or_invalid_center_is_not_profiled_on_a_subset_or_clamped.
    expect(pg.locator("#group-profile-state")).to_contain_text("2 из 2")
    expect(pg.locator("#compatibility")).not_to_be_visible()
    assert (
        pg.evaluate("checked.size") == 2
        and pg.locator("#cohort-editor .plateau-dot[data-level=unknown]").count() == 1
    )
    with post("run"):
        pg.locator("#all-windows").uncheck()
    ready(pg, "validation")
    expect(pg.locator("#cohort-editor")).to_be_hidden()
    expect(pg.locator("#centry + .plateau-dot")).to_be_visible()
    assert pg.evaluate("payload.selection.pair") == a and "plateau" in pg.evaluate("payload")
    assert not errors, errors
    pg.close()


MESSAGE = "Group means, two rows of one asset, unchecked/unavailable focus, edits, split, races, in-flight tuning and single fallback: OK"


def scenario(browser):
    # Exact trials: the assertions read search-derived UI state (selection, lamps, ledger).
    with (
        exact_trials(),
        temporary_root() as root,
        serve_fixture(root, two_calendar_windows) as url,
        scenario_isolation(browser),
    ):
        run(browser, url)


if __name__ == "__main__":
    from _harness import standalone

    standalone(scenario, MESSAGE)
