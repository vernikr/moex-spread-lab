# W4/W5 analytical execution ledger

Date: 2026-11-03

## Purpose

The first external-data ledger makes the execution boundary explicit. It consumes an `analytical-baseline.v1` report and produces an append-only `execution-ledger.v1` report with:

- signal and simulated decision timestamps;
- one execution attempt for every baseline entry and exit;
- explicit non-execution reason codes;
- proxy positions, margin reservation, cash and variation-margin fields;
- matched `V`, `B`, `X`, `H` and `D` projections (`V_proxy` is explicitly separated from executable wealth);
- deterministic replay from the event stream.

## Safety boundary

The current candle inputs contain no historical executable bid/ask or fills. Therefore every external-data execution attempt is recorded as `unresolved_no_executable_quote` with reason `historical_bid_ask_unavailable`. The candle-close valuation remains available as a labelled analytical proxy, but it is not converted into a fill and does not make `V` an executable account value.

The report consequently exposes two separate states:

- `strategy_value_proxy`, which reproduces the analytical baseline;
- `executable_value_available=false`, because a defensible historical execution path is absent.

No partial fill is invented, no leg is silently balanced, and no broker or account action is performed.

## Reproduce

```bash
uv run python -m moex_spread_lab.cli execution-ledger \
  --baseline data/analytics/cnyrubf-crz6-1d-baseline.json \
  --output data/analytics/cnyrubf-crz6-1d-ledger.json
```

The generated JSON is ignored under `data/`. The 2026-11-03 daily replay produced 22 unresolved attempts (11 entries and 11 exits), 344 portfolio marks and final proxy `X = −RUB 7,572.30`, matching the baseline. Actual account reconciliation remains unresolved.

The module is pinned in `config/run-manifest.json` through `config/modules/execution-ledger.json`; the analytical baseline module is pinned alongside it.

## Gate status

| Gate | Status | Meaning |
|---|---|---|
| W4 delayed attempt lifecycle | partial | timestamps and attempts are recorded; executable revalidation cannot pass without quotes |
| W4 non-execution handling | passed | missing quote evidence is explicit and fail-closed |
| W5 event replay | passed | event stream rebuilds the same proxy projection |
| W5 matched proxy ledger | passed | `V/B/X/H/D`, margin reservation and funding fields are present |
| W5 actual account reconciliation | blocked | account statements, collateral and executable fills are unavailable |

The release state is `exploratory_inconclusive`; this artifact does not authorize a broker connection, funding or trading.
