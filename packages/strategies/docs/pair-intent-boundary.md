# Pair price proof plugins

A trusted installed `StrategyPlugin` may opt in with a unique `family_id`, exactly
`{"pair_screen"}`, and all three price fills. Declare
`proof_adapter = "daily_pair_price_intents.v1"` and `requires_daily_bars = True`
for daily only, or `proof_adapter = "pair_price_intents.v1"` and
`requires_daily_bars = False` for daily and hourly.
Discovery alone does not enable an adapter. Proof/search supports daily and hourly bars where declared, with automatic or
fixed-quantity sizing. Funding-only plugins are not supported by this contract. Hourly proof deliberately
excludes daily SWAPRATE rather than assigning it to individual bars, matching the
existing hourly proof; it cannot be interpreted as funding-inclusive P&L. Plugin
code is trusted, not sandboxed, and conformance tests cannot prove causality.

The plugin receives a copy of the synchronized positive-volume analysis prefix;
auxiliary funding, benchmark, carry and extra snapshot objects are withheld.
Each unit intent specifies `side`, entry/exit dates, near/far entry and exit
fills, a `signal` mapping (`date`, `kind`, numeric `value`), and `reason`.
`long_spread` is long near and short far; spread prices are RUB-normalized.
The boundary checks both leg prices against market proxies, volume, eligibility,
causal event order and overlap. Plugin-supplied P&L, cost, quantity and funding
claims are ignored. `end_of_sample` uses the final close as a non-executable mark,
including for `next_open`. Same-bar exit/re-entry is currently rejected.

The proof route uses the existing pair-data, liquidity, capital and evidence
screens. For daily perpetual windows, the near-leg `FundingWindow` must resolve every analysis
session through the applicable training cutoff. A published zero, estimated rate
and confirmed no-clearing day remain distinct. Unknown rates never become zero;
for registry-confirmed dated pairs funding is not applicable. Funding applies to
accepted exposure at entry and terminal marks, not an ordinary exit before
clearing. Journal-only bars do not revalue positions or generate funding. The
funded account sets quantity and computes tariffs, capital, ledger and summary;
search retains the same family order, seed derivation and 80-trial budget.

The drop-in integration test compares trades, ledger and holdout evaluation with
the native mean-reversion proof for all fills, fixed and automatic sizing, and
training cutoffs, both sizing modes and both timeframes; it exercises search and verifies identical run IDs and parameters
across serial and two-worker three-window cohorts. Generic funding-carry plugins remain unsupported. The pair catalog and web UI are not
yet dynamically extended by this adapter; API proof and search requests can use
its registered strategy ID.
