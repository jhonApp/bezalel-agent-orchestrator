from __future__ import annotations

import asyncio
import re
from pathlib import Path

from schemas.models import CommandResult

from .command import run_command


class GitError(RuntimeError):
    pass


class GitManager:
    def __init__(self, root: Path):
        self.root = root.resolve()

    async def _git(self, *args: str, cwd: Path | None = None, timeout: int = 120) -> CommandResult:
        result = await run_command(["git", *args], cwd or self.root, timeout=timeout)
        if not result.ok:
            raise GitError(result.stderr.strip() or result.error or f"git {' '.join(args)} failed")
        return result

    async def current_branch(self) -> str:
        return (await self._git("branch", "--show-current")).stdout.strip()

    async def is_repo(self) -> bool:
        result = await run_command(["git", "rev-parse", "--is-inside-work-tree"], self.root, timeout=30)
        return result.ok and result.stdout.strip() == "true"

    async def diff(self, cwd: Path | None = None) -> str:
        return (await self._git("diff", "--stat", cwd=cwd)).stdout.strip()

    async def create_worktree(self, branch: str, path: Path) -> tuple[Path, str]:
        if not await self.is_repo():
            raise GitError(f"not a git repository: {self.root}")
        safe_branch = re.sub(r"[^A-Za-z0-9._/-]+", "-", branch).strip("-")
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._git("worktree", "add", "-b", safe_branch, str(path), "HEAD")
        return path, safe_branch

    async def commit(self, cwd: Path, message: str) -> str:
        if not await self.diff(cwd):
            return ""
        await self._git("add", "--all", cwd=cwd)
        await self._git("commit", "-m", message, cwd=cwd)
        return (await self._git("rev-parse", "HEAD", cwd=cwd)).stdout.strip()

    async def merge(self, branch: str, base_branch: str, cwd: Path | None = None) -> str:
        target = cwd or self.root
        await self._git("switch", base_branch, cwd=target)
        await self._git("merge", "--no-ff", branch, cwd=target)
        return (await self._git("rev-parse", "HEAD", cwd=target)).stdout.strip()

    async def branch(self, cwd: Path | None = None) -> str:
        return (await self._git("branch", "--show-current", cwd=cwd)).stdout.strip()
