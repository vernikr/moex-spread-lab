# Flagship futures-pair screen

Date: 2026-11-03

## Scope

This is the first broad offline screen after the dedicated CNYRUBF/CRZ6 work. It uses the six same-asset perpetual-to-quarterly relationships identified in R06 as the strongest documented candidate family:

`GLDRUBF/GLZ6`, `IMOEXF/MMZ6`, `GAZPF/GZZ6`, `SBERF/SRZ6`, `RGBIF/RBZ6`, `USDRUBF/SiZ6`.

The screen is a prefilter, not six approved strategies. Each pair uses:

- synchronized daily ISS candle closes from `2026-03-23` through `2026-10-27`;
- the same fixed H1 control: 20-observation lookback, entry `|z| >= 2`, exit `|z| <= 0.5`, one-observation delay;
- illustrative stress transaction costs;
- matched observed LQDT benchmark;
- current ISS margin sum, no broker spread offset, RUB 5,000 VM buffer, RUB 5,000 uncertainty buffer and 2×/3× margin shocks;
- price-unit normalization using `tick_value / tick_size` per leg. This prevents different quotation formats from being mistaken for different economic units.

Historical order-book and actual-fill data are excluded under A-08. All results are candle-close proxies.

## Results

| Rank | Pair | Common rows | Pair ГО | 3×-stress remaining envelope | H1 final `X` | Decision |
|---:|---|---:|---:|---:|---:|---|
| 1 | USDRUBF/SiZ6 | 155 | RUB 24,952.14 | −RUB 54,856.42 | −RUB 629.24 | Reject capital screen |
| 2 | SBERF/SRZ6 | 155 | RUB 10,568.75 | −RUB 11,706.25 | −RUB 1,384.24 | Reject capital screen |
| 3 | RGBIF/RBZ6 | 155 | RUB 2,312.49 | RUB 13,062.53 | −RUB 3,175.24 | No promotion |
| 4 | GLDRUBF/GLZ6 | 155 | RUB 2,257.96 | RUB 13,226.12 | −RUB 3,744.94 | No promotion |
| 5 | GAZPF/GZZ6 | 155 | RUB 4,092.83 | RUB 7,721.51 | −RUB 3,811.24 | No promotion |
| 6 | IMOEXF/MMZ6 | 155 | RUB 5,394.01 | RUB 3,817.97 | −RUB 4,268.24 | No promotion |

Summary:

- 6/6 passed the input and unit-normalization checks;
- 4/6 passed all working capital/3× stress scenarios;
- 0/6 had positive final `X` under the fixed H1 control;
- 0/6 qualified for a new hypothesis under the current promotion prefilter.

The negative H1 results do not reject the economic carry hypothesis for the perpetual-to-quarterly family. They do show that the existing price-only mean-reversion control is not a sufficient reason to advance any of these pairs.

## Historical reproduction

This result is a dated historical record. The legacy `pair-screen` CLI/API was retired in #234 when the proof workbench became the supported pair-research surface; the command below is retained only as evidence of the original intake recipe and no longer reproduces a screen result. New analyses should use the proof workbench.

The original screen used locally downloaded, ignored ISS candle artifacts and the existing benchmark/catalog:

```bash
uv run python -m moex_spread_lab.cli iss-candles \
  --secid GLDRUBF --secid GLZ6 \
  --secid IMOEXF --secid MMZ6 \
  --secid GAZPF --secid GZZ6 \
  --secid SBERF --secid SRZ6 \
  --secid RGBIF --secid RBZ6 \
  --secid USDRUBF --secid SiZ6 \
  --interval 24 \
  --from-date 2026-03-23 \
  --till-date 2026-10-27 \
  --output-dir data/w1/candles-screen \
  --cache-dir data/w1/candles-screen-cache

uv run python -m moex_spread_lab.cli pair-screen \
  --candles-dir data/w1/candles-screen \
  --benchmark data/benchmark/lqdt-lineage.json \
  --contracts data/w1/current-intake.json \
  --initial-capital 30000 \
  --output data/analytics/flagship-pair-screen.json
```

## Next decision

Do not optimize the H1 control and do not treat the four stress-passing pairs as profitable. The next economic branch, if continued, is a separately frozen funding-aware hypothesis for one documented pair at a time. It requires the corresponding perpetual funding field, dated execution/fixing rules and the same LQDT/risk gates. The current screen itself provides no authorization to fund or trade.
