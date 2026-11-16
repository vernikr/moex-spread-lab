# Unified execution proof (`/proof`)

Status: **slice 1 of 3 delivered** (payload + adapters + tests, 2026-11-16). Slice 2 (Lightweight Charts UI rewrite + unified table) and slice 3 (deep links, CSV export, decisions/README polish) are pending. Design: `execution-proof-screen-design.md` (workspace artifact, not committed).

## What slice 1 adds

- `proof.py` (`ProofService`): single entry point for `/api/proof/*`, dispatched by `universe` — `calendar_native` (legacy, compatibility preserved) or `pair_screen` (new).
- Pair adapter reproduces both screener strategies with fill-level detail:
  - `mean_reversion`: fills at the next daily open (candle-open proxy); z-score at the signal bar; itemized fees (`exchange_clearing` / `broker` = 0 / `slippage`) per side from the published tariff.
  - `funding_only`: same-day settlement fill proxy; funding received tracked per day (`daily_funding_included_rub`); exit-day funding correctly excluded; open position at data end flagged `open_at_end`.
- Payload `execution-proof.v1` (superset of `calendar-proof.v1`; calendar keys preserved): unified `series` / `trades` / `ledger` / `summary` with `cost_totals`, per-side `entry_costs` / `exit_costs`, day volumes (`near_volume` / `far_volume`; min-leg rule flag), `max_excess_drawdown_rub`, `ledger_reconciliation_rub` (must be 0.00), per-trade `lqdt_on_margin_rub` and `excess_rub`.
- `/proof` page unchanged for now (slice 2 rewrites it on Lightweight Charts). `/vendor/lightweight-charts.standalone.production.js` is served same-origin (vendored, Apache-2.0, see `vendor/README.md`); CSP unchanged, no CDN.
- Engines extended **additively** (`analytics.py` trade fill/cost fields, `screener.py` candle volumes, `carry.py` volumes/basis/funding components); no metric semantics changed.

## Reconciliation contract (asserted in tests)

- `Σ ledger.costs == Σ trade entry+exit costs` (±0.02 ₽ rounding).
- `Σ ledger.mark_pnl == Σ trade gross` (telescoping; prices for the pair book are RUB-normalized `near*mv − far*mv`, constant multipliers — no FX drift on this universe).
- `last(ledger.cumulative_net) == Σ trade net` (±0.02 ₽); `ledger_reconciliation_rub == 0.0`.
- `cost_totals` equals the sum of per-side component dictionaries; broker component always 0.0 (VTB zero fee to 2026-12-31).
- Same inputs → identical `run_id`.

## Known gaps (slice 3)

- Calendar provenance hashes are not surfaced (`provenance: {}`); pair provenance is complete (near/far candles or history, benchmark, contracts).
- `/api/proof/run` without `universe` defaults to `calendar_native` (backward compatibility).
- Per-day LQDT-on-margin is not shown in the ledger (per-trade hurdle only, same semantics as the calendar proof).

## Gate notes

- Pair proof applies the same admissions as `/api/run`: unknown pair, missing local inputs, or a failed working-capital stress screen are hard errors (400), not degraded output.
- Prices remain ISS candle/settlement **proxies**; nothing here is executable fills or a broker/account reconciliation.
