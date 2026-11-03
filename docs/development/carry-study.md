# H2 — Funding-aware CNY/RUB carry decomposition

Date: 2026-11-03

## Purpose

This is a separate hypothesis from the price-only H1 mean-reversion baseline. It treats the candidate as:

> short `CNYRUBF` (perpetual) + long `CRZ6` (dated CNY future), 1:1 in 1,000 CNY lots.

The economic decomposition uses MOEX EOD settlement history and the `SWAPRATE` field. A positive CNYRUBF funding value is treated as received by the short perpetual leg, following the received R06 specification analysis. The study uses the current contract snapshot for unit conversion and therefore does not claim point-in-time historical specification completeness.

This is **not yet a strategy backtest**: it has no entry rule, no delayed execution, no bid/ask fills, no verified costs, no fixing-vs-TOM adjustment and no historical margin series.

## EOD result

The read-only MOEX history endpoint returned 349 common settlement dates for `2025-06-17`–`2026-10-27`:

| Component | RUB |
|---|---:|
| Basis P&L for short perpetual / long dated leg | −2,737.00 |
| CNYRUBF funding received by the short leg | +1,863.26 |
| Gross decomposition total | **−873.74** |

The basis component is calculated from daily settlement changes using the current RUB 1 tick conversion. Funding starts after the first observation, so the first row is an initial mark rather than a holding-day cash flow.

The result is a gross decomposition, not an investable return. It does not subtract exchange, clearing, broker, slippage, exit, financing or fixing-basis costs.

## Important limitations

1. `CNYRUBF` follows CNYRUB_TOM while `CRZ6` settles against CNYFIXME, so the two legs have a residual fixing-vs-TOM risk at expiry.
2. The study spans the documented `2026-01-19` funding-parameter change and the `2026-03-23` unified-session change. A production-quality historical carry study needs effective-dated rule segments.
3. Current tick value and initial margin are used as labelled fallback assumptions.
4. The history endpoint provides EOD settlement and funding fields, not historical executable bid/ask or fills.
5. `CRZ6` is one fixed dated contract; the study does not roll into `CRH7` and therefore is not a recurring calendar strategy.

## Reproduce

```bash
uv run python -m moex_spread_lab.cli carry-study \
  --near CNYRUBF \
  --far CRZ6 \
  --from-date 2025-06-17 \
  --till-date 2026-10-27 \
  --contracts data/w1/current-intake.json \
  --cache-dir data/carry-history-cache \
  --output data/analytics/cnyrubf-crz6-carry-study.json
```

## Next H2 gate

Before turning this decomposition into a strategy test, define and freeze:

- entry rule based on basis versus expected remaining funding and fixing risk;
- exit, expiry buffer and no-roll rule;
- point-in-time funding/specification segments;
- verified or explicitly scenario-based costs;
- delayed execution and non-execution paths;
- matched LQDT ledger and chronological validation.

H2 remains `exploratory_inconclusive`; its gross decomposition does not promote the pair or authorize a pilot.
