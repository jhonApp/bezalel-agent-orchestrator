from __future__ import annotations

from pathlib import Path
from typing import Any

from .command import run_command


async def push_branch(cwd: Path, branch: str, timeout: int = 120, command: list[str] | None = None) -> dict[str, Any]:
    """Push an agent's worktree branch to origin so a pull request can be opened against it."""
    result = await run_command((command or ["git"]) + ["push", "-u", "origin", branch], cwd, timeout=timeout)
    if not result.ok:
        return {"ok": False, "error": (result.stderr or result.error or "git push failed").strip()[:2000]}
    return {"ok": True}


async def create_pull_request(cwd: Path, base: str, head: str, title: str, body: str,
                              timeout: int = 120, command: list[str] | None = None) -> dict[str, Any]:
    """Open a PR via `gh pr create`, which prints the created PR's URL on stdout on success."""
    result = await run_command(
        (command or ["gh"]) + ["pr", "create", "--base", base, "--head", head, "--title", title, "--body", body],
        cwd, timeout=timeout,
    )
    if not result.ok:
        return {"ok": False, "error": (result.stderr or result.error or "gh pr create failed").strip()[:2000]}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"ok": True, "url": lines[-1] if lines else ""}
