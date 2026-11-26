# First analytical baseline

Date: 2026-11-03

## Scope

This is the first external-data analytical baseline for the conditionally admitted `CNYRUBF/CRZ6` candidate. It uses synchronized daily ISS candles from `2025-06-17` through `2026-10-27` and the flow-neutralized LQDT lineage benchmark. Candle close remains the A-06 analytical price proxy; it is not a historical fill.

The fixed mean-reversion control is deliberately simple and predeclared for this run:

- spread: `CNYRUBF - CRZ6`;
- one spread pair maximum;
- 20-observation rolling lookback;
- entry at `|z| >= 2.0`;
- exit at `|z| <= 0.5`;
- signal at one daily close and execution proxy at the next daily close;
- initial virtual envelope: RUB 30,000;
- one contract per leg;
- no emergency funding and no scale-in.

The economic conversion uses the current ISS contract snapshot as the explicitly labelled fallback: both legs have a `0.001` tick, `RUB 1` tick value and `1,000` CNY lot. One spread tick is therefore RUB 1 per pair. The combined current initial-margin snapshot is RUB 2,124.46 for one pair.

## Result

| Metric | Result |
|---|---:|
| Common daily rows | 344 |
| Virtual round trips | 11 |
| Gross futures P&L | −RUB 567 |
| Final strategy value | RUB 29,433 |
| Final matched LQDT value | RUB 37,005.30 |
| Final `X = V − B` | **−RUB 7,572.30** |
| Maximum `D` | RUB 7,572.30 |

The benchmark rows used in this overlap are all `observed` LQDT rows. The extended RUONIA/FXMM/RUSFAR proxy is available for older research but is not needed for this pair's current common window.

## Interpretation boundary

The result is `exploratory_inconclusive`, not a profitability conclusion:

- exchange, clearing, broker, slippage, funding and exit costs are explicit scenarios; the user-confirmed VTB broker fee is zero through 2026-12-31, while other components remain separate and dated;
- fills are next-close analytical proxies, not historical bid/ask executions;
- current contract terms stand in for dated historical specifications;
- the benchmark is a price-path analytical proxy, not an account statement reconciliation;
- no live broker access, order submission or trading occurred.

Run reproducibly from the repository root:

```bash
uv run python -m moex_spread_lab.cli analytical-baseline \
  --near-candles data/w1/candles/CNYRUBF_1d.json \
  --far-candles data/w1/candles/CRZ6_1d.json \
  --benchmark data/benchmark/lqdt-lineage.json \
  --contracts data/w1/current-intake.json \
  --output data/analytics/cnyrubf-crz6-1d-baseline.json
```

### Output format

The command writes a single `analytical-baseline.v1` JSON report: compact result identity, parameters, economics, costs, summary, plateau/rejection facts and input provenance, with the daily marks and trades embedded in the same file. `execution-ledger` and `validate-analytics` consume this JSON path directly, so the report is the complete reproducible baseline artifact.

## Intraday delay sensitivity

The same fixed parameters were replayed on the synchronized H1 and M10 close proxies. The delay is measured in bars; no same-bar fill is used.

| Timeframe | Delay | Marked rows | Trades | Gross P&L, RUB | Final `X`, RUB |
|---|---:|---:|---:|---:|---:|
| H1 | 1 bar | 4,386 | 228 | −989 | −8,018 |
| H1 | 3 bars | 4,386 | 228 | −469 | −7,498 |
| M10 | 1 bar | 19,060 | 1,110 | +1,014 | −6,015 |
| M10 | 3 bars | 19,060 | 1,110 | −545 | −7,574 |

For intraday rows, the matched LQDT mark uses the previous available daily close within the day and the same-date daily close only on the last common bar of that day. This avoids repeating a future end-of-day value across earlier bars. The resulting intraday `X` is still analytical because the benchmark does not have intraday account marks.

These runs are delay diagnostics, not a parameter selection. The high turnover makes unresolved exchange, clearing, broker, slippage and exit costs especially material; no net advantage is claimed.

## Illustrative cost sensitivity

The runner now supports complete profiles with separate components. The values below are deliberately **illustrative and unverified**; they are not the user's tariff and are not historical execution evidence. Units are per leg and side unless stated otherwise:

| Scenario | Exchange | Clearing | Broker | Slippage | Funding | Exit |
|---|---:|---:|---:|---:|---:|---:|
| low | RUB 0.50 | RUB 0.50 | RUB 0.50 | 0.5 tick | RUB 0.25/leg-day | RUB 0.50 |
| base | RUB 1.00 | RUB 1.00 | RUB 1.00 | 1.0 tick | RUB 0.50/leg-day | RUB 1.00 |
| stress | RUB 2.00 | RUB 2.00 | RUB 2.00 | 2.0 ticks | RUB 1.00/leg-day | RUB 2.00 |

The current VTB tariff document establishes the broker-fee component through 2026-12-31. The separate [VTB account risk profile](../../accounting/docs/vtb-account-risk-profile.md) records the public margin, collateral and liquidation mechanics plus labelled working assumptions for EBS and КПУР; individual coefficients, offsets and account reconciliation remain outside this baseline. Exchange, slippage, funding and exit profiles remain sensitivity branches.

| Timeframe | Scenario | Trades | Gross P&L, RUB | Modeled costs, RUB | Final `X`, RUB |
|---|---|---:|---:|---:|---:|
| 1d | low | 11 | −567 | 232.50 | −7,804.80 |
| 1d | base | 11 | −567 | 465.00 | −8,037.30 |
| 1d | stress | 11 | −567 | 930.00 | −8,502.30 |
| H1 | low | 228 | −989 | 2,193.50 | −10,211.51 |
| H1 | base | 228 | −989 | 4,387.00 | −12,405.01 |
| H1 | stress | 228 | −989 | 8,774.00 | −16,792.01 |
| M10 | low | 1,110 | +1,014 | 10,580.00 | −16,595.01 |
| M10 | base | 1,110 | +1,014 | 21,160.00 | −27,175.01 |
| M10 | stress | 1,110 | +1,014 | 42,320.00 | −48,335.01 |

All cost runs use delay 1 bar. The M10 gross positive result is eliminated by even the low illustrative cost branch. This is a scenario result, not evidence that the real tariff equals any listed value.

Reproduce a branch by adding `--cost-scenario low`, `--cost-scenario base` or `--cost-scenario stress` to the baseline command. A custom `cost-profile.v1` JSON can be supplied with `--cost-profile`.

## M1 diagnostic

M1 was replayed only to inspect turnover and delay sensitivity, with `analysis_role=diagnostic_only`. It is not a primary strategy result and was not used to select a timeframe or parameters:

| Delay | Common minute rows | Virtual round trips | Gross P&L, RUB | Final `X`, RUB |
|---:|---:|---:|---:|---:|
| 1 bar | 107,364 | 6,835 | +4,635 | −2,394.01 |
| 3 bars | 107,364 | 6,835 | +1,519 | −5,510.01 |

Costs remain unknown in this diagnostic. With 6,835 round trips, any executable conclusion would require validated quotes, per-leg fill outcomes and effective costs; the M1 result therefore remains diagnostic/inconclusive.
