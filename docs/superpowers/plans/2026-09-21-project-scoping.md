# Scope Domain Agents by Feature Relevance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop dispatching a real Codex session to a domain (frontend/backend/python) a feature request doesn't actually need, so a single-domain change doesn't wait on unrelated agents before code review can run.

**Architecture:** A new lightweight `classify_projects` graph node runs right after `discover_projects`. It asks a cheap Codex call (a new `classifier` role, JSON-schema-constrained to 3 booleans, no file access) which domains the feature request plausibly touches, or uses an explicit `target_projects` override from the request if one was given. `create_plan` then only creates a frontend/backend/python_ai task for a domain that both exists in the workspace and was marked relevant — every downstream node (contracts/qa/security/reviewer) already adapts automatically since they depend on the real list of domain tasks created, not a hardcoded count of three.

**Tech Stack:** Python 3.13, Pydantic, LangGraph 0.4.1, pytest + pytest-asyncio (existing stack — no new dependency).

## Global Constraints

- The classifier can never shrink the pipeline by failing: any error (timeout, non-zero exit, malformed JSON, a missing boolean field) resolves to "relevant" for the affected domain(s) — fail-open, matching today's always-dispatch-everything behavior as the safe fallback.
- `CodexCLI.execute_json` is a new, separate method from `execute()` — it returns a raw parsed `dict | None`, never an `AgentResult`, and takes no `cancel_event` (the classifier runs once, early, for at most 120 seconds; the next cancellation checkpoint is `dispatch_agents`, which already checks `_cancelled` — acceptable given the short ceiling).
- `CLASSIFIER_SCHEMA` is a separate JSON schema from `AGENT_SCHEMA` — the classifier's response shape has nothing to do with `AgentResult`.
- The `classifier` role is excluded from `orchestrator/quality.py`'s `_DOMAIN_AGENTS` set and from the reviewer's quality-scoring flow — it doesn't produce a diff and is never reviewed. Nothing in this plan touches `orchestrator/quality.py`.
- Follow existing code style: `from __future__ import annotations` already present in every touched file, one-line "why" comments only where non-obvious, dict-based state.
- Windows/this repo's pytest quirk: `AppData\Local\Temp\pytest-of-user` has a permission error, so every test run in this plan uses `--basetemp=<absolute path> -p no:cacheprovider`. If working inside a `.claude/worktrees/...` path, the basetemp path must also avoid any `.claude` segment (the security scanner's `SKIP` set treats it as a skip-directory and silently breaks `tests/test_security.py`).

---

### Task 1: `CodexCLI.execute_json` — a lightweight, schema-constrained JSON call

**Files:**
- Modify: `src/adapters/codex_cli.py:20-70` (add `CLASSIFIER_SCHEMA` after `AGENT_SCHEMA`), `:180-182` (add `execute_json` method between `execute` and `_pump`)
- Test: `tests/test_codex_yolo_integration.py`

**Interfaces:**
- Produces: `CLASSIFIER_SCHEMA: dict` (module-level constant); `CodexCLI.execute_json(prompt: str, workdir: Path, schema: dict[str, Any], label: str, timeout: int = 120) -> dict[str, Any] | None`. Task 4 calls this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_codex_yolo_integration.py`. First, change the import line:

```python
from adapters.codex_cli import CodexCLI
```

to:

```python
from adapters.codex_cli import CLASSIFIER_SCHEMA, CodexCLI
```

Then add, anywhere after the existing tests in the file:

```python
FAKE_CLASSIFIER_OK = r'''
import json
import pathlib
import sys
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps({"frontend": True, "backend": False, "python": False}), encoding="utf-8")
sys.exit(0)
'''

FAKE_CLASSIFIER_FAILS = r'''
import sys
sys.exit(1)
'''

FAKE_CLASSIFIER_MALFORMED = r'''
import pathlib
import sys
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text("not json", encoding="utf-8")
sys.exit(0)
'''

FAKE_CLASSIFIER_HANGS = r'''
import time
time.sleep(5)
'''


@pytest.mark.asyncio
async def test_execute_json_returns_the_parsed_dict_on_success(tmp_path: Path):
    script = tmp_path / "fake_classifier.py"
    script.write_text(FAKE_CLASSIFIER_OK, encoding="utf-8")
    settings = settings_for(tmp_path, f'"{sys.executable}" "{script}"')

    result = await CodexCLI(settings).execute_json("classify this", tmp_path, CLASSIFIER_SCHEMA, "Classifier")

    assert result == {"frontend": True, "backend": False, "python": False}


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_nonzero_exit(tmp_path: Path):
    script = tmp_path / "fake_classifier_fails.py"
    script.write_text(FAKE_CLASSIFIER_FAILS, encoding="utf-8")
    settings = settings_for(tmp_path, f'"{sys.executable}" "{script}"')

    result = await CodexCLI(settings).execute_json("classify this", tmp_path, CLASSIFIER_SCHEMA, "Classifier")

    assert result is None


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_malformed_json(tmp_path: Path):
    script = tmp_path / "fake_classifier_malformed.py"
    script.write_text(FAKE_CLASSIFIER_MALFORMED, encoding="utf-8")
    settings = settings_for(tmp_path, f'"{sys.executable}" "{script}"')

    result = await CodexCLI(settings).execute_json("classify this", tmp_path, CLASSIFIER_SCHEMA, "Classifier")

    assert result is None


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_timeout(tmp_path: Path):
    script = tmp_path / "fake_classifier_hangs.py"
    script.write_text(FAKE_CLASSIFIER_HANGS, encoding="utf-8")
    settings = settings_for(tmp_path, f'"{sys.executable}" "{script}"')

    result = await CodexCLI(settings).execute_json("classify this", tmp_path, CLASSIFIER_SCHEMA, "Classifier", timeout=1)

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_codex_yolo_integration.py -k execute_json -v --basetemp=<abs-tmp-path>/t1 -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'CLASSIFIER_SCHEMA' from 'adapters.codex_cli'`.

- [ ] **Step 3: Add `CLASSIFIER_SCHEMA`**

In `src/adapters/codex_cli.py`, find (the closing of `AGENT_SCHEMA`):

```python
    "required": [
        "status", "summary", "files_changed", "tests", "contracts_changed",
        "errors", "next_action", "tokens_input", "tokens_output", "quality",
    ],
}


def resolve_codex_command(configured: str) -> list[str]:
```

Change to:

```python
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
```

- [ ] **Step 4: Add `execute_json`**

In `src/adapters/codex_cli.py`, find (the end of `execute` and the start of `_pump`):

```python
            diagnostic_output = response_text or raw or err
            parsed.raw_response = self._redact(diagnostic_output[-self.settings.max_output_chars:])
            return parsed

    async def _pump(self, stream: asyncio.StreamReader, label: str, buffer: list[bytes],
```

Change to:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_codex_yolo_integration.py -v --basetemp=<abs-tmp-path>/t1b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q --basetemp=<abs-tmp-path>/t1c -p no:cacheprovider`
Expected: PASS (baseline count + 4 new tests)

- [ ] **Step 7: Commit**

```bash
git add src/adapters/codex_cli.py tests/test_codex_yolo_integration.py
git commit -m "feat: add a lightweight schema-constrained Codex call for non-agent JSON tasks"
```

---

### Task 2: `classifier` agent role and prompt

**Files:**
- Modify: `src/agents/registry.py`
- Create: `prompts/classifier.md`
- Test: `tests/test_agents_registry.py`

**Interfaces:**
- Produces: `AGENT_ROLES["classifier"]` with `prompt: "classifier.md"`, `prompt_version: "1.0"`. Task 4's `classify_relevant_projects` calls `role_prompt("classifier", ...)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agents_registry.py` (add `from pathlib import Path` and `role_prompt` to the existing `from agents.registry import AGENT_ROLES` import — change it to `from agents.registry import AGENT_ROLES, role_prompt`):

```python
def test_classifier_role_exists_and_points_at_its_prompt_file():
    assert AGENT_ROLES["classifier"]["prompt"] == "classifier.md"


def test_classifier_prompt_file_loads_and_mentions_all_three_domains():
    prompts_root = Path(__file__).resolve().parents[1] / "prompts"
    text = role_prompt("classifier", prompts_root)
    assert "frontend" in text
    assert "backend" in text
    assert "python" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agents_registry.py -v --basetemp=<abs-tmp-path>/t2 -p no:cacheprovider`
Expected: FAIL — `KeyError: 'classifier'` on the first new test; the second fails the same way (or, if reached, on `role_prompt`'s fallback text not containing "frontend"/"backend"/"python" verbatim).

- [ ] **Step 3: Add the `classifier` role**

In `src/agents/registry.py`, find:

```python
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1"},
}
```

Change to:

```python
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1"},
    "classifier": {"label": "Domain Classifier", "project_id": None, "prompt": "classifier.md", "prompt_version": "1.0"},
}
```

- [ ] **Step 4: Create `prompts/classifier.md`**

```markdown
# Domain classifier

Decide which of this workspace's domains a feature request needs, without editing any file.

Domains:
- `frontend` — bezalel-app, React 18 + TypeScript + Vite. UI, pages, components, client-side behavior.
- `backend` — Bezalel, .NET 8 C# BFF. API endpoints, business logic, database, auth, cloud/AWS integration.
- `python` — Workflow-IA, LangGraph pipeline (Python). Topic research, copywriting, visual prompts, carousel generation workflow.

Mark a domain `true` if the feature request plausibly touches it, or if you are unsure. Mark it `false` only when it is clearly irrelevant to the request. When in doubt, include the domain — a domain agent that finds nothing to do is cheaper than missing a domain that needed a change.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agents_registry.py -v --basetemp=<abs-tmp-path>/t2b -p no:cacheprovider`
Expected: PASS — all tests in the file, including the pre-existing generic `test_every_agent_role_declares_a_non_empty_prompt_version` (which now also covers `classifier` automatically, no change needed to it).

- [ ] **Step 6: Commit**

```bash
git add src/agents/registry.py prompts/classifier.md tests/test_agents_registry.py
git commit -m "feat: add a domain classifier agent role and prompt"
```

---

### Task 3: `target_projects` / `relevant_projects` schema fields

**Files:**
- Modify: `src/schemas/models.py:141-192` (`ExecutionRequest`, `ExecutionStateModel`, `initial_state`)
- Modify: `src/orchestrator/state.py:6-9` (`ExecutionState` TypedDict)
- Test: `tests/test_project_scoping.py` (new)

**Interfaces:**
- Produces: `ExecutionRequest.target_projects: list[str] | None = None`; `ExecutionStateModel.target_projects: list[str] | None = None` and `.relevant_projects: list[str] = []`; same two fields on the `ExecutionState` TypedDict; `initial_state()` carries `request.target_projects` through. Task 5's `classify_projects` node reads `state["target_projects"]` and writes `state["relevant_projects"]`; Task 6's `create_plan` reads `state["relevant_projects"]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_project_scoping.py`:

```python
from __future__ import annotations

from schemas.models import ExecutionRequest, initial_state


def test_initial_state_carries_target_projects_override_through():
    request = ExecutionRequest(feature_request="add a button", target_projects=["frontend"])
    state = initial_state(request, "exec-1")
    assert state["target_projects"] == ["frontend"]


def test_initial_state_defaults_target_projects_to_none():
    request = ExecutionRequest(feature_request="add a button")
    state = initial_state(request, "exec-1")
    assert state["target_projects"] is None


def test_initial_state_defaults_relevant_projects_to_empty_list():
    request = ExecutionRequest(feature_request="add a button")
    state = initial_state(request, "exec-1")
    assert state["relevant_projects"] == []


def test_initial_state_preserves_an_explicit_empty_target_projects_list():
    request = ExecutionRequest(feature_request="tweak a config file only", target_projects=[])
    state = initial_state(request, "exec-1")
    assert state["target_projects"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_project_scoping.py -v --basetemp=<abs-tmp-path>/t3 -p no:cacheprovider`
Expected: FAIL — `KeyError: 'target_projects'` on every test. `target_projects` isn't declared on `ExecutionRequest` yet (Pydantic v2 silently ignores the unrecognized kwarg rather than erroring, since this codebase doesn't set `extra="forbid"`), and even if it were, `initial_state()` doesn't pass it through to `ExecutionStateModel` yet, and `ExecutionStateModel`/`.model_dump()` doesn't declare the field either — so `state["target_projects"]` and `state["relevant_projects"]` are both simply absent from the dumped dict.

- [ ] **Step 3: Add `target_projects` to `ExecutionRequest`**

In `src/schemas/models.py`, find:

```python
class ExecutionRequest(BaseModel):
    feature_request: str
    project_id: str = "bezalel"
    execution_id: str | None = None
    dry_run: bool = False
    analysis_only: bool = False
```

Change to:

```python
class ExecutionRequest(BaseModel):
    feature_request: str
    project_id: str = "bezalel"
    execution_id: str | None = None
    dry_run: bool = False
    analysis_only: bool = False
    target_projects: list[str] | None = None
```

- [ ] **Step 4: Add `target_projects` and `relevant_projects` to `ExecutionStateModel`**

In `src/schemas/models.py`, find:

```python
class ExecutionStateModel(BaseModel):
    execution_id: str
    project_id: str
    feature_request: str
    detected_projects: list[ProjectDetection] = Field(default_factory=list)
    architecture_summary: str = ""
```

Change to:

```python
class ExecutionStateModel(BaseModel):
    execution_id: str
    project_id: str
    feature_request: str
    target_projects: list[str] | None = None
    detected_projects: list[ProjectDetection] = Field(default_factory=list)
    relevant_projects: list[str] = Field(default_factory=list)
    architecture_summary: str = ""
```

- [ ] **Step 5: Wire `target_projects` through `initial_state`**

In `src/schemas/models.py`, find:

```python
def initial_state(request: ExecutionRequest, execution_id: str) -> dict[str, Any]:
    return ExecutionStateModel(
        execution_id=execution_id,
        project_id=request.project_id,
        feature_request=request.feature_request,
        status="created",
        approvals={"dry_run": request.dry_run, "analysis_only": request.analysis_only},
    ).model_dump(mode="json")
```

Change to:

```python
def initial_state(request: ExecutionRequest, execution_id: str) -> dict[str, Any]:
    return ExecutionStateModel(
        execution_id=execution_id,
        project_id=request.project_id,
        feature_request=request.feature_request,
        target_projects=request.target_projects,
        status="created",
        approvals={"dry_run": request.dry_run, "analysis_only": request.analysis_only},
    ).model_dump(mode="json")
```

- [ ] **Step 6: Add the same two fields to the `ExecutionState` TypedDict**

In `src/orchestrator/state.py`, find:

```python
class ExecutionState(TypedDict, total=False):
    execution_id: str
    project_id: str
    feature_request: str
    detected_projects: list[dict[str, Any]]
    architecture_summary: str
```

Change to:

```python
class ExecutionState(TypedDict, total=False):
    execution_id: str
    project_id: str
    feature_request: str
    target_projects: list[str] | None
    detected_projects: list[dict[str, Any]]
    relevant_projects: list[str]
    architecture_summary: str
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_project_scoping.py -v --basetemp=<abs-tmp-path>/t3b -p no:cacheprovider`
Expected: PASS — all 4 tests.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -q --basetemp=<abs-tmp-path>/t3c -p no:cacheprovider`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/schemas/models.py src/orchestrator/state.py tests/test_project_scoping.py
git commit -m "feat: add target_projects override and relevant_projects state fields"
```

---

### Task 4: `ExecutionRuntime.classify_relevant_projects`

**Files:**
- Modify: `src/orchestrator/nodes.py:13` (import), `:147-154` (append method to `ExecutionRuntime`)
- Test: `tests/test_project_scoping.py`

**Interfaces:**
- Consumes: `CodexCLI.execute_json`, `CLASSIFIER_SCHEMA` (Task 1); `role_prompt` (already imported); `AGENT_ROLES["classifier"]` (Task 2, via `role_prompt`).
- Produces: `ExecutionRuntime.classify_relevant_projects(self, feature_request: str, existing: set[str]) -> list[str]`. Task 5's `classify_projects` node calls this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_project_scoping.py` (add these imports at the top of the file, alongside the existing ones):

```python
from pathlib import Path

import pytest

from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime
from persistence.checkpointer import SQLiteCheckpointer
```

Then add:

```python
def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


class FakeCodexForClassifier:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute_json(self, prompt, workdir, schema, label, timeout=120):
        self.calls.append({"prompt": prompt, "workdir": workdir, "schema": schema, "label": label})
        return self._result


@pytest.mark.asyncio
async def test_classify_relevant_projects_filters_by_the_classifier_result(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True, "backend": False, "python": False}))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert relevant == ["frontend"]


@pytest.mark.asyncio
async def test_classify_relevant_projects_fails_open_when_the_classifier_returns_none(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier(None))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend"})

    assert relevant == ["backend", "frontend"]


@pytest.mark.asyncio
async def test_classify_relevant_projects_defaults_a_missing_field_to_relevant(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True}))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert set(relevant) == {"frontend", "backend", "python"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_project_scoping.py -k classify_relevant_projects -v --basetemp=<abs-tmp-path>/t4 -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'ExecutionRuntime' object has no attribute 'classify_relevant_projects'`.

- [ ] **Step 3: Import `CLASSIFIER_SCHEMA` in `nodes.py`**

In `src/orchestrator/nodes.py`, find:

```python
from adapters.codex_cli import CodexCLI
```

Change to:

```python
from adapters.codex_cli import CLASSIFIER_SCHEMA, CodexCLI
```

- [ ] **Step 4: Add the method to `ExecutionRuntime`**

In `src/orchestrator/nodes.py`, find (the end of `announce_agent_finished`, right before the class closes):

```python
    async def announce_agent_finished(self, state: dict[str, Any], task: dict[str, Any]) -> None:
        finished_event = {
            "type": "agent.finished", "execution_id": state["execution_id"], "agent": task["agent"],
            "task_id": task["task_id"], "status": task.get("status"), "result": task.get("result"),
        }
        self.store.event(state["execution_id"], "agent.finished", finished_event, utc_now())
        await self.publish(finished_event)


def _cancelled(runtime: ExecutionRuntime, state: dict[str, Any]) -> bool:
```

Change to:

```python
    async def announce_agent_finished(self, state: dict[str, Any], task: dict[str, Any]) -> None:
        finished_event = {
            "type": "agent.finished", "execution_id": state["execution_id"], "agent": task["agent"],
            "task_id": task["task_id"], "status": task.get("status"), "result": task.get("result"),
        }
        self.store.event(state["execution_id"], "agent.finished", finished_event, utc_now())
        await self.publish(finished_event)

    async def classify_relevant_projects(self, feature_request: str, existing: set[str]) -> list[str]:
        """Ask Codex which domains this feature request actually touches, so create_plan
        doesn't dispatch a full coding-agent session for a project with nothing to do.
        Fails open (every existing project) on any classifier error — this call must
        never be able to shrink the pipeline by failing.
        """
        prompt = role_prompt("classifier", self.settings.orchestrator_root / "prompts")
        prompt += f"\n\nFeature request:\n{feature_request}"
        result = await self.codex.execute_json(prompt, self.settings.workspace_root, CLASSIFIER_SCHEMA, "Classifier")
        if result is None:
            return sorted(existing)
        domains = {"frontend": "frontend", "backend": "backend", "python": "python"}
        return [project for domain, project in domains.items() if result.get(domain, True) and project in existing]


def _cancelled(runtime: ExecutionRuntime, state: dict[str, Any]) -> bool:
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_project_scoping.py -v --basetemp=<abs-tmp-path>/t4b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q --basetemp=<abs-tmp-path>/t4c -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_project_scoping.py
git commit -m "feat: classify which domains a feature request actually needs"
```

---

### Task 5: `classify_projects` graph node

**Files:**
- Modify: `src/orchestrator/nodes.py:170-180` (`discover_projects_node`'s `next_action`, and add the new node function right after it)
- Modify: `src/orchestrator/graph.py:13-16` (import), `:26-34` (node list)
- Test: `tests/test_project_scoping.py`

**Interfaces:**
- Consumes: `ExecutionRuntime.classify_relevant_projects` (Task 4); `state["target_projects"]`, `state["detected_projects"]` (Task 3).
- Produces: `classify_projects(runtime: ExecutionRuntime, state: dict) -> dict` — sets `state["relevant_projects"]`. Task 6's `create_plan` reads it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_project_scoping.py` (add `from orchestrator import nodes` to the imports):

```python
class StubClassifyRuntime:
    def __init__(self, relevant):
        self._relevant = relevant
        self.classify_calls = []

    async def classify_relevant_projects(self, feature_request, existing):
        self.classify_calls.append((feature_request, existing))
        return self._relevant

    async def persist(self, state, node, event=None, payload=None):
        return state


@pytest.mark.asyncio
async def test_classify_projects_node_uses_the_override_and_skips_the_classifier():
    runtime = StubClassifyRuntime(relevant=["frontend", "backend", "python"])
    state = {
        "feature_request": "add a button", "target_projects": ["frontend"],
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["frontend"]
    assert runtime.classify_calls == []


@pytest.mark.asyncio
async def test_classify_projects_node_calls_the_classifier_when_no_override_is_given():
    runtime = StubClassifyRuntime(relevant=["backend"])
    state = {
        "feature_request": "fix the endpoint", "target_projects": None,
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["backend"]
    assert runtime.classify_calls == [("fix the endpoint", {"frontend", "backend"})]


@pytest.mark.asyncio
async def test_classify_projects_node_filters_the_override_by_existing_projects():
    runtime = StubClassifyRuntime(relevant=[])
    state = {
        "feature_request": "add a button", "target_projects": ["frontend", "python"],
        "detected_projects": [{"project_id": "frontend", "exists": True}],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["frontend"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_project_scoping.py -k classify_projects_node -v --basetemp=<abs-tmp-path>/t5 -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'orchestrator.nodes' has no attribute 'classify_projects'`.

- [ ] **Step 3: Add the `classify_projects` node function and update `discover_projects_node`**

In `src/orchestrator/nodes.py`, find:

```python
async def discover_projects_node(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    projects = discover_projects(runtime.settings)
    state["detected_projects"] = [p.model_dump(mode="json") for p in projects]
    report_dir = runtime.settings.orchestrator_root / "docs" / "discovery"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "project-discovery-latest.md").write_text(discovery_markdown(projects, runtime.settings), encoding="utf-8")
    missing = [p.expected_name for p in projects if not p.exists]
    if missing:
        state.setdefault("errors", []).append("missing project aliases: " + ", ".join(missing))
    state["next_action"] = "create_plan"
    return await runtime.persist(state, "discover_projects", "projects.discovered", {"count": len(projects), "missing": missing})


async def create_plan(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
```

Change to:

```python
async def discover_projects_node(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    projects = discover_projects(runtime.settings)
    state["detected_projects"] = [p.model_dump(mode="json") for p in projects]
    report_dir = runtime.settings.orchestrator_root / "docs" / "discovery"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "project-discovery-latest.md").write_text(discovery_markdown(projects, runtime.settings), encoding="utf-8")
    missing = [p.expected_name for p in projects if not p.exists]
    if missing:
        state.setdefault("errors", []).append("missing project aliases: " + ", ".join(missing))
    state["next_action"] = "classify_projects"
    return await runtime.persist(state, "discover_projects", "projects.discovered", {"count": len(projects), "missing": missing})


async def classify_projects(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
    override = state.get("target_projects")
    if override is not None:
        relevant = [p for p in override if p in existing]
    else:
        relevant = await runtime.classify_relevant_projects(state["feature_request"], existing)
    state["relevant_projects"] = relevant
    state["next_action"] = "create_plan"
    return await runtime.persist(state, "classify_projects", "projects.classified", {"relevant": relevant})


async def create_plan(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
```

- [ ] **Step 4: Wire the node into the graph**

In `src/orchestrator/graph.py`, find:

```python
from orchestrator.nodes import (ExecutionRuntime, analyze_request, code_review, collect_agent_results, commit_changes,
                                create_contracts, create_plan, deploy, discover_projects_node, dispatch_agents,
                                generate_final_report, merge_changes, resolve_dependencies, run_contract_validation,
                                run_tests, security_review)
```

Change to:

```python
from orchestrator.nodes import (ExecutionRuntime, analyze_request, classify_projects, code_review, collect_agent_results,
                                commit_changes, create_contracts, create_plan, deploy, discover_projects_node,
                                dispatch_agents, generate_final_report, merge_changes, resolve_dependencies,
                                run_contract_validation, run_tests, security_review)
```

Then find:

```python
        nodes: list[tuple[str, Callable]] = [
            ("analyze_request", analyze_request), ("discover_projects", discover_projects_node),
            ("create_plan", create_plan), ("resolve_dependencies", resolve_dependencies),
```

Change to:

```python
        nodes: list[tuple[str, Callable]] = [
            ("analyze_request", analyze_request), ("discover_projects", discover_projects_node),
            ("classify_projects", classify_projects),
            ("create_plan", create_plan), ("resolve_dependencies", resolve_dependencies),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_project_scoping.py -v --basetemp=<abs-tmp-path>/t5b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite and a compile check**

Run: `python -m pytest -q --basetemp=<abs-tmp-path>/t5c -p no:cacheprovider`
Expected: PASS

Run: `python -m compileall -q src tests`
Expected: no output (success) — this specifically catches a malformed import list in `graph.py`.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/nodes.py src/orchestrator/graph.py tests/test_project_scoping.py
git commit -m "feat: wire domain classification into the graph between discovery and planning"
```

---

### Task 6: `create_plan` only dispatches relevant domains

**Files:**
- Modify: `src/orchestrator/nodes.py:183-193` (`create_plan`)
- Test: `tests/test_project_scoping.py`

**Interfaces:**
- Consumes: `state["relevant_projects"]` (Task 3/5).
- Produces: no interface change — `create_plan`'s existing return shape (`state["plan"]`) is unchanged, only which domain tasks it contains.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_project_scoping.py` (add `import asyncio` to the imports):

```python
class StubPersistRuntime:
    async def persist(self, state, node, event=None, payload=None):
        return state


def test_create_plan_only_creates_tasks_for_relevant_projects():
    state = {
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
            {"project_id": "python", "exists": True},
        ],
        "relevant_projects": ["frontend"],
    }

    result = asyncio.run(nodes.create_plan(StubPersistRuntime(), state))

    domain_tasks = [t for t in result["plan"] if t["agent"] in ("frontend", "backend", "python_ai")]
    assert [t["agent"] for t in domain_tasks] == ["frontend"]


def test_create_plan_gate_tasks_depend_only_on_the_relevant_domain_tasks():
    state = {
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
        "relevant_projects": ["backend"],
    }

    result = asyncio.run(nodes.create_plan(StubPersistRuntime(), state))

    contracts = next(t for t in result["plan"] if t["agent"] == "contracts")
    backend_task = next(t for t in result["plan"] if t["agent"] == "backend")
    assert contracts["dependencies"] == [backend_task["task_id"]]


def test_create_plan_falls_back_to_existing_when_relevant_projects_is_absent():
    """Defensive default: relevant_projects should always be set by classify_projects by
    the time create_plan runs, but if it's ever missing, don't silently create zero tasks."""
    state = {
        "detected_projects": [{"project_id": "frontend", "exists": True}],
    }

    result = asyncio.run(nodes.create_plan(StubPersistRuntime(), state))

    domain_tasks = [t for t in result["plan"] if t["agent"] in ("frontend", "backend", "python_ai")]
    assert [t["agent"] for t in domain_tasks] == ["frontend"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_project_scoping.py -k create_plan -v --basetemp=<abs-tmp-path>/t6 -p no:cacheprovider`
Expected: FAIL — `test_create_plan_only_creates_tasks_for_relevant_projects` and `test_create_plan_gate_tasks_depend_only_on_the_relevant_domain_tasks` fail because `create_plan` still creates a task for every existing project regardless of `relevant_projects` (assertion mismatch, e.g. `['frontend', 'backend', 'python_ai'] != ['frontend']`); `test_create_plan_falls_back_to_existing_when_relevant_projects_is_absent` passes already (that's fine — it's a regression guard for the next step, not new behavior).

- [ ] **Step 3: Filter `create_plan` by `relevant_projects`**

In `src/orchestrator/nodes.py`, find:

```python
async def create_plan(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
    tasks: list[TaskSpec] = []
    for number, (role, project, description) in enumerate([
        ("frontend", "frontend", "Implement the frontend portion of the feature using the detected stack and design system."),
        ("backend", "backend", "Implement backend/API/domain changes and preserve current AWS and authorization conventions."),
        ("python_ai", "python", "Implement Python/LangGraph workflow or prompt changes required by the feature."),
    ], 1):
        if project in existing:
```

Change to:

```python
async def create_plan(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
    relevant = state.get("relevant_projects") or existing
    tasks: list[TaskSpec] = []
    for number, (role, project, description) in enumerate([
        ("frontend", "frontend", "Implement the frontend portion of the feature using the detected stack and design system."),
        ("backend", "backend", "Implement backend/API/domain changes and preserve current AWS and authorization conventions."),
        ("python_ai", "python", "Implement Python/LangGraph workflow or prompt changes required by the feature."),
    ], 1):
        if project in existing and project in relevant:
```

(`state.get("relevant_projects") or existing` falls back to `existing` both when the key is absent and when it's an empty list from a source other than an explicit `target_projects=[]` override — `classify_projects` always sets a real list before `create_plan` runs, so this fallback only matters for tests or any future caller that skips straight to `create_plan`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_project_scoping.py -v --basetemp=<abs-tmp-path>/t6b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Run the full suite and a compile check**

Run: `python -m pytest -q --basetemp=<abs-tmp-path>/t6c -p no:cacheprovider`
Expected: PASS

Run: `python -m compileall -q src tests`
Expected: no output (success)

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_project_scoping.py
git commit -m "feat: create_plan only dispatches domain agents the feature actually needs"
```

---

## Manual verification (after all tasks)

1. Restart the API (targeted PID restart, never a broad kill) so it picks up the new graph shape.
2. Trigger a real execution with a feature request that's obviously single-domain (e.g. "muda o texto de um botão no frontend").
3. Confirm in the panel: only the frontend agent card goes to "executando agora"; backend and python_ai never leave idle for this execution, and `code_review` starts as soon as frontend (plus the gate agents) finish — not blocked on backend/python_ai Codex sessions that had nothing to do.
4. Trigger a second execution with an explicit `target_projects` override via the MCP bridge/API (if the calling surface exposes it) to confirm the classifier is skipped entirely for that run.
