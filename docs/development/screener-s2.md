# Stage-9 S2 — Pair table and common-parameter search

Date: 2026-11-08

## Delivered

The local dark-only view is now table-first. One strategy, its parameters and the illustrative cost scenario are applied to every compatible pair when changed. Each row shows the economic relationship, dated-contract expiry, sample length, current-margin share, closed proxy trades, the difference between annualized strategy and matched LQDT returns (percentage points per year), maximum benchmark-relative drawdown and worst shortfall as percentages of the fixed initial research allocation. A row opens indexed strategy/LQDT and percentage-excess charts. **The interface displays no absolute currency amounts.** Internal cash and margin accounting still uses RUB under the pilot policy.

The registry has twelve named research relationships: the earlier CNY pair and six flagship pairs, plus silver and EUR perpetual/dated pairs, a short-history BTC perpetual/dated diagnostic and two dated calendar controls (`GLZ6/GLH7`, `CRZ6/CRH7`). New candle and EOD history inputs are ignored local data, not committed market datasets. The strategy is unavailable for dated calendars when it requires perpetual funding. In the local stress-cost screen, eight rows have annualized price-only results, three fail the working capital screen and the BTC pair is withheld for short history. This is a data/feasibility inventory, not a profitable-pair ranking. The funding-only strategy has a different availability set.

Checkboxes select the pairs for a **single shared parameter set**, not a combined trading book. The bounded search tests a small neighborhood around the displayed parameters: for mean reversion, lookback, entry and exit thresholds; for funding-only carry, lookback. It logs every attempt, uses common dates and chronological training/validation, and ranks by the *weakest selected pair's* annualized excess over LQDT. Current working rules require positive training and validation results for every selected pair, no breach of the provisional benchmark-relative risk boundaries, and at least three adjacent mean-reversion configurations (or two funding-only neighbors) retaining at least 70% of the candidate's validation score. Among qualifying neighborhoods it favors the strongest worst neighbor, not an isolated peak. These are versioned research heuristics from the product hypothesis, not statistically proved safety thresholds.

Confirmation is calculated only for the chosen neighborhood, not used to select it. Previously viewed history, current-specification fallbacks, candle-price proxies and shared-source uncertainties mean even that confirmation is **not a sealed prospective test**. If no positive plateau exists, the result says so and exposes the full trial list; it does not apply a losing or isolated setting automatically. Searches and all distinct manual screen configurations are logged under ignored `data/analytics/` files.

## Operating boundary

Run as before:

```bash
uv run python -m moex_spread_lab.cli screener --port 8765
```

Open `http://127.0.0.1:8765/` on the workstation. The server binds only to loopback and never downloads data. Data preparation follows [S1](screener-s1.md); new pairs require daily ISS candles in `data/w1/candles-screen/`, and their perpetual funding variants require cached FORTS EOD history in `data/carry-screen-history-cache/`. The CLI `carry-study` keeps non-CNY funding caps unmodelled rather than borrowing CNY rules.

The table's annualization uses each pair's own dated interval and therefore does not make all rows directly comparable. Short history withholds annualization. The optimizer intersects selected pairs' dates, but **does not sum stand-alone curves, share capital, model concurrent positions or claim basket performance**. That remains S4. `SBERF/SRZ6` is a perpetual/dated pair; the positive near/far SBRF calendar reference has not been reproduced and remains S3. No broker, VPS, Telegram or live trading is enabled.

## Acceptance evidence

Hermetic tests cover a global strategy/parameter change across the table, percentage-only projections, blocked/short rows, deterministic trial registry, a synthetic stable plateau beating an isolated historical peak, negative/no-plateau outcomes, confirmation-independent selection, saved local results and loopback HTTP boundaries. A workstation/browser smoke test displayed all twelve rows and a two-pair search with 27 trials; its outcome was `no_stable_positive_plateau`, not an optimized trading rule. The existing tests and wheel build remain required before any release claim.
