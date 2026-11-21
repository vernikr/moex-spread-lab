"""Optional real-browser regression; uses synthetic data, never workstation inputs.

After installing Chromium for Playwright: uv run --with playwright python tests/browser/period_settings_smoke.py
"""

import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proof_fixtures import fixture

_STREAM_SPY = """
(() => {
  window.__proofResults = [];
  const orig = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await orig(...args);
    const url = String(args[0] && args[0].url ? args[0].url : args[0]);
    if (url.includes('/api/')) {
      const clone = res.clone();
      clone.text().then(txt => {
        const ctype = (res.headers.get('content-type') || '').toLowerCase();
        let data = null;
        if (ctype.includes('ndjson')) {
          for (const line of txt.split(String.fromCharCode(10))) {
            const s = line.trim();
            if (!s) continue;
            const msg = JSON.parse(s);
            if (msg.type === 'result') data = msg.data;
            else if (msg.type === 'error') data = { error: msg.error };
          }
        } else if (txt.trim()) {
          data = JSON.parse(txt);
        }
        window.__proofResults.push({ url, status: res.status, data });
      }).catch(() => {});
    }
    return res;
  };
})();
"""


def install_stream_spy(target):
    target.add_init_script(_STREAM_SPY)


@contextmanager
def expect_api_json(page, suffix, timeout=120000):
    box = {}
    start = page.evaluate("window.__proofResults ? window.__proofResults.length : 0")
    yield box
    page.wait_for_function(
        "([s, n]) => (window.__proofResults || []).slice(n).some(r => r.url.endsWith(s))",
        arg=[suffix, start],
        timeout=timeout,
    )
    box["data"] = page.evaluate(
        "([s, n]) => { const m = (window.__proofResults || []).slice(n).filter(r => r.url.endsWith(s)); return m.length ? m[m.length - 1].data : null; }",
        arg=[suffix, start],
    )


def ready(page, basis):
    expect(page.locator("#period-result")).to_be_visible(timeout=20000)
    expect(page.locator("#period-result")).to_have_attribute(
        "data-basis", basis, timeout=20000
    )
    expect(page.locator("#ev-runid")).to_have_text(re.compile(r"^[0-9a-f]{16}$"))
    expect(page.locator("#render-progress")).to_have_attribute(
        "aria-hidden", "true", timeout=20000
    )


def apply(page, share):
    page.locator("#open-settings").click()
    page.locator("#validation-percent").fill(str(share))
    with page.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        page.locator("#apply-settings").click()
    ready(page, "validation" if share else "training")
    assert page.evaluate("payload.evaluation.validation_percent") == share


def run(browser, url):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    pair, cal = context.new_page(), context.new_page()
    errors = []
    for page in (pair, cal):
        page.on("pageerror", lambda e: errors.append(str(e)))
    pair.goto(url + "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6")
    cal.goto(url + "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3")
    ready(pair, "training")
    ready(cal, "training")
    run = pair.locator("#ev-runid").text_content()
    assert not pair.locator(".validation-line").count()
    pair.locator("#open-settings").click()
    expect(pair.get_by_role("dialog")).to_be_visible()
    expect(pair.locator("#validation-percent")).to_have_value("0")
    expect(pair.locator("#global-fill")).to_have_value("reference_same_bar")
    expect(pair.locator('#global-timeframe[role="radiogroup"]')).to_be_visible()
    expect(pair.locator('#global-timeframe [role="radio"][data-value="1d"]')).to_have_attribute("aria-checked", "true")
    settings_text = pair.locator("#global-settings").inner_text()
    assert "Снимите галочку, чтобы сделать проверку обязательной" not in settings_text
    assert "Настройки общие для всех пар и стратегий" not in settings_text
    assert pair.locator("#global-settings .settings-footer").evaluate("n=>getComputedStyle(n).position") == "sticky"
    # Clicking outside on the backdrop closes the dialog.
    pair.mouse.click(5, 5)
    expect(pair.get_by_role("dialog")).not_to_be_visible()
    pair.locator("#open-settings").click()
    pair.locator("#validation-percent").fill("51")
    pair.locator("#apply-settings").click()
    expect(pair.get_by_role("dialog")).to_be_visible()
    assert not pair.locator("#validation-percent").evaluate("n=>n.validity.valid")
    pair.keyboard.press("Escape")
    expect(pair.get_by_role("dialog")).not_to_be_visible()
    assert pair.locator("#ev-runid").text_content() == run
    apply(pair, 25)
    ready(cal, "validation")  # open tab, same server preference
    assert pair.locator("#ev-runid").text_content() != run
    assert pair.evaluate("payload.evaluation.observations") == {
        "training": 42,
        "validation": 13,
    }
    expect(pair.locator("#chart-h-spread .validation-line")).to_be_visible()
    expect(pair.locator("#chart-scope")).to_contain_text("пунктир")
    pair.locator("#tab-table").click()
    expect(pair.locator("tr.validation-start")).to_contain_text(
        pair.evaluate("payload.evaluation.periods.validation[0]")
    )
    assert pair.locator("tr[data-period=validation]").count() == 13
    assert pair.locator("tr[data-period=training]").count() == 42
    pair.locator("#lookback + .plateau-dot").hover()
    expect(pair.locator("#plateau-tip")).to_contain_text("Проверка:")
    expect(pair.locator("#plateau-tip")).to_contain_text(
        "ни на обучении, ни на проверке"
    )
    with pair.expect_response(lambda r: r.url.endswith("/api/proof/run")):
        pair.locator("#auto-tune").click()
    ready(pair, "validation")
    expect(pair.locator("#tune-state")).to_be_empty()
    expect(pair.locator("#batch-result")).to_be_visible()
    pair.locator("#toggle-summary").click()
    with pair.expect_download() as download:
        pair.locator("#download-csv").click()
    csv = Path(download.value.path()).read_text()
    assert (
        "# scope: full_history" in csv
        and '"validation_percent":25' in csv
        and ",validation," in csv
    )
    cal.reload()
    ready(cal, "validation")
    assert cal.evaluate("payload.evaluation.validation_percent") == 25
    cal.set_viewport_size({"width": 400, "height": 850})
    apply(cal, 50)
    assert cal.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(pair.locator("#period-result")).to_contain_text("Проверка 50%")
    apply(cal, 0.5)
    assert cal.evaluate("payload.evaluation.observations.validation") == 1
    apply(cal, 0)
    ready(pair, "training")
    assert (
        not cal.locator(".validation-line").count()
        and not pair.locator("tr.validation-start").count()
    )
    assert not errors, errors
    context.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        root = Path(tmp)
        fixture(root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = pw.chromium.launch()
        try:
            run(browser, "http://127.0.0.1:" + str(server.server_port))
            print(
                "Global settings, both routes/universes, holdout, chart, journal and CSV: OK"
            )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
