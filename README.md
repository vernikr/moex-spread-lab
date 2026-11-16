# MOEX Spread Lab

Private, research-first MOEX futures-spread screener. Select a pair, strategy and parameters, compare the resulting finite-capital wealth/risk path with matched LQDT, and search parameters for a pair or basket with later-period validation. Manual VTB signals and BCS automation are later gated options; no validated profitable strategy or trading service exists yet.

## Current status

Stage 9 development is in progress. The offline W0–W7 mechanics, ISS inputs, LQDT benchmark, CNYRUBF/CRZ6 exploratory strategies, six-pair price screen and four-pair funding screen are delivered. **S2 now provides a local dark-only, table-first research screener**: one strategy/parameter set across twelve named pairs, percentage-only annualized performance versus LQDT, percentage risk, selectable rows and bounded automatic search for a stable common-parameter neighborhood. The table uses ignored local data and labelled candle/settlement price proxies, not actual fills or a reconciled account. The current frozen rules have not beaten matched LQDT under their stated assumptions; a two-pair search found no stable positive plateau. The native calendar-spread study and a unified execution-proof view (pair screen and native calendars) are now available for cached historical inputs; results remain exploratory settlement/first-trade proxies, not actual fills. Shared-capital basket search (S4) is not delivered. No broker connection, project VPS deployment or orders occurred.

- **[Research pack: what to run first](docs/research/README.md)**
- [Focused research intake and architecture direction](docs/research/accepted/2026-10-26-critical-gates.md)
- [Stage 3: environment preflight](docs/stages/03-environment.md)
- [Stage 4: functional and business requirements](docs/stages/04-requirements.md)
- [Stage 5: architecture](docs/stages/05-architecture.md)
- [Stage 6: stack and data sources](docs/stages/06-stack-and-data.md)
- [Stage 7: reuse analysis](docs/stages/07-reuse-analysis.md)
- [Stage 8: delivery and validation plan](docs/stages/08-planning.md)
- [W0: reproducible run and evidence manifest](docs/development/w0.md)
- [W1–W7: offline research waves](docs/development/w1-w7.md)
- [W1: MOEX ISS catalog and price-role intake](docs/development/w1-intake.md)
- [First analytical baseline](docs/development/analytical-baseline.md)
- [W4/W5 analytical execution ledger](docs/development/execution-ledger.md)
- [W7 chronological validation](docs/development/w7-validation.md)
- [Stage-9 research gate decision](docs/development/research-gate.md)
- [CNYRUBF/CRZ6 contract-history audit](docs/development/contract-history-audit.md)
- [H2 funding-aware carry decomposition](docs/development/carry-study.md)
- [H2-S1 frozen carry strategy](docs/development/carry-strategy.md)
- [H3-F1 funding-only carry hypothesis](docs/development/funding-only-strategy.md)
- [Flagship futures-pair screen](docs/development/pair-screen.md)
- [Funding-aware flagship-pair screen](docs/development/funding-screen.md)
- [S1 local screener: input preparation](docs/development/screener-s1.md)
- [S2 pair table and plateau search](docs/development/screener-s2.md)
- [Unified execution proof (`/proof`)](docs/development/execution-proof.md)
- [CNYRUBF/CRZ6 point-in-time evidence audit](docs/development/point-in-time-evidence-audit.md)
- [VTB account risk profile](docs/development/vtb-account-risk-profile.md)
- [VTB EBS/KПУР account-risk screen](docs/development/vtb-account-risk-check.md)
- [Stage 2 artifact and changes](docs/stages/02-research.md)
- [Provisional delegated risk rules](docs/risk/pilot-policy.md)
- [User decisions](docs/decisions.md)
- [Stage 1: unpacking, objections and questions](docs/stages/01-unpacking.md)
- [Stage sequence](docs/process.md)
- [Inputs and evidence handling](docs/inputs/README.md)
- [Account/risk blockers](docs/blockers/b01-account-and-risk.md)
- [Workflow/research blockers](docs/blockers/b02-research-and-workflow.md)

## How to continue

Run the [local screener](docs/development/screener-s2.md) with `uv run python -m moex_spread_lab.cli screener --port 8765`, then open `http://127.0.0.1:8765/`. The screener table deep-links each pair to the [**execution proof**](docs/development/execution-proof.md) screen (`/proof?universe=pair_screen&pair=…&strategy=…`); the proof page needs cached calendar history for the native calendar universe, prepared by `uv run python -m moex_spread_lab.cli calendar-study --till-date YYYY-MM-DD --output /tmp/calendar-study.json`. Both views need ignored local research inputs; [S1](docs/development/screener-s1.md) describes preparation. The proof page itemizes fills, per-side fees, volumes, daily marks and costs for both universes; its historical prices remain proxies, not actual fills. The current search evaluates selected pairs independently; it does not simulate a combined portfolio. The [profit audit](docs/development/profit-audit.md) verifies the calculation/data path, reproduces the SBRF reference (S3, not accepted as evidence) and maps where profit could come from. Follow the [Stage-8 plan](docs/stages/08-planning.md) for S4 shared-capital basket evaluation/search, which waits for better inputs. Each slice has its own acceptance gate. The detailed `СКРИНЕР_epub.md` is [preserved as a product hypothesis](docs/research/reuse/СКРИНЕР_epub.md), not verified market evidence. The received R01–R15 reports and [research pack](docs/research/README.md) remain source material for evidence gaps, not a prerequisite to showing the first local view.

Delegated numeric risk rules are provisional controls, not evidence of safe or profitable trading. Internal funding ceilings do not ring-fence the core brokerage portfolio.
