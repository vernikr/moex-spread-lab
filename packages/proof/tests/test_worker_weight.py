"""Worker shard weights preserve sealed and legacy input precedence."""

from unittest.mock import patch

from moex_spread_lab.proof import pool_reports as worker_weight
from moex_spread_lab.proof import proof_pool


def test_unsealed_weights_use_screen_then_legacy_without_reopening_seal(tmp_path):
    target = {"universe": "pair_screen", "pair": "FAR/NEAR"}
    cache = {}
    screen = tmp_path / "data/w1/candles-screen/NEAR_1h.json"
    legacy = tmp_path / "data/w1/candles/NEAR_1h.json"
    screen.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    screen.write_bytes(b"x" * (128 * 200))
    legacy.write_bytes(b"x" * (128 * 300))
    with patch.object(worker_weight, "SealedH1Reader") as reader:
        reader.return_value.payload.return_value = None
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 200
        screen.unlink()
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 300
        legacy.unlink()
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 1000
        assert reader.call_count == 1
        assert reader.return_value.payload.call_count == 1
    assert proof_pool._estimate_weight is worker_weight._estimate_weight


def test_sealed_weight_uses_logical_bytes_not_present_legacy_file(tmp_path):
    target = {"universe": "pair_screen", "pair": "FAR/NEAR"}
    legacy = tmp_path / "data/w1/candles-screen/NEAR_1h.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x" * (128 * 500))
    cache = {}
    with patch.object(worker_weight, "SealedH1Reader") as factory:
        reader = factory.return_value
        reader.root = tmp_path
        reader.select.return_value = legacy
        reader.payload.return_value = {
            "plan": {
                "files": [
                    {
                        "legacy_path": legacy.relative_to(tmp_path).as_posix(),
                        "logical_bytes": 128 * 250,
                    }
                ]
            }
        }
        assert worker_weight._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 250
        reader.select.return_value = tmp_path / "missing.json"
        assert worker_weight._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 1000
        assert factory.call_count == 1


def test_weight_defaults_do_not_read_seal(tmp_path):
    with patch.object(worker_weight, "SealedH1Reader") as reader:
        assert worker_weight._estimate_weight(tmp_path, {}, "1d") == 200
        assert worker_weight._estimate_weight(tmp_path, {}, "1h") == 1000
        assert (
            worker_weight._estimate_weight(
                tmp_path, {"universe": "pair_screen", "pair": "invalid"}, "1h"
            )
            == 1000
        )
        reader.assert_not_called()
