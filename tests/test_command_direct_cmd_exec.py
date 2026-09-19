from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from adapters.command import run_command


WRITE_ARGV_BAT = "@echo off\r\necho %*> argv.txt\r\n"


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe wrapping only applies on Windows")
@pytest.mark.asyncio
async def test_direct_cmd_exec_runs_a_bat_file_without_cmd_exe_wrapping(tmp_path: Path) -> None:
    script = tmp_path / "fake-tool.bat"
    script.write_text(WRITE_ARGV_BAT, encoding="utf-8")

    result = await run_command([str(script), "list", "--json"], tmp_path, direct_cmd_exec=True)

    assert result.ok, result.stderr
    assert (tmp_path / "argv.txt").read_text(encoding="utf-8").strip() == "list --json"


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe wrapping only applies on Windows")
@pytest.mark.asyncio
async def test_direct_cmd_exec_resolves_a_bare_name_through_path(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "fake-tool.bat"
    script.write_text(WRITE_ARGV_BAT, encoding="utf-8")
    # shutil.which() (used for resolution) reads the real process PATH, not a subprocess env override.
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))

    result = await run_command(["fake-tool", "hello"], tmp_path, direct_cmd_exec=True)

    assert result.ok, result.stderr
    assert (tmp_path / "argv.txt").read_text(encoding="utf-8").strip() == "hello"
