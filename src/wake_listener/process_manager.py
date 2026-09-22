from __future__ import annotations

import asyncio
import subprocess
import time

import httpx


class ProcessManager:
    """Spawns the real orchestrator backend on demand, waits for it to become
    healthy, and stops only the process it itself spawned — never a process
    that was already running before this manager touched it, and never by
    process name (see the project's earlier incident: a broad `taskkill` by
    image name once killed an unrelated in-progress execution).
    """

    def __init__(self, start_command: list[str], health_url: str,
                health_timeout_seconds: float = 30.0, health_poll_interval_seconds: float = 0.5,
                cwd: str | None = None) -> None:
        self.start_command = start_command
        self.health_url = health_url
        self.health_timeout_seconds = health_timeout_seconds
        self.health_poll_interval_seconds = health_poll_interval_seconds
        self.cwd = cwd
        self._process: subprocess.Popen | None = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def ensure_awake(self) -> None:
        if self.is_running():
            return
        if await self._is_healthy():
            return
        self._process = subprocess.Popen(self.start_command, cwd=self.cwd)
        try:
            await self._wait_healthy()
        except TimeoutError:
            self.stop_if_idle()
            raise

    async def _is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(self.health_url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.health_timeout_seconds
        while time.monotonic() < deadline:
            if await self._is_healthy():
                return
            await asyncio.sleep(self.health_poll_interval_seconds)
        raise TimeoutError(f"backend did not become healthy within {self.health_timeout_seconds}s")

    def stop_if_idle(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            self._process = None
            return False
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None
        return True
