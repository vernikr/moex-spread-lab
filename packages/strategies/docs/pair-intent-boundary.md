# Pair price-intent validation boundary (not wired to proof)

`pair_screener.intents.pair_price_intent_trades` validates a trusted plugin's
unit-sized price intents against an already synchronized, positive-volume pair
window. This is a preparation step, **not** a proof accounting adapter and not a
registration mechanism: new pair/funding plugins remain excluded from proof search.

The pair convention is near minus far in RUB; `long_spread` means long near and
short far. Each intent declares the side, `entry_date`, `exit_date`, a signal with
`date`, `kind`, numeric `value`, both near/far fills at entry and exit, and `reason`.
The boundary checks chronology, signal delay, nonoverlap, volumes, eligibility and
both legs' prices. It derives spread fills from the market prices and contract
point values. It isolates mutable plugin inputs and ignores plugin P&L, cost and
funding claims. For `end_of_sample`, the final close is a non-executable mark,
including under `next_open`; it is not an actual exit.

Integration still requires proven handling of settlement-time funding, missing
funding evidence, fixed-quantity and automatic sizing, tariffs, train/holdout
cutoffs, event and ledger reconciliation, all fill modes, daily and intraday data,
and worker-process determinism. Do not enable plugins in the registry merely
because this boundary passes. The tests compare the boundary with legacy pair
mean-reversion intent output and explicitly reject malformed trades; they do not
certify arbitrary strategy logic or prevent hidden lookahead in trusted code.

## Funding replay preparation

`proof_runtime.pair_funding_replay.replay_pair_funding` is an isolated internal
primitive, not wired to proof. It reproduces baseline ordering: funding includes
entry bars and terminal marks, but excludes executable exit bars. Missing rates
while a position is held produce `None`, not zero. The caller must independently
resolve clearing evidence and map it to unique exact bar keys; daily rates are
not implicitly projected to intraday bars. The primitive rejects same-bar
exit/re-entry (even though the price boundary currently permits it), pending an
explicit cross-engine event-order contract. It does not calculate P&L, tariffs,
capital or summary metrics. Parity tests cover long/short, open/close fills,
quantity scaling and normal versus terminal exit against the existing baseline.

`replay_daily_pair_funding` resolves the existing `FundingWindow` before replay.
It requires an exact match to daily analysis keys, honors prefix and training
cutoffs, rejects unresolved evidence and carries the existing provenance. Published
zero, an interpolated rate, and a confirmed no-clearing day remain distinguishable;
only the last explicitly has no payment without having a numerical rate. Hourly
bars are rejected: resolving the exchange clearing-to-bar mapping remains open.
The resulting daily funding rows are not yet connected to ledger or sizing.

The experimental `attach_daily_unit_funding` prepares unit-quantity funding
fields for `size_proof` without mutating source rows. It accepts only the exact
eligible daily window, never journal-only extras. Tests exercise the existing
sizing engine with accepted and unaffordable intents, both directions and terminal
versus executable exit; funding scales only to accepted quantities. This is an
isolated integration test, not end-to-end plugin accounting or registry support.

The internal `proof_runtime.pair_plugin_accounting.size_daily_pair_plugin` composes the price boundary,
daily funding evidence, unit-funding preparation and existing funded sizing. For
both sides, all three price fills and both terminal/ordinary exits it compares
trade gross, funding, costs, net, daily funding and final account value with the
legacy mean-reversion engine at the accepted quantity. It also checks that plugin
claims about economic results do not affect the output or run ID. This internal
composition does not expose a new public proof route, resolve intraday funding,
cover journal-only rows, perform proof catalog/volume/capital screens, support
fixed-size requests or establish arbitrary plugin causality. It requires explicit
daily perpetual funding evidence and cannot infer zero funding for dated pairs.
