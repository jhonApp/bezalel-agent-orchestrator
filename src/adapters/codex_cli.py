from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from orchestrator.config import Settings
from observability.langsmith import LangSmithObserver
from schemas.models import AgentResult, CommandResult

from .command import run_command, which


AGENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "failed", "blocked", "skipped"]},
        "summary": {"type": "string"}, "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["name", "status", "details"],
            },
        },
        "contracts_changed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "resource": {"type": "string"},
                    "change": {"type": "string"},
                    "breaking": {"type": "boolean"},
                },
                "required": ["resource", "change", "breaking"],
            },
        },
        "errors": {"type": "array", "items": {"type": "string"}}, "next_action": {"type": ["string", "null"]},
        "tokens_input": {"type": "integer"}, "tokens_output": {"type": "integer"},
        "quality": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "relevante": {"type": "integer", "minimum": 0, "maximum": 100},
                "fonte_utilizada": {"type": "integer", "minimum": 0, "maximum": 100},
                "alucinacao": {"type": "integer", "minimum": 0, "maximum": 100},
                "cumprimento_regras": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["relevante", "fonte_utilizada", "alucinacao", "cumprimento_regras"],
        },
    },
    "required": [
        "status", "summary", "files_changed", "tests", "contracts_changed",
        "errors", "next_action", "tokens_input", "tokens_output", "quality",
    ],
}

CLASSIFIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "frontend": {"type": "boolean"},
        "backend": {"type": "boolean"},
        "python": {"type": "boolean"},
    },
    "required": ["frontend", "backend", "python"],
}


def resolve_codex_command(configured: str) -> list[str]:
    parts = shlex.split(configured, posix=os.name != "nt") or ["codex"]
    # ``posix=False`` preserves Windows quoting, but subprocess expects the
    # executable/path without the surrounding quotes.
    if os.name == "nt":
        parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts]
    if len(parts) == 1 and parts[0].lower() == "codex" and os.name == "nt":
        candidate = which("codex.cmd")
        if candidate:
            return [candidate]
    return parts


class CodexCLI:
    """Controlled, version-aware adapter for `codex exec`.

    Codex CLI 0.153.4 exposes --json, --output-schema, --output-last-message,
    --cd, --skip-git-repo-check and --ephemeral. The adapter only uses these
    *confirmed* flags. It does not assume a custom-agent flag exists; role
    instructions are supplied in the prompt for portability across CLI versions.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.command = resolve_codex_command(settings.codex_command)
        self.version: str | None = None
        self.help_output: str = ""
        self.tracing = LangSmithObserver(settings)

    async def check(self) -> dict[str, Any]:
        version = await run_command(self.command + ["--version"], self.settings.orchestrator_root, timeout=30)
        help_result = await run_command(self.command + ["exec", "--help"], self.settings.orchestrator_root, timeout=30)
        self.version = (version.stdout or version.stderr).strip().splitlines()[-1] if (version.stdout or version.stderr) else None
        self.help_output = help_result.stdout + help_result.stderr
        return {"version": self.version, "command": self.command, "version_ok": version.ok, "exec_help_ok": help_result.ok,
                "supported_flags": [flag for flag in ("--json", "--output-schema", "--output-last-message", "--cd", "--ephemeral", "--skip-git-repo-check") if flag in self.help_output],
                "errors": [x for x in (version.stderr, help_result.stderr) if x]}

    async def execute(self, prompt: str, workdir: Path, role: str, timeout: int | None = None,
                      cancel_event: asyncio.Event | None = None,
                      trace_metadata: dict[str, Any] | None = None,
                      on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> AgentResult:
        timeout = timeout or self.settings.agent_timeout_seconds
        started = time.perf_counter()
        workdir = workdir.resolve()
        with tempfile.TemporaryDirectory(prefix="bezalel-codex-") as temp:
            temp_path = Path(temp)
            schema_path = temp_path / "agent-result.schema.json"
            output_path = temp_path / "last-message.txt"
            schema_path.write_text(json.dumps(AGENT_SCHEMA), encoding="utf-8")
            full_prompt = f"""You are the {role} agent in the Bezalel orchestrator.\n\n{prompt}\n\nReturn only a JSON object matching the supplied schema in your final response. Never include secrets or private model reasoning.\n"""
            command = self._exec_command(workdir, schema_path, output_path)
            try:
                metadata = {"agent": role, "workdir": str(workdir), **(trace_metadata or {})}
                with self.tracing.codex_session("orchestrator.codex_task", {"role": role, "prompt": prompt}, metadata) as environment:
                    process = await asyncio.create_subprocess_exec(*command, cwd=str(workdir), env=environment,
                                                                    stdin=asyncio.subprocess.PIPE,
                                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                                    limit=self.settings.codex_stream_limit)

                    async def write_stdin() -> None:
                        try:
                            process.stdin.write(full_prompt.encode("utf-8"))
                            await process.stdin.drain()
                        finally:
                            process.stdin.close()

                    stdout_buffer: list[bytes] = []
                    stderr_buffer: list[bytes] = []
                    stdin_task = asyncio.create_task(write_stdin())
                    stdout_task = asyncio.create_task(
                        self._pump(process.stdout, "stdout", stdout_buffer, on_event))
                    stderr_task = asyncio.create_task(
                        self._pump(process.stderr, "stderr", stderr_buffer, on_event))
                    wait_task = asyncio.create_task(process.wait())
                    combined = asyncio.gather(stdin_task, stdout_task, stderr_task, wait_task)
                    deadline = time.monotonic() + timeout
                    while not combined.done():
                        if cancel_event and cancel_event.is_set():
                            process.kill()
                            await combined
                            return AgentResult(agent=role, status="blocked", summary="cancelled", errors=["execution cancelled"],
                                               duration_seconds=time.perf_counter() - started)
                        if time.monotonic() >= deadline:
                            process.kill()
                            await combined
                            return AgentResult(agent=role, status="failed", summary="Codex timeout", errors=["agent timeout"],
                                               duration_seconds=time.perf_counter() - started)
                        await asyncio.sleep(0.2)
                    await combined
                    stdout = b"".join(stdout_buffer)
                    stderr = b"".join(stderr_buffer)
            except OSError as exc:
                return AgentResult(agent=role, status="failed", summary="Codex CLI unavailable", errors=[str(exc)],
                                   duration_seconds=time.perf_counter() - started)
            raw = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            response_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else raw
            parsed = self._parse_response(response_text, raw)
            parsed.agent = role
            parsed.duration_seconds = time.perf_counter() - started
            if process.returncode != 0 and parsed.status == "completed":
                parsed.status = "failed"
            if err and (process.returncode != 0 or parsed.status == "failed"):
                parsed.errors.append(self._redact(err[-2000:]))
            diagnostic_output = response_text or raw or err
            parsed.raw_response = self._redact(diagnostic_output[-self.settings.max_output_chars:])
            return parsed

    async def execute_json(self, prompt: str, workdir: Path, schema: dict[str, Any], label: str,
                           timeout: int = 120) -> dict[str, Any] | None:
        """Run one short, read-only Codex turn constrained to `schema` and return the raw
        parsed JSON — for callers that don't need (or don't fit) the AgentResult contract,
        like the domain-relevance classifier. Returns None on any failure; the caller
        decides the fallback.
        """
        workdir = workdir.resolve()
        with tempfile.TemporaryDirectory(prefix="bezalel-codex-") as temp:
            temp_path = Path(temp)
            schema_path = temp_path / "result.schema.json"
            output_path = temp_path / "last-message.txt"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            full_prompt = f"You are the {label} in the Bezalel orchestrator.\n\n{prompt}\n\nReturn only a JSON object matching the supplied schema in your final response.\n"
            command = self._exec_command(workdir, schema_path, output_path)
            try:
                process = await asyncio.create_subprocess_exec(*command, cwd=str(workdir),
                                                                stdin=asyncio.subprocess.PIPE,
                                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                                limit=self.settings.codex_stream_limit)
                try:
                    await asyncio.wait_for(process.communicate(full_prompt.encode("utf-8")), timeout=timeout)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return None
            except OSError:
                return None
            if process.returncode != 0 or not output_path.exists():
                return None
            try:
                return json.loads(output_path.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                return None

    async def _pump(self, stream: asyncio.StreamReader, label: str, buffer: list[bytes],
                    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None) -> None:
        """Drain a subprocess stream line-by-line, forwarding each line as it arrives.

        Reading incrementally (instead of ``process.communicate()``) is what makes
        real-time progress possible: the caller learns about a line the moment Codex
        writes it, not after the whole ``codex exec`` session finishes.
        """
        while True:
            try:
                line = await stream.readline()
            except ValueError as exc:
                # readline() raises plain ValueError (wrapping its own internal
                # LimitOverrunError) when no separator turns up within the stream's buffer
                # limit — a single oversized Codex --json line (a tool result embedding
                # several files' content, say) must not take down the whole agent task.
                # asyncio has already drained/cleared its internal buffer by this point, so
                # the pump can just note the truncation and keep reading subsequent lines.
                note = f"[stream line skipped: {exc}]".encode()
                buffer.append(note + b"\n")
                if on_event is not None:
                    try:
                        await on_event({"stream": label, "parsed": None, "raw": self._redact(note.decode())})
                    except Exception:
                        pass
                continue
            if not line:
                break
            buffer.append(line)
            if on_event is None:
                continue
            text = self._redact(line.decode("utf-8", errors="replace").rstrip("\r\n"))
            parsed: dict[str, Any] | None = None
            stripped = text.strip()
            if stripped.startswith("{"):
                try:
                    candidate = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    candidate = None
                if isinstance(candidate, dict):
                    parsed = candidate
            try:
                await on_event({"stream": label, "parsed": parsed, "raw": text})
            except Exception:
                pass

    def _exec_command(self, workdir: Path, schema_path: Path, output_path: Path) -> list[str]:
        """Build an invocation compatible with the installed non-interactive CLI.

        Recent Codex CLI versions removed ``--ask-for-approval`` from ``exec``.
        The equivalent non-interactive policy is still available as a config
        override, so keep it as one argv item and let Codex parse the TOML value.
        """
        approval = json.dumps(self.settings.codex_approval_policy)
        return self.command + [
            "exec", "--json", "--ephemeral", "--cd", str(workdir),
            "--output-schema", str(schema_path), "--output-last-message", str(output_path),
            "--skip-git-repo-check", "--ignore-user-config", "-c", f"approval_policy={approval}",
            "-c", "features.plugin_hooks=true",
            "-c", 'plugins."tracing@langsmith-codex-plugins".enabled=true',
            "-s", self.settings.codex_sandbox, "-",
        ]

    _USAGE_LIMIT_MARKERS = ("usage limit", "limite de uso", "upgrade to pro", "purchase more credits")

    @staticmethod
    def _extract_turn_failure_message(text: str) -> str | None:
        """Codex can error out before ever emitting a schema-conforming result (hitting its own
        usage limit, a provider-side network error) — pull the human-readable reason out of its
        raw JSONL ``error``/``turn.failed`` events so the caller isn't stuck with a generic
        "no valid structured result" summary that hides what actually happened.
        """
        message: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(candidate, dict):
                continue
            if candidate.get("type") == "error" and candidate.get("message"):
                message = candidate["message"]
            elif candidate.get("type") == "turn.failed":
                error = candidate.get("error")
                if isinstance(error, dict) and error.get("message"):
                    message = error["message"]
        return message

    @classmethod
    def _is_usage_limit_message(cls, message: str) -> bool:
        lowered = message.lower()
        return any(marker in lowered for marker in cls._USAGE_LIMIT_MARKERS)

    @classmethod
    def _parse_response(cls, text: str, raw: str) -> AgentResult:
        candidates = [text.strip()]
        candidates += [line.strip() for line in raw.splitlines() if line.strip().startswith("{")]
        candidates += re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.S)
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return AgentResult.model_validate({"agent": "unknown", **value})
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        failure_message = cls._extract_turn_failure_message(raw) or cls._extract_turn_failure_message(text)
        if failure_message:
            if cls._is_usage_limit_message(failure_message):
                return AgentResult(agent="unknown", status="rate_limited", summary=failure_message, errors=[failure_message])
            return AgentResult(agent="unknown", status="failed", summary=f"Codex turn failed: {failure_message}", errors=[failure_message])
        return AgentResult(agent="unknown", status="failed", summary="Codex returned no valid structured result",
                           errors=["structured output could not be parsed"])

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
        value = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "[REDACTED PRIVATE KEY]", value, flags=re.S)
        return value
