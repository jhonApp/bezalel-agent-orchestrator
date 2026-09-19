from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .command import run_command

DEFAULT_SKILLS_COMMAND = ["npx", "--yes", "skills"]


async def list_skills(project_path: Path, command: list[str] | None = None, timeout: int = 45) -> dict[str, Any]:
    """List skill packages installed in a project via the `skills` CLI (skills.sh)."""
    result = await run_command((command or DEFAULT_SKILLS_COMMAND) + ["list", "--json"], project_path, timeout=timeout,
                               direct_cmd_exec=command is None)
    if not result.ok:
        return {"skills": [], "error": (result.error or result.stderr or "skills command failed").strip()[:2000]}
    try:
        return {"skills": json.loads(result.stdout), "error": None}
    except (json.JSONDecodeError, ValueError):
        return {"skills": [], "error": "could not parse skills output: " + result.stdout[:500]}


async def install_skill(project_path: Path, package: str, skill: str | None = None,
                        command: list[str] | None = None, timeout: int = 180) -> dict[str, Any]:
    """Install a skill package into a project via `skills add <package> --skill <skill>`."""
    args = (command or DEFAULT_SKILLS_COMMAND) + ["add", package, "-y", "--json"]
    if skill:
        args += ["--skill", skill]
    result = await run_command(args, project_path, timeout=timeout, direct_cmd_exec=command is None)
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    payload: dict[str, Any] = {"ok": result.ok, "result": parsed}
    if not result.ok:
        payload["error"] = (result.error or result.stderr or "skills add failed").strip()[:2000]
    return payload


async def remove_skill(project_path: Path, skill: str, command: list[str] | None = None, timeout: int = 60) -> dict[str, Any]:
    """Remove an installed skill via `skills remove --skill <skill>`.

    Unlike `list`/`add`, the `remove` subcommand has no `--json` output mode, so the
    result carries the raw text output instead of a parsed structure.
    """
    args = (command or DEFAULT_SKILLS_COMMAND) + ["remove", "--skill", skill, "-y"]
    result = await run_command(args, project_path, timeout=timeout, direct_cmd_exec=command is None)
    payload: dict[str, Any] = {"ok": result.ok, "output": (result.stdout or result.stderr or "").strip()[:2000]}
    if not result.ok:
        payload["error"] = (result.error or result.stderr or "skills remove failed").strip()[:2000]
    return payload
