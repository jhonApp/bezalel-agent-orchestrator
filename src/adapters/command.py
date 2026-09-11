from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

from schemas.models import CommandResult


async def run_command(command: list[str], cwd: Path, timeout: int = 900, env: dict[str, str] | None = None) -> CommandResult:
    started = time.perf_counter()
    try:
        executable = command
        if os.name == "nt" and command:
            resolved = shutil.which(command[0]) or command[0]
            if resolved.lower().endswith((".cmd", ".bat")):
                executable = ["cmd.exe", "/d", "/c", resolved, *command[1:]]
        process = await asyncio.create_subprocess_exec(
            *executable, cwd=str(cwd), env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return CommandResult(command=command, cwd=str(cwd), returncode=process.returncode,
                                 stdout="", stderr="command timed out", duration_seconds=time.perf_counter() - started,
                                 timed_out=True)
        return CommandResult(command=command, cwd=str(cwd), returncode=process.returncode,
                             stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"),
                             duration_seconds=time.perf_counter() - started)
    except (OSError, ValueError) as exc:
        return CommandResult(command=command, cwd=str(cwd), error=str(exc), duration_seconds=time.perf_counter() - started)


def which(command: str) -> str | None:
    return shutil.which(command)
