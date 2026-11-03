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

The output keeps source ticker, source kind, board provenance and response hashes. If the Central Bank endpoint is unavailable or unparseable, the run remains `exploratory_proxy_only` and reports `CBR_RUONIA` as missing; no rate rows are invented. Only the `observed` VTBM/LQDT segment is eligible for a clean actual-fund benchmark conclusion.
