# Calendar price-intent proof adapter

A trusted `StrategyPlugin` can opt into the generic calendar proof path by declaring
`proof_adapter = "calendar_price_intents.v1"`. It must have a unique `family_id`,
only support `calendar_native`, and support all three price fills (`next_open`,
`next_close`, `reference_same_bar`). It is discovered through the existing strategy
entry-point group or plugin folder. Pair/funding plugins cannot use this adapter.
The reference fill remains an explicitly non-executable same-bar benchmark.

`evaluate_intents` receives a copied `WindowSnapshot` containing the causally
eligible rows of the selected window (or training prefix), the validated parameters
and fill. Each returned unit intent supplies `side`, `entry_date`, `exit_date`,
`entry_price`, `exit_price`, `signal` (`date`, `kind`, numeric `value`) and `reason`.
The adapter checks identity, parameter agreement, event order and delay, unique
entries/exits, non-overlap, bar eligibility, finite values and exact observed fill
proxies. Only `end_of_window` may mark an ineligible final bar; it is not an
executable fill. Existing accounting derives costs, benchmark, margin and funded
sizing from the trusted market rows, not plugin-provided financial figures.
A trade must span at least two bars. Invalid output fails closed.

Opt-in is not certification. Python plugins execute trusted code; event checks do
not prove a signal was computed without future information. Every plugin needs its
own determinism, truncation/causality, compatibility and accounting regression
tests. `moex_spread_lab.strategies.conformance.assert_causal_conformance` is a
reusable test helper: provide a plugin-owned aligned window and chart fixture,
parameters and multiple cutoffs; it tests every declared fill. Core tests retain
fixtures for built-ins and do not automatically certify external plugins.
Discovery rejects duplicate strategy IDs instead of silently replacing a plugin. This is offline research only, not trading authorization. Browser strategy
forms/catalog discovery remain Phase 5 work.
