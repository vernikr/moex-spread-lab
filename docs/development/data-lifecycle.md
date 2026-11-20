# Local market-data inventory and retention (Phase 0)

## Purpose

Phase 0 adds a read-only inventory before any storage migration or cleanup. It makes the ignored local `data/` tree observable without changing downloaded market data, caches, research results, or runtime behavior.

The inventory is a planning and evidence aid only. A path marked `archive_candidate` or `rebuildable_cache` is **not** an authorization to delete it. Preserve a pinned source/dataset manifest for any result that must remain reproducible.

## Command

From the repository root:

```sh
# Fast size-and-policy status; does not parse JSON payloads or write a file.
uv run python -m moex_spread_lab.cli data-inventory --root . --fast

# Full manifest: schema, row counts, date ranges and SHA-256 of every local file.
uv run python -m moex_spread_lab.cli data-inventory \
  --root . \
  --hashes \
  --output data/analytics/data-inventory.json
```

The command writes only when `--output` is explicitly supplied. Otherwise it prints a compact JSON summary. The optional output remains under ignored `data/`; do not commit local data manifests that expose workstation paths or inventory.

## Classifications

| Classification | Meaning | Current retention posture |
|---|---|---|
| `active_input` | Offline reader consumes it directly. | Keep while the corresponding research branch is active. |
| `derived_runtime` | Local UI settings or short run history. | Keep with rotation. |
| `rebuildable_cache` | Intake cache, not a screener runtime input. | TTL or rebuild after an explicit review. |
| `archive_candidate` | Legacy/unregistered timeframe or discovery artifact. | Pin relevant runs, then archive rather than delete. |
| `derived_archive_candidate` | Detailed generated analysis result. | Keep summary and run manifest; archive detail. |
| `unclassified` | No policy registered. | Review manually; never delete automatically. |

The current policy treats 1d and 1h `w1/candles/` series as active because those are the registered runtime timeframes. Other timeframe outputs, `candles-common`, and discovery artifacts are candidates for archival only. FORTS history caches are classified as active inputs because offline funding proof reads them.

## Manifest contents and limits

The full inventory records each file's relative path, logical and allocated bytes, mtime, policy, schema/timeframe metadata where available, row count/date range where discoverable, and optional SHA-256. It also aggregates datasets and reports potential reclaimable bytes for candidates. That byte total intentionally requires review; it is not a deletion plan.

The scanner does not follow symbolic links and does not emit source URLs or credential values from local JSON. JSON inspection can be expensive for multi-gigabyte data trees; use `--fast` for routine status and `--hashes` only when preparing a pinned snapshot.
