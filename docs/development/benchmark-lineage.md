# LQDT benchmark lineage

The benchmark has two explicit layers:

```text
observed: VTBM -> LQDT
extended proxy: CBR_RUONIA -> FXMM -> RUSFAR -> VTBM -> LQDT
```

`VTBM` and `LQDT` are the observed benchmark layer. `FXMM` is an observed exchange price used only as an explicitly labelled predecessor proxy; `RUSFAR` and `CBR_RUONIA` are rate series converted into cumulative-price proxies. None of the proxy periods is an observed LQDT price. The return-stitching method follows the inspected reference project: correct VTBM's internal 1000:1 split, calculate daily returns, prefer the newest available source on overlapping dates and reconstruct the historical level backwards from the latest observed LQDT price.

Run:

```bash
uv run python -m moex_spread_lab.cli benchmark-lineage \
  --from-date 2010-01-01 \
  --till-date 2026-10-27 \
  --output data/benchmark/lqdt-lineage.json \
  --cache-dir data/benchmark-cache
```

The CBR adapter follows the reference project's recovery path: request the RUONIA HTML table in bounded yearly chunks, then use the current XLSX export endpoint (`posted=False`, `backUrl=/hd_base/ruonia/dynamics/`) when the HTML response contains no rows. The XLSX is parsed through the small `openpyxl` dependency. The output keeps source ticker, source kind, board provenance, response hashes and the method used per chunk. If both CBR responses are unavailable or unparseable, the run remains `exploratory_proxy_only` and reports `CBR_RUONIA` as missing; no rate rows are invented. Only the `observed` VTBM/LQDT segment is eligible for a clean actual-fund benchmark conclusion.

## Verified intake

The 2026-11-03 run recovered CBR RUONIA from 2010-01-11 through 2026-10-27: 4,139 daily rates across 17 yearly chunks, all via the HTML method. The complete lineage now has 4,227 rows and status `observed_plus_labelled_proxy`; the observed VTBM/LQDT layer still begins on 2020-01-20. The ignored local artifact is `data/benchmark/lqdt-lineage.json`.
