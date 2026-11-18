# W7 — Chronological validation report

Date: 2026-11-03

**Historical reproduction only.** This fixed experiment and its `validate-analytics` CLI retain their original protocol. The current interactive screener uses [revised BR-10](../stages/04-requirements.md); it does not inherit this split.

## Method

The fixed daily CNYRUBF/CRZ6 baseline was evaluated without parameter selection. The 344-row overlap was split chronologically into:

- train: 206 rows, through `2026-04-14`;
- validation: 69 rows, `2026-04-15`–`2026-07-22`;
- sealed confirmation: 69 rows, `2026-07-23`–`2026-10-27`.

The sealed period was not used to tune parameters. H1/M10 delay runs and low/base/stress cost branches were supplied as sensitivity inputs. The sign-flip branch reverses gross strategy value changes and is a falsification diagnostic, not a p-value or probability estimate.

## Result

| Segment | Rows | Final `X`, RUB | Change in `X`, RUB | Max `D`, RUB |
|---|---:|---:|---:|---:|
| Train | 206 | −4,835.81 | −4,835.81 | 4,835.81 |
| Validation | 69 | −6,177.17 | −1,327.38 | 6,177.17 |
| Sealed confirmation | 69 | −7,572.30 | −1,362.91 | 7,572.30 |

The observed baseline final `X` is −RUB 7,572.30. The sign-flip diagnostic final `X` is −RUB 6,438.30, so the reversed branch outperformed the observed branch in this diagnostic. This does not establish statistical significance; it is a warning against treating the observed path as evidence of edge.

The 11 baseline trades are not dominated by one trade: the largest absolute gross trade contributes 29.3% of total absolute gross trade P&L. This concentration check is descriptive only and does not compensate for missing execution evidence.

## Gate result

The report verdict is **`inconclusive_no_pass`**. Chronological split, sealed confirmation, sign-flip diagnostic and delay/cost sensitivity gates are present. The following blockers prevent a PASS:

1. historical executable quotes and fills are unavailable;
2. trading and funding costs are unknown or only illustrative;
3. dated historical contract terms are not evidenced;
4. the input execution status is not historical bid/ask available;
5. actual account, collateral and LQDT attribution reconciliation is unavailable.

The result is not a live-trading recommendation and does not authorize funding, broker access or deployment.

The resulting promotion decision is recorded in the [Stage-9 research gate](research-gate.md): do not promote this baseline configuration to an executable or funded pilot. The pair itself remains conditionally admitted for offline research; this run does not test or reject every other strategy hypothesis.

## Reproduce

```bash
uv run python -m moex_spread_lab.cli validate-analytics \
  --baseline data/analytics/cnyrubf-crz6-1d-baseline.json \
  --ledger data/analytics/cnyrubf-crz6-1d-ledger.json \
  --sensitivity data/analytics/cnyrubf-crz6-1h-delay1.json \
  --sensitivity data/analytics/cnyrubf-crz6-1h-delay3.json \
  --sensitivity data/analytics/cnyrubf-crz6-10m-delay1.json \
  --sensitivity data/analytics/cnyrubf-crz6-10m-delay3.json \
  --sensitivity data/analytics/cnyrubf-crz6-1d-cost-base.json \
  --sensitivity data/analytics/cnyrubf-crz6-1h-cost-base.json \
  --sensitivity data/analytics/cnyrubf-crz6-10m-cost-base.json \
  --output data/analytics/cnyrubf-crz6-w7-validation.json
```

The detailed report is ignored under `data/`; this document records its interpretation and gate status.
