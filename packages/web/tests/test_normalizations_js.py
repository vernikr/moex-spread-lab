"""UI normalization regressions executed against the shipped ES module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).resolve().parents[1] / "src/moex_spread_lab/web/static/js"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is optional")
def test_ols_normalization_evaluates_fit_at_the_current_gap_preserving_x() -> None:
    script = f"""
const mod = await import({json.dumps((_JS_DIR / "normalizations.js").as_uri())});
const series = Array.from({{ length: 12 }}, (_, i) => ({{
  spread_rel: i === 4 || i === 5 ? null : i * 0.1,
}}));
const out = mod.spreadDisplayValues(series, 'ols20');
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "--no-warnings", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(proc.stdout)

    assert values[4:6] == [None, None]
    assert abs(values[1] - 0.05) < 1e-12
    assert all(abs(value) < 1e-12 for value in values[6:] if value is not None)
