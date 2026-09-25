# CLI-Agnostic Agent Execution (Spec 1 of 2) — Design

## Problem

Every agent dispatch in this orchestrator is hardwired to `CodexCLI`
(`src/adapters/codex_cli.py`): `ExecutionRuntime.run_task` always calls
`self.codex.execute(...)`. This session already hit a real consequence —
Codex's own usage limit stopped every in-flight agent at once, with no way to
keep working on a different provider while it reset.

`ExecutionRuntime` already accepts `codex` as an injectable constructor
parameter (tests already pass in fakes), so the seam for swapping
implementations exists — there is just only one real implementation behind it,
and no way to pick a different one per agent role.

## Goal

Let each agent role in `agents/registry.py` declare which CLI runs it, and
optionally a fallback CLI to try once if the primary hits its usage limit —
so a rate limit on one provider's plan no longer stops every agent at once.
This spec covers only the architecture and config plumbing, proven with
Codex as the only real adapter. A second spec adds a real second adapter
(Claude Code) once this shape is validated — building both at once would mean
guessing at a second CLI's real behavior while still shaping the interface,
which this project's own history (the Codex `turn.completed` token fix, the
project-scoping classifier bug) shows is exactly how subtle bugs get missed.

## Chosen approach

Formalize the existing informal seam into an explicit `AgentCLIAdapter`
protocol; make `CodexCLI` its first implementation; add `cli` / `fallback_cli`
fields to each `AGENT_ROLES` entry; give `ExecutionRuntime` a dict of adapters
instead of one, and add fallback-on-`rate_limited` logic to `run_task`.

Considered and rejected: a shared low-level subprocess runner with pluggable
per-CLI response parsers (separating "how to spawn" from "how to parse").
Rejected because it assumes different CLIs share enough of their invocation
shape (how the prompt is passed, what flags exist, whether structured output
via a schema file is even supported) to justify forcing them through one
runner — an assumption nothing in this codebase has verified yet. Building
that shared layer before a second real CLI exists risks the same mistake as
guessing at Codex's token-usage shape instead of reading its real session
logs. A full adapter per CLI, sharing nothing but the interface, costs a
little duplication now and avoids designing a shared abstraction around a
CLI shape that hasn't been observed for real.

## Architecture

```
agents/registry.py
  AGENT_ROLES["frontend"] = {..., "cli": "codex", "fallback_cli": None}
  AGENT_ROLES["backend"]  = {..., "cli": "codex", "fallback_cli": "claude_code"}  # example once Spec 2 exists
  GATE_ROLES = {"contracts", "reviewer"}   # unchanged

adapters/agent_cli.py (new)
  AgentCLIAdapter (Protocol) — the shape CodexCLI already has
  build_cli(name: str, settings: Settings) -> AgentCLIAdapter
  KNOWN_CLI_NAMES = {"codex"}   # grows to {"codex", "claude_code"} in Spec 2

adapters/codex_cli.py
  CodexCLI — unchanged behavior; now explicitly documented as implementing
  AgentCLIAdapter

orchestrator/nodes.py (ExecutionRuntime)
  self.clis: dict[str, AgentCLIAdapter]   # was: self.codex: CodexCLI
  run_task(...) resolves the adapter(s) for the task's role, runs the
  existing retry loop against the primary, and falls back once if the
  primary's final result is rate_limited and a fallback_cli is configured.
```

Nothing about prompt construction, project-context injection, or the
retry/backoff loop's handling of genuine failures changes — those are already
CLI-agnostic (they just build a prompt string and inspect an `AgentResult`).
Only the one line that calls `self.codex.execute(...)` and its immediate
surroundings change.

## Components

### `src/adapters/agent_cli.py` (new file)

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from schemas.models import AgentResult
from orchestrator.config import Settings


class AgentCLIAdapter(Protocol):
    async def execute(self, prompt: str, workdir: Path, role: str, timeout: int | None = None,
                      cancel_event: Any = None, trace_metadata: dict[str, Any] | None = None,
                      on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                      reasoning_effort: str | None = None) -> AgentResult: ...

    async def execute_json(self, prompt: str, workdir: Path, schema: dict[str, Any], label: str,
                           timeout: int = 120, reasoning_effort: str | None = None) -> dict[str, Any] | None: ...


KNOWN_CLI_NAMES = {"codex"}


def build_cli(name: str, settings: Settings) -> AgentCLIAdapter:
    if name == "codex":
        from adapters.codex_cli import CodexCLI
        return CodexCLI(settings)
    raise ValueError(f"unknown agent CLI '{name}' — must be one of {sorted(KNOWN_CLI_NAMES)}")
```

This is the exact signature `CodexCLI.execute`/`execute_json` already have
today (confirmed against the current source) — the protocol documents the
existing shape, it does not change it.

### `src/agents/registry.py`

Every existing entry gains two fields, both defaulted to today's real
behavior:

```python
AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md",
                "prompt_version": "1.0", "cli": "codex", "fallback_cli": None},
    # ... same two new keys added to every other entry, all "codex" / None ...
}
```

`GATE_ROLES` is unchanged.

### `src/orchestrator/config.py`

Add one validation step to `Settings.load()` (or a method called once at
`ExecutionRuntime.__init__`): every `cli` and non-`None` `fallback_cli` value
across `AGENT_ROLES` must be in `agent_cli.KNOWN_CLI_NAMES`. Raise
`ValueError` immediately (fail at boot) if not — a typo in a role's `cli`
value must never silently fall back to Codex without saying so.

No new cost-per-token settings are needed for this spec: `CodexCLI.execute`
already computes `estimated_cost` internally from `self.settings.*`, scoped
to that one adapter — cost is already adapter-owned, not a shared global rate
the orchestration layer applies after the fact. A second adapter (Spec 2)
brings its own cost fields when it's built; nothing here needs to anticipate
their shape.

### `src/schemas/models.py`

`AgentResult` gains one new optional field:

```python
cli_used: str | None = None
```

Set by `run_task` (not by the adapters themselves) after a call returns, so
the panel/dashboard can show which CLI actually served a task — a direct,
free extension of the "Nós" pipeline screen already built this session, which
already renders per-node token/cost.

### `src/orchestrator/nodes.py` (`ExecutionRuntime`)

```python
def __init__(self, settings: Settings, store: SQLiteCheckpointer | None = None,
            clis: dict[str, "AgentCLIAdapter"] | None = None,
            event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None):
    self.settings = settings
    self.store = store or SQLiteCheckpointer(settings.checkpoint_path)
    self.clis = clis or {"codex": CodexCLI(settings)}
    ...
```

`run_task` changes from calling `self.codex.execute(...)` to resolving the
role's configured adapter(s) first, and adds the fallback branch after the
existing retry loop:

```python
    async def run_task(self, state: dict[str, Any], task: dict[str, Any]) -> AgentResult:
        role = task["agent"]
        role_config = AGENT_ROLES.get(role, {})
        cli = self.clis[role_config.get("cli", "codex")]
        fallback_cli_name = role_config.get("fallback_cli")
        ...  # prompt building, project_context injection: unchanged
        reasoning_effort = self.settings.codex_reasoning_effort_gates if role in GATE_ROLES else None
        attempts = 0
        last: AgentResult | None = None
        while attempts <= self.settings.max_retries:
            attempts += 1
            task["attempts"] = attempts
            result = await cli.execute(prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                                       timeout=self.settings.agent_timeout_seconds,
                                       cancel_event=self.cancel_event(state["execution_id"]),
                                       trace_metadata={...}, on_event=on_event,
                                       reasoning_effort=reasoning_effort)
            last = result
            if result.status in ("completed", "rate_limited"):
                break
            state["retries"] = int(state.get("retries", 0)) + 1
            if attempts <= self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (2 ** (attempts - 1)))

        if last is not None and last.status == "rate_limited" and fallback_cli_name:
            fallback = self.clis.get(fallback_cli_name)
            if fallback is not None:
                fallback_result = await fallback.execute(
                    prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                    timeout=self.settings.agent_timeout_seconds,
                    cancel_event=self.cancel_event(state["execution_id"]),
                    trace_metadata={...}, on_event=on_event, reasoning_effort=reasoning_effort,
                )
                fallback_result.cli_used = fallback_cli_name
                return fallback_result

        if last is not None:
            last.cli_used = role_config.get("cli", "codex")
        return last or AgentResult(agent=role, status="failed", summary="agent did not return")
```

The fallback call is a single attempt — no retry loop of its own, matching
this session's own fix earlier (`ensure_awake`/`rate_limited` handling):
hammering a second limited resource is not a recovery strategy either. If the
fallback also comes back `rate_limited` or `failed`, that becomes the task's
final result exactly as an unrecoverable failure does today — `gates_pass()`
already treats `rate_limited` as blocking, no new gate logic is needed.

`main.py`'s CLI entrypoint, `orchestrator/graph.py`, and `api/app.py`'s app
factory all construct `ExecutionRuntime(settings, event_sink=...)` today
without passing `codex` at all — they keep working unmodified, since a
missing `clis` argument defaults to `{"codex": CodexCLI(settings)}`.

**Migration note (real, not hypothetical):** three test files construct
`ExecutionRuntime(..., codex=<fake>)` directly and must be updated to
`ExecutionRuntime(..., clis={"codex": <fake>})` — confirmed by grep, exactly
these three and no others:
- `tests/test_orchestration_noop.py`
- `tests/test_project_scoping.py`
- `tests/test_analysis_only.py`

This is mechanical (the constructor's positional/keyword shape for every
other argument is unchanged) but real: the implementation plan must include
it as an explicit step, not leave it for the plan executor to discover via a
failing test run.

## Data flow (one task, with fallback)

1. `create_plan` builds the task for a role exactly as today.
2. `run_task` reads `AGENT_ROLES[role]["cli"]`, resolves the adapter from
   `self.clis`.
3. Runs the existing retry loop against that adapter only.
4. If the loop's final result is `rate_limited` and the role has a
   `fallback_cli` configured, calls that second adapter once.
5. Tags the result actually returned with which CLI served it
   (`cli_used`) — visible later on the "Nós" panel screen next to that
   node's token/cost.
6. Everything downstream (`collect_agent_results`, gates, the panel) reads
   `AgentResult` exactly as before; `cli_used` is additive, nothing existing
   breaks if it's `None`.

## Error handling

- An unknown `cli`/`fallback_cli` name in `AGENT_ROLES` fails at startup
  (`ValueError`), never silently at dispatch time.
- A configured `fallback_cli` that isn't present in `self.clis` (e.g.
  registered in the role config but the adapter was never constructed) is
  treated as "no fallback available" — `self.clis.get(fallback_cli_name)`
  returns `None`, the primary's `rate_limited` result is returned as final,
  exactly like today. This is deliberately lenient here (unlike the
  registry-name validation above) because it lets a role declare a fallback
  aspirationally before Spec 2's adapter exists, without breaking Spec 1.
- The fallback attempt reuses the exact same prompt and project context as
  the primary attempt — no re-derivation, no drift between what the two CLIs
  are asked to do.

## Testing

- `agent_cli.py`: `build_cli("codex", settings)` returns a working `CodexCLI`;
  `build_cli("nonsense", settings)` raises `ValueError`.
- Startup validation: a `Settings`/registry combination with a typo'd `cli`
  name raises at `ExecutionRuntime.__init__`, not later.
- `run_task` fallback, using the project's existing real-subprocess fake-CLI
  pattern (`FAKE_CODEX_*` scripts): two fake adapters wired into
  `self.clis` as `{"primary": ..., "secondary": ...}` — primary script
  returns a usage-limit `turn.failed` (status `rate_limited`), secondary
  returns `completed`. Assert the task's final `AgentResult.status ==
  "completed"`, `cli_used == "secondary"`, and the primary's tokens/cost are
  not added into the final result (they belong to a rejected attempt, not the
  task's outcome).
- Regression: a role with no `fallback_cli` still returns the primary's
  `rate_limited` result unchanged when it hits the limit — byte-identical to
  today's behavior, proven by re-running the existing
  `test_run_task_does_not_retry_a_rate_limited_result` test unmodified.
- Full existing suite (186 tests as of this session) stays green.

## Out of scope (this spec)

- The real Claude Code (or any other) adapter — Spec 2.
- Automatic retry chains longer than one fallback (a priority list of 3+
  CLIs) — explicitly deferred; revisit only if a third real CLI is actually
  added.
- Per-adapter cost configuration beyond what `CodexCLI` already has — added
  when the adapter that needs it is built.
- Choosing a CLI dynamically at runtime based on live load/cost rather than
  static per-role config — a materially bigger feature, not requested here.
