# MOEX Spread Lab

Private, research-first MOEX futures-spread screener. Select a pair, strategy and parameters, compare the resulting finite-capital wealth/risk path with matched LQDT, and search one common strategy/parameter set for checked independent windows with optional later-period validation. Manual VTB signals and BCS automation are later gated options; no validated profitable strategy or trading service exists yet.

## Run locally

```sh
uv run python -m moex_spread_lab.cli screener --port 8765
```

Open `http://127.0.0.1:8765/` (or `/proof`). Select a window to inspect its chart and ledger; check windows to search one common strategy across them. See the [execution-proof guide](docs/development/execution-proof.md) for settings, scores and diagnostic limits.

Both universes use ignored local inputs: [pair setup](docs/development/screener-s1.md) and native-calendar cache preparation:

```sh
uv run python -m moex_spread_lab.cli calendar-study --till-date YYYY-MM-DD --output data/calendar-study.json
```

## Status and references

Stage 9 is in progress. Modular-architecture Phases 0–6 are complete: the six `packages/*` workspace members own the research runtime, while the root umbrella keeps the `cli.py` and `__init__.py` entry points. Package-local CLI entry points are discovered deterministically. Proof supports registered calendar price-intent, daily/hourly pair price-intent, and funding-settlement adapters through the existing accounting; unsupported modes fail closed. The proof workbench supersedes the S2 homepage; the S2 compatibility API remains. The [profit audit](docs/development/profit-audit.md) explains why historical proxies do not establish an edge. Shared-capital basket search (S4), deployment and live execution are not delivered. The [EPUB-derived screener proposal](docs/research/reuse/СКРИНЕР_epub.md) is a product hypothesis.

### Workspace map and agent scopes

| Package | Layer | Ownership |
|---|---:|---|
| `packages/core` (`msl-core`) | 0 | Contracts, settings, hashing, timeframes, universe constants, C99 fastcore |
| `packages/data` (`msl-data`) | 1a | ISS intake, data lake/H1 seal, retention, benchmark lineage |
| `packages/accounting` (`msl-accounting`) | 1b | Costs, account risk, sizing, ledger, run artifacts, validation |
| `packages/strategies` (`msl-strategies`) | 2 | Isolated strategy plugins, pair/calendar datasets and signal adapters |
| `packages/proof` (`msl-proof`) | 3 | Proof orchestration, replay adapters, cohort search and audit |
| `packages/web` (`msl-web`) | 4 | Loopback HTTP server/routes and modular same-origin UI |

Default agent work is scoped to one package subtree, including its tests/docs/fixtures. Cross-package changes start with a contract and conformance test; `data` and `accounting` remain independent siblings. Internal code and tests use canonical package imports. Root modules beyond `cli.py` and `__init__.py` are backward-compatibility shims; each shim names its canonical target, and workspace code/tests must use canonical imports.

```sh
uv run pytest packages/proof/tests -q          # focused package suite
uv run python scripts/architecture_gate.py     # global DAG, discovery, manifest, secret gate
uv run pytest -q -n auto --dist loadfile        # full package-scoped suite
```

`pytest` discovers package tests only; `tests/` remains for shared synthetic fixtures. CI splits by package and uses xdist `loadfile` scheduling rather than a shared duration file. See [CI](docs/development/ci.md) and the [modular architecture plan](docs/development/modular-architecture-plan.md) for the full ownership and compatibility rules.

- **[Research pack: what to run first](docs/research/README.md)**
- [Focused research intake and architecture direction](docs/research/accepted/2026-10-26-critical-gates.md)
- [Stage 3: environment preflight](docs/stages/03-environment.md)
- [Stage 4: functional and business requirements](docs/stages/04-requirements.md)
- [Stage 5: architecture](docs/stages/05-architecture.md)
- [Stage 6: stack and data sources](docs/stages/06-stack-and-data.md)
- [Stage 7: reuse analysis](docs/stages/07-reuse-analysis.md)
- [Stage 8: delivery and validation plan](docs/stages/08-planning.md)
- [W0: reproducible run and evidence manifest](docs/development/w0.md)
- [W1–W7: offline research waves](packages/accounting/docs/w1-w7.md)
- [W1: MOEX ISS catalog and price-role intake](packages/data/docs/w1-intake.md)
- [Local data lifecycle and verified candidate archival](packages/data/docs/data-lifecycle.md)
- [First analytical baseline](packages/strategies/docs/analytical-baseline.md)
- [W4/W5 analytical execution ledger](packages/accounting/docs/execution-ledger.md)
- [W7 chronological validation](packages/accounting/docs/w7-validation.md)
- [Stage-9 research gate decision](docs/development/research-gate.md)
- [CNYRUBF/CRZ6 contract-history audit](docs/development/contract-history-audit.md)
- [H2 funding-aware carry decomposition](packages/strategies/docs/carry-study.md)
- [H2-S1 frozen carry strategy](packages/strategies/docs/carry-strategy.md)
- [H3-F1 funding-only carry hypothesis](packages/strategies/docs/funding-only-strategy.md)
- [Flagship futures-pair screen](packages/strategies/docs/pair-screen.md)
- [Funding-aware flagship-pair screen](packages/strategies/docs/funding-screen.md)
- [Perpetual funding coverage: exchange recheck, short-gap estimates and session evidence](packages/data/docs/historical-pair-intake.md#funding-reconciliation-2026-11-18)
- [S1 local screener: input preparation](docs/development/screener-s1.md)
- [S2 pair table and plateau search](docs/development/screener-s2.md)
- [CNYRUBF/CRZ6 point-in-time evidence audit](docs/development/point-in-time-evidence-audit.md)
- [VTB account risk profile](packages/accounting/docs/vtb-account-risk-profile.md)
- [VTB EBS/KПУР account-risk screen](packages/accounting/docs/vtb-account-risk-check.md)
- [Stage 2 artifact and changes](docs/stages/02-research.md)
- [Provisional delegated risk rules](docs/risk/pilot-policy.md)
- [User decisions](docs/decisions.md)
- [Stage 1: unpacking, objections and questions](docs/stages/01-unpacking.md)
- [Stage sequence](docs/process.md)
- [Inputs and evidence handling](docs/inputs/README.md)
- [Account/risk blockers](docs/blockers/b01-account-and-risk.md)
- [Workflow/research blockers](docs/blockers/b02-research-and-workflow.md)
- [CI and fast test runs](docs/development/ci.md)
- [Remote agent testing and benchmarking design](docs/development/remote-agent-testing.md)
- [Modular architecture and multi-agent workspace plan](docs/development/modular-architecture-plan.md)
- [Post-migration architectural review and Phase 7 plan](docs/development/modular-architecture-review.md)
- [Calendar proof plugin contract](packages/strategies/docs/calendar-proof-plugins.md)
- [Pair intent boundary and remaining accounting work](packages/strategies/docs/pair-intent-boundary.md)
- [Automated code health and AI-readability plan](docs/development/code-health-plan.md)
- [Agent session latency and context-bloat prevention plan](docs/development/agent-performance-plan.md)
