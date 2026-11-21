# VTB EBS/KPUR account-risk screen

Date: 2026-11-03

## Purpose

This is the completed offline screen for the minimum one-contract CNYRUBF/CRZ6 position under the user-confirmed VTB EBS + КПУР profile. It uses the current ISS margin snapshot and deliberately conservative working buffers. It is not a client-specific broker calculation, a safety guarantee or a live-trading approval.

## Inputs

| Input | Value | Status |
|---|---:|---|
| Initial pilot envelope | RUB 30,000 | Project policy |
| CNYRUBF current initial margin | RUB 970.82 | ISS snapshot, 2026-10-27 |
| CRZ6 current initial margin | RUB 1,153.64 | ISS snapshot, 2026-10-27 |
| Pair margin used | RUB 2,124.46 | Sum of legs; no margin offset credited |
| Observed worst one-day gross carry loss | RUB 450.38 | EOD proxy history, not an intraday tail |
| Variation-margin buffer | RUB 5,000 | R07 working proposal / pilot policy |
| Uncertainty buffer | RUB 5,000 | R07 working proposal / pilot policy |
| Key rate for financing sensitivity | 14% | Dated R02 input; not a current-rate claim |

The screen excludes core LQDT from available strategy cash. It tests cash and margin against the RUB 30,000 envelope and records the 8% КПУР LQDT discount only as collateral context.

## Results

| Scenario | Margin | Total required | Remaining envelope | Result |
|---|---:|---:|---:|---|
| Current snapshot, no offset | RUB 2,124.46 | RUB 12,124.46 | RUB 17,875.54 | Passes screen |
| One-leg failure | RUB 1,153.64 | RUB 11,153.64 | RUB 18,846.36 | Passes screen |
| 2× margin shock | RUB 4,248.92 | RUB 14,248.92 | RUB 15,751.08 | Passes screen |
| 3× margin shock | RUB 6,373.38 | RUB 16,373.38 | RUB 13,626.62 | Passes screen |

All four working scenarios pass the offline arithmetic screen. This does not establish that the broker will apply the ISS values, retain a pair offset, accept the contracts, or leave the core LQDT untouched.

## EBS under-covered-margin sensitivity

The published EBS formula is kept separate from ordinary transaction costs:

`max(ГО − RubCash, 0) × (key rate + 2%) × days / 365`

For the RUB 2,124.46 pair margin and one day:

- with RUB 30,000 cash: **RUB 0**;
- with zero RUB cash: **RUB 0.93**.

These are sensitivities only. The actual client calculation, valuation moment, cash classification and debit timing remain unverified.

## Perpetual-futures event

The model now carries an explicit event record for the MOEX perpetual-to-quarterly execution mechanism:

- event: `perpetual_to_quarterly_execution_window`;
- policy: exit before the window;
- published payment rate: 3%;
- payment added to H2-S1: no;
- status for the current input: `not_observable_in_input_window`.

The available history ends 2026-10-27, while CRZ6 expires 2026-12-17. No assignment payment or invented exit is added.

## Reproduction

```bash
uv run python -m moex_spread_lab.cli account-risk-check \
  --near CNYRUBF \
  --far CRZ6 \
  --contracts data/w1/current-intake.json \
  --carry-study data/analytics/cnyrubf-crz6-carry-study.json \
  --initial-capital 30000 \
  --output data/analytics/cnyrubf-crz6-account-risk.json
```

The same account-risk object is embedded in the H2-S1 strategy output. H2-S1 was rerun over its complete original window without resetting the benchmark-relative accounting or changing the frozen rule:

- 14 closed proxy round trips;
- one position open at data end;
- gross P&L: **−RUB 345.29**;
- current MOEX/VTB cost sensitivity: **RUB 32.63**;
- final `X`: **−RUB 7,383.21**.

## Conclusion

The minimum pair passes the current offline working screen, including 3× margin shock, but this is only a feasibility result. H2-S1 remains an exploratory, non-promoted candle-close proxy result. Individual VTB coefficients, historical ГО, client recourse, actual account reconciliation and executable prices remain outside the project scope or unverified.
