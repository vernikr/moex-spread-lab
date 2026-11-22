# Market data locality for cloud agents

Status: **Delivered guidance** (2026-11-22). Answers the recurring question "the trading data is
already on my workstation — does that help the cloud agent, and how should the plan change if
`iss.moex.com` is slow or blocked from outside Russia?".

## 1. Short answer

- **For P0 correctness and speed: no.** The bottleneck was not missing data, it was a *silent
  network fallback*: `CalendarData` had `offline=False` semantics whenever `MOEX_OFFLINE_ONLY` /
  `PYTEST_CURRENT_TEST` were unset. Every cold catalog call therefore tried ~1,050 uncached contract
  secids against ISS (0.05 s rate-limit sleep each) before falling back to the cache. Fixing the
  default (P0) makes the local workstation data irrelevant for latency: the same synthetic fixture
  catalog went **181.02 s -> 0.05 s** with zero sockets opened.
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

1. **Keep compute on the workstation (default, zero copies).** `docs/development/remote-agent-testing.md`
   already specifies this: the loopback server is the only data holder, the cloud agent talks to it
   through the authenticated Arena Bridge, and P3 adds `moex-spread-lab remote sync|status` plus a
   `launchd` screener so the agent never spawns its own data-holding process. This is the cheapest
   and the only option with no redistribution question at all. Remaining gap: a cloud agent still
   cannot *unit-test* real data paths — it tests synthetic fixtures instead.
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

## 5. Recommendation

Keep P0–P2 as planned (no plan surgery needed for data locality). Add, in priority order:

1. **P3 first** — `remote sync`/`status` + `launchd` screener: measured real data with zero copies.
2. **`inputs-manifest.json` + `scripts/fetch_inputs.py`** (new, ~1 slice): verify local inputs
   against a manifest, and later pull them from a private artifact/release when a cloud agent must
   run real numbers. Blocked on a user decision about where that store lives; recorded in
   `docs/blockers/b02-research-and-workflow.md` rather than guessed here.
3. Only if a specific task demands cloud-side real-data runs, evaluate option 4/5 with an explicit
   written decision in `docs/decisions.md`.
