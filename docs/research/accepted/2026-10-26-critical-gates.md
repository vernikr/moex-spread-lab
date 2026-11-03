# Focused research intake: critical gates and architecture direction

Date: 2026-10-26

## Scope and status

This is a focused acceptance pass over the supplied R01–R15 reports. It accepts the strongest cross-report conclusions as working inputs for requirements and architecture. It does not establish account-specific broker permissions, data rights, executable prices, profitability, legal conclusions or trading authorization.

The supplied reports are the best available research corpus for the project. Non-critical disagreements and low-impact source gaps are deferred; decision-critical unknowns remain explicit.

## Working inputs accepted for architecture

1. **Matched LQDT accounting.** The primary result is the RUB difference between the strategy-enabled wealth path and a flow-matched no-pilot LQDT path. Keep economic wealth, available cash/margin, collateral capacity and funding events as separate ledgers. Do not double-count retained LQDT return, variation margin or internal transfers.
2. **One constrained virtual portfolio.** Evaluate every eligible idea through one finite-capital portfolio. Preserve stale, rejected, unfilled, partial and one-leg outcomes; do not turn a historical signal into an assumed fill.
3. **Point-in-time data.** Store event time, source publication/update time and local receipt time separately. Version contract specifications, risk parameters, calendars, costs and source provenance. Historical data must represent what was knowable at the decision time.
4. **Execution as a separate layer.** Signal generation and delayed revalidation are different operations. Entry must use current executable sides, session state, capital, margin, costs and validity rules. Manual VTB execution is the initial route; automated BCS execution is a later adapter behind a separate gate.
5. **Narrow economic universe.** Start from documented contract relationships and liquidity filters. Do not begin with an unrestricted all-pairs correlation search. A zero-feasible-pair result is valid.
6. **Validation before complexity.** Use no-trade and simple deterministic baselines, chronological walk-forward evaluation, overlap-aware purge/embargo, a frozen holdout and a complete trial registry. R13–R15 remain optional extensions, not architecture requirements.
7. **Split research and runtime environments.** Heavy historical work belongs on the MacBook. The shared VPS, if later approved, is limited to a small monitor, virtual ledger and alert outbox. No public dashboard, database server or heavy analytics stack is assumed on that host.

## Critical gates that remain open

| Gate | Current disposition | Consequence |
|---|---|---|
| Shared-account recourse, applied KPUR category, LQDT haircut and liquidation path | EBS and КПУР are user-confirmed; public VTB/NCC rules and dated LQDT haircut are recorded, while individual application remains unverified | No live trading decision; core LQDT is not treated as available pilot cash |
| VTB/BCS account tariff, order route, spread support and client-specific fees | VTB current tariff is user-confirmed; exchange fees, route, spread support and client-specific controls remain parameterized | Keep fees and order capabilities parameterized |
| ISS/data-use rights and current data availability | Technical access is reported; permitted automated collection is unresolved | Build a replaceable data-provider boundary; do not make automated collection a legal assumption |
| Historical executable bid/ask and fill evidence | Not available and excluded from this project under A-08 | Historical results remain explicitly candle-proxy research; no historical fill claim is made |
| Actual reaction-time, VPS headroom and operational reliability | Proposed or estimated only | Measure in later gates; do not convert estimates into safety limits |
| Strategy edge versus LQDT | Not tested | No strategy, parameter set or profitability claim is accepted |

## Architecture direction unlocked now

The project can proceed with a provisional modular design:

- a point-in-time market-data and contract-specification boundary;
- a domain layer for spreads, costs, margin/liquidity reservations and the `V/B/X/H/D` ledgers;
- a single event-sourced virtual portfolio and execution simulator;
- a manual-notification adapter for the initial VTB workflow;
- a separate future BCS data/order adapter, disabled by default;
- laptop-oriented research storage and compute, with a thin optional VPS runtime;
- immutable raw events plus derived tables and reproducible reports.

These are architecture boundaries, not a final technology selection. Concrete stack choices belong to later stages after the local/VPS resource check and data-rights decision.

## Immediate sequence

1. **Completed:** stage 3 read-only inventory and non-destructive VPS preflight. The VPN, existing services and ports were protected; no installation or deployment was performed.
2. **Completed:** stage-4 requirements around the virtual portfolio, data lineage, manual workflow, LQDT accounting and rejection reasons. Account-specific broker facts remain configuration inputs rather than hard-coded assumptions.
3. **Completed:** stage 5 architecture boundaries and decision records, including predeclared scale-in/out ladders, explicit portfolio events and contract-first modularity for independent providers/teams.
4. **Completed:** stage 6 conditional stack and data-source baseline. The rights, account, executable-data and broker gates remain explicit; no implementation or deployment is authorized by the selection. The later user-confirmed EBS/KПУР profile is recorded in `docs/development/vtb-account-risk-profile.md`.
5. **Completed:** stage 7 reuse analysis. Previous projects strengthen the ISS ingestion pattern, price-role separation and dividend provenance requirements; they do not close rights, account, execution or profitability gates.
6. **Completed:** stage 8 delivery and validation plan. The first slice starts with offline fixtures, one candidate family, daily data, versioned specifications, costs, delayed revalidation and the matched LQDT ledger.
7. Move to stage 9 development only after the fixture-level gates in `docs/stages/08-planning.md` pass. Broker access, deployment and live trading remain separately blocked.
