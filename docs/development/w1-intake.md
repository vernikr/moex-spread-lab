# W1 — MOEX ISS catalog and price-role intake

Date: 2026-10-27

## Delivered

`src/moex_spread_lab/iss.py` adds a read-only MOEX ISS adapter for the futures RFUD board. It:

- requests an explicit catalog and market-data field list;
- preserves the request URL, UTC receipt time and raw-response SHA-256;
- creates a stable identity from board, SECID and expiry;
- keeps settlement, current last trade, bid, ask and unavailable analytical/close/live-mark roles separate;
- records current-snapshot status instead of treating the response as historical evidence;
- preserves malformed market quality rows with a `crossed_quote` or `missing_quote` flag;
- returns an explicit candidate admission decision.

## Local probe

Run from the repository root:

```bash
uv run python -m moex_spread_lab.cli iss-intake \
  --near CNYRUBF \
  --far CRZ6 \
  --output data/w1/current-intake.json
```

The output is intentionally under ignored `data/`. It contains current market observations and must not be committed or redistributed.

## Observed outcome

The read-only probe returned a current RFUD catalog and found both configured CNY/RUB instruments. The candidate is **`not_admitted`** because:

1. a current catalog snapshot is not point-in-time historical evidence;
2. the response does not provide historical executable bid/ask or an analytical close for the candidate;
3. the applicable ISS automated-use and storage rights remain unresolved.

The full catalog is retained in the local report only. A crossed quote is preserved as a quality issue and does not invalidate unrelated instruments.

## Boundary

This adapter does not collect history, reconstruct fills, infer a live mark, authorize ISS storage/automation, connect a broker or admit a strategy. The next data step is a rights-cleared, dated daily-candle intake for both legs with explicit coverage and contract-vintage checks.
