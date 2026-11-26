"""Unit tests for `_fastcore` static-analysis config and sanitizer loader paths."""

from __future__ import annotations

import json
import sysconfig
from pathlib import Path

import pytest
from scripts import gen_compile_commands, run_sanitized_pytest

from moex_spread_lab.core import _fastcore_loader as loader


def test_gen_compile_commands_emits_valid_c99_entry(tmp_path: Path) -> None:
    c_rel = Path("packages/core/src/moex_spread_lab/core/_fastcore.c")
    c_file = tmp_path / c_rel
    c_file.parent.mkdir(parents=True)
    c_file.write_text("/* stub */\n", encoding="utf-8")

    out_path = tmp_path / "compile_commands.json"
    entries = gen_compile_commands.build_compile_commands(root=tmp_path)
    out_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(parsed) == 1
    row = parsed[0]
    assert row["directory"] == str(tmp_path)
    assert row["file"] == str(c_file)
    args = row["arguments"]
    assert "-std=c99" in args
    assert "-ffp-contract=off" in args
    assert "-Wall" in args
    assert "-Wextra" in args
    inc = sysconfig.get_path("include")
    assert f"-I{inc}" in args


def test_compile_command_separates_release_and_sanitized_flags(tmp_path: Path) -> None:
    c_path = tmp_path / "_fastcore.c"
    rel_so = tmp_path / "_fastcore.so"
    san_so = tmp_path / "_fastcore_asan.so"

    rel_cmd = loader._build_compile_command(
        "clang", "/usr/include/python3.12", c_path, rel_so, sanitize=False
    )
    san_cmd = loader._build_compile_command(
        "clang", "/usr/include/python3.12", c_path, san_so, sanitize=True
    )

    assert "-O3" in rel_cmd
    assert "-fsanitize=address,undefined" not in rel_cmd
    assert "-DMOEX_FASTCORE_SANITIZE=1" not in rel_cmd

    assert "-O1" in san_cmd
    assert "-g" in san_cmd
    assert "-fno-omit-frame-pointer" in san_cmd
    assert "-fsanitize=address,undefined" in san_cmd
    assert "-fno-sanitize-recover=all" in san_cmd
    assert "-DMOEX_FASTCORE_SANITIZE=1" in san_cmd
    assert "-ffp-contract=off" in san_cmd


def test_sanitized_load_requires_preloaded_asan_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOEX_FASTCORE_SANITIZE", "1")
    monkeypatch.delenv("MOEX_PROOF_DISABLE_C", raising=False)
    monkeypatch.setitem(loader._CACHE, True, None)
    loader._CACHE.pop(True, None)
    monkeypatch.setattr(loader, "_has_asan_runtime", lambda: False)

    with pytest.raises(RuntimeError, match="preloading the clang AddressSanitizer runtime"):
        loader.load_fastcore()


def test_ensure_compiled_uses_distinct_asan_artifact_and_scrubs_compiler_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c_path = tmp_path / "_fastcore.c"
    c_path.write_text("int x = 0;\n", encoding="utf-8")
    prod_so = tmp_path / "_fastcore.so"
    prod_so.write_bytes(b"production-binary")

    captured: dict[str, object] = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_bytes(b"sanitized-binary")
        return _FakeCompleted()

    monkeypatch.setenv("LD_PRELOAD", "/tmp/libclang_rt.asan.so")
    monkeypatch.setenv("ASAN_OPTIONS", "detect_leaks=1:halt_on_error=1")
    monkeypatch.setattr(loader.shutil, "which", lambda name: "/usr/bin/clang")
    monkeypatch.setattr(loader.subprocess, "run", _fake_run)

    san_path = loader._ensure_compiled(tmp_path, ".so", sanitize=True)

    assert san_path == tmp_path / "_fastcore_asan.so"
    assert san_path.read_bytes() == b"sanitized-binary"
    assert prod_so.read_bytes() == b"production-binary"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "LD_PRELOAD" not in env
    assert "ASAN_OPTIONS" not in env


def test_build_sanitized_env_configures_platform_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supp = tmp_path / "lsan.supp"
    rt = tmp_path / "libclang_rt.asan.so"

    monkeypatch.setattr(run_sanitized_pytest.platform, "system", lambda: "Linux")
    linux_env = run_sanitized_pytest.build_sanitized_env(rt, supp, {"MOEX_PROOF_DISABLE_C": "1"})
    assert linux_env["MOEX_FASTCORE_SANITIZE"] == "1"
    assert "MOEX_PROOF_DISABLE_C" not in linux_env
    assert linux_env["LD_PRELOAD"] == str(rt)
    assert "detect_leaks=1" in linux_env["ASAN_OPTIONS"]
    assert linux_env["LSAN_OPTIONS"] == f"suppressions={supp}"

    monkeypatch.setattr(run_sanitized_pytest.platform, "system", lambda: "Darwin")
    mac_env = run_sanitized_pytest.build_sanitized_env(rt, supp, {})
    assert mac_env["MOEX_FASTCORE_SANITIZE"] == "1"
    assert mac_env["MOEX_FASTCORE_ASAN_RUNTIME"] == str(rt)
    assert mac_env["DYLD_INSERT_LIBRARIES"] == str(rt)
    assert "detect_leaks=0" in mac_env["ASAN_OPTIONS"]


@pytest.mark.parametrize("field", ["funding_rub", "funding_contracts"])
def test_sizing_propagates_funding_key_comparison_errors(field: str) -> None:
    class Collision:
        def __hash__(self) -> int:
            return hash(field)

        def __eq__(self, other: object) -> bool:
            raise MemoryError("funding lookup failed")

    fastcore = loader.load_fastcore()
    assert fastcore is not None
    row = {Collision(): 1}
    with pytest.raises(MemoryError, match="funding lookup failed"):
        fastcore.size_proof_metrics(
            [0],
            [1.0],
            [0.0],
            [1.0],
            [0.0],
            [True],
            [True],
            [True],
            [row],
            [],
            100.0,
            False,
            False,
            False,
            True,
        )
