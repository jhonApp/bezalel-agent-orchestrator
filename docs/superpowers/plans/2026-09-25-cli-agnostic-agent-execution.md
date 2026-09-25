# CLI-Agnostic Agent Execution (1 of 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each agent role declare which CLI runs it and an optional
fallback CLI to try once if the primary hits its usage limit, formalizing the
existing `codex`-injection seam into an explicit adapter interface — proven
with Codex as the only real adapter (a second real adapter is a separate,
later spec).

**Architecture:** A new `AgentCLIAdapter` protocol in
`src/adapters/agent_cli.py`, with `CodexCLI` as its first implementation.
`agents/registry.py`'s `AGENT_ROLES` entries gain `cli`/`fallback_cli` keys.
`ExecutionRuntime` holds a `dict[str, AgentCLIAdapter]` instead of a single
`CodexCLI`, validates every role's `cli` against it at construction time, and
`run_task` tries the role's `fallback_cli` once, only when the primary's
final result is `rate_limited`.

**Tech Stack:** Python 3.11+, `typing.Protocol`, pytest/pytest-asyncio, the
project's existing real-subprocess fake-CLI testing pattern (see
`tests/test_orchestration_noop.py`'s `RATE_LIMIT_SCRIPT_TEMPLATE` /
`FAKE_CODEX_PROGRESS`).

## Global Constraints

- No behavior change for any deployment that configures nothing: every
  `AGENT_ROLES` entry defaults to `"cli": "codex"`, `"fallback_cli": None`,
  and `ExecutionRuntime(settings, ...)` with no `clis` argument still builds
  `{"codex": CodexCLI(settings)}` exactly as `codex=None` does today.
- The fallback is a single attempt — never its own retry loop. Hammering a
  second rate-limited resource is not a recovery strategy (the same reasoning
  already applied to not retrying `rate_limited` on the primary).
- Fallback triggers only on a final result of `rate_limited` — never on
  `failed`, `blocked`, or any other status. A generic failure is not evidence
  a different CLI would do better; a usage-limit hit specifically is.
- An unknown `cli` name in `AGENT_ROLES` must fail at `ExecutionRuntime`
  construction (`ValueError`), never silently at dispatch time. An unknown
  `fallback_cli` name must NOT fail construction — it resolves to "no
  fallback available" lazily, at dispatch time, so a role can declare a
  fallback before that adapter exists (Spec 2).
- Tests that need a fake CLI use the project's real-subprocess pattern
  (a `sys.executable`-launched script), not `unittest.mock`.

---

### Task 1: `AgentCLIAdapter` protocol and `build_cli` factory

**Files:**
- Create: `src/adapters/agent_cli.py`
- Test: `tests/test_agent_cli.py`

**Interfaces:**
- Produces: `AgentCLIAdapter` (a `typing.Protocol` — documents, does not
  change, `CodexCLI`'s existing `execute`/`execute_json` signatures);
  `KNOWN_CLI_NAMES: set[str]`; `build_cli(name: str, settings: Settings) ->
  AgentCLIAdapter` (raises `ValueError` for an unknown name);
  `validate_agent_roles(agent_roles: dict[str, dict], available_clis:
  set[str]) -> None` (raises `ValueError` naming the offending role and
  value when a role's `"cli"` is not in `available_clis`; does **not**
  check `"fallback_cli"` at all — that stays unvalidated by design).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_cli.py
from __future__ import annotations

from pathlib import Path

import pytest

from adapters.agent_cli import KNOWN_CLI_NAMES, build_cli, validate_agent_roles
from adapters.codex_cli import CodexCLI
from orchestrator.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_known_cli_names_contains_codex():
    assert "codex" in KNOWN_CLI_NAMES


def test_build_cli_returns_a_codex_cli_for_the_codex_name(tmp_path: Path):
    adapter = build_cli("codex", settings_for(tmp_path))

    assert isinstance(adapter, CodexCLI)


def test_build_cli_raises_for_an_unknown_name(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown agent CLI 'bogus'"):
        build_cli("bogus", settings_for(tmp_path))


def test_validate_agent_roles_passes_when_every_cli_is_available():
    roles = {"frontend": {"cli": "codex", "fallback_cli": None}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise


def test_validate_agent_roles_raises_for_an_unavailable_primary_cli():
    roles = {"frontend": {"cli": "bogus", "fallback_cli": None}}

    with pytest.raises(ValueError, match="frontend.*bogus"):
        validate_agent_roles(roles, available_clis={"codex"})


def test_validate_agent_roles_defaults_a_missing_cli_key_to_codex():
    roles = {"frontend": {}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise


def test_validate_agent_roles_never_checks_fallback_cli():
    """A role may declare a fallback_cli that doesn't exist yet (e.g. before
    a second real adapter is built) — that must not fail construction."""
    roles = {"frontend": {"cli": "codex", "fallback_cli": "not_built_yet"}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'adapters.agent_cli'`

- [ ] **Step 3: Implement the module**

```python
# src/adapters/agent_cli.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from orchestrator.config import Settings
from schemas.models import AgentResult


class AgentCLIAdapter(Protocol):
    """The shape every agent-execution backend must implement. CodexCLI
    (adapters/codex_cli.py) is the first implementation; a second real CLI
    (Claude Code) is a separate, later spec.
    """

    async def execute(self, prompt: str, workdir: Path, role: str, timeout: int | None = None,
                      cancel_event: Any = None, trace_metadata: dict[str, Any] | None = None,
                      on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                      reasoning_effort: str | None = None) -> AgentResult: ...

    async def execute_json(self, prompt: str, workdir: Path, schema: dict[str, Any], label: str,
                           timeout: int = 120, reasoning_effort: str | None = None) -> dict[str, Any] | None: ...


KNOWN_CLI_NAMES: set[str] = {"codex"}


def build_cli(name: str, settings: Settings) -> AgentCLIAdapter:
    if name == "codex":
        from adapters.codex_cli import CodexCLI
        return CodexCLI(settings)
    raise ValueError(f"unknown agent CLI '{name}' — must be one of {sorted(KNOWN_CLI_NAMES)}")


def validate_agent_roles(agent_roles: dict[str, dict[str, Any]], available_clis: set[str]) -> None:
    """Every role's primary "cli" must resolve to a real adapter the runtime
    was actually given — a typo must fail at startup, not at dispatch time.
    "fallback_cli" is deliberately NOT checked here: a role may name a
    fallback that doesn't exist yet (e.g. before a second real adapter is
    built), and that must not block startup.
    """
    for role, config in agent_roles.items():
        cli_name = config.get("cli", "codex")
        if cli_name not in available_clis:
            raise ValueError(
                f"role '{role}' declares cli='{cli_name}', which is not in the "
                f"configured clis {sorted(available_clis)}"
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/adapters/agent_cli.py tests/test_agent_cli.py
git commit -m "feat: add AgentCLIAdapter protocol and build_cli factory"
```

---

### Task 2: `cli`/`fallback_cli` fields on every `AGENT_ROLES` entry

**Files:**
- Modify: `src/agents/registry.py`
- Test: `tests/test_agent_cli.py` (append)

**Interfaces:**
- Consumes: `validate_agent_roles` (Task 1, exact signature above).
- Produces: every `AGENT_ROLES[role]` dict now has `"cli"` and
  `"fallback_cli"` keys (all `"codex"` / `None` for now — no role has a real
  fallback configured yet, since only one real CLI exists).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_cli.py`:

```python
def test_the_real_agent_roles_validate_cleanly_against_codex_only():
    from agents.registry import AGENT_ROLES

    validate_agent_roles(AGENT_ROLES, available_clis={"codex"})  # must not raise


def test_every_real_agent_role_defaults_to_codex_with_no_fallback():
    from agents.registry import AGENT_ROLES

    for role, config in AGENT_ROLES.items():
        assert config.get("cli") == "codex", f"role '{role}' should default to cli='codex'"
        assert config.get("fallback_cli") is None, f"role '{role}' should default to no fallback_cli"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli.py -v -k real_agent_roles`
Expected: FAIL — `config.get("cli")` is `None`, not `"codex"` (the key doesn't exist yet)

- [ ] **Step 3: Add the fields to the registry**

In `src/agents/registry.py`, find:

```python
AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md", "prompt_version": "1.0"},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md", "prompt_version": "1.0"},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md", "prompt_version": "1.0"},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md", "prompt_version": "1.0"},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md", "prompt_version": "1.0"},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md", "prompt_version": "1.0"},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1"},
    "classifier": {"label": "Domain Classifier", "project_id": None, "prompt": "classifier.md", "prompt_version": "1.0"},
}
```

Replace with:

```python
AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md", "prompt_version": "1.0",
                "cli": "codex", "fallback_cli": None},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md", "prompt_version": "1.0",
               "cli": "codex", "fallback_cli": None},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md", "prompt_version": "1.0",
                 "cli": "codex", "fallback_cli": None},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md", "prompt_version": "1.0",
                 "cli": "codex", "fallback_cli": None},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md", "prompt_version": "1.0",
          "cli": "codex", "fallback_cli": None},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md", "prompt_version": "1.0",
                "cli": "codex", "fallback_cli": None},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1",
                "cli": "codex", "fallback_cli": None},
    "classifier": {"label": "Domain Classifier", "project_id": None, "prompt": "classifier.md", "prompt_version": "1.0",
                  "cli": "codex", "fallback_cli": None},
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/agents/registry.py tests/test_agent_cli.py
git commit -m "feat: add cli/fallback_cli fields to every agent role"
```

---

### Task 3: `ExecutionRuntime.clis` + fallback logic in `run_task`

**Files:**
- Modify: `src/schemas/models.py:62-76` (`AgentResult`)
- Modify: `src/orchestrator/nodes.py:1-25` (imports), `:28-41` (`__init__`),
  `:96-141` (`run_task`), `:200-203` (`classify_relevant_projects`)
- Test: `tests/test_agent_cli_fallback.py`

**Interfaces:**
- Consumes: `AgentCLIAdapter`, `build_cli`, `validate_agent_roles` (Task 1);
  `AGENT_ROLES` with `cli`/`fallback_cli` (Task 2).
- Produces: `AgentResult.cli_used: str | None` (new field);
  `ExecutionRuntime.__init__(self, settings, store=None, clis:
  dict[str, AgentCLIAdapter] | None = None, event_sink=None)` (the `codex`
  parameter is **removed**, not kept alongside `clis` — Task 4 migrates the
  three call sites that used it).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_cli_fallback.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from adapters.codex_cli import CodexCLI
from agents.registry import AGENT_ROLES
from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime


RATE_LIMIT_SCRIPT = r'''
import json
import sys
print(json.dumps({"type": "turn.failed", "error": {"message": "You\u2019ve hit your usage limit. Upgrade to Pro or try again later."}}), flush=True)
sys.exit(1)
'''

COMPLETED_SCRIPT = r'''
import json
import sys
output = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(output, "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "status": "completed", "summary": "handled by fallback",
        "files_changed": [], "tests": [], "contracts_changed": [],
        "errors": [], "next_action": None, "tokens_input": 5, "tokens_output": 3,
    }))
sys.exit(0)
'''

FAILED_SCRIPT = r'''
import json
import sys
output = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(output, "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "status": "failed", "summary": "boom",
        "files_changed": [], "tests": [], "contracts_changed": [],
        "errors": ["boom"], "next_action": None, "tokens_input": 1, "tokens_output": 1,
    }))
sys.exit(1)
'''


def settings_with_command(tmp_path: Path, script_name: str, script_source: str) -> Settings:
    script = tmp_path / script_name
    script.write_text(script_source, encoding="utf-8")
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / f"checkpoints-{script_name}.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / f"langgraph-{script_name}.sqlite3",
        max_retries=0, retry_backoff_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_run_task_falls_back_once_when_the_primary_hits_its_rate_limit(tmp_path: Path, monkeypatch):
    primary_settings = settings_with_command(tmp_path, "primary.py", RATE_LIMIT_SCRIPT)
    secondary_settings = settings_with_command(tmp_path, "secondary.py", COMPLETED_SCRIPT)
    clis = {"primary": CodexCLI(primary_settings), "secondary": CodexCLI(secondary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", "secondary")
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.summary == "handled by fallback"
    assert result.cli_used == "secondary"


@pytest.mark.asyncio
async def test_run_task_returns_the_rate_limited_result_unchanged_with_no_fallback_configured(tmp_path: Path, monkeypatch):
    primary_settings = settings_with_command(tmp_path, "primary.py", RATE_LIMIT_SCRIPT)
    clis = {"primary": CodexCLI(primary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", None)
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "rate_limited"
    assert result.cli_used == "primary"


@pytest.mark.asyncio
async def test_run_task_does_not_fall_back_on_a_genuine_failure(tmp_path: Path, monkeypatch):
    """The fallback exists for usage limits specifically, not for every failure — a role
    that fails for an ordinary reason should not silently retry on a different CLI."""
    primary_settings = settings_with_command(tmp_path, "primary.py", FAILED_SCRIPT)
    secondary_settings = settings_with_command(tmp_path, "secondary.py", COMPLETED_SCRIPT)
    clis = {"primary": CodexCLI(primary_settings), "secondary": CodexCLI(secondary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", "secondary")
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "failed"
    assert result.cli_used == "primary"


@pytest.mark.asyncio
async def test_execution_runtime_raises_at_construction_for_an_unavailable_primary_cli(tmp_path: Path, monkeypatch):
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "totally_not_registered")

    with pytest.raises(ValueError, match="frontend.*totally_not_registered"):
        ExecutionRuntime(settings)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli_fallback.py -v`
Expected: FAIL — `ExecutionRuntime.__init__() got an unexpected keyword argument 'clis'`

- [ ] **Step 3: Add `cli_used` to `AgentResult`**

In `src/schemas/models.py`, find:

```python
    raw_response: str = ""
    quality: dict[str, Any] | None = None
```

Replace with:

```python
    raw_response: str = ""
    quality: dict[str, Any] | None = None
    cli_used: str | None = None
```

- [ ] **Step 4: Update `ExecutionRuntime`'s imports and constructor**

In `src/orchestrator/nodes.py`, find:

```python
from agents.registry import AGENT_ROLES, GATE_ROLES, role_prompt
from agents.security import scan_project
from adapters.codex_cli import CLASSIFIER_SCHEMA, CodexCLI
```

Replace with:

```python
from agents.registry import AGENT_ROLES, GATE_ROLES, role_prompt
from agents.security import scan_project
from adapters.agent_cli import AgentCLIAdapter, build_cli, validate_agent_roles
from adapters.codex_cli import CLASSIFIER_SCHEMA
```

Find:

```python
class ExecutionRuntime:
    def __init__(self, settings: Settings, store: SQLiteCheckpointer | None = None, codex: CodexCLI | None = None,
                 event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None):
        self.settings = settings
        self.store = store or SQLiteCheckpointer(settings.checkpoint_path)
        self.codex = codex or CodexCLI(settings)
        self.deploy = DeployAdapter(settings)
        self.health = ContextHealthMonitor(settings)
        self.tracing = LangSmithObserver(settings)
        self.event_sink = event_sink
        self.cancel_events: dict[str, asyncio.Event] = {}
```

Replace with:

```python
class ExecutionRuntime:
    def __init__(self, settings: Settings, store: SQLiteCheckpointer | None = None,
                 clis: dict[str, AgentCLIAdapter] | None = None,
                 event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None):
        self.settings = settings
        self.store = store or SQLiteCheckpointer(settings.checkpoint_path)
        self.clis = clis or {"codex": build_cli("codex", settings)}
        validate_agent_roles(AGENT_ROLES, available_clis=set(self.clis.keys()))
        self.deploy = DeployAdapter(settings)
        self.health = ContextHealthMonitor(settings)
        self.tracing = LangSmithObserver(settings)
        self.event_sink = event_sink
        self.cancel_events: dict[str, asyncio.Event] = {}
```

- [ ] **Step 5: Update `run_task`'s dispatch and add the fallback branch**

Find:

```python
        reasoning_effort = self.settings.codex_reasoning_effort_gates if role in GATE_ROLES else None
        attempts = 0
        last: AgentResult | None = None
        while attempts <= self.settings.max_retries:
            attempts += 1
            task["attempts"] = attempts
            result = await self.codex.execute(prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                                              timeout=self.settings.agent_timeout_seconds, cancel_event=self.cancel_event(state["execution_id"]),
                                              trace_metadata={
                                                  "orchestrator_execution_id": state["execution_id"],
                                                  "orchestrator_task_id": task["task_id"],
                                                  "orchestrator_project_id": task["project_id"],
                                                  "context_health": state.get("context_health", {}),
                                              },
                                              on_event=on_event, reasoning_effort=reasoning_effort)
            last = result
            # A real usage-limit hit is not transient the way a timeout or a flaky tool call
            # is: the account-wide quota it reports against does not reset within this loop's
            # 2s/4s backoff, so retrying here only guarantees hitting the same wall two more
            # times, each burning a full prompt's worth of input tokens at the worst possible
            # moment. Stop after one attempt and let the gate/report surface it instead.
            if result.status in ("completed", "rate_limited"):
                break
            state["retries"] = int(state.get("retries", 0)) + 1
            if attempts <= self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (2 ** (attempts - 1)))
        return last or AgentResult(agent=role, status="failed", summary="agent did not return")
```

Replace with:

```python
        role_config = AGENT_ROLES.get(role, {})
        cli = self.clis[role_config.get("cli", "codex")]
        fallback_cli_name = role_config.get("fallback_cli")
        reasoning_effort = self.settings.codex_reasoning_effort_gates if role in GATE_ROLES else None
        attempts = 0
        last: AgentResult | None = None
        while attempts <= self.settings.max_retries:
            attempts += 1
            task["attempts"] = attempts
            result = await cli.execute(prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                                       timeout=self.settings.agent_timeout_seconds, cancel_event=self.cancel_event(state["execution_id"]),
                                       trace_metadata={
                                           "orchestrator_execution_id": state["execution_id"],
                                           "orchestrator_task_id": task["task_id"],
                                           "orchestrator_project_id": task["project_id"],
                                           "context_health": state.get("context_health", {}),
                                       },
                                       on_event=on_event, reasoning_effort=reasoning_effort)
            last = result
            # A real usage-limit hit is not transient the way a timeout or a flaky tool call
            # is: the account-wide quota it reports against does not reset within this loop's
            # 2s/4s backoff, so retrying here only guarantees hitting the same wall two more
            # times, each burning a full prompt's worth of input tokens at the worst possible
            # moment. Stop after one attempt and let the gate/report surface it instead.
            if result.status in ("completed", "rate_limited"):
                break
            state["retries"] = int(state.get("retries", 0)) + 1
            if attempts <= self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (2 ** (attempts - 1)))

        # The fallback is a single attempt on a different CLI, never its own retry loop -
        # hammering a second rate-limited resource is not a recovery strategy. It only
        # fires for rate_limited specifically: a genuine failure is not evidence a
        # different CLI would have done better.
        if last is not None and last.status == "rate_limited" and fallback_cli_name:
            fallback = self.clis.get(fallback_cli_name)
            if fallback is not None:
                fallback_result = await fallback.execute(
                    prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                    timeout=self.settings.agent_timeout_seconds, cancel_event=self.cancel_event(state["execution_id"]),
                    trace_metadata={
                        "orchestrator_execution_id": state["execution_id"],
                        "orchestrator_task_id": task["task_id"],
                        "orchestrator_project_id": task["project_id"],
                        "context_health": state.get("context_health", {}),
                    },
                    on_event=on_event, reasoning_effort=reasoning_effort,
                )
                fallback_result.cli_used = fallback_cli_name
                return fallback_result

        if last is not None:
            last.cli_used = role_config.get("cli", "codex")
        return last or AgentResult(agent=role, status="failed", summary="agent did not return")
```

- [ ] **Step 6: Point the classifier at the codex adapter explicitly**

In `src/orchestrator/nodes.py`, find:

```python
        result = await self.codex.execute_json(prompt, self.settings.workspace_root, CLASSIFIER_SCHEMA, "Classifier",
                                               reasoning_effort=self.settings.codex_reasoning_effort_gates)
```

Replace with:

```python
        # The classifier is infrastructure, not a per-role dispatched agent — it always
        # runs on the codex adapter regardless of any role's cli/fallback_cli config.
        result = await self.clis["codex"].execute_json(prompt, self.settings.workspace_root, CLASSIFIER_SCHEMA, "Classifier",
                                                        reasoning_effort=self.settings.codex_reasoning_effort_gates)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli_fallback.py -v`
Expected: FAIL first, for a different reason — three existing test files still construct
`ExecutionRuntime(settings, codex=...)`, which no longer exists as a parameter. This is
expected and fixed in Task 4. For now, run only this task's own new test file, which does
not use the removed parameter:

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli_fallback.py tests/test_agent_cli.py -v`
Expected: PASS (13 tests: 9 from Tasks 1-2 + 4 new)

- [ ] **Step 8: Commit**

```bash
git add src/schemas/models.py src/orchestrator/nodes.py tests/test_agent_cli_fallback.py
git commit -m "feat: give ExecutionRuntime a clis dict and add rate_limited fallback to run_task"
```

---

### Task 4: Migrate the three test files that construct `ExecutionRuntime(..., codex=...)`

**Files:**
- Modify: `tests/test_orchestration_noop.py:489`, `:531`
- Modify: `tests/test_project_scoping.py:61-62`, `:68`, `:74-75`, `:86-87`
- Modify: `tests/test_analysis_only.py:147`, `:174`

**Interfaces:**
- Consumes: `ExecutionRuntime.__init__(..., clis: dict[str, AgentCLIAdapter]
  | None = None, ...)` (Task 3 — the `codex` parameter no longer exists).

- [ ] **Step 1: Update `tests/test_orchestration_noop.py`**

Find (appears twice, at line 489 and line 531 — both identical):

```python
    runtime = ExecutionRuntime(settings, codex=CapturingCodex())
```

Replace **each** occurrence with:

```python
    runtime = ExecutionRuntime(settings, clis={"codex": CapturingCodex()})
```

- [ ] **Step 2: Update `tests/test_project_scoping.py`**

Find:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True, "backend": False, "python": False}))

    relevant, source = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert relevant == ["frontend"]
    assert source == "classifier"
    assert runtime.codex.calls[0]["reasoning_effort"] == settings.codex_reasoning_effort_gates
```

Replace with:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               clis={"codex": FakeCodexForClassifier({"frontend": True, "backend": False, "python": False})})

    relevant, source = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert relevant == ["frontend"]
    assert source == "classifier"
    assert runtime.clis["codex"].calls[0]["reasoning_effort"] == settings.codex_reasoning_effort_gates
```

Find:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier(None))
```

Replace with:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               clis={"codex": FakeCodexForClassifier(None)})
```

Find:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True}))
```

Replace with:

```python
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               clis={"codex": FakeCodexForClassifier({"frontend": True})})
```

- [ ] **Step 3: Update `tests/test_analysis_only.py`**

Find (appears twice, at line 147 and line 174 — both identical):

```python
    runtime = ExecutionRuntime(settings, codex=codex)
```

Replace **each** occurrence with:

```python
    runtime = ExecutionRuntime(settings, clis={"codex": codex})
```

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" -q`
Expected: All tests pass — 186 tests from before this plan, plus 13 new
(`test_agent_cli.py` + `test_agent_cli_fallback.py`) = 199, zero failures.

- [ ] **Step 5: Commit**

```bash
git add tests/test_orchestration_noop.py tests/test_project_scoping.py tests/test_analysis_only.py
git commit -m "test: migrate ExecutionRuntime(codex=...) call sites to clis={...}"
```
