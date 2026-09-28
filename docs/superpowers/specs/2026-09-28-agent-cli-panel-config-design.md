# Agent CLI Panel Config — Design

## Problem

`agents/registry.py`'s `AGENT_ROLES` hardcodes each role's `cli`/`fallback_cli`
(all currently `"codex"`/`None`). Changing which CLI a role uses today means
editing this Python module and restarting the API process — there is no way
to do it from the dashboard, and no way to do it without a restart.

Separately, `AgentResult.cli_used` already exists on every result (set by
`ExecutionRuntime.run_task`, see `src/orchestrator/nodes.py:169-171` and the
fallback path at `:166`) but nothing in the UI renders it. The dashboard
(`web/Plataforma de Administração.dc.html`) has no way to show which CLI
actually served a given execution — a real gap after this session's
CLI-agnostic execution work (two working adapters, `codex` and `claude_code`,
but the panel can't tell you which one ran).

A third, previously-invisible gap surfaced while investigating this: even
though `claude_code` is a fully working adapter (`src/adapters/claude_code_cli.py`,
registered in `KNOWN_CLI_NAMES`), the production runtime never actually
constructs it. `ExecutionRuntime.__init__` (`src/orchestrator/nodes.py:35`)
defaults to `self.clis = clis or {"codex": build_cli("codex", settings)}` —
only `codex` is ever built unless a caller passes `clis=` explicitly, and
none of the three call sites (`api/app.py:146`, `orchestrator/graph.py:98`,
`orchestrator/main.py:55`) do. So today, even editing `registry.py` by hand
to set `"cli": "claude_code"` would fail `validate_agent_roles` at startup —
`claude_code` is not in `available_clis`. This must be fixed as part of this
work, or the panel would have nothing real to switch to.

## Scope

**In scope:** a way to view and change, per role, which CLI (`codex` /
`claude_code`) it dispatches to and which CLI it falls back to on
`rate_limited` — from the dashboard, without editing code or restarting the
API — plus rendering `cli_used` wherever the dashboard already shows a
finished agent run.

**Out of scope (explicitly deferred, per earlier scoping decision):** a
"dispatch a new execution from the panel" form. The dashboard has never had
one — executions are created externally (MCP tool call, `curl`, `python -m
orchestrator run`) — and this feature does not add one. The CLI picker
configures what an *existing, externally-triggered* execution will use, the
same way `registry.py` does today.

## Architecture

Overrides are stored in a new SQLite table (`role_cli_overrides`), not
back in `registry.py` — the code module stays the source of *defaults*;
the database holds *live operator overrides* on top of it, the same
separation the checkpoint store already uses for execution state.
`ExecutionRuntime` resolves the effective `cli`/`fallback_cli` for a role
by merging the two at dispatch time (override wins per-field), so a change
made in the panel takes effect on the very next task dispatched — no
restart, because nothing about the running process's code changes, only a
row it reads before making its next CLI call.

Validation stays where it already is, conceptually: `validate_agent_roles`
checks every role's `cli` against the runtime's actually-constructed
adapters at startup; the new `PUT` endpoint runs the same check against the
same live `self.clis` before persisting a write, so a typo'd or
not-yet-implemented CLI name can never be saved, matching the boot-time
guarantee instead of adding a second, looser one.

The default `self.clis` dict is fixed to build every adapter in
`KNOWN_CLI_NAMES`, not just `"codex"` — this is what makes `claude_code`
a real, selectable option in the panel rather than a name that would
immediately 400.

## Components

### 1. `src/orchestrator/nodes.py` — build both CLIs by default

```python
self.clis = clis or {name: build_cli(name, settings) for name in KNOWN_CLI_NAMES}
```

replacing the current `src/orchestrator/nodes.py:35`:
```python
self.clis = clis or {"codex": build_cli("codex", settings)}
```

Requires importing `KNOWN_CLI_NAMES` alongside the existing
`AgentCLIAdapter, build_cli, validate_agent_roles` import at
`src/orchestrator/nodes.py:13`. Safe to build both unconditionally: neither
`CodexCLI.__init__` nor `ClaudeCodeCLI.__init__` touches the filesystem or
raises — `resolve_claude_code_command`/`resolve_codex_command` fall back to
a literal command string when `which()` finds nothing, and a truly missing
binary only surfaces later, as a caught `OSError` inside `execute()`
(`src/adapters/claude_code_cli.py:89-91`). A CLI with no binary installed
simply fails at dispatch time with a clear `AgentResult(status="failed", ...)`
— the same behavior a bad `registry.py` value produces today — not at boot.

### 2. `src/persistence/checkpointer.py` — override storage

Add a table to the existing `_init()` `executescript(...)` call
(`src/persistence/checkpointer.py`, the block currently ending after
`agent_runs`):

```python
             CREATE TABLE IF NOT EXISTS role_cli_overrides (
               role TEXT PRIMARY KEY, cli TEXT NOT NULL, fallback_cli TEXT, updated_at TEXT NOT NULL
             );
```

Two new methods on `SQLiteCheckpointer`, following the file's existing
`with self._connect() as db:` / `db.execute(...)` style:

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

(`datetime`/`timezone` are already imported in `checkpointer.py` — same
pattern as the existing `save`/`event` methods at lines 100/130 — no new
import needed.) No delete/reset method —
out of scope; an operator who wants to revert sets the override back to the
role's code default explicitly, which round-trips through the same `PUT`.

### 3. `src/orchestrator/nodes.py` — resolve overrides at dispatch

New `ExecutionRuntime` method, placed alongside `workdir_for`/`prepare_worktree`:

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

`run_task` (`src/orchestrator/nodes.py:118-120`) changes from:
```python
        role_config = AGENT_ROLES.get(role, {})
        cli = self.clis[role_config.get("cli", "codex")]
        fallback_cli_name = role_config.get("fallback_cli")
```
to:
```python
        role_config = self._effective_role_config(role)
        cli = self.clis[role_config.get("cli", "codex")]
        fallback_cli_name = role_config.get("fallback_cli")
```

`classify_relevant_projects` (`src/orchestrator/nodes.py:236`) changes from:
```python
        classifier_cli = self.clis[AGENT_ROLES.get("classifier", {}).get("cli", "codex")]
```
to:
```python
        classifier_cli = self.clis[self._effective_role_config("classifier").get("cli", "codex")]
```

`__init__`'s boot-time `validate_agent_roles(AGENT_ROLES, ...)` call
(`src/orchestrator/nodes.py:36`) is extended to also validate any
already-persisted overrides, so a stale override left over from a CLI that
was later removed from `KNOWN_CLI_NAMES` fails loudly at boot instead of
crashing the first task that hits it:

```python
        validate_agent_roles(AGENT_ROLES, available_clis=set(self.clis.keys()))
        for role, override in self.store.get_role_cli_overrides().items():
            if override.get("cli") not in self.clis:
                raise ValueError(
                    f"persisted override for role '{role}' declares cli='{override.get('cli')}', "
                    f"which is not in the configured clis {sorted(self.clis.keys())}"
                )
```

`fallback_cli` overrides are deliberately not boot-validated here, for the
same reason `validate_agent_roles` already excludes `fallback_cli` from its
own check (`src/adapters/agent_cli.py:41-43`): a fallback may legitimately
name a CLI that doesn't exist in this particular deployment yet.

### 4. `src/api/app.py` — two new endpoints

Both follow the existing `/projects/{project_id}/skills` shape
(`src/api/app.py:431-434`, reading `app.state.graph.runtime` the same way
`settings_store()` at `:208-209` already reaches into the running graph).

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

`AgentCLIConfigRequest` is a new Pydantic model in `schemas/models.py`,
next to the existing `ExecutionRequest`:

```python
class AgentCLIConfigRequest(BaseModel):
    cli: str
    fallback_cli: str | None = None
```

Both need `from schemas.models import ..., AgentCLIConfigRequest` added to
`src/api/app.py:36`'s existing import line.

### 5. `src/api/app.py` — surface `cli_used` in `/dashboard-data`

One field added to the per-task dict built at `src/api/app.py:294-298`:

```python
                tasks.append({
                    "task_id": task.get("task_id"), "agent": task["agent"], "status": task.get("status", "pending"),
                    "summary": result.get("summary", ""), "files_changed": result.get("files_changed", []),
                    "errors": result.get("errors", []), "cli_used": result.get("cli_used"),
                })
```

### 6. Dashboard — CLI picker in the agent detail drawer

The per-agent drawer (`web/Plataforma de Administração.dc.html:270-320`,
opened by clicking a card in the "Agentes" screen) gains a CLI section
after the existing "Métricas" block: a `<select>` bound to
`selectedAgent.cli` with options built from `GET /agents/config`'s
`available` list, a same-row `<select>` for `fallback_cli` (plus a
"nenhum" / none option), and a "Salvar" button that calls
`PUT /agents/{role}/config` via `fetch(this.apiUrl(...), { method: "PUT",
headers: this.authHeaders({"Content-Type": "application/json"}), body:
JSON.stringify({cli, fallback_cli}) })`, following the exact pattern
`installSkill`/`removeSkill` already use for POST/DELETE with
`authHeaders` + `pushLog` + `showToast` (`web/Plataforma de
Administração.dc.html:1347-1361`). Unlike the pre-existing `AUTO_COMMIT`/
`AUTO_MERGE`/`ALLOW_PRODUCTION_DEPLOY` toggles (`:1694-1698`), which only
call `this.setState`/`this.pushLog` and never touch the backend, this
control's state change is not considered applied until the `PUT` call
resolves — on success it updates local state and shows a success toast; on
failure (network error or 400 from an invalid combination) it reverts the
`<select>` to its last-saved value and shows a failure toast, so the panel
never silently displays a choice that was not actually persisted.

`GET /agents/config` is fetched once when the drawer opens (alongside the
existing per-agent data already assembled for `selectedAgent`), not
polled — an override change only matters to the operator making it, and
the next full `/dashboard-data` refresh (already on a poll/SSE-driven
timer per the existing dashboard architecture) will reflect it for anyone
else.

### 7. Dashboard — `cli_used` chip on the "Nós" screen

`buildPipelineNodes()` (`web/Plataforma de Administração.dc.html:1548-1571`)
gains a `cliLabel` field, sourced from a new `nodeCli` state map populated
the same way `nodeTokens` already is, in `handlePipelineEvent`
(`:1157-1166`):

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

`nodeCli: {}` added to initial state next to `nodeStatus`/`nodeTokens`
(`:975-976`); `buildPipelineNodes()` reads `this.state.nodeCli` the same
way it reads `this.state.nodeTokens`, and adds:

```javascript
        cliLabel: hasAgent ? (cliMap[n.id] || "—") : "—",
```

Rendered as a fourth stat next to Tokens/Custo in the node card
(`web/Plataforma de Administração.dc.html:437-450`), same markup shape as
the existing three `<div>` stat blocks, labeled "CLI".

A gate role (`contracts`/`reviewer`) that ran across multiple projects with
disagreeing CLIs already arrives here as a comma-joined string (see
`_run_gate_agent_across_projects`, `src/orchestrator/nodes.py:475-476`) —
the chip renders that string as-is, no special-casing needed.

## Data Flow

1. Operator opens the "Agentes" drawer for a role → dashboard fetches
   `GET /agents/config` → picker shows current effective `cli`/`fallback_cli`
   (override if one exists, else the code default) and the live `available`
   list.
2. Operator picks a different CLI, clicks Salvar → `PUT
   /agents/{role}/config` validates against `runtime.clis`, writes to
   `role_cli_overrides` via `SQLiteCheckpointer.set_role_cli_override`.
3. Next time any execution reaches `run_task`/`classify_relevant_projects`
   for that role, `_effective_role_config` reads the override fresh from
   SQLite and dispatches to the newly configured CLI — no restart.
4. That dispatch's `AgentResult.cli_used` is set as it already is today
   (`run_task` lines 166/170) — unchanged by this feature.
5. `cli_used` now flows through `/dashboard-data`'s per-task list and the
   `agent.finished` SSE event (already carries `event.result` in full) to
   both the "Agentes" drawer's last-result view and the "Nós" screen's new
   chip.

## Error Handling

- **Unknown role in `PUT /agents/{role}/config`:** 404, matching
  `project_path()`'s existing 404-on-unknown-id pattern (`src/api/app.py:428`).
- **Unknown `cli`/`fallback_cli` name:** 400 with the live `available` list
  in the message — never persisted. This is checked against
  `runtime.clis` (what THIS process actually built), not `KNOWN_CLI_NAMES`
  (what the factory merely knows how to build), so a name that is
  structurally valid but not constructed in this deployment is still
  rejected.
- **Stale persisted override for a CLI no longer available at boot:**
  `ExecutionRuntime.__init__` raises `ValueError` immediately, the same
  fail-fast behavior `validate_agent_roles` already gives a bad
  `registry.py` value — an operator must fix or clear the override (by
  writing a new one via the same `PUT`, pointed at the API's previous
  instance, or directly in SQLite) before the API can start.
- **Panel `PUT` request fails (network or 400):** the `<select>` reverts
  to its last confirmed value and a failure toast is shown; the in-memory
  drawer state is never allowed to drift from what is actually persisted.
- **A role with no override:** `get_role_cli_overrides()` simply omits it;
  `_effective_role_config` falls back to `AGENT_ROLES[role]` unchanged —
  behavior for every role is identical to today until an operator
  explicitly sets an override.

## Testing

- `tests/test_persistence_checkpointer.py` (or wherever the existing
  checkpointer tests live): `set_role_cli_override` then
  `get_role_cli_overrides` round-trips; a second `set_role_cli_override`
  for the same role overwrites rather than duplicating (exercises the
  `ON CONFLICT` upsert).
- `tests/test_orchestrator_nodes.py`-equivalent: `_effective_role_config`
  returns the code default when no override exists; returns the override's
  `cli` when one is set; returns the override's `fallback_cli` even when
  it's explicitly `None` (must distinguish "not overridden" from
  "overridden to no fallback" — covered by the `"fallback_cli" in
  override` check rather than a truthiness check).
- `ExecutionRuntime.__init__` raises `ValueError` when a persisted override
  names a `cli` not in the constructed `self.clis` — fixture pre-seeds the
  store with a bad override before construction.
- `run_task` end-to-end: seed an override that swaps a role's `cli` from
  `codex` to `claude_code` (both fakes already exist as test doubles from
  the earlier CLI-agnostic execution specs), dispatch a task, assert the
  fake `claude_code` double (not the `codex` one) received the call.
- API tests for `/agents/config` (GET) and `/agents/{role}/config` (PUT):
  200 with defaults when no overrides exist; PUT then GET reflects the
  change; PUT with an unknown role → 404; PUT with an unknown `cli` → 400,
  and confirm no row was written (GET immediately after still shows the
  old value).
- Confirm building both `KNOWN_CLI_NAMES` by default in
  `ExecutionRuntime.__init__` doesn't raise even when the `claude` binary
  isn't on `PATH` — construct a runtime with `CLAUDE_CODE_COMMAND` pointed
  at a nonexistent path and assert `__init__` succeeds (the adapter is
  built; only `execute()` would fail, and that's outside this test's
  concern).
- Dashboard: no automated test infra exists for the `.dc.html` file in
  this codebase today (consistent with how the "Nós" screen and existing
  skill-install UI were verified this session) — verified manually by
  running the dev server, opening the drawer, changing a role's CLI,
  confirming the `PUT` succeeds and `GET /agents/config` reflects it, and
  confirming a live dispatch after the change actually uses the new CLI
  (observable via the "Nós" chip and/or `raw_response`).
