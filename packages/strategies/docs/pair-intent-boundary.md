# Pair price-intent boundary (`daily_pair_price_intents.v1` / `pair_price_intents.v1`)

Shared plugin discovery, trust-tier, and `assert_causal_conformance` rules are defined in [calendar-proof-plugins.md](calendar-proof-plugins.md).

## Opt-in & adapter scope

A `StrategyPlugin` opts into the pair price-intent proof path with a unique `family_id`, `supported_universes = {"pair_screen"}`, all three price fills (`next_open`, `next_close`, `reference_same_bar`), and either:

- `proof_adapter = "daily_pair_price_intents.v1"` with `requires_daily_bars = True` (daily only), or
- `proof_adapter = "pair_price_intents.v1"` with `requires_daily_bars = False` (daily and hourly).

Funding-only plugins use `carry_settlement_intents.v1` ([carry-intent-boundary.md](carry-intent-boundary.md)), not this adapter. Proof and search support automatic or fixed-quantity sizing on the declared timeframes. Hourly proof excludes daily `SWAPRATE` rather than allocating it to intraday bars and cannot be interpreted as funding-inclusive P&L.

## `ScreenerInputs` & intent validation

The plugin receives a copied, synchronized positive-volume analysis prefix (`ScreenerInputs`); auxiliary funding, benchmark, carry, and extra snapshot objects are withheld. Each unit intent specifies `side`, entry/exit dates, near/far entry and exit fills, `signal` (`date`, `kind`, numeric `value`), and `reason`. `long_spread` is long near and short far; spread prices are RUB-normalized.

The boundary validates both leg prices against market proxies, executable volume, eligibility, causal event order, and non-overlap, failing closed on violations. Same-bar exit/re-entry is rejected. `end_of_sample` uses the final close as a non-executable mark (including for `next_open`). Plugin-supplied P&L, cost, quantity, and funding claims are discarded.

## Accounting, funding & search

The proof path applies the pair-data, liquidity, capital, and evidence screens. For daily perpetual windows, the near-leg `FundingWindow` must resolve every analysis session through the training cutoff: published zero, estimated rate, and confirmed no-clearing days remain distinct, and unknown rates never become zero. For registry-confirmed dated pairs, funding is not applicable. Funding applies to accepted exposure at entry and terminal marks, not an ordinary exit before clearing; journal-only bars neither revalue positions nor accrue funding.

The funded account sets quantity and computes tariffs, capital, ledger, and summary. Parameter search preserves family ordering, deterministic seed derivation, and serial/multi-worker reproducibility across cohorts.
