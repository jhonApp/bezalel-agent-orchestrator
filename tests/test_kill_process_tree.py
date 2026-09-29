from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from adapters.command import kill_process_tree


PARENT_SCRIPT = """
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(300)
"""

CHILD_SCRIPT = """
import sys
import time

path = sys.argv[1]
while True:
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    time.sleep(0.05)
"""


@pytest.mark.asyncio
async def test_kill_process_tree_terminates_a_grandchild_process(tmp_path: Path) -> None:
    """A plain process.kill() only terminates the one PID asyncio tracks — on Windows,
    Codex/Claude Code CLI run through a .cmd shim that spawns a real node.exe process
    (and that process may spawn further MCP-server children), none of which die when
    only the top PID is killed. This reproduces that shape directly: a parent process
    that spawns its own child and outlives it, without asyncio ever seeing the child."""
    child_script = tmp_path / "child.py"
    child_script.write_text(CHILD_SCRIPT, encoding="utf-8")
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(PARENT_SCRIPT, encoding="utf-8")
    heartbeat = tmp_path / "heartbeat.txt"

    process = await asyncio.create_subprocess_exec(
        sys.executable, str(parent_script), str(child_script), str(heartbeat), cwd=str(tmp_path),
    )
    try:
        deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert heartbeat.exists(), "child process never started heartbeating"

        await kill_process_tree(process)
        await process.wait()

        last_seen = heartbeat.read_text(encoding="utf-8")
        await asyncio.sleep(0.5)
        assert heartbeat.read_text(encoding="utf-8") == last_seen, (
            "grandchild process kept running (and writing) after the parent was killed"
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
