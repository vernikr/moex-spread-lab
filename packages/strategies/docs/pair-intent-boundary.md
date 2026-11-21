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
