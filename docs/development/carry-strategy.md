# H2-S1 — Frozen funding-filtered basis strategy

Date: 2026-11-03

## Status

`H2-S1` is an exploratory proxy backtest, not an executable or funded-pilot candidate. The rule is frozen for this run; no parameter search or selection from alternative results was performed.

## Frozen specification

Position: short one `CNYRUBF` contract and long one `CRZ6` contract.

At least 20 common EOD observations are required. On each subsequent observation, using only the previous 20 observations:

- enter when mean received funding is positive and the current `CRZ6 − CNYRUBF` basis is at or below the previous-window median;
- exit when the current basis reaches the previous-window median, or mean received funding is non-positive, or the date is within five calendar days of `CRZ6` expiry;
- execute each signal on the next common EOD settlement observation;
- hold at most one spread and do not roll `CRZ6`;
- do not open or carry a position into the confirmed ten-trading-day perpetual execution window; if that window is observable in the input, exit on the next common observation;
- mark an unclosed position at the last available observation as `open_at_data_end_no_executable_exit`.

This rule is a deliberately simple funding filter plus basis reversion condition. It does not claim that basis convergence is guaranteed. The TOM-versus-CNYFIXME difference remains an explicit residual risk.

## Run result

Run window: `2025-06-17`–`2026-10-27`; 349 common EOD observations; initial virtual capital RUB 30,000; base illustrative transaction-cost scenario.

| Metric | Result |
|---|---:|
| Closed proxy round trips | 14 |
| Position open at data end | 1 |
| Gross carry P&L on held intervals | −RUB 345.29 |
| Illustrative transaction costs | RUB 260.00 |
| Final strategy value proxy | RUB 29,394.71 |
| Matched LQDT value | RUB 37,005.30 |
| Final `X = V_proxy − B` | **−RUB 7,610.59** |

For comparison, applying the current official MOEX FX taker exchange/clearing rate of `0.00462%` to the end-of-window reference prices gives **RUB 32.63** of exchange/clearing transaction costs and `X = −RUB 7,383.21`. Under the user-provided VTB `Мой Онлайн Прайм` tariff, the VTB broker commission for derivatives is zero through 2026-12-31 inclusive, so this current-period sensitivity does not add a VTB broker fee. Historical exchange-fee applicability, post-2026 broker pricing, slippage and account controls remain open.

The matched LQDT path is observed over this window. The strategy marks are EOD settlement proxies under A-08: this project will not obtain historical order-book or actual-fill data. The model therefore represents the user's manual reaction delay and cost sensitivity rather than claiming real fills. The open position is not treated as a completed exit. The input ends on 27.10.2026, before the CRZ6 ten-trading-day execution window; therefore that event is labelled `not_observable_in_input_window` and no assignment payment is manufactured.

## Interpretation

The frozen H2-S1 rule does not pass a promotion gate in this run. Its result is close to the already rejected H1 baseline and remains before verified real costs and execution effects. This is evidence against this specific frozen rule over this window, not a rejection of CNYRUBF/CRZ6 or of every future carry design.

The result must not be combined with the H1 ledger as if both strategies had been traded together. The H2-S1 run has its own proxy path, costs, open-position state and benchmark comparison.

## Reproduce

```bash
uv run python -m moex_spread_lab.cli carry-strategy \
  --carry-study data/analytics/cnyrubf-crz6-carry-study.json \
  --benchmark data/benchmark/lqdt-lineage.json \
  --contracts data/w1/current-intake.json \
  --cost-scenario base \
  --initial-capital 30000 \
  --output data/analytics/cnyrubf-crz6-carry-strategy.json
```

Current VTB-tariff sensitivity:

```bash
uv run python -m moex_spread_lab.cli carry-strategy \
  --carry-study data/analytics/cnyrubf-crz6-carry-study.json \
  --benchmark data/benchmark/lqdt-lineage.json \
  --contracts data/w1/current-intake.json \
  --cost-scenario vtb_online_prime_current \
  --initial-capital 30000 \
  --output data/analytics/cnyrubf-crz6-carry-strategy-moex-current.json
```

The cost scenario is illustrative and unverified. Its funding component is not added because the actual MOEX `SWAPRATE` field is already included in the gross carry decomposition; this does not make exchange, clearing, broker or slippage values verified.

## Next gate

Do not optimize H2-S1. The [point-in-time evidence audit](point-in-time-evidence-audit.md) confirms the two CNYRUBF funding regimes in this window, while assignment/continuation handling, margin and account controls remain open. Historical quote/fill collection is excluded by A-08. Before another strategy hypothesis, improve evidence for expiry/fixing rules, dated costs and account risk controls. Any new hypothesis requires a new frozen rule, separate run, continuous funding/benchmark accounting and the same no-promotion gate.
