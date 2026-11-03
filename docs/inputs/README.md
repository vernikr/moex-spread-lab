# Input register

Received 2026-10-07; the current tariff file was received 2026-11-03. Source documents remain in their original language; newly authored interpretation is English.

| File | Handling | Role |
|---|---|---|
| `brief.md` | Verbatim | User objectives, constraints and mandatory stage sequence |
| `AGENTS.source.md` | Credential removed | Original workstation guidance; root `AGENTS.md` is the applicable cloud adaptation |
| `server-spec.redacted.md` | Production host address replaced with `<VPS_HOST>` | User-provided hardware and production protection rules; not remotely verified |
| `prof_privilegiya.pdf` | Verbatim, five pages | Earlier user-provided VTB tariff evidence; not used as the current tariff after A-09 |
| `moy_online_praym.pdf` | Verbatim, four pages | Current user-provided VTB tariff evidence for A-09 |
| `manifest.json` | SHA-256 of committed source copies | Detect changes; redacted copies are not claimed to match original byte streams |

## Tariff observations

Earlier PDF extraction, reviewed 2026-10-07:
- Page 5 states no broker commission on derivatives through **2026-12-31 inclusive**; exchange commission depends on instrument.
- Page 1 states a no-broker-commission promotion on exchange-traded funds through the same date. This does not establish zero total cost of liquidating/rebuying LQDT.
- Page 2 lists RUB 150 for securities settlement operations, with shareholder discounts. Incremental applicability to this strategy/account needs clarification.
- Pages 2–4 describe optional Advisory services with different charges, including RUB 10 per derivatives contract in specified cases. Do not substitute these for the base tariff without confirming service subscription.
- Page 5 lists overnight borrowing charges by borrowed asset. These are not automatically financing charges on futures notional or on every margin allocation.

The current user confirmation and `moy_online_praym.pdf` establish the working VTB base tariff for this project. The current PDF states: broker commission for the derivatives market is zero through 2026-12-31 inclusive; exchange commission on the derivatives market depends on the instrument; margin lending is free during the trading day and has separate overnight rates; optional Advisory Pro charges are separate and are not assumed active. This does not establish margin rules, collateral eligibility, KPUR treatment, liquidation procedure or pricing after 2026-12-31. Historical tests must not apply this promotion retroactively.

## Security

The uploaded agent file contained a GitHub credential. Its value is absent from all committed copies. The credential was used only for the requested GitHub actions. User should revoke/rotate it after this task because it appeared in an uploaded document. Do not reproduce it in issues, logs, remote URLs or future prompts.
