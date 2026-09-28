# Agent CLI Panel Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator change which CLI (`codex`/`claude_code`) an agent role dispatches to — and see which CLI actually served each execution — from the dashboard, with no code edit and no API restart.

**Architecture:** Overrides live in a new SQLite table (`role_cli_overrides`), separate from the `AGENT_ROLES` code defaults in `agents/registry.py`. `ExecutionRuntime` merges the two at dispatch time via a new `_effective_role_config(role)` method, so a write to the table takes effect on the next task dispatched. Two new REST endpoints (`GET /agents/config`, `PUT /agents/{role}/config`) expose this to the dashboard, validating against the runtime's live, actually-constructed adapters — which requires fixing `ExecutionRuntime.__init__` to build every known adapter by default instead of only `codex`. `AgentResult.cli_used` (already produced by `run_task`) is threaded through `/dashboard-data` and the existing `agent.finished` SSE event into two dashboard surfaces: the per-agent config drawer and the "Nós" pipeline screen.

**Tech Stack:** Python 3 (FastAPI, Pydantic, sqlite3, pytest + pytest-asyncio), vanilla JS in a single-file `.dc.html` templated dashboard (`<x-dc>` custom elements, no build step).

## Global Constraints

- Two CLI names exist today and this plan does not add a third: `"codex"`, `"claude_code"` (`src/adapters/agent_cli.py:25`, `KNOWN_CLI_NAMES`).
- `validate_agent_roles` never checks `fallback_cli` (`src/adapters/agent_cli.py:38-51`) — this plan's new boot-time override validation follows the same rule: only a persisted `cli` override is validated at startup, never a persisted `fallback_cli`.
- `PUT /agents/{role}/config` must validate against `runtime.clis` (what THIS process actually built), never against `KNOWN_CLI_NAMES` (what the factory merely knows how to build).
- No "dispatch a new execution from the panel" form is in scope. Do not add one.
- Dashboard state changes that talk to the backend must not be decorative: every control this plan adds must reflect the real, persisted server state, and must revert on a failed request (see spec's Error Handling section) — unlike the pre-existing `AUTO_COMMIT`/`AUTO_MERGE`/`ALLOW_PRODUCTION_DEPLOY` toggles at `web/Plataforma de Administração.dc.html:1694-1698`, which is a known, separate, un-fixed issue — do not extend that pattern.

---

### Task 1: `role_cli_overrides` table + checkpointer methods

**Files:**
- Modify: `src/persistence/checkpointer.py`
- Test: `tests/test_role_cli_overrides.py` (new)

**Interfaces:**
- Produces: `SQLiteCheckpointer.get_role_cli_overrides() -> dict[str, dict[str, Any]]` — keys are role names, values are `{"cli": str, "fallback_cli": str | None}`. A role with no override is simply absent from the dict.
- Produces: `SQLiteCheckpointer.set_role_cli_override(role: str, cli: str, fallback_cli: str | None) -> None` — upserts; a second call for the same role overwrites, never duplicates.

Read `src/persistence/checkpointer.py` first — the `_init()` method builds the schema with one `executescript(...)` call, and `save`/`event`/`finish_active_agents` already use `datetime.now(timezone.utc).isoformat()` (imported at the top of the file) for timestamps and `with self._connect() as db:` for every query. Match this style exactly; do not import anything new.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_role_cli_overrides.py`:

```python
from __future__ import annotations

from pathlib import Path

from persistence.checkpointer import SQLiteCheckpointer


def test_get_role_cli_overrides_is_empty_before_any_write(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    assert store.get_role_cli_overrides() == {}


def test_set_then_get_round_trips_an_override(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")

    assert store.get_role_cli_overrides() == {"frontend": {"cli": "claude_code", "fallback_cli": "codex"}}


def test_set_role_cli_override_accepts_a_null_fallback(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)

    assert store.get_role_cli_overrides()["frontend"] == {"cli": "claude_code", "fallback_cli": None}


def test_setting_the_same_role_twice_overwrites_rather_than_duplicates(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")
    store.set_role_cli_override("frontend", "codex", None)

    overrides = store.get_role_cli_overrides()
    assert overrides == {"frontend": {"cli": "codex", "fallback_cli": None}}


def test_overrides_for_different_roles_do_not_interfere(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)
    store.set_role_cli_override("backend", "codex", "claude_code")

    overrides = store.get_role_cli_overrides()
    assert overrides == {
        "frontend": {"cli": "claude_code", "fallback_cli": None},
        "backend": {"cli": "codex", "fallback_cli": "claude_code"},
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_role_cli_overrides.py -v`
(Create the basetemp dir first if it doesn't exist: it must live outside any `.claude`-containing directory or pytest's collection fails.)
Expected: FAIL with `AttributeError: 'SQLiteCheckpointer' object has no attribute 'get_role_cli_overrides'`

- [ ] **Step 3: Add the table to `_init()`**

In `src/persistence/checkpointer.py`, find the `_init()` method's `executescript(...)` call — it currently ends with the `agent_runs` table definition, immediately before the closing `""")`. Add a new `CREATE TABLE` statement right after `agent_runs` and before the closing triple-quote:

```python
             CREATE TABLE IF NOT EXISTS agent_runs (
               id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
               agent TEXT NOT NULL, task_id TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS role_cli_overrides (
               role TEXT PRIMARY KEY, cli TEXT NOT NULL, fallback_cli TEXT, updated_at TEXT NOT NULL
             );
             """)
```

- [ ] **Step 4: Add the two methods**

Add these methods to `SQLiteCheckpointer` (anywhere among its other public methods, e.g. after `agent_metrics`):

```python
    def get_role_cli_overrides(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT role, cli, fallback_cli FROM role_cli_overrides").fetchall()
        return {row["role"]: {"cli": row["cli"], "fallback_cli": row["fallback_cli"]} for row in rows}

    def set_role_cli_override(self, role: str, cli: str, fallback_cli: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO role_cli_overrides (role, cli, fallback_cli, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(role) DO UPDATE SET cli=excluded.cli, fallback_cli=excluded.fallback_cli, updated_at=excluded.updated_at",
                (role, cli, fallback_cli, now),
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_role_cli_overrides.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add src/persistence/checkpointer.py tests/test_role_cli_overrides.py
git commit -m "feat: persist per-role CLI overrides in a new sqlite table"
```

---

### Task 2: Build every known CLI by default in `ExecutionRuntime`

**Files:**
- Modify: `src/orchestrator/nodes.py:13,35`
- Test: `tests/test_agent_cli_fallback.py` (add one test)

**Interfaces:**
- Consumes: `KNOWN_CLI_NAMES: set[str]` and `build_cli(name, settings)` from `src/adapters/agent_cli.py` (both already exist — `KNOWN_CLI_NAMES = {"codex", "claude_code"}`).
- Produces: `ExecutionRuntime(settings)` (no explicit `clis=`) now sets `self.clis` to a dict with an entry for every name in `KNOWN_CLI_NAMES`, not just `"codex"`.

Today, `src/orchestrator/nodes.py:35` reads:
```python
        self.clis = clis or {"codex": build_cli("codex", settings)}
```
This is the only reason `claude_code` isn't a real, dispatchable option anywhere the API actually runs (`src/api/app.py:146`, `src/orchestrator/graph.py:98`, `src/orchestrator/main.py:55` all construct `ExecutionRuntime(settings, ...)` with no `clis=` override). Building both by default is safe: neither `CodexCLI.__init__` nor `ClaudeCodeCLI.__init__` touches the filesystem, spawns a process, or raises — `resolve_claude_code_command` (`src/adapters/claude_code_cli.py:20-33`) falls back to a literal `["claude"]` when `which()` finds nothing. A genuinely missing binary only surfaces later, as a caught `OSError` inside `execute()` (`src/adapters/claude_code_cli.py:89-91`), the same place it already surfaces today for a bad `codex_command`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_cli_fallback.py` (it already imports `ExecutionRuntime`, `Settings`, `AGENT_ROLES`):

```python
def test_execution_runtime_builds_every_known_cli_by_default(tmp_path: Path) -> None:
    from adapters.agent_cli import KNOWN_CLI_NAMES

    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        claude_code_command="C:/definitely/not/a/real/path/claude-nonexistent.exe",
        checkpoint_sqlite_path=tmp_path / "checkpoints-default-clis.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph-default-clis.sqlite3",
    )

    runtime = ExecutionRuntime(settings)

    assert set(runtime.clis.keys()) == KNOWN_CLI_NAMES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli_fallback.py::test_execution_runtime_builds_every_known_cli_by_default -v`
Expected: FAIL — `assert {'codex'} == {'codex', 'claude_code'}`

- [ ] **Step 3: Fix the default**

In `src/orchestrator/nodes.py`, change the import at line 13 from:
```python
from adapters.agent_cli import AgentCLIAdapter, build_cli, validate_agent_roles
```
to:
```python
from adapters.agent_cli import AgentCLIAdapter, KNOWN_CLI_NAMES, build_cli, validate_agent_roles
```

Change line 35 from:
```python
        self.clis = clis or {"codex": build_cli("codex", settings)}
```
to:
```python
        self.clis = clis or {name: build_cli(name, settings) for name in KNOWN_CLI_NAMES}
```

- [ ] **Step 4: Run the full existing suite for this file to check for regressions**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_cli_fallback.py tests/test_claude_code_cli.py tests/test_codex_cli.py tests/test_orchestration_noop.py -v`
Expected: PASS, all tests (this default only changes what's built when a test doesn't override `clis=` — every existing test either passes its own `clis=` or only asserts behavior for roles/CLIs it explicitly configured).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_agent_cli_fallback.py
git commit -m "fix: construct every known CLI adapter by default, not just codex"
```

---

### Task 3: `_effective_role_config` — resolve overrides at dispatch

**Files:**
- Modify: `src/orchestrator/nodes.py` (`ExecutionRuntime.__init__`, `run_task`, `classify_relevant_projects`, plus one new method)
- Test: `tests/test_role_cli_overrides.py` (add tests)

**Interfaces:**
- Consumes: `self.store.get_role_cli_overrides()` from Task 1.
- Consumes: `self.clis` from Task 2 (now has both `codex` and `claude_code` by default).
- Produces: `ExecutionRuntime._effective_role_config(role: str) -> dict[str, Any]` — merges `AGENT_ROLES[role]` with any persisted override (override wins per-field: a role with only `cli` overridden keeps its code-default `fallback_cli`, and vice versa).

Read `src/orchestrator/nodes.py:99-171` (`run_task`) and `:218-243` (`classify_relevant_projects`) before starting — both currently read `AGENT_ROLES` directly for a role's `cli`/`fallback_cli`, and this task redirects both through the new method without changing anything else about their behavior.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_role_cli_overrides.py`:

```python
import pytest

from agents.registry import AGENT_ROLES
from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime


def runtime_settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / f"checkpoints-{name}.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / f"langgraph-{name}.sqlite3",
    )


def test_effective_role_config_falls_back_to_the_code_default_when_no_override_exists(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "no-override"))

    config = runtime._effective_role_config("frontend")

    assert config["cli"] == AGENT_ROLES["frontend"]["cli"]
    assert config["fallback_cli"] == AGENT_ROLES["frontend"]["fallback_cli"]


def test_effective_role_config_uses_the_persisted_cli_override(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "cli-override"))
    runtime.store.set_role_cli_override("frontend", "claude_code", None)

    config = runtime._effective_role_config("frontend")

    assert config["cli"] == "claude_code"


def test_effective_role_config_distinguishes_no_override_from_an_explicit_null_fallback(tmp_path: Path) -> None:
    """A role that has never been overridden must keep its code-default fallback_cli;
    a role explicitly overridden to have NO fallback must show None, not the default."""
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "null-fallback"))
    runtime.store.set_role_cli_override("frontend", "codex", "claude_code")
    runtime.store.set_role_cli_override("backend", "codex", None)

    assert runtime._effective_role_config("frontend")["fallback_cli"] == "claude_code"
    assert runtime._effective_role_config("backend")["fallback_cli"] is None


def test_execution_runtime_raises_at_construction_for_a_persisted_override_naming_an_unavailable_cli(tmp_path: Path) -> None:
    from persistence.checkpointer import SQLiteCheckpointer

    settings = runtime_settings(tmp_path, "stale-override")
    store = SQLiteCheckpointer(settings.checkpoint_path)
    store.set_role_cli_override("frontend", "totally_not_registered", None)

    with pytest.raises(ValueError, match="frontend.*totally_not_registered"):
        ExecutionRuntime(settings, store=store)


@pytest.mark.asyncio
async def test_run_task_dispatches_through_an_override_to_a_different_cli(tmp_path: Path) -> None:
    """End-to-end: an override changes which CLI a role actually uses, with no
    AGENT_ROLES edit and no monkeypatching of the registry."""
    import sys

    from adapters.codex_cli import CodexCLI

    codex_script = tmp_path / "fake_codex.py"
    codex_script.write_text(
        "import sys\nsys.exit('codex should never run once frontend is overridden to claude_code')\n",
        encoding="utf-8",
    )
    claude_script = tmp_path / "fake_claude.py"
    claude_script.write_text(
        "import json\nprint(json.dumps({"
        "'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok', "
        "'total_cost_usd': 0.01, 'usage': {'input_tokens': 4, 'output_tokens': 2}, "
        "'structured_output': {'status': 'completed', 'summary': 'handled via override', "
        "'files_changed': [], 'tests': [], 'contracts_changed': [], 'errors': [], "
        "'next_action': None, 'tokens_input': 0, 'tokens_output': 0, 'quality': None}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{codex_script}"',
        claude_code_command=f'"{sys.executable}" "{claude_script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints-e2e.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph-e2e.sqlite3",
    )
    from adapters.claude_code_cli import ClaudeCodeCLI
    clis = {"codex": CodexCLI(settings), "claude_code": ClaudeCodeCLI(settings)}
    runtime = ExecutionRuntime(settings, clis=clis)
    runtime.store.set_role_cli_override("frontend", "claude_code", None)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.summary == "handled via override"
    assert result.cli_used == "claude_code"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_role_cli_overrides.py -v`
Expected: FAIL — `AttributeError: 'ExecutionRuntime' object has no attribute '_effective_role_config'` (and the "stale override" test fails differently: `ExecutionRuntime` does not yet validate persisted overrides at all, so it raises nothing).

- [ ] **Step 3: Add `_effective_role_config` and wire it in**

In `src/orchestrator/nodes.py`, add this method to `ExecutionRuntime`, placed after `cancel_event` (right before `persist`):

```python
    def _effective_role_config(self, role: str) -> dict[str, Any]:
        config = dict(AGENT_ROLES.get(role, {}))
        override = self.store.get_role_cli_overrides().get(role)
        if override:
            if override.get("cli"):
                config["cli"] = override["cli"]
            if "fallback_cli" in override:
                config["fallback_cli"] = override["fallback_cli"]
        return config
```

In `run_task`, change (currently at `src/orchestrator/nodes.py:118`):
```python
        role_config = AGENT_ROLES.get(role, {})
```
to:
```python
        role_config = self._effective_role_config(role)
```

In `classify_relevant_projects`, change (currently at `src/orchestrator/nodes.py:236`):
```python
        classifier_cli = self.clis[AGENT_ROLES.get("classifier", {}).get("cli", "codex")]
```
to:
```python
        classifier_cli = self.clis[self._effective_role_config("classifier").get("cli", "codex")]
```

- [ ] **Step 4: Extend boot-time validation to persisted overrides**

In `ExecutionRuntime.__init__`, immediately after the existing line (`src/orchestrator/nodes.py:36`):
```python
        validate_agent_roles(AGENT_ROLES, available_clis=set(self.clis.keys()))
```
add:
```python
        for override_role, override in self.store.get_role_cli_overrides().items():
            if override.get("cli") not in self.clis:
                raise ValueError(
                    f"persisted override for role '{override_role}' declares cli='{override.get('cli')}', "
                    f"which is not in the configured clis {sorted(self.clis.keys())}"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_role_cli_overrides.py tests/test_agent_cli_fallback.py tests/test_claude_code_cli.py tests/test_orchestration_noop.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_role_cli_overrides.py
git commit -m "feat: resolve per-role CLI overrides at dispatch time"
```

---

### Task 4: `GET /agents/config` and `PUT /agents/{role}/config`

**Files:**
- Modify: `src/schemas/models.py` (new `AgentCLIConfigRequest`)
- Modify: `src/api/app.py`
- Test: `tests/test_agent_config_routes.py` (new)

**Interfaces:**
- Consumes: `runtime.clis` (Task 2), `runtime.store.get_role_cli_overrides()` / `set_role_cli_override()` (Task 1), `AGENT_ROLES` (`agents/registry.py`, already imported in `api/app.py:33`).
- Produces: `AgentCLIConfigRequest(BaseModel)` with fields `cli: str`, `fallback_cli: str | None = None`.
- Produces: `GET /agents/config` → `{"available": [...], "roles": {role: {"default_cli", "default_fallback_cli", "cli", "fallback_cli", "overridden"}}}`.
- Produces: `PUT /agents/{role}/config` → `{"role", "cli", "fallback_cli"}` on success; `404` for an unknown role; `400` for a `cli`/`fallback_cli` not in `runtime.clis`.

Read `src/api/app.py:425-438` (`project_path`, the two `/projects/{project_id}/skills` routes) first — this task's two routes follow that exact shape: a plain function, a `settings_store()`/`app.state.graph.runtime`-style accessor, `HTTPException(status_code=..., detail=...)` for errors.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_config_routes.py`:

```python
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from orchestrator.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path / "frontend",
        backend_path=tmp_path / "backend", python_path=tmp_path / "python",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_get_agents_config_returns_code_defaults_when_nothing_is_overridden(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.get("/agents/config")

    assert response.status_code == 200
    body = response.json()
    assert set(body["available"]) == {"codex", "claude_code"}
    assert body["roles"]["frontend"] == {
        "default_cli": "codex", "default_fallback_cli": None,
        "cli": "codex", "fallback_cli": None, "overridden": False,
    }


def test_put_then_get_reflects_the_override(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        put_response = client.put("/agents/frontend/config", json={"cli": "claude_code", "fallback_cli": "codex"})
        get_response = client.get("/agents/config")

    assert put_response.status_code == 200
    assert put_response.json() == {"role": "frontend", "cli": "claude_code", "fallback_cli": "codex"}
    assert get_response.json()["roles"]["frontend"] == {
        "default_cli": "codex", "default_fallback_cli": None,
        "cli": "claude_code", "fallback_cli": "codex", "overridden": True,
    }


def test_put_agent_config_rejects_an_unknown_role(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/not-a-role/config", json={"cli": "codex"})

    assert response.status_code == 404


def test_put_agent_config_rejects_an_unknown_cli_and_persists_nothing(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/frontend/config", json={"cli": "ghost_cli"})
        get_response = client.get("/agents/config")

    assert response.status_code == 400
    assert get_response.json()["roles"]["frontend"]["overridden"] is False


def test_put_agent_config_rejects_an_unknown_fallback_cli(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/frontend/config", json={"cli": "codex", "fallback_cli": "ghost_cli"})

    assert response.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_config_routes.py -v`
Expected: FAIL with `404 Not Found` for both routes (they don't exist yet).

- [ ] **Step 3: Add `AgentCLIConfigRequest`**

In `src/schemas/models.py`, add this class after `ExecutionRequest` (currently ending at line 148):

```python
class AgentCLIConfigRequest(BaseModel):
    cli: str
    fallback_cli: str | None = None
```

- [ ] **Step 4: Add the two routes**

In `src/api/app.py`, change the import at line 36 from:
```python
from schemas.models import ExecutionRequest
```
to:
```python
from schemas.models import AgentCLIConfigRequest, ExecutionRequest
```

Add the two routes immediately after `delete_project_skill` (the last route currently in the file, at `src/api/app.py:440-441` and whatever follows it):

```python
    @app.get("/agents/config")
    async def get_agents_config() -> dict[str, Any]:
        runtime = app.state.graph.runtime
        overrides = runtime.store.get_role_cli_overrides()
        available = sorted(runtime.clis.keys())
        roles = {}
        for role, defaults in AGENT_ROLES.items():
            override = overrides.get(role, {})
            roles[role] = {
                "default_cli": defaults.get("cli", "codex"),
                "default_fallback_cli": defaults.get("fallback_cli"),
                "cli": override.get("cli", defaults.get("cli", "codex")),
                "fallback_cli": override.get("fallback_cli", defaults.get("fallback_cli")),
                "overridden": role in overrides,
            }
        return {"available": available, "roles": roles}

    @app.put("/agents/{role}/config")
    async def put_agent_config(role: str, request: AgentCLIConfigRequest) -> dict[str, Any]:
        if role not in AGENT_ROLES:
            raise HTTPException(status_code=404, detail=f"unknown role '{role}'")
        runtime = app.state.graph.runtime
        if request.cli not in runtime.clis:
            raise HTTPException(status_code=400, detail=f"unknown cli '{request.cli}' — available: {sorted(runtime.clis.keys())}")
        if request.fallback_cli is not None and request.fallback_cli not in runtime.clis:
            raise HTTPException(status_code=400, detail=f"unknown fallback_cli '{request.fallback_cli}' — available: {sorted(runtime.clis.keys())}")
        runtime.store.set_role_cli_override(role, request.cli, request.fallback_cli)
        return {"role": role, "cli": request.cli, "fallback_cli": request.fallback_cli}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_agent_config_routes.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add src/schemas/models.py src/api/app.py tests/test_agent_config_routes.py
git commit -m "feat: add GET/PUT /agents/config endpoints for per-role CLI overrides"
```

---

### Task 5: Surface `cli_used` in `/dashboard-data`

**Files:**
- Modify: `src/api/app.py:294-298`
- Modify: `tests/test_dashboard_data.py` (existing test's exact-equality assertion needs the new field)

**Interfaces:**
- Produces: each entry in `/dashboard-data`'s `items[].tasks[]` now includes `"cli_used": <str | None>`, read from `result.get("cli_used")` — the same `result` dict already used for `summary`/`files_changed`/`errors`.

The existing test `test_dashboard_data_includes_trimmed_per_agent_task_summaries` (`tests/test_dashboard_data.py:103-122`) asserts `tasks["T001"]` equals an exact dict with no `cli_used` key — adding the field to the endpoint without updating this assertion will break it, since the seeded fixture result (`tests/test_dashboard_data.py:36-41`) has no `cli_used` key, and `result.get("cli_used")` will be `None`.

- [ ] **Step 1: Update the existing test's assertion first (red)**

In `tests/test_dashboard_data.py`, change the assertion at lines 115-118 from:
```python
    assert tasks["T001"] == {
        "task_id": "T001", "agent": "frontend", "status": "completed",
        "summary": "Added the Button component", "files_changed": ["src/Button.tsx"], "errors": [],
    }
```
to:
```python
    assert tasks["T001"] == {
        "task_id": "T001", "agent": "frontend", "status": "completed",
        "summary": "Added the Button component", "files_changed": ["src/Button.tsx"], "errors": [],
        "cli_used": None,
    }
```

Also add a new test in the same file, after `test_dashboard_data_includes_trimmed_per_agent_task_summaries`:

```python
@pytest.mark.asyncio
async def test_dashboard_data_surfaces_which_cli_served_a_task(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    state = await _seed_completed_execution(settings, "exec-completed-cli")
    state["plan"][0]["result"]["cli_used"] = "claude_code"
    SQLiteCheckpointer(settings.checkpoint_path).save("exec-completed-cli", state, "generate_final_report")
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = state
        await saver.aput(
            {"configurable": {"thread_id": "exec-completed-cli", "checkpoint_ns": ""}}, checkpoint,
            {"source": "input", "step": -1, "writes": {}, "parents": {}}, {},
        )

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/dashboard-data")

    item = next(i for i in response.json()["items"] if i["execution_id"] == "exec-completed-cli")
    tasks = {t["task_id"]: t for t in item["tasks"]}
    assert tasks["T001"]["cli_used"] == "claude_code"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_dashboard_data.py -v`
Expected: FAIL — the updated assertion at Step 1 fails because `/dashboard-data` doesn't emit `cli_used` yet (`KeyError`-shaped dict mismatch), and the new test's `tasks["T001"]["cli_used"]` raises `KeyError`.

- [ ] **Step 3: Add the field**

In `src/api/app.py`, change the `tasks.append(...)` block at lines 294-298 from:
```python
                tasks.append({
                    "task_id": task.get("task_id"), "agent": task["agent"], "status": task.get("status", "pending"),
                    "summary": result.get("summary", ""), "files_changed": result.get("files_changed", []),
                    "errors": result.get("errors", []),
                })
```
to:
```python
                tasks.append({
                    "task_id": task.get("task_id"), "agent": task["agent"], "status": task.get("status", "pending"),
                    "summary": result.get("summary", ""), "files_changed": result.get("files_changed", []),
                    "errors": result.get("errors", []), "cli_used": result.get("cli_used"),
                })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_dashboard_data.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/api/app.py tests/test_dashboard_data.py
git commit -m "feat: surface cli_used in /dashboard-data per-task summaries"
```

---

### Task 6: Dashboard — CLI picker in the agent detail drawer

**Files:**
- Modify: `web/Plataforma de Administração.dc.html`

**Interfaces:**
- Consumes: `GET /agents/config`, `PUT /agents/{role}/config` (Task 4).
- Consumes existing helpers: `this.apiUrl(path)` (`:1588-1590`), `this.authHeaders(extra)` (`:1592-1598`), `this.pushLog(text)` (`:1283`), `this.showToast(...)` (used at `:1815` and elsewhere — same call signature).

This dashboard has no build step or automated test harness (confirmed for the existing "Nós" screen and skill-install UI this session) — verify this task by running the dev server and using the drawer in a browser, per the spec's Testing section. There is no failing-test step for this task.

- [ ] **Step 1: Add state fields**

Find the component's initial `state` object (`web/Plataforma de Administração.dc.html:975-976`, next to `nodeStatus: {}` and `nodeTokens: {}`). Add two fields:

```javascript
    agentCliConfig: null,
    agentCliDraft: null,
```

`agentCliConfig` holds the last-fetched `{available, roles}` response; `agentCliDraft` holds `{cli, fallback_cli}` for whichever role's drawer is currently open, tracking unsaved `<select>` changes.

- [ ] **Step 2: Fetch config when the drawer opens**

`buildAgents()` (`web/Plataforma de Administração.dc.html:1398`) builds the `agents` list for the "Agentes" screen; each card's click handler is currently, at `web/Plataforma de Administração.dc.html:1496`:
```javascript
        onSelect: function () { self.setState({ selectedAgentKey: a.key }); self.loadSkills(pid); },
```
Add a new method next to `loadSkills` (used right there) that fetches the CLI config:

```javascript
  openAgentCliConfig(agentKey) {
    fetch(this.apiUrl("/agents/config"), { headers: this.authHeaders() })
      .then((r) => r.json())
      .then((data) => {
        const roleConfig = data.roles[agentKey] || { cli: "codex", fallback_cli: null };
        this.setState({
          agentCliConfig: data,
          agentCliDraft: { cli: roleConfig.cli, fallback_cli: roleConfig.fallback_cli },
        });
      })
      .catch(() => this.pushLog("Falha ao carregar configuração de CLI para " + agentKey + "."));
  }
```

Change the `onSelect` handler at `:1496` to also call it:
```javascript
        onSelect: function () { self.setState({ selectedAgentKey: a.key }); self.loadSkills(pid); self.openAgentCliConfig(a.key); },
```

- [ ] **Step 3: Render the picker in the drawer**

In the agent detail drawer (`web/Plataforma de Administração.dc.html:270-320`), add a new section after the "Métricas" block (after the `</div>` that closes the metrics grid, before the `hasLastResult` block at line 301):

```html
            <div>
              <div style="font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:#5A564C; margin-bottom:8px; border-bottom:1px solid #14140F; padding-bottom:4px;">CLI</div>
              <sc-if value="{{ hasAgentCliDraft }}" hint-placeholder-val="{{ false }}">
                <div style="display:flex; flex-direction:column; gap:8px;">
                  <label style="font-size:10.5px; text-transform:uppercase; color:#5A564C;">Principal
                    <select onChange="{{ onAgentCliChange }}" value="{{ agentCliDraftCli }}" style="width:100%; padding:6px; border:1.5px solid #14140F; background:#F6F4EC; margin-top:2px;">
                      <sc-for list="{{ agentCliOptions }}" as="o" hint-placeholder-count="2">
                        <option value="{{ o }}">{{ o }}</option>
                      </sc-for>
                    </select>
                  </label>
                  <label style="font-size:10.5px; text-transform:uppercase; color:#5A564C;">Reserva (fallback)
                    <select onChange="{{ onAgentFallbackChange }}" value="{{ agentCliDraftFallback }}" style="width:100%; padding:6px; border:1.5px solid #14140F; background:#F6F4EC; margin-top:2px;">
                      <option value="">nenhum</option>
                      <sc-for list="{{ agentCliOptions }}" as="o" hint-placeholder-count="2">
                        <option value="{{ o }}">{{ o }}</option>
                      </sc-for>
                    </select>
                  </label>
                  <button onClick="{{ onSaveAgentCli }}" style="align-self:flex-start; padding:7px 14px; background:#14140F; color:#fff; border:none; font-size:10.5px; text-transform:uppercase; letter-spacing:0.04em; cursor:pointer;">Salvar</button>
                </div>
              </sc-if>
            </div>
```

- [ ] **Step 4: Add the render-vals bindings and handlers**

In `renderVals()`, inside whatever block already computes `selectedAgent` (used for the drawer), add these keys to the returned object:

```javascript
      hasAgentCliDraft: !!this.state.agentCliDraft,
      agentCliOptions: this.state.agentCliConfig ? this.state.agentCliConfig.available : [],
      agentCliDraftCli: this.state.agentCliDraft ? this.state.agentCliDraft.cli : "",
      agentCliDraftFallback: this.state.agentCliDraft ? (this.state.agentCliDraft.fallback_cli || "") : "",
      onAgentCliChange: (e) => this.setState((s) => ({ agentCliDraft: Object.assign({}, s.agentCliDraft, { cli: e.target.value }) })),
      onAgentFallbackChange: (e) => this.setState((s) => ({ agentCliDraft: Object.assign({}, s.agentCliDraft, { fallback_cli: e.target.value || null }) })),
      onSaveAgentCli: () => this.saveAgentCliConfig(),
```

Add the save method next to `openAgentCliConfig` (Step 2):

```javascript
  saveAgentCliConfig() {
    const role = this.state.selectedAgentKey;
    const draft = this.state.agentCliDraft;
    const previous = this.state.agentCliConfig.roles[role];
    fetch(this.apiUrl("/agents/" + role + "/config"), {
      method: "PUT",
      headers: this.authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ cli: draft.cli, fallback_cli: draft.fallback_cli }),
    })
      .then((r) => { if (!r.ok) throw new Error("bad status " + r.status); return r.json(); })
      .then((saved) => {
        this.setState((s) => ({
          agentCliConfig: Object.assign({}, s.agentCliConfig, {
            roles: Object.assign({}, s.agentCliConfig.roles, {
              [role]: Object.assign({}, s.agentCliConfig.roles[role], { cli: saved.cli, fallback_cli: saved.fallback_cli, overridden: true }),
            }),
          }),
        }));
        this.pushLog("CLI de " + role + " definida para " + saved.cli + ".");
        this.showToast("Configuração salva para " + role + ".");
      })
      .catch(() => {
        this.setState({ agentCliDraft: { cli: previous.cli, fallback_cli: previous.fallback_cli } });
        this.pushLog("Falha ao salvar CLI para " + role + " — revertido.");
        this.showToast("Falha ao salvar. Revertido ao valor anterior.");
      });
  }
```

- [ ] **Step 5: Manual verification**

Start the dev server (project's existing run command, e.g. `python -m orchestrator.main` or `uvicorn api.app:create_app --factory`), open the dashboard, open the "Agentes" screen, click a card to open its drawer. Confirm:
- The CLI section shows the role's current `cli`/`fallback_cli`.
- Changing the dropdowns and clicking Salvar shows a success toast and a `pushLog` line.
- Reloading the page and reopening the same drawer shows the saved value (proves the `PUT` actually persisted, not just local state).
- Stopping the API and clicking Salvar again shows a failure toast and the dropdown reverts to its last-saved value (proves the revert-on-failure path).

- [ ] **Step 6: Commit**

```bash
git add "web/Plataforma de Administração.dc.html"
git commit -m "feat: add per-agent CLI picker to the dashboard drawer"
```

---

### Task 7: Dashboard — `cli_used` chip on the "Nós" screen

**Files:**
- Modify: `web/Plataforma de Administração.dc.html`

**Interfaces:**
- Consumes: `event.result.cli_used` from the existing `agent.finished` SSE event (already delivered in full via `event.result`, per `announce_agent_finished` in `src/orchestrator/nodes.py:210-216` — no backend change needed here, `cli_used` already rides on `result`).

- [ ] **Step 1: Add `nodeCli` state**

Next to `nodeStatus: {}, nodeTokens: {},` (`web/Plataforma de Administração.dc.html:975-976`), add:

```javascript
    nodeCli: {},
```

- [ ] **Step 2: Populate it in `handlePipelineEvent`**

In `handlePipelineEvent` (`web/Plataforma de Administração.dc.html:1135-1169`), the destructuring at the top of the `setState` callback currently reads:
```javascript
      let nodeStatus = s.nodeStatus;
      let nodeTokens = s.nodeTokens;
      let execId = s.pipelineExecId;
      if (execId !== event.execution_id) {
        execId = event.execution_id;
        nodeStatus = {};
        nodeTokens = {};
      }
```
Change to also track `nodeCli`:
```javascript
      let nodeStatus = s.nodeStatus;
      let nodeTokens = s.nodeTokens;
      let nodeCli = s.nodeCli;
      let execId = s.pipelineExecId;
      if (execId !== event.execution_id) {
        execId = event.execution_id;
        nodeStatus = {};
        nodeTokens = {};
        nodeCli = {};
      }
```

The `agent.finished` branch currently reads:
```javascript
      } else if (event.type === "agent.finished" && event.result) {
        const nodeId = Component.AGENT_NODE_MAP[event.agent];
        if (nodeId) {
          const prev = nodeTokens[nodeId] || { input: 0, output: 0, cost: 0 };
          nodeTokens = Object.assign({}, nodeTokens, { [nodeId]: {
            input: prev.input + (event.result.tokens_input || 0),
            output: prev.output + (event.result.tokens_output || 0),
            cost: prev.cost + (event.result.estimated_cost || 0),
          } });
        }
      }
```
Change to also capture `cli_used`:
```javascript
      } else if (event.type === "agent.finished" && event.result) {
        const nodeId = Component.AGENT_NODE_MAP[event.agent];
        if (nodeId) {
          const prev = nodeTokens[nodeId] || { input: 0, output: 0, cost: 0 };
          nodeTokens = Object.assign({}, nodeTokens, { [nodeId]: {
            input: prev.input + (event.result.tokens_input || 0),
            output: prev.output + (event.result.tokens_output || 0),
            cost: prev.cost + (event.result.estimated_cost || 0),
          } });
          if (event.result.cli_used) {
            nodeCli = Object.assign({}, nodeCli, { [nodeId]: event.result.cli_used });
          }
        }
      }
```

And the final return of that `setState` callback, currently:
```javascript
      return { pipelineExecId: execId, nodeStatus: nodeStatus, nodeTokens: nodeTokens };
```
becomes:
```javascript
      return { pipelineExecId: execId, nodeStatus: nodeStatus, nodeTokens: nodeTokens, nodeCli: nodeCli };
```

- [ ] **Step 3: Read it in `buildPipelineNodes`**

In `buildPipelineNodes()` (`web/Plataforma de Administração.dc.html:1548-1571`), add a line next to `const tokensMap = this.state.nodeTokens || {};`:

```javascript
    const cliMap = this.state.nodeCli || {};
```

and add a field to the object returned inside `Component.NODE_DEFS.map((n) => { ... })`, next to `costLabel`:

```javascript
        cliLabel: hasAgent ? (cliMap[n.id] || "—") : "—",
```

- [ ] **Step 4: Render the chip**

In the "Nós" screen's per-node stat row (`web/Plataforma de Administração.dc.html:437-450`), add a fourth stat block after the "Custo estimado" block:

```html
                  <div>
                    <div style="font-size:9px; color:#9B9689; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:2px;">CLI</div>
                    <div style="font-size:11px; color:#14140F; font-weight:700;">{{ n.cliLabel }}</div>
                  </div>
```

- [ ] **Step 5: Manual verification**

Trigger a real execution (or reuse SSE test tooling already in this repo, e.g. `tests/test_events_view.py`'s approach, for manual replay) and watch the "Nós" screen: each node that dispatches to an agent should show its CLI once that agent finishes. Change a role's CLI via the Task 6 drawer first, trigger another execution, and confirm the chip for that node now shows the new CLI.

- [ ] **Step 6: Commit**

```bash
git add "web/Plataforma de Administração.dc.html"
git commit -m "feat: show which CLI served each node on the Nós pipeline screen"
```

---

## Self-Review

**Spec coverage:**
- Problem (no runtime toggle, no `cli_used` rendering, `claude_code` not actually buildable by default) → Tasks 1, 2, 3, 4 (storage + resolution + endpoints), 6, 7 (UI).
- Architecture (overrides in SQLite, merged at dispatch, validated against live `self.clis`) → Tasks 1, 2, 3.
- Components 1–7 from the spec → map 1:1 to plan Tasks 2, 1, 3, 4, 4, 6, 7 respectively.
- Data Flow → exercised end-to-end by Task 3's `test_run_task_dispatches_through_an_override_to_a_different_cli` and Task 6/7's manual verification.
- Error Handling (404 unknown role, 400 unknown cli/fallback, stale-override boot failure, revert-on-failed-PUT) → covered by Task 4's tests and Task 3's boot-validation test; UI revert covered by Task 6 Step 5's manual check.
- Testing section's explicit list → every bullet has a corresponding automated test in Tasks 1, 3, 4, 5, except the two dashboard bullets, which the spec itself scopes to manual verification (no test infra exists for the `.dc.html` file) — matched in Tasks 6 and 7.
- Out of scope (no new-execution-dispatch form) → not built; no task adds one.

**Placeholder scan:** no TBD/TODO; every step has literal code or an exact command with expected output.

**Type consistency:** `_effective_role_config` returns a `dict[str, Any]` with `cli`/`fallback_cli` keys in Tasks 3 and consumed identically in `run_task`/`classify_relevant_projects`; `AgentCLIConfigRequest.cli`/`fallback_cli` (Task 4) match the JSON body shape the Task 6 dashboard code sends (`{cli, fallback_cli}`); `get_role_cli_overrides()`'s return shape (`{"cli":..., "fallback_cli":...}`, Task 1) matches what `_effective_role_config` (Task 3) and `get_agents_config` (Task 4) both read from it.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-28-agent-cli-panel-config.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
