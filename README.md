# MOEX Spread Lab

Private, research-first MOEX futures-spread screener. Select a pair, strategy and parameters, compare the resulting finite-capital wealth/risk path with matched LQDT, and search one common strategy/parameter set for checked independent windows with optional later-period validation. Manual VTB signals and BCS automation are later gated options; no validated profitable strategy or trading service exists yet.

## Run locally

```sh
uv run python -m moex_spread_lab.cli screener --port 8765
```

Open `http://127.0.0.1:8765/` (or `/proof`). The server keeps its loopback-only guard; a cloud-agent preview opts in with `MSL_ALLOWED_HOSTS=<preview host>` behind a reverse proxy. Select a window to inspect its chart and ledger; check windows to search one common strategy across them. See the [execution-proof guide](packages/proof/docs/execution-proof.md) for settings, scores and diagnostic limits.

Both universes use ignored local inputs; native-calendar cache preparation:

```sh
uv run python -m moex_spread_lab.cli calendar-study --till-date YYYY-MM-DD --output data/calendar-study.json
```

## Workspace map and agent scopes

| Package | Layer | Ownership |
|---|---:|---|
| `packages/core` (`msl-core`) | 0 | Contracts, settings, hashing, timeframes, universe constants, C99 fastcore |
| `packages/data` (`msl-data`) | 1a | ISS intake, data lake/H1 seal, retention, benchmark lineage |
| `packages/accounting` (`msl-accounting`) | 1b | Costs, account risk, sizing, ledger, validation |
| `packages/strategies` (`msl-strategies`) | 2 | Isolated strategy plugins, pair/calendar datasets and signal adapters |
| `packages/proof` (`msl-proof`) | 3 | Proof orchestration, replay adapters, cohort search and audit |
| `packages/web` (`msl-web`) | 4 | Loopback HTTP server/routes and modular same-origin UI |

Default agent work is scoped to one `packages/<name>/` subtree, including its tests/docs/fixtures. Cross-package changes start with a contract and conformance test; `data` and `accounting` remain independent siblings. Internal code and tests use canonical `moex_spread_lab.<domain>` imports. `packages/proof/docs/execution-proof.md` owns the proof/accounting contracts; never infer shared capital or expanded trading authority from the offline UI.

```sh
uv run pytest packages/proof/tests -q          # focused package suite
uv run python scripts/architecture_gate.py     # global DAG, discovery, manifest, secret gate
uv run pytest -q -n auto --dist loadfile        # full package-scoped suite
```

`pytest` discovers package tests only; `tests/` holds shared synthetic fixtures. CI splits by package and uses xdist `loadfile` scheduling. See [CI](docs/development/ci.md) and `AGENTS.md` for current ownership and compatibility rules.

## Status and durable references

The proof workbench supersedes the retired S2 table-first grid. Current scope and status for shared-capital basket search (S4) are tracked in [Issue #17](https://github.com/vernikr/moex-spread-lab/issues/17); deployment and live execution remain outside the delivered research workbench. See [Project #2](https://github.com/orgs/vernikr/projects/2) for live work status, the [Phase 7 Epic #12](https://github.com/vernikr/moex-spread-lab/issues/12) for remaining architecture work, and [Issue #35](https://github.com/vernikr/moex-spread-lab/issues/35) for the completed modular migration history.

- [User decisions and safety boundaries](docs/decisions.md)
- [Provisional delegated risk rules](docs/risk/pilot-policy.md)
- [Stage-9 research gate decision](docs/development/research-gate.md)
- [GitHub Project and agent Issue workflow](docs/development/github-project.md)
- [CI and fast test runs](docs/development/ci.md)
- [Market data locality for cloud agents](docs/development/data-locality.md)
- [Research pack: what to run first](docs/research/README.md)
- [Inputs and evidence handling](docs/inputs/README.md)
- [Account/risk blockers](docs/blockers/b01-account-and-risk.md)
- [Workflow/research blockers](docs/blockers/b02-research-and-workflow.md)
- [Stage 8: delivery and validation plan](docs/stages/08-planning.md)
