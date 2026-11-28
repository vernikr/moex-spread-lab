# LQDT benchmark lineage

The LQDT benchmark has two explicit layers (`A-07`):

```text
observed:        VTBM -> LQDT
extended proxy:  CBR_RUONIA -> FXMM -> RUSFAR -> VTBM -> LQDT
```

- `VTBM` and `LQDT` are the observed benchmark layer; only this segment supports a clean actual-fund benchmark conclusion.
- `FXMM` is an observed exchange price used only as an explicitly labelled predecessor proxy. `RUSFAR` and `CBR_RUONIA` are rate series compounded into cumulative price proxies (ACT/365, base 1000); no proxy period is an observed LQDT price.
- Stitching follows the reference methodology: correct the VTBM internal 1000:1 split, compute returns, prefer the newest available source on overlapping dates and reconstruct the historical level backwards. VTBM breaks are adjusted by the documented 1000:1 factor, preserving the split-day return; a detected break inconsistent with that factor fails closed. Each selected source interval is applied once; dates strictly inside that interval are omitted and listed in `omitted_interior_dates`, not estimated. Disconnected intervals or a skipped higher-ranked observation fail closed. Consumers may apply their own explicit prior-mark policy to missing dates; such a mark is stale, not an observation on that date. Missing source rows stay missing; no rate or price is invented. The anchor selection's limitation is tracked in #306.

## Sources

- `FXMM`, `VTBM`, `LQDT` and `RUSFAR` come from MOEX ISS history and reference endpoints; each output row keeps its source ticker, source kind, board provenance and response hashes.
- `CBR_RUONIA` comes from the CBR RUONIA HTML table, requested in bounded yearly chunks; when a response contains no rows, the current XLSX export endpoint (`posted=False`, `backUrl=/hd_base/ruonia/dynamics/`) is used and parsed with `openpyxl`. The method used is recorded per chunk; if both responses are unavailable or unparseable the run stays `exploratory_proxy_only` and reports `CBR_RUONIA` as missing.

## Run

```bash
uv run python -m moex_spread_lab.cli benchmark-lineage \
  --from-date 2010-01-01 \
  --till-date <session date> \
  --output data/benchmark/lqdt-lineage.json \
  --cache-dir data/benchmark-cache
```

The output (`lqdt-lineage.v1`) is written under git-ignored `data/` and records the stitched series, the method used per chunk and the source hashes.
