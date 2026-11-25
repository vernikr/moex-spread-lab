#!/usr/bin/env bash
# Tier-1 repository gate; see scripts/check_health.py (--help) for modes and exit codes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -x "$ROOT/.venv/bin/python" ]; then exec "$ROOT/.venv/bin/python" "$ROOT/scripts/check_health.py" "$@"; fi
exec uv run --no-sync python "$ROOT/scripts/check_health.py" "$@"
