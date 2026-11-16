# Unified execution proof (`/proof`)

Status: **slices 1–2 delivered** (payload + adapters + tests 2026-11-16; LWC UI rewrite 2026-11-16). Slice 3 (screener deep links, decisions/README polish) is pending. Design: `execution-proof-screen-design.md` (workspace artifact, not committed).

## What slice 1 adds

- `proof.py` (`ProofService`): single entry point for `/api/proof/*`, dispatched by `universe` — `calendar_native` (legacy, compatibility preserved) or `pair_screen` (new).
- Pair adapter reproduces both screener strategies with fill-level detail:
  - `mean_reversion`: fills at the next daily open (candle-open proxy); z-score at the signal bar; itemized fees (`exchange_clearing` / `broker` = 0 / `slippage`) per side from the published tariff.
  - `funding_only`: same-day settlement fill proxy; funding received tracked per day (`daily_funding_included_rub`); exit-day funding correctly excluded; open position at data end flagged `open_at_end`.
- Payload `execution-proof.v1` (superset of `calendar-proof.v1`; calendar keys preserved): unified `series` / `trades` / `ledger` / `summary` with `cost_totals`, per-side `entry_costs` / `exit_costs`, day volumes (`near_volume` / `far_volume`; min-leg rule flag), `max_excess_drawdown_rub`, `ledger_reconciliation_rub` (must be 0.00), per-trade `lqdt_on_margin_rub` and `excess_rub`.
- `/vendor/lightweight-charts.standalone.production.js` is served same-origin (vendored, Apache-2.0, see `vendor/README.md`); CSP unchanged, no CDN.
- Engines extended **additively** (`analytics.py` trade fill/cost fields, `screener.py` candle volumes, `carry.py` volumes/basis/funding components); no metric semantics changed.

## What slice 2 adds

- `proof_ui.html` rewritten as a single Lightweight Charts 5.3.0 screen (dark, EN, CSP-clean, no CDN; vendored script only).
- Universe switch in the toolbar: **pair screen** (pair / strategy / mean-reversion z-params or funding carry, fill next-open or same-day settlement) and **native calendar spread** (asset / front window / strategy / fill rule). Deep links: `?universe=pair_screen&pair=…&strategy=…` and `?universe=calendar_native&asset=…&pair=…&strategy=…`.
- Stacked synchronized panes (shared visible logical range + crosshair across panes): near leg, far leg, spread (unified markers `▲/▼` entries, `●` exits, hollow/forced-mark styling for `open_at_end`; optional dashed last-trade line on native calendars), volume histogram (min(near, far) proxy for pairs, exchange spread volume for calendars; dashed rule line at `MIN_DAILY_VOLUME`), funding accrual panel (funding_only only: per-day funding, total in the tagline), cumulative net RUB line with drawdown-from-peak on the left axis and a zero priceLine.
- **One unified ledger table** (the only table): every day with signal/z/pp, near & far price + volume, spread settle/open/last, event badges, day P&L, funding, costs, cumulative. Entry/exit row click expands the inline fill card (legs' fill prices, spread fill, contracts, day volumes, itemized fees exchange/broker/slippage, LQDT-on-margin hurdle, win/loss chip). Footer: totals row + reconciliation row (net − cumulative, FX point-value drift when present) + forced-exit caveat row. Clicking a badge selects the trade on the chart without re-rendering the table.
- Evidence bar: `run_id` + copy button, provenance summary (count + full hashes in the tooltip title), period, selection, price-role proxy label, reconciliation chip, CSV download (client-side `Blob`; header comments `# run_id` / `# universe` / `# selection` / `# price_role` / `# provenance` / `# notes`).
- Tiles from `summary` (trades, gross/costs/net, LQDT-on-margin hurdle, excess over LQDT, max excess drawdown, reconciliation, optional FX point-value drift); flags for volume/capital screens, fee model, open positions.
- Backend (additive): the calendar adapter now emits the same `instrument` block and `provenance` file hashes as the pair adapter (`near/far/spread history` cache + contracts + LQDT lineage + RUONIA), plus `price_role` and `run_id` (previously missing on the calendar path).
- Routing fix: `/proof`, `/vendor/*`, `/api/proof/*` (and all other routes) now strip the query string before matching — deep links with `?universe=…` previously 404'd. Local-host and Origin allowlists are unchanged.
- Verified end-to-end with headless Chromium (playwright) against synthetic offline fixtures: both universes render with zero console/page errors, inline cards and badge selection work, CSV downloads, no external requests. Payload keys cross-checked against the UI for both universes; `node --check` on the inline script.

## Reconciliation contract (asserted in tests)

- `Σ ledger.costs == Σ trade entry+exit costs` (±0.02 ₽ rounding).
- `Σ ledger.mark_pnl == Σ trade gross` (telescoping; prices for the pair book are RUB-normalized `near*mv − far*mv`, constant multipliers — no FX drift on this universe).
- `last(ledger.cumulative_net) == Σ trade net` (±0.02 ₽); `ledger_reconciliation_rub == 0.0`.
- `cost_totals` equals the sum of per-side component dictionaries; broker component always 0.0 (VTB zero fee to 2026-12-31).
- Same inputs → identical `run_id`.

## Known gaps (slice 3)

- Per-day LQDT-on-margin is not shown in the ledger (per-trade hurdle only, same semantics as the calendar proof).
- Calendar `instrument.far_expiry` is populated only when the far contract is present in `data/w1/current-intake.json` (otherwise `null`); the calendar window block carries no `observations` count by design (UI falls back to the series length).
- Deep-link parameters other than `universe`/`pair`/`asset`/`strategy` are not encoded (z-scores, quantity, slippage keep UI defaults).

## Gate notes

- Pair proof applies the same admissions as `/api/run`: unknown pair, missing local inputs, or a failed working-capital stress screen are hard errors (400), not degraded output.
- Prices remain ISS candle/settlement **proxies**; nothing here is executable fills or a broker/account reconciliation.
