# Claude Code CLI Adapter (Spec 2 of 2) — Design

## Problem

Spec 1 (merged: `docs/superpowers/specs/2026-09-25-cli-agnostic-agent-execution-design.md`)
built the `AgentCLIAdapter` protocol, per-role `cli`/`fallback_cli` config, and a
single-attempt fallback in `run_task` — validated against `CodexCLI` as the only real
adapter. It exists to solve a real outage this session: Codex's own usage limit stopped
every in-flight agent at once. That problem isn't actually fixed until a second real CLI
exists to fall back to.

## Goal

A real `ClaudeCodeCLI` adapter implementing `AgentCLIAdapter`, so any role can set
`"cli": "claude_code"` or `"fallback_cli": "claude_code"` and it actually runs — no role
defaults to it (see Decisions below); it becomes an available, opt-in second option.

## Empirical research (done live, 2026-09-25, Claude Code CLI v2.1.214)

Building this without running the real CLI would repeat the exact mistake Spec 1 was
designed to avoid (guessing at Codex's token-usage shape instead of reading its real
session logs). Five real, minimal, paid invocations were made and read directly:

1. `claude -p "reply with exactly the word: pong" --output-format json` — one JSON
   object on stdout (not a JSONL stream to scan, unlike Codex): `result` (string),
   `total_cost_usd` (pre-computed), `usage.input_tokens`/`usage.output_tokens`,
   `is_error`, `session_id`.
2. Same, plus `--json-schema '<inline JSON schema>'` — the response gained a
   `structured_output` field holding the **already-parsed object** matching the schema.
   No text-scanning/candidate-parsing fallback is needed the way Codex's
   `_parse_response` needs one.
3. **Without `--safe-mode`, the CALLING session's own hooks, skills, and CLAUDE.md
   leaked into the dispatched sub-session** — confirmed directly: this very session's
   CAVEMAN-mode hook output and superpowers skill text appeared inside the probe's own
   `--verbose --output-format stream-json` trace. `--safe-mode` disables CLAUDE.md,
   skills, plugins, hooks, and MCP servers while leaving OAuth auth and file-editing
   tools working normally. `--bare` is stricter (also skips built-in tool/context
   loading, cutting cost further) but requires `ANTHROPIC_API_KEY` or `apiKeyHelper` —
   this machine has neither set (`env | grep -i anthropic` found nothing; login is
   OAuth-based) — so `--bare` is not usable here without asking the user to set up
   separate API billing, which was not requested. **`--safe-mode` is the isolation flag
   this adapter uses.**
4. `claude -p "Create a new file called hello.txt with the exact content: ..." --safe-mode --permission-mode bypassPermissions` really wrote the file, non-interactively, no
   hang, `permission_denials: []`. This is the confirmed sandbox-equivalent setting —
   analogous to Codex's `danger-full-access`.
5. `claude -p "say pong" --output-format json --safe-mode --model totally-not-a-real-model-xyz` — the real error shape: exit code 1, `is_error: true`,
   `api_error_status: 404` (a real HTTP status), `result` holding a human-readable
   message, `total_cost_usd: 0` and all `usage` fields `0` (confirms an error response
   costs nothing to observe).

**What was not empirically confirmed** (would require exhausting real account quota,
which was not done): the exact `api_error_status` value and response shape for an actual
rate-limit rejection. `429` (HTTP's standard "Too Many Requests", and Anthropic's
documented status for rate limits) is used as the detection value — flagged, like
Codex's usage-limit marker text was before its own first real trigger, as **verify against
a real rate-limit hit before fully trusting**. A `rate_limit_event` stream event
(`utilization`, `status`, `resetsAt`) was observed in `--output-format stream-json` mode
on a *successful* call (`status: "allowed_warning"`) — real and useful for future
proactive warnings, but this spec's `execute()`/`execute_json()` use non-streaming
`--output-format json` (see Out of Scope), so this event isn't consumed here.

**Cost note:** even under `--safe-mode`, a trivial call cost $0.04–$0.05 — Claude Code
loads ~25–31k tokens of its own built-in tool definitions from cache on every fresh CLI
invocation (no cross-invocation cache reuse in this stateless dispatch model), a real
fixed tax per call that Codex does not have. `total_cost_usd` reflects the CLI's own
computed API-equivalent cost, which may not equal what a subscription plan actually bills
the user — documented as a code comment where `estimated_cost` is set, not resolved here.

## Decisions from brainstorming

- No role gets `"cli"`/`"fallback_cli"` set to `"claude_code"` by default — the user
  explicitly chose this to avoid a surprise cost increase. The adapter exists and is
  fully wired; using it anywhere is a manual registry edit.
- Reuse the exact same `AGENT_SCHEMA`/`CLASSIFIER_SCHEMA` dicts already defined in
  `src/adapters/codex_cli.py` — one schema, delivered two ways (a file for Codex's
  `--output-schema`, an inline JSON string for Claude Code's `--json-schema`).

## Architecture

```
adapters/claude_code_cli.py (new)
  ClaudeCodeCLI — implements AgentCLIAdapter (execute, execute_json)

adapters/agent_cli.py
  KNOWN_CLI_NAMES gains "claude_code"
  build_cli gains an elif branch constructing ClaudeCodeCLI(settings)

orchestrator/config.py
  Settings gains claude_code_command: str = "claude" (same pattern as codex_command —
  lets tests point at a fake script, matches CodexCLI's existing convention)
```

Nothing in `agents/registry.py`, `orchestrator/nodes.py`, or the fallback logic changes —
that machinery (built in Spec 1) is already CLI-agnostic. This spec only adds the second
real implementation the interface was designed to accept.

## Components

### `src/orchestrator/config.py`

Add one field, following `codex_command`'s exact existing pattern:

```python
claude_code_command: str = "claude"
```

And in `Settings.load()`:

```python
claude_code_command=os.getenv("CLAUDE_CODE_COMMAND", "claude"),
```

### `src/adapters/agent_cli.py`

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

### `src/adapters/claude_code_cli.py` (new)

```python
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from adapters.codex_cli import AGENT_SCHEMA
from orchestrator.config import Settings
from schemas.models import AgentResult

_RATE_LIMIT_API_STATUS = 429


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

    def _base_command(self, schema: dict[str, Any] | None) -> list[str]:
        command = self.settings.claude_code_command.split() + [
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

## Data flow (one call)

1. `run_task` (unchanged, Spec 1) resolves `self.clis["claude_code"]` when a role's `cli`
   or `fallback_cli` names it.
2. `ClaudeCodeCLI.execute` spawns `claude -p - --output-format json --safe-mode
   --permission-mode bypassPermissions --strict-mcp-config --json-schema <AGENT_SCHEMA>`,
   feeding the prompt on stdin, `cwd` set to the task's worktree — the same isolation
   boundary Codex's git-worktree-per-task already provides, `claude` just runs inside it.
3. On completion, `_parse` reads one JSON object: an error (`is_error: true`) maps to
   `rate_limited` (HTTP 429) or `failed` (anything else); success reads
   `structured_output` directly into `AgentResult`'s fields, `usage` into token counts,
   `total_cost_usd` into `estimated_cost` — no text-parsing fallback chain, no
   self-reported-token correction, both real simplifications versus `CodexCLI`.
4. The cancellation/timeout loop mirrors `CodexCLI.execute`'s own polling structure
   (check `cancel_event`, check deadline, sleep 0.2s) for consistency with the existing
   adapter's shape, even though the underlying wait is `process.communicate()` rather than
   a line-by-line stream reader.

## Error handling

- Malformed/empty stdout → `failed`, `"structured output could not be parsed"` (mirrors
  `CodexCLI`'s equivalent fallback message).
- `is_error: true` with `api_error_status == 429` → `rate_limited` (unverified live, see
  Empirical research above — flagged for the implementer to double-check against a real
  hit, not to block shipping this version).
- `is_error: true` with any other status → `failed`, using the CLI's own human-readable
  `result` text as both `summary` and the sole `errors` entry.
- Cancellation and timeout both kill the process and await the already-created
  `communicate()` task before returning, so no orphaned subprocess is left running —
  same discipline as `ProcessManager.ensure_awake`'s cleanup-before-raise fix from the
  Wake Listener feature earlier this session.

## Testing

Real `claude` invocations cost real money and require a live login — not something to run
in an automated suite. Tests use a fake `claude` script (the project's established
real-subprocess pattern, `sys.executable`-launched), writing the exact JSON shapes
observed in this spec's live research:

- A "success with structured output" script, mirroring finding #2 exactly (a
  `structured_output` object plus real `usage`/`total_cost_usd`) — asserts `AgentResult`
  fields are populated from `structured_output`/`usage`/`total_cost_usd`, not left at
  their model defaults.
- A "404-style error" script mirroring finding #5 — asserts `status == "failed"` and the
  summary is the CLI's own message text, not a generic fallback.
- A "429-style error" script (same shape as #5, `api_error_status: 429`) — asserts
  `status == "rate_limited"`.
- Malformed/empty stdout — asserts `status == "failed"` with the parse-failure message.
- Cancellation and timeout — mirrors `CodexCLI`'s own two equivalent tests
  (`tests/test_codex_yolo_integration.py`), same real-process-kill assertions.
- `build_cli("claude_code", settings)` returns a `ClaudeCodeCLI` instance;
  `KNOWN_CLI_NAMES` contains both `"codex"` and `"claude_code"`.
- One end-to-end `run_task` test with a role's `cli` set to a fake `claude_code` adapter
  (real subprocess, fake script) — proves the whole Spec-1 dispatch path actually resolves
  and runs a non-Codex adapter, not just that `ClaudeCodeCLI` works in isolation.

## Out of scope (this spec)

- **Live streaming (`on_event`) for Claude Code.** `execute`/`execute_json` use
  non-streaming `--output-format json`; the `on_event` parameter is accepted (interface
  compliance) but never invoked for this adapter — the panel simply shows no live
  character-by-character feed for a Claude-Code-served task, only start/finish. Real
  streaming would use `--output-format stream-json` and the `rate_limit_event`/assistant
  message-chunk shapes observed in the research above, but parsing that stream correctly
  is its own empirical-verification effort — a legitimate Spec 3 candidate, not bundled
  in here.
- **Wiring any real role to use `claude_code`.** Per the brainstorming decision, this
  spec makes the adapter available; choosing to actually use it anywhere is a manual,
  separate registry edit the user makes when ready.
- **Confirming the real rate-limit response shape.** Flagged above as unverified;
  revisit if/when a real 429 is actually observed in production, the same way Codex's
  usage-limit marker text was only trusted after it fired for real.
- **Reconciling `total_cost_usd` against actual OAuth subscription billing.** Used as
  reported; whether it overstates or understates real cost under a subscription (versus
  pay-per-token API billing) is not resolved here.
