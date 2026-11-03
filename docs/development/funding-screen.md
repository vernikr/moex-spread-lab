# Funding-aware flagship-pair screen

Date: 2026-11-04

## Decision

The frozen funding-only rule was evaluated independently on the four pairs that passed the prior capital prefilter. **No pair is promoted to a new strategy hypothesis.** The four candidates passed the working account stress screen, but all ended below matched LQDT under the stress-cost scenario and one-observation EOD delay.

This result does not reject the perpetual-to-quarterly family as an economic concept. It rejects this funding-only rule for this fixed window and evidence package.

## Frozen method

Each pair uses the same separately reported `H3-F1/<pair>` application:

- short the perpetual leg and long the dated leg, one contract per leg;
- enter only when the previous 20 observations have positive mean funding received by the short perpetual leg;
- execute one EOD observation after the signal;
- exit when trailing funding becomes non-positive or five calendar days before dated expiry;
- exit before a perpetual execution window if it is observable;
- use EOD `SETTLEPRICE` as the labelled price proxy;
- include `SWAPRATE × perpetual lot size` as daily funding;
- compare the whole virtual allocation with matched LQDT;
- apply illustrative stress costs and the working VTB EBS/KПУР capital screen.

The input window is `2026-03-23`–`2026-10-27`, with 155 common EOD observations per pair. The history endpoint returned a non-null `SWAPRATE` field for each perpetual leg and zero values for each dated leg. R06 and the cited MOEX specification analysis support the unit interpretation as RUB per contract-day and the sign convention that positive funding is received by the short perpetual leg. Effective-dated K1/K2 caps for these non-CNY pairs are not substituted from the CNY model and are marked as not modelled.

## Results

| Pair | Rows | Funding received, RUB | Positive / negative rows | Basis P&L, RUB | Gross carry, RUB | Closed trades | Stress-cost `final X`, RUB | Costs, RUB | Pair ГО, RUB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IMOEXF/MMZ6 | 155 | 1,870.95 | 153 / 1 | −1,786.50 | +84.45 | 0 | −2,491.21 | 23.00 | 5,394.01 |
| GLDRUBF/GLZ6 | 155 | 982.70 | 154 / 0 | −1,127.20 | −144.50 | 0 | −2,657.56 | 12.40 | 2,257.96 |
| RGBIF/RBZ6 | 155 | 228.55 | 99 / 55 | −425.00 | −196.45 | 2 | −2,727.46 | 88.00 | 2,312.49 |
| GAZPF/GZZ6 | 155 | 968.47 | 154 / 0 | −1,184.00 | −215.53 | 0 | −2,825.16 | 16.00 | 4,092.83 |

Summary:

- `4/4` pairs were evaluated successfully;
- `4/4` passed the current working capital/3× margin-shock scenarios;
- `0/4` had positive final `X` against matched LQDT;
- `0/4` qualified for a separately promoted hypothesis;
- three pairs had an open proxy position at data end because the funding signal remained positive and no expiry window was present in the input.

The positive gross result for IMOEXF/MMZ6 is not sufficient: the rule did not close a trade in the input window, while the matched LQDT path advanced faster. The result remains a proxy, not an executable P&L.

## Evidence and limitations

- Historical order-book, bid/ask and actual fills remain excluded under A-08.
- Current contract parameters remain a fallback for historical conversion.
- The funding unit and sign are supported by R06's specification analysis; the individual historical K1/K2 rule versions for all four pairs are not fully reconstructed.
- The R06 report separately describes 93-observation `swaprates` candle history for several perpetuals, while this run uses the 155-row FORTS EOD history field. That source distinction remains explicit and requires further point-in-time reconciliation before any stronger claim.
- RGBIF/RBZ6 remains economically imperfect because RGBIF references RGBILP while RBZ6 references RGBI; its unstable funding sign is visible in the result.
- GAZPF and SBERF perpetuals have unresolved dividend treatment around future ex-dates; no such event was invented in this screen.

## Reproduce

```bash
uv run python -m moex_spread_lab.cli funding-screen \
  --from-date 2026-03-23 \
  --till-date 2026-10-27 \
  --contracts data/w1/current-intake.json \
  --benchmark data/benchmark/lqdt-lineage.json \
  --cache-dir data/carry-screen-history-cache \
  --cost-scenario stress \
  --initial-capital 30000 \
  --output data/analytics/flagship-funding-screen.json
```

## Next decision

Do not optimize this rule, increase quantity or combine results across pairs. The next useful offline task is a dated evidence audit for the two economically strongest candidates, GLDRUBF/GLZ6 and IMOEXF/MMZ6: reconcile `swaprates` versus FORTS-history funding rows, recover applicable K1/K2 versions and check the expiry/fixing methodology. If that evidence is not improved, this pair family can be closed at the research gate without broker integration or funding.
