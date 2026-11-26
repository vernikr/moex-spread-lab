# Carry settlement-intent boundary (`carry_settlement_intents.v1`)

Shared plugin discovery, trust-tier, and `assert_causal_conformance` rules are defined in [calendar-proof-plugins.md](calendar-proof-plugins.md).

## Opt-in & adapter scope

A `StrategyPlugin` opts into the funding-carry proof path by declaring:

- `proof_adapter = "carry_settlement_intents.v1"` and a unique `family_id`
- `supported_universes = {"pair_screen"}`
- `same_day_settlement` fill and daily bars (`requires_daily_bars = True`; hourly intervals reject carry plugins)
- a non-empty numeric parameter schema whose fields are all tunable

## `ProofFunding` & intent validation

The plugin receives a copied, evidence-resolved carry-study prefix (`ProofFunding`) and returns a `StrategyIntentRun` with short-spread direction, entry/exit dates, entry signal date, exit reason, `open_at_end` flag, and optional descriptive `entry_reason`. Input mutations in compatibility or intent callbacks do not alter the trusted study. All plugin-supplied prices, rates, costs, and P&L are discarded.

The boundary independently enforces event chronology, non-overlap, executable volumes, native expiry/assignment limits, funding coverage, and evidence cutoff. A terminal mark may lack executable volume but must precede the applicable cutoff. Authors must ensure funding rates are consumed only when decision-available—an inferred midpoint is never decision-available before its right-hand clearing.

## Accounting & settlement semantics

Existing pair screens, fixed or funded-account sizing, tariffs, matched LQDT benchmark, provenance, and reconciliation remain authoritative. Ordinary settlement exits receive the price move but not exit-day funding; a terminal open mark receives its final clearing and pays no exit fee. Output remains an offline settlement-price proxy, not an executable fill or trading authorization.
