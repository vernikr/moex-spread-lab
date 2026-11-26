# VTB EBS/KPUR account-risk profile

VTB EBS (unified brokerage account) and КПУР are user-confirmed account attributes ([A-10](../../../docs/decisions.md)). This is an offline working contract, not a client-specific broker calculation, collateral ring-fence or authorization to fund or trade.

## Risk and cash boundary

- [R01](../../../docs/research/results/received/R01/R01.md) gives `НПР1 = S − M0 − Sблок`, `НПР2 = S − Mx` and `Mx = 0.5 × M0`; `S` is broker-valued portfolio wealth, not settled RUB. The broker may calculate `M0 = ГО × k` under client terms. `НПР2 < 0` triggers broker closure; for КПУР, `НПР2 = 0` is a working closure target subject to stricter terms. Restrictive times are broker deadlines, not guaranteed notice, reaction time or preservation of both legs.
- EBS can expose core LQDT to broker recourse. Verify its eligibility, discount and actual client treatment; never count it as pilot cash, variation-margin liquidity, a pair-margin offset or a cap on account loss. Entry funding and cash reserves stay within the [pilot policy](../../../docs/risk/pilot-policy.md).
- Use supplied contract margins as labelled offline inputs, add the two legs without an offset and stress a single leg and higher requirements. ISS inputs do not establish VTB's client coefficients or historical margin. Keep cash available for variation margin independently of collateral value.
- The [R02](../../../docs/research/results/received/R02/R02.md) EBS under-covered-margin sensitivity is `max(ГО − RUB_cash, 0) × (key_rate + 0.02) × calendar_days / 365(366)`. The CLI uses 365 for its scenario. Verify the applicable portfolio/tariff clause (EBS §16 versus separate futures §17), key rate, cash classification and valuation time; this is not a known account debit or a substitute for transaction costs.

## Offline CLI

Supply current contract IDs, a sourced ISS intake, reviewed cash/buffers and a key rate as a decimal fraction; CLI defaults are not evidence that any input is current:

```bash
uv run python -m moex_spread_lab.cli account-risk-check \
  --near "$NEAR_SECID" --far "$FAR_SECID" --contracts "$ISS_INTAKE" \
  --initial-capital "$PILOT_CASH_RUB" --key-rate "$KEY_RATE_FRACTION" \
  --stress-vm-buffer "$VM_BUFFER_RUB" --uncertainty-buffer "$UNCERTAINTY_BUFFER_RUB" \
  --output /tmp/account-risk.json
```

The screen compares margin plus VM and uncertainty reserves with the supplied cash for the unoffset pair, the larger single leg and 2×/3× pair-margin shocks. Optional `--carry-study` raises the VM reserve to at least the worst observed daily gross loss, not an intraday bound. CLI field `all_working_scenarios_passed` (JSON `summary.all_scenarios_passed`) is arithmetic only; the separate EBS charge is a sensitivity, not broker reconciliation.

The exact EBS perimeter, individual risk coefficients, pair offset, LQDT attribution, RUB settlement for VM/fees, liquidation sequence and shared-account recourse remain unresolved under [b01](../../../docs/blockers/b01-account-and-risk.md). Stress calibration, funding arrival/availability and applicable optional fees also need evidence; emergency funding needs explicit approval. Unknown safety-critical terms cannot be treated as zero cost or unlimited capacity; live activation stays blocked.
