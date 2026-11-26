from pathlib import Path

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.retention.h1_archive import execute


@pytest.mark.parametrize("mode", ["dry-run", "archive-only", "apply"])
def test_retired_h1_command_fails_closed(tmp_path: Path, mode: str):
    source = tmp_path / "source.json"
    source.write_text("{}")
    with pytest.raises(W0Error, match="retired"):
        execute(tmp_path, tmp_path / "plan.json", mode, archive=tmp_path / "archive")
    assert source.read_text() == "{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.json"]
