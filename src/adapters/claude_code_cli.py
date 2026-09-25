# src/adapters/claude_code_cli.py
from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from adapters.codex_cli import AGENT_SCHEMA
from adapters.command import which
from orchestrator.config import Settings
from schemas.models import AgentResult

_RATE_LIMIT_API_STATUS = 429


def resolve_claude_code_command(configured: str) -> list[str]:
    """Mirrors adapters.codex_cli.resolve_codex_command's Windows-safe quoting: a
    configured command string (e.g. a quoted python.exe path plus a quoted script path,
    both of which may contain spaces) must be split respecting quotes, not naively on
    whitespace.
    """
    parts = shlex.split(configured, posix=os.name != "nt") or ["claude"]
    if os.name == "nt":
        parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts]
        if len(parts) == 1 and parts[0].lower() == "claude":
            candidate = which("claude.cmd")
            if candidate:
                return [candidate]
    return parts


class ClaudeCodeCLI:
    """Second AgentCLIAdapter implementation, alongside CodexCLI. See
    docs/superpowers/specs/2026-09-25-claude-code-cli-adapter-design.md for the live CLI
    behavior this is built against (v2.1.214) — a single JSON object on stdout (not a
    JSONL stream), a pre-parsed `structured_output` field, real `usage`/`total_cost_usd`
    from the first call. --safe-mode is required: without it, the CALLING session's own
    hooks/skills/CLAUDE.md leak into the dispatched sub-session (confirmed empirically).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.command = resolve_claude_code_command(settings.claude_code_command)

    def _base_command(self, schema: dict[str, Any] | None) -> list[str]:
        command = self.command + [
            "-p", "-",
            "--output-format", "json",
            "--safe-mode",
            "--permission-mode", "bypassPermissions",
            "--strict-mcp-config",
        ]
        if schema is not None:
            command += ["--json-schema", json.dumps(schema)]
        return command

    @staticmethod
    def _model_for_effort(reasoning_effort: str) -> str:
        # Claude Code has no reasoning-effort dial the way Codex does — the nearest
        # equivalent for a judgment-only gate role is a cheaper model tier.
        return {"low": "haiku"}.get(reasoning_effort, reasoning_effort)

    async def execute(self, prompt: str, workdir: Path, role: str, timeout: int | None = None,
                      cancel_event: asyncio.Event | None = None,
                      trace_metadata: dict[str, Any] | None = None,
                      on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                      reasoning_effort: str | None = None) -> AgentResult:
        timeout = timeout or self.settings.agent_timeout_seconds
        started = time.perf_counter()
        command = self._base_command(AGENT_SCHEMA)
        if reasoning_effort:
            command += ["--model", self._model_for_effort(reasoning_effort)]
        full_prompt = (f"You are the {role} agent in the Bezalel orchestrator.\n\n{prompt}\n\n"
                       "Return only a JSON object matching the supplied schema in your final "
                       "response. Never include secrets or private model reasoning.\n")
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(workdir.resolve()),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        communicate_task = asyncio.create_task(process.communicate(full_prompt.encode("utf-8")))
        deadline = time.monotonic() + timeout
        while not communicate_task.done():
            if cancel_event and cancel_event.is_set():
                process.kill()
                await communicate_task
                return AgentResult(agent=role, status="blocked", summary="cancelled", errors=["execution cancelled"],
                                   duration_seconds=time.perf_counter() - started)
            if time.monotonic() >= deadline:
                process.kill()
                await communicate_task
                return AgentResult(agent=role, status="failed", summary="Claude Code timeout", errors=["agent timeout"],
                                   duration_seconds=time.perf_counter() - started)
            await asyncio.sleep(0.2)
        stdout, _stderr = communicate_task.result()
        result = self._parse(stdout.decode(errors="replace"), role)
        result.duration_seconds = time.perf_counter() - started
        return result

    def _parse(self, raw: str, role: str) -> AgentResult:
        raw = raw.strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return AgentResult(agent=role, status="failed", summary="Claude Code returned no valid JSON",
                               errors=["structured output could not be parsed"])
        if data.get("is_error"):
            message = data.get("result") or "Claude Code reported an error"
            status = "rate_limited" if data.get("api_error_status") == _RATE_LIMIT_API_STATUS else "failed"
            return AgentResult(agent=role, status=status, summary=message, errors=[message])
        structured = data.get("structured_output") or {}
        usage = data.get("usage", {})
        # total_cost_usd is Claude Code's own computed API-equivalent cost — it may not
        # equal what an OAuth subscription plan actually bills; used as-is since it's the
        # most accurate figure this CLI reports, with no per-token rate to reconstruct.
        #
        # Key order matters here: AGENT_SCHEMA requires the model to self-report
        # tokens_input/tokens_output inside `structured` too (same schema Codex uses), and
        # that self-report is exactly as unreliable here as it was for Codex (this
        # project's own token-usage bug, fixed earlier this session, was caused by trusting
        # a model's self-reported usage instead of the CLI's real figure). Spreading
        # `structured` FIRST and placing the real `usage`-derived keys AFTER it means the
        # real values win the dict-literal's later-key-wins rule — never reorder this.
        return AgentResult.model_validate({
            "agent": role,
            **structured,
            "tokens_input": usage.get("input_tokens", 0),
            "tokens_output": usage.get("output_tokens", 0),
            "estimated_cost": data.get("total_cost_usd", 0.0),
        })

    async def execute_json(self, prompt: str, workdir: Path, schema: dict[str, Any], label: str,
                           timeout: int = 120, reasoning_effort: str | None = None) -> dict[str, Any] | None:
        command = self._base_command(schema)
        if reasoning_effort:
            command += ["--model", self._model_for_effort(reasoning_effort)]
        full_prompt = (f"You are the {label} in the Bezalel orchestrator.\n\n{prompt}\n\n"
                       "Return only a JSON object matching the supplied schema in your final response.\n")
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(workdir.resolve()),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(full_prompt.encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None
        try:
            data = json.loads(stdout.decode(errors="replace").strip())
        except json.JSONDecodeError:
            return None
        if data.get("is_error"):
            return None
        return data.get("structured_output")
