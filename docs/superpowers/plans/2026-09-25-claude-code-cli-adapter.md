# Claude Code CLI Adapter (2 of 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A real `ClaudeCodeCLI` adapter implementing the `AgentCLIAdapter` protocol from
Spec 1, so any role can be configured with `"cli": "claude_code"` or
`"fallback_cli": "claude_code"` and it actually runs.

**Architecture:** One new file, `src/adapters/claude_code_cli.py`, spawning
`claude -p - --output-format json --safe-mode --permission-mode bypassPermissions
--strict-mcp-config [--json-schema <schema>]` as a subprocess and parsing its single
JSON response — no streaming, no self-reported-token correction, no text-scanning
fallback chain (all three needed by `CodexCLI` but not by this CLI, per the design's
empirical research). `agents/agent_cli.py`'s factory gains a second branch;
`orchestrator/config.py` gains the one new setting needed to point at it.

**Tech Stack:** Python 3.11+, `asyncio.create_subprocess_exec`, pytest/pytest-asyncio,
the project's real-subprocess fake-CLI testing pattern.

## Global Constraints

- No real `claude` CLI invocation in any automated test — it costs real money and
  requires a live login. Every test uses a fake `claude` script (a `sys.executable`
  -launched Python script), matching the project's established pattern for `codex`.
- The adapter must send `--safe-mode` on every invocation — without it, the calling
  session's own hooks/skills/CLAUDE.md leak into the dispatched sub-session (confirmed
  empirically in the design spec).
- `--permission-mode bypassPermissions` is required for non-interactive file edits to
  actually happen (confirmed empirically) — no other permission mode is used.
- A response with `is_error: true` and `api_error_status == 429` maps to
  `status="rate_limited"`; any other `is_error: true` maps to `status="failed"`. This
  429 mapping is explicitly flagged in the spec as unverified against a real rate-limit
  hit — implement it as specified, do not weaken or remove the flag/comment explaining
  why.
- `AgentResult.tokens_input`/`tokens_output`/`estimated_cost` must come from the
  response's `usage`/`total_cost_usd` fields, never from the model's own
  `structured_output` self-report (which contains the same unreliable
  self-reported-token fields Codex has) — the dict-literal key order in `_parse` that
  makes this true must not be changed.
- No role in `agents/registry.py` is wired to use `claude_code` by default — this plan
  only makes the adapter available, per the explicit brainstorming decision.

---

### Task 1: `ClaudeCodeCLI` adapter

**Files:**
- Create: `src/adapters/claude_code_cli.py`
- Modify: `src/orchestrator/config.py`
- Modify: `src/adapters/agent_cli.py:25-32` (`KNOWN_CLI_NAMES`, `build_cli`)
- Test: `tests/test_claude_code_cli.py`

**Interfaces:**
- Consumes: `AgentCLIAdapter` protocol (`src/adapters/agent_cli.py`, already built —
  exact signature: `execute(self, prompt, workdir, role, timeout=None, cancel_event=None,
  trace_metadata=None, on_event=None, reasoning_effort=None) -> AgentResult` and
  `execute_json(self, prompt, workdir, schema, label, timeout=120, reasoning_effort=None)
  -> dict[str, Any] | None`); `AGENT_SCHEMA` from `src/adapters/codex_cli.py` (already
  defined — reused as-is, not redefined).
- Produces: `ClaudeCodeCLI` class; `resolve_claude_code_command(configured: str) ->
  list[str]` (module-level function in the new file).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_code_cli.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from adapters.claude_code_cli import ClaudeCodeCLI, resolve_claude_code_command
from orchestrator.config import Settings


def settings_for(tmp_path: Path, command: str) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        claude_code_command=command,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def write_script(tmp_path: Path, name: str, source: str) -> str:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_resolve_claude_code_command_splits_a_quoted_path_with_spaces():
    command = resolve_claude_code_command('"C:\\Program Files\\Python\\python.exe" "C:\\my scripts\\claude.py"')

    assert command == ["C:\\Program Files\\Python\\python.exe", "C:\\my scripts\\claude.py"]


def test_resolve_claude_code_command_defaults_to_bare_claude():
    assert resolve_claude_code_command("claude") == ["claude"]


FAKE_CLAUDE_SUCCESS = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "{\"status\": \"completed\", \"summary\": \"ok\"}",
    "total_cost_usd": 0.0537833,
    "usage": {"input_tokens": 4, "output_tokens": 145},
    "structured_output": {
        "status": "completed", "summary": "ok", "files_changed": [], "tests": [],
        "contracts_changed": [], "errors": [], "next_action": None,
        "tokens_input": 0, "tokens_output": 0, "quality": None,
    },
}))
sys.exit(0)
'''

FAKE_CLAUDE_404_ERROR = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 404,
    "result": "There's an issue with the selected model. It may not exist or you may not have access to it.",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
}))
sys.exit(1)
'''

FAKE_CLAUDE_429_ERROR = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 429,
    "result": "Rate limit exceeded. Please try again later.",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
}))
sys.exit(1)
'''

FAKE_CLAUDE_MALFORMED = r'''
import sys
print("not valid json at all")
sys.exit(1)
'''

FAKE_CLAUDE_HANGS = r'''
import time
time.sleep(30)
'''


@pytest.mark.asyncio
async def test_execute_populates_agent_result_from_the_real_usage_not_the_models_self_report(tmp_path: Path):
    """AGENT_SCHEMA requires the model to self-report tokens_input/tokens_output inside
    structured_output too - this proves the real usage-derived values win, exactly like
    the equivalent Codex fix earlier this session."""
    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "completed"
    assert result.summary == "ok"
    assert result.tokens_input == 4
    assert result.tokens_output == 145
    assert result.estimated_cost == pytest.approx(0.0537833)


@pytest.mark.asyncio
async def test_execute_maps_a_404_style_error_to_failed(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_404.py", FAKE_CLAUDE_404_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert "issue with the selected model" in result.summary
    assert result.errors == [result.summary]


@pytest.mark.asyncio
async def test_execute_maps_a_429_style_error_to_rate_limited(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_429.py", FAKE_CLAUDE_429_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "rate_limited"
    assert "Rate limit exceeded" in result.summary


@pytest.mark.asyncio
async def test_execute_fails_cleanly_on_malformed_output(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_malformed.py", FAKE_CLAUDE_MALFORMED)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert result.errors == ["structured output could not be parsed"]


@pytest.mark.asyncio
async def test_execute_kills_the_process_and_returns_failed_on_timeout(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend", timeout=1)

    assert result.status == "failed"
    assert result.errors == ["agent timeout"]


@pytest.mark.asyncio
async def test_execute_kills_the_process_and_returns_blocked_on_cancellation(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.3)
        cancel_event.set()

    result, _ = await asyncio.gather(
        ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend", timeout=30, cancel_event=cancel_event),
        cancel_soon(),
    )

    assert result.status == "blocked"
    assert result.errors == ["execution cancelled"]


@pytest.mark.asyncio
async def test_execute_json_returns_structured_output_on_success(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result == {
        "status": "completed", "summary": "ok", "files_changed": [], "tests": [],
        "contracts_changed": [], "errors": [], "next_action": None,
        "tokens_input": 0, "tokens_output": 0, "quality": None,
    }


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_error(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_404.py", FAKE_CLAUDE_404_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result is None


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_timeout(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier", timeout=1)

    assert result is None


def test_build_cli_returns_a_claude_code_cli_for_the_claude_code_name(tmp_path: Path):
    from adapters.agent_cli import KNOWN_CLI_NAMES, build_cli

    settings = settings_for(tmp_path, "claude")

    adapter = build_cli("claude_code", settings)

    assert isinstance(adapter, ClaudeCodeCLI)
    assert "claude_code" in KNOWN_CLI_NAMES
    assert "codex" in KNOWN_CLI_NAMES
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_claude_code_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'adapters.claude_code_cli'`

- [ ] **Step 3: Add the `claude_code_command` setting**

In `src/orchestrator/config.py`, find:

```python
    codex_command: str = "codex"
```

Replace with:

```python
    codex_command: str = "codex"
    claude_code_command: str = "claude"
```

Find:

```python
            codex_command=os.getenv("CODEX_COMMAND", "codex"),
```

Replace with:

```python
            codex_command=os.getenv("CODEX_COMMAND", "codex"),
            claude_code_command=os.getenv("CLAUDE_CODE_COMMAND", "claude"),
```

- [ ] **Step 4: Implement the adapter**

```python
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
```

- [ ] **Step 5: Wire it into the factory**

In `src/adapters/agent_cli.py`, find:

```python
KNOWN_CLI_NAMES: set[str] = {"codex"}


def build_cli(name: str, settings: Settings) -> AgentCLIAdapter:
    if name == "codex":
        from adapters.codex_cli import CodexCLI
        return CodexCLI(settings)
    raise ValueError(f"unknown agent CLI '{name}' — must be one of {sorted(KNOWN_CLI_NAMES)}")
```

Replace with:

```python
KNOWN_CLI_NAMES: set[str] = {"codex", "claude_code"}


def build_cli(name: str, settings: Settings) -> AgentCLIAdapter:
    if name == "codex":
        from adapters.codex_cli import CodexCLI
        return CodexCLI(settings)
    if name == "claude_code":
        from adapters.claude_code_cli import ClaudeCodeCLI
        return ClaudeCodeCLI(settings)
    raise ValueError(f"unknown agent CLI '{name}' — must be one of {sorted(KNOWN_CLI_NAMES)}")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_claude_code_cli.py -v`
Expected: PASS (12 tests)

Also re-run Task 1's existing sibling test file from Spec 1, since `KNOWN_CLI_NAMES` changed:

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli.py -v`
Expected: PASS (9 tests) — `test_known_cli_names_contains_codex` only asserts `"codex" in
KNOWN_CLI_NAMES`, which is still true; no existing test asserts the set's exact size, so
adding `"claude_code"` does not break it.

- [ ] **Step 7: Commit**

```bash
git add src/adapters/claude_code_cli.py src/adapters/agent_cli.py src/orchestrator/config.py tests/test_claude_code_cli.py
git commit -m "feat: add a real ClaudeCodeCLI adapter"
```

---

### Task 2: End-to-end `run_task` dispatch test

**Files:**
- Test: `tests/test_claude_code_cli.py` (append)

**Interfaces:**
- Consumes: `ExecutionRuntime` (`src/orchestrator/nodes.py`, from Spec 1 — exact
  signature: `ExecutionRuntime(settings, store=None, clis: dict[str, AgentCLIAdapter] |
  None = None, event_sink=None)`); `ClaudeCodeCLI` (Task 1); `AGENT_ROLES` (`src/agents/registry.py`).

This task proves the whole Spec-1 dispatch path — not just `ClaudeCodeCLI` in
isolation — actually resolves and runs a non-Codex adapter end to end, the same way
`tests/test_agent_cli_fallback.py` proved the fallback mechanics using only `CodexCLI`
pointed at different fake scripts.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claude_code_cli.py`:

```python
@pytest.mark.asyncio
async def test_run_task_dispatches_to_a_real_claude_code_adapter_when_a_role_is_configured_for_it(tmp_path: Path, monkeypatch):
    """Proves the Spec-1 dispatch path (ExecutionRuntime.run_task resolving
    AGENT_ROLES[role]["cli"] through self.clis) actually reaches a real ClaudeCodeCLI,
    not just that ClaudeCodeCLI works when called directly."""
    from agents.registry import AGENT_ROLES
    from orchestrator.nodes import ExecutionRuntime

    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    claude_settings = settings_for(tmp_path, command)
    codex_settings = settings_for(tmp_path, "codex")  # never invoked; satisfies validate_agent_roles for the other 7 roles
    clis = {"codex": ClaudeCodeCLI(codex_settings), "claude_code": ClaudeCodeCLI(claude_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "claude_code")
    runtime = ExecutionRuntime(claude_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.cli_used == "claude_code"
    assert result.tokens_input == 4
```

Note: the `"codex"` entry above is deliberately also a `ClaudeCodeCLI` instance (never a
real `CodexCLI`) so this test needs no `codex` fake script at all — it is never
dispatched to (`frontend` is patched to `"claude_code"`), it exists purely to satisfy
`validate_agent_roles` for the other 7 roles that still default to `cli="codex"`,
exactly like the inert `"codex"` scaffolding entries in `tests/test_agent_cli_fallback.py`
from Spec 1.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_claude_code_cli.py -v -k dispatches_to_a_real_claude_code`
Expected: FAIL if any import or fixture is wrong; otherwise this test requires no new
production code (Task 1 already wired everything) — if it fails for a reason other than
a typo in the test itself, stop and report rather than adding production code here that
duplicates Task 1's work.

- [ ] **Step 3: Run it again to confirm it passes**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_claude_code_cli.py -v`
Expected: PASS (13 tests total)

- [ ] **Step 4: Run the full project test suite**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" -q`
Expected: All tests pass — 203 tests from before this plan, plus 13 new
(`tests/test_claude_code_cli.py`) = 216, zero failures.

- [ ] **Step 5: Commit**

```bash
git add tests/test_claude_code_cli.py
git commit -m "test: prove run_task dispatches to a real ClaudeCodeCLI end to end"
```
