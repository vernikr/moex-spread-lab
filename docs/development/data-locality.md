# Market data locality for cloud agents

Scope: **Durable data-governance guidance** (not a live work plan). Current workstation work is tracked in GitHub Issues #30 and #51. This page answers the recurring question "the trading data is
already on my workstation — does that help the cloud agent, and how should the plan change if
`iss.moex.com` is slow or blocked from outside Russia?".

## 1. Short answer

- **For offline-fixture correctness and cold-catalog behavior: no.** The bottleneck was not missing
  workstation data but a silent network fallback: web/proof paths now use the offline-by-default
  `CalendarData` contract, so a cache miss fails closed rather than querying ISS. The regression
  test `packages/web/tests/test_offline_catalog.py` guards the no-socket behavior. Historical
  investigation and performance evidence are archived with the Agent Performance history in
  [GitHub Issue #19](https://github.com/vernikr/moex-spread-lab/issues/19).
- **For real research runs: yes, it matters a lot.** A cloud agent cannot produce a real
  wealth/risk path without the real cache; MOEX ISS may throttle or refuse non-RU origins. So the
  data must reach the compute exactly once and then be treated as a read-only input.
- **Never commit datasets to git.** Push market data to git and you get a permanently polluted,
  unbounded history and a license incident. Use the patterns in §3 instead.

## 2. Current data layout (what actually has to travel)

Ignored, restart-safe, read-mostly (see `.gitignore` and `packages/data/docs/data-lifecycle.md`):

| Path | Content | Typical size | Rebuildable |
|---|---|---|---|
| `data/spread-history-cache/*.json` | EOD ISS history per leg/spread secid | tens–hundreds of MB | yes (`calendar-study`) |
| `data/w1/`, `data/w1/candles/1h/` | sealed H1 seal + hourly candles | tens of MB | yes (ISS intake) |
| `data/benchmark*/`, `data/w1/current-intake.json` | RUONIA, LQDT lineage, contract terms | < 5 MB | yes (`fetch_iss_catalog`) |
| `data-lake/` | DuckDB/Parquet lake | hundreds of MB | derived |
| `data/analytics/*.jsonl`, `data/analytics/research-settings.json` | user state, not market data | small | **must not be shared** |

Only the first three matter to a cloud agent that re-runs the screener; `data-lake/` is derived and
`data/analytics/` is user state that agents must not touch.

## 3. How teams usually move private market data to remote compute

Ranked by fit for this repository (private repo, single researcher, no SaaS upload allowed):

1. **Keep compute on the workstation (default, zero copies).** The workstation handoff and benchmark design/history are archived in [GitHub Epic #30](https://github.com/vernikr/moex-spread-lab/issues/30); the remaining standalone benchmark scope is tracked in [Issue #51](https://github.com/vernikr/moex-spread-lab/issues/51). The loopback server remains the only data holder, and the cloud agent talks to it through the authenticated Arena Bridge so it never spawns its own data-holding process. This is the cheapest option and avoids a data-redistribution question. A cloud agent still cannot unit-test real data paths without the workstation; it tests synthetic fixtures instead.
2. **A separately authenticated private artifact store, not git.** Standard industry pattern:
   `dvc push`/`dvc pull`, an S3/R2 bucket, or GitHub Actions artifacts + cache. Data objects live
   outside the commit graph; the repo stores only a small manifest (sha256 + row count + date
   coverage, e.g. an `inputs-manifest.json`). Cache the download by manifest hash so a warm agent
   session pays nothing. This is the right next step if a cloud agent must own real numbers.
3. **Git LFS — not recommended here.** LFS keeps pointers in git but adds a second server, quota
   billing, and trains everyone to clone 100s of MB by default; rotating history is painful and the
   license status of MOEX history is a personal-use assumption, not a redistribution grant.
4. **A dedicated private *data* repository (e.g. `vernikr/moex-spread-lab-data`), never the code
   repo.** Acceptable compromise when only GitHub is available: the data repo can be shallow, has
   its own (separate) access list and can be pruned by re-generating a *single* commit from a
   manifest (orphan branch, squashed). Still a copies-outside-my-control decision; keep it private.
5. **Release assets / CI cache as transport only.** A private GitHub Release asset uploaded by a
   local `make data-publish` script is a legitimate, non-committed transport. Cloud sessions then
   `gh release download` on demand. Same license caveat as (4).

Anti-patterns, all previously observed or plausible in this repo: committing `data/` (bloats history
forever), committing `*.sqlite`, "temporary" workflows that dump data/secrets into Actions artifacts
(already prohibited in `AGENTS.md`), and letting tests reach the live network (what P0 fixes).

## 4. Guard rails this repository should keep

- **Offline by default in code, not by environment variable.** `CalendarData(root, offline=True)`
  is the default contract; `offline=False` only for explicit intake/study commands. Web, proof and
  worker pools construct offline views.
- **Fail closed, log loudly.** An offline miss returns "no local data for `secid`" instead of a
  silent HTTP attempt, so a cloud session reports the real problem ("cache absent") in seconds.
- **Manifest, not payload.** Any future sharing step must publish a manifest (secid, from/till,
  page, sha256) so parity between workstation and cloud inputs is verifiable before a run is
  trusted.
- **`MOEX_OFFLINE_ONLY=1` remains the belt-and-braces flag** for harnesses and for anyone who wants a
  hard guarantee; it is now an override, not the primary mechanism.

## 5. Work tracking and durable recommendation

The durable recommendation remains: keep compute on the workstation by default, do not commit or upload private market data without an explicit decision, and require a manifest/hash check before any future data-sharing path. The workstation CLI/benchmark implementation and remaining acceptance scope are tracked in [GitHub Epic #30](https://github.com/vernikr/moex-spread-lab/issues/30) and [Issue #51](https://github.com/vernikr/moex-spread-lab/issues/51). Any future input-manifest or artifact-store work must first resolve the owner decision in [`docs/blockers/b02-research-and-workflow.md`](../blockers/b02-research-and-workflow.md). Current execution plans, progress and dated measurements belong only in those Issues, not in this guidance document.
