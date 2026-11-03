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

- exchange, clearing, broker, slippage, funding and exit costs are unresolved and are not silently set to zero;
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

The detailed ignored local report is `data/analytics/cnyrubf-crz6-1d-baseline.json`.

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
