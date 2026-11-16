# Unified execution proof (`/proof`)

Status: **slices 1–3 delivered** (payload + adapters + tests 2026-11-16; LWC UI rewrite 2026-11-16; screener deep links + docs 2026-11-16). Design: `execution-proof-screen-design.md` (workspace artifact, not committed).

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

## What slice 3 adds

- Screener table (`screener_ui.html`): every pair row carries a `Proof →` deep link to `/proof?universe=pair_screen&pair=…&strategy=…` (current strategy dropdown applied; `%2F`-encoded ids; click does not select the row). The header already linked to `/proof`.
- `README.md` and `docs/decisions.md` (A-14) reference the unified screen and the vendored-LWC/`execution-proof.v1` schema superset; proof doc linked from the README index.
- Final acceptance on **real local data was not possible in the build environment** (`data/` is gitignored and absent from the clone); verification used the same offline synthetic fixtures as the test suite. The user-side acceptance run on real cached inputs remains open (see below).
- Deep-link parameters beyond `universe`/`pair`/`asset`/`strategy` are not encoded (z-scores, quantity, slippage keep UI defaults) — unchanged from slice 2.

## Spread display & causal normalization (2026-11-16)

- Series now carry `spread_rel` / `spread_open_rel` / `spread_last_trade_rel`: the spread as a percentage of the far leg — pair screen: spread ÷ far contract value (RUB-normalized near−far over far contract value); calendar: spread ÷ far price (points over points). Pure same-row ratio; no look-ahead anywhere.
- Per-fill blocks carry `spread_fill_rel` (used for marker labels); the spread pane plots the relative spread. The chart has a normalization select (`#norm`, spread chart only): raw; residual vs expanding mean; residual vs trailing rolling mean (20/60 obs); rolling z-score (20 obs); residual vs trailing OLS detrend (60 obs). Every transform is strictly causal (trailing windows; the expanding variant is the point-in-time running mean) and is a visual scale only — it never feeds strategy signals, markers remain tied to trades.
- Table spread cell shows % of far with the absolute value in the secondary line; tooltip shows relative + absolute + the normalized value when a non-raw mode is selected; CSV gains `spread_rel` / `spread_open_rel` / `spread_last_trade_rel` (absolute columns kept). Fill cards show the absolute fill with the relative value in parentheses. P&L, ledger, tiles and totals stay in RUB.
- `run_id` values change versus slice-3 payloads for identical selections (series content is hashed and the payload gained additive fields); pre-change `run_id`s are not reproducible after this change. `execution-proof.v1` remains a strict superset — additive optional fields only.
- Real-data acceptance (GLDRUBF/GLZ6 mean reversion, USDRUBF/SiZ6 stress-screen 400 path, BTCUSDF/BTZ6 funding carry, calendar SRU6/SRZ6; screener `Proof →` click-through; CDP console capture: zero errors/exceptions) was executed on the user's machine on 2026-11-16 via a remote desktop bridge. The ledger-table CSS change (page scroll, sticky header) shipped separately as commit `1c71cbf`.

## Proof UI layout and interaction (2026-11-16)

- The proof screen uses the full viewport width. The pair/parameter and normalization columns can be resized by dragging their separators; widths are keyboard-adjustable and saved in the browser.
- Pair options are grouped by family. Each group's unavailable pairs stay disabled and appear after its available pairs; family labels are not repeated in each pair name.
- Calendar windows are shown as a vertically expanding list. Selecting a pair/window or changing a strategy/asset starts a new analysis automatically. Numeric parameter inputs run on Enter or blur; the explicit “Показать разбор” buttons are removed.
- The header has no offline badge and includes a button to hide/show the evidence bar, quality flags, and summary tiles as one block. The chart-hover hint is removed; the near- and far-leg charts start collapsed and can be expanded independently.
- Mouse-wheel scrolling/zooming is disabled for charts. In the ledger, only event rows that expand an execution card receive a hover highlight; ordinary rows have no hover reaction. A successful analysis no longer leaves a trade/reconciliation summary in the left-column status area; loading, error, and catalog status messages remain.
- These are presentation/interaction changes only: API payload, evidence, price-proxy labels and execution semantics are unchanged.

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
- Real-data acceptance on cached local inputs was executed 2026-11-16 on the user's machine (see the section above); pairs that fail the working-capital stress screen return a hard 400 and the proof page shows the honest empty state. Everything else is fixture-verified plus this run.
