# Strategy plugins & `calendar_price_intents.v1`

## Shared plugin discovery, trust, and causality contract

Strategy plugins are discovered through the `moex_spread_lab.strategies` entry-point group or the 1-folder plugin layout; discovery rejects duplicate strategy IDs instead of silently replacing a plugin. Discovery alone does not enable a proof adapter—each `StrategyPlugin` must explicitly opt in by declaring a supported `proof_adapter` and a unique `family_id`.

Installed plugins execute trusted Python code (not sandboxed) and can inspect the supplied input prefix. Event-boundary validation cannot prove that a signal was computed without future information, so opt-in is not certification. Every plugin requires its own determinism, causal truncation, compatibility, and accounting regression tests. Use `moex_spread_lab.strategies.conformance.assert_causal_conformance` with a plugin-owned aligned window/chart fixture, parameters, and multiple cutoffs across all declared fills. All proof outputs are offline research proxies, not executable fills or trading authorization.

## `calendar_price_intents.v1`

A `StrategyPlugin` opts into the calendar proof path by declaring:

- `proof_adapter = "calendar_price_intents.v1"`
- `supported_universes = {"calendar_native"}`
- all three price fills (`next_open`, `next_close`, `reference_same_bar`; `reference_same_bar` is an explicitly non-executable same-bar benchmark)

Pair and funding-carry plugins cannot use this adapter.

`evaluate_intents` receives a copied `WindowSnapshot` containing the causally eligible rows of the selected window (or training prefix), validated parameters, and fill. Each returned unit intent supplies `side`, `entry_date`, `exit_date`, `entry_price`, `exit_price`, `signal` (`date`, `kind`, numeric `value`), and `reason`.

The boundary validates identity, parameter agreement, causal event order and delay, unique entries/exits, non-overlap, minimum two-bar trade span, bar eligibility, finite values, and exact observed fill proxies, failing closed on any violation. Only `end_of_window` may mark an ineligible final bar (non-executable). Accounting independently derives costs, benchmark, margin, and funded sizing from trusted market rows; plugin-supplied financial figures are ignored.
