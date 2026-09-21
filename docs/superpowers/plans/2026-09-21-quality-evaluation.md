# Per-Agent Quality Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every reviewed (execution, project) on 7 quality axes — correctness, relevance, sourcing, hallucination, valid format, rule compliance, security — and let the panel compare quality/cost/latency across prompt versions of the same agent.

**Architecture:** 3 heuristic axes (correta, formato_valido, seguranca) come free from data the orchestrator already has (`test_results`, `security_findings`, Codex parse success). The other 4 (relevante, fonte_utilizada, alucinacao, cumprimento_regras) ride on the `reviewer` agent's existing Codex call — no new agent, no new Codex call. A new pure module `orchestrator/quality.py` computes and blends both kinds of axis into one `quality_scores` state entry per project, stamped with the prompt version of whichever domain agent (frontend/backend/python_ai) produced that project's diff. Two new dashboard surfaces read it: `/dashboard-data`'s per-execution items, and a new `/quality-data` endpoint for the version-comparison table.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, pytest + pytest-asyncio (existing stack — no new dependency).

## Global Constraints

- Every domain (frontend/backend/python) has exactly one exclusive owning agent (`CLAUDE.md`) — a project_id maps to exactly one producing agent per execution. `_producing_task` in `orchestrator/quality.py` relies on this.
- `quality_score` = average of {correta, relevante, fonte_utilizada, formato_valido, cumprimento_regras, seguranca} plus `(100 - alucinacao)`; a `None` axis is excluded from the average, never treated as 0.
- Delta computation (quality/cost/latency vs. the previous prompt version) happens client-side in the panel from consecutive rows of `/quality-data`'s `by_version` list — the backend only returns sorted per-version averages, it does not compute deltas itself. (This refines one implementation detail from the approved spec — same UI-visible outcome, less backend surface.)
- Windows/this repo's pytest quirk: `AppData\Local\Temp\pytest-of-user` has a permission error, so every test run in this plan uses `--basetemp=.test-tmp-<short-name> -p no:cacheprovider`, matching every prior test run this session.
- Follow existing code style: no docstrings beyond a one-line "why" comment where non-obvious, `from __future__ import annotations` at the top of new modules, dict-based state (no new Pydantic models unless the plan says so).

---

### Task 1: Schema fields — `SecurityFinding.project_id`, `AgentResult.quality`, `quality_scores` on execution state

**Files:**
- Modify: `src/schemas/models.py:96-100` (`SecurityFinding`), `:62-75` (`AgentResult`), `:147-178` (`ExecutionStateModel`)
- Modify: `src/orchestrator/state.py:18-20` (`ExecutionState` TypedDict)
- Modify: `src/orchestrator/nodes.py:410-426` (`security_review`)
- Test: `tests/test_orchestration_noop.py`

**Interfaces:**
- Produces: `SecurityFinding.project_id: str | None`, `AgentResult.quality: dict[str, int] | None`, `ExecutionStateModel.quality_scores: list[dict[str, Any]]` (default `[]`), `ExecutionState.quality_scores: list[dict[str, Any]]`. Every later task reads/writes `state["quality_scores"]` and `finding["project_id"]` assuming these exist.

- [ ] **Step 1: Write the failing test for `security_review` tagging findings with their project**

Add to `tests/test_orchestration_noop.py` (needs `SecurityFinding` added to the existing `from schemas.models import AgentResult` import line — change it to `from schemas.models import AgentResult, SecurityFinding`):

```python
@pytest.mark.asyncio
async def test_security_review_tags_each_finding_with_its_project_id(monkeypatch):
    async def fake_changed_paths(runtime, state, project_id):
        return ["src/config.py"] if project_id == "frontend" else ["appsettings.json"]

    def fake_scan_project(project, paths):
        return [SecurityFinding(severity="blocking", path=paths[0], message="looks like a secret")]

    monkeypatch.setattr(nodes, "_changed_paths", fake_changed_paths)
    monkeypatch.setattr(nodes, "scan_project", fake_scan_project)

    class StubRuntime:
        def workdir_for(self, state, project_id):
            return Path(".")

        async def persist(self, state, node, event=None, payload=None):
            return state

    state = {
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
        "plan": [],
    }

    result = await nodes.security_review(StubRuntime(), state)

    findings = result["security_findings"]
    assert {f["project_id"] for f in findings} == {"frontend", "backend"}


def test_execution_state_model_defaults_quality_scores_to_an_empty_list():
    from schemas.models import ExecutionStateModel

    model = ExecutionStateModel(execution_id="exec-1", project_id="bezalel", feature_request="add a button")

    assert model.quality_scores == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_orchestration_noop.py -k "security_review_tags or quality_scores_to_an_empty" -v --basetemp=.test-tmp-q1 -p no:cacheprovider`
Expected: FAIL — `test_security_review_tags_each_finding_with_its_project_id` fails with `KeyError: 'project_id'` (findings don't have that key yet); `test_execution_state_model_defaults_quality_scores_to_an_empty_list` fails with `AttributeError: 'ExecutionStateModel' object has no attribute 'quality_scores'`.

- [ ] **Step 3: Add `project_id` to `SecurityFinding`**

In `src/schemas/models.py`, change:

```python
class SecurityFinding(BaseModel):
    severity: Literal["info", "warning", "blocking"]
    path: str
    message: str
    evidence: str = ""
```

to:

```python
class SecurityFinding(BaseModel):
    severity: Literal["info", "warning", "blocking"]
    path: str
    message: str
    evidence: str = ""
    project_id: str | None = None
```

- [ ] **Step 4: Add `quality` to `AgentResult`**

In `src/schemas/models.py`, change:

```python
class AgentResult(BaseModel):
    agent: str
    status: Literal["completed", "failed", "blocked", "skipped", "rate_limited"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    contracts_changed: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_action: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    duration_seconds: float = 0.0
    estimated_cost: float = 0.0
    raw_response: str = ""
```

to (only the new last line is added):

```python
class AgentResult(BaseModel):
    agent: str
    status: Literal["completed", "failed", "blocked", "skipped", "rate_limited"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    contracts_changed: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_action: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    duration_seconds: float = 0.0
    estimated_cost: float = 0.0
    raw_response: str = ""
    quality: dict[str, int] | None = None
```

- [ ] **Step 5: Add `quality_scores` to `ExecutionStateModel`**

In `src/schemas/models.py`, find:

```python
    review_results: list[ReviewResult] = Field(default_factory=list)
    deploy_results: list[DeployResult] = Field(default_factory=list)
```

Change to:

```python
    review_results: list[ReviewResult] = Field(default_factory=list)
    quality_scores: list[dict[str, Any]] = Field(default_factory=list)
    deploy_results: list[DeployResult] = Field(default_factory=list)
```

- [ ] **Step 6: Add `quality_scores` to the `ExecutionState` TypedDict**

In `src/orchestrator/state.py`, find:

```python
    review_results: list[dict[str, Any]]
    deploy_results: list[dict[str, Any]]
```

Change to:

```python
    review_results: list[dict[str, Any]]
    quality_scores: list[dict[str, Any]]
    deploy_results: list[dict[str, Any]]
```

- [ ] **Step 7: Make `security_review` tag every finding with its project**

In `src/orchestrator/nodes.py`, find (inside `security_review`, currently lines 410-419):

```python
async def security_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    findings: list[SecurityFinding] = []
    for project in state.get("detected_projects", []):
        if project.get("exists"):
            paths = await _changed_paths(runtime, state, project["project_id"])
            if paths:
                worktree_project = {**project, "path": str(runtime.workdir_for(state, project["project_id"]))}
                findings.extend(scan_project(type("Project", (), worktree_project)(), paths))
```

Change to:

```python
async def security_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    findings: list[SecurityFinding] = []
    for project in state.get("detected_projects", []):
        if project.get("exists"):
            paths = await _changed_paths(runtime, state, project["project_id"])
            if paths:
                worktree_project = {**project, "path": str(runtime.workdir_for(state, project["project_id"]))}
                project_findings = scan_project(type("Project", (), worktree_project)(), paths)
                for finding in project_findings:
                    finding.project_id = project["project_id"]
                findings.extend(project_findings)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestration_noop.py -v --basetemp=.test-tmp-q1b -p no:cacheprovider`
Expected: PASS — all tests in the file, including the 2 new ones.

- [ ] **Step 9: Commit**

```bash
git add src/schemas/models.py src/orchestrator/state.py src/orchestrator/nodes.py tests/test_orchestration_noop.py
git commit -m "feat: add quality_scores state field and tag security findings by project"
```

---

### Task 2: Codex contract — optional `quality` sub-schema for the `reviewer` agent

**Files:**
- Modify: `src/adapters/codex_cli.py:20-59` (`AGENT_SCHEMA`)
- Modify: `prompts/reviewer.md`
- Test: `tests/test_codex_cli.py`

**Interfaces:**
- Consumes: `AgentResult.quality` (Task 1).
- Produces: `AGENT_SCHEMA["properties"]["quality"]` — a JSON-schema object with keys `relevante`, `fonte_utilizada`, `alucinacao`, `cumprimento_regras` (all integers 0-100), optional at the top level (not in `AGENT_SCHEMA["required"]`). Task 4/5 read these same 4 key names off `AgentResult.quality`.

- [ ] **Step 1: Write the failing test for the new schema shape**

Add to `tests/test_codex_cli.py`:

```python
def test_agent_schema_has_an_optional_quality_object_for_the_reviewer():
    quality = AGENT_SCHEMA["properties"]["quality"]
    assert AGENT_SCHEMA["additionalProperties"] is False
    assert "quality" not in AGENT_SCHEMA["required"]
    assert quality["additionalProperties"] is False
    assert set(quality["required"]) == {"relevante", "fonte_utilizada", "alucinacao", "cumprimento_regras"}
```

Then update the existing strict-schema test (it currently asserts every property is required, which `quality` deliberately breaks) — change:

```python
def test_agent_schema_is_strict_for_structured_outputs():
    assert AGENT_SCHEMA["additionalProperties"] is False
    assert set(AGENT_SCHEMA["required"]) == set(AGENT_SCHEMA["properties"])
    assert AGENT_SCHEMA["properties"]["tests"]["items"]["additionalProperties"] is False
    assert AGENT_SCHEMA["properties"]["contracts_changed"]["items"]["additionalProperties"] is False
```

to:

```python
def test_agent_schema_is_strict_for_structured_outputs():
    assert AGENT_SCHEMA["additionalProperties"] is False
    # "quality" is the one deliberately optional property — only the reviewer role
    # populates it (see prompts/reviewer.md); every other property stays required.
    assert set(AGENT_SCHEMA["required"]) == set(AGENT_SCHEMA["properties"]) - {"quality"}
    assert AGENT_SCHEMA["properties"]["tests"]["items"]["additionalProperties"] is False
    assert AGENT_SCHEMA["properties"]["contracts_changed"]["items"]["additionalProperties"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_codex_cli.py -v --basetemp=.test-tmp-q2 -p no:cacheprovider`
Expected: FAIL — `test_agent_schema_has_an_optional_quality_object_for_the_reviewer` fails with `KeyError: 'quality'`; `test_agent_schema_is_strict_for_structured_outputs` fails because `required == properties` still (no `quality` key exists yet, so this one may actually still pass by accident — that's fine, it must still pass after Step 3 either way).

- [ ] **Step 3: Add `quality` to `AGENT_SCHEMA`**

In `src/adapters/codex_cli.py`, find:

```python
        "errors": {"type": "array", "items": {"type": "string"}}, "next_action": {"type": ["string", "null"]},
        "tokens_input": {"type": "integer"}, "tokens_output": {"type": "integer"},
    },
    "required": [
        "status", "summary", "files_changed", "tests", "contracts_changed",
        "errors", "next_action", "tokens_input", "tokens_output",
    ],
}
```

Change to:

```python
        "errors": {"type": "array", "items": {"type": "string"}}, "next_action": {"type": ["string", "null"]},
        "tokens_input": {"type": "integer"}, "tokens_output": {"type": "integer"},
        "quality": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "relevante": {"type": "integer"},
                "fonte_utilizada": {"type": "integer"},
                "alucinacao": {"type": "integer"},
                "cumprimento_regras": {"type": "integer"},
            },
            "required": ["relevante", "fonte_utilizada", "alucinacao", "cumprimento_regras"],
        },
    },
    "required": [
        "status", "summary", "files_changed", "tests", "contracts_changed",
        "errors", "next_action", "tokens_input", "tokens_output",
    ],
}
```

- [ ] **Step 4: Instruct the reviewer prompt to fill it in**

Replace the full contents of `prompts/reviewer.md` (currently 3 lines) with:

```markdown
# Code reviewer

Read-only review. Inspect the diff, error handling, duplication, observability, compatibility, tests and rollback safety. Lead with concrete blocking findings and file references. Approve only when the acceptance criteria and required evidence are complete.

Also return a `quality` object scoring this diff, each field an integer 0-100:
- `relevante`: does the diff actually address the requested task, without unrelated changes?
- `fonte_utilizada`: are the changes grounded in the real code/contracts of this repo, not invented?
- `alucinacao`: how likely is it that the diff or its summary references a file, function or API that does not exist in this repo, or that the summary misrepresents the actual diff? (0 = no sign of this, 100 = certain)
- `cumprimento_regras`: did the diff respect this repo's `CLAUDE.md` conventions (exclusive domain ownership, contract rules, resolver conventions)?
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_codex_cli.py -v --basetemp=.test-tmp-q2b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `python -m pytest -q --basetemp=.test-tmp-q2c -p no:cacheprovider`
Expected: PASS — all tests (this schema change is additive; nothing outside `test_codex_cli.py` should notice).

- [ ] **Step 7: Commit**

```bash
git add src/adapters/codex_cli.py prompts/reviewer.md tests/test_codex_cli.py
git commit -m "feat: reviewer emits a quality score alongside its findings"
```

---

### Task 3: `prompt_version` per agent role

**Files:**
- Modify: `src/agents/registry.py`
- Test: `tests/test_agents_registry.py` (new)

**Interfaces:**
- Produces: `AGENT_ROLES[role]["prompt_version"]: str` for every role. Task 5 (`_producing_task`/`build_quality_entry`) and Task 7 (`dispatch_agents`) read this key.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agents_registry.py`:

```python
from __future__ import annotations

from agents.registry import AGENT_ROLES


def test_every_agent_role_declares_a_non_empty_prompt_version():
    for role, config in AGENT_ROLES.items():
        version = config.get("prompt_version")
        assert isinstance(version, str) and version, f"{role} is missing prompt_version"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agents_registry.py -v --basetemp=.test-tmp-q3 -p no:cacheprovider`
Expected: FAIL — every role's `config.get("prompt_version")` is `None`.

- [ ] **Step 3: Add `prompt_version` to every role**

In `src/agents/registry.py`, change:

```python
AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md"},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md"},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md"},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md"},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md"},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md"},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md"},
}
```

to:

```python
AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md", "prompt_version": "1.0"},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md", "prompt_version": "1.0"},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md", "prompt_version": "1.0"},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md", "prompt_version": "1.0"},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md", "prompt_version": "1.0"},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md", "prompt_version": "1.0"},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1"},
}
```

(`reviewer` starts at `1.1` since Task 2 just changed its prompt text — every other role's `.md` is untouched by this feature, so they start at `1.0`. Bump any role's string the next time its `.md` file's content changes.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agents_registry.py -v --basetemp=.test-tmp-q3b -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agents/registry.py tests/test_agents_registry.py
git commit -m "feat: version every agent role's prompt for quality comparisons"
```

---

### Task 4: `orchestrator/quality.py` — heuristic axes and score blending

**Files:**
- Create: `src/orchestrator/quality.py`
- Test: `tests/test_quality_scoring.py` (new)

**Interfaces:**
- Consumes: `state["plan"]` (task dicts with `agent`, `project_id`, `status`, `result`), `state["test_results"]` (dicts with `project_id`, `status`), `state["security_findings"]` (dicts with `project_id`, `severity`) — all already produced by existing nodes plus Task 1.
- Produces: `_producing_task(project_id: str, state: dict) -> dict | None`, `heuristic_correta(project_id: str, state: dict) -> int | None`, `heuristic_formato_valido(project_id: str, state: dict) -> int | None`, `heuristic_seguranca(project_id: str, state: dict) -> int`, `blend_quality_score(axes: dict[str, int | None]) -> float | None`. Task 5 calls all four.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quality_scoring.py`:

```python
from __future__ import annotations

from orchestrator.quality import (
    blend_quality_score,
    heuristic_correta,
    heuristic_formato_valido,
    heuristic_seguranca,
)


def test_heuristic_correta_is_100_when_task_completed_and_tests_passed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed"}],
        "test_results": [{"project_id": "frontend", "status": "passed"}],
    }
    assert heuristic_correta("frontend", state) == 100


def test_heuristic_correta_is_0_when_a_test_failed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed"}],
        "test_results": [{"project_id": "frontend", "status": "failed"}],
    }
    assert heuristic_correta("frontend", state) == 0


def test_heuristic_correta_is_0_when_the_producing_task_itself_failed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "failed"}],
        "test_results": [],
    }
    assert heuristic_correta("frontend", state) == 0


def test_heuristic_correta_is_none_when_no_domain_task_produced_that_project():
    state = {"plan": [], "test_results": []}
    assert heuristic_correta("frontend", state) is None


def test_heuristic_correta_ignores_gate_agents_like_reviewer_and_contracts():
    state = {
        "plan": [{"agent": "reviewer", "project_id": "frontend", "status": "failed"}],
        "test_results": [],
    }
    assert heuristic_correta("frontend", state) is None


def test_heuristic_formato_valido_is_0_when_codex_output_failed_to_parse():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "failed",
                  "result": {"errors": ["structured output could not be parsed"]}}],
    }
    assert heuristic_formato_valido("frontend", state) == 0


def test_heuristic_formato_valido_is_100_when_parse_succeeded():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed",
                  "result": {"errors": []}}],
    }
    assert heuristic_formato_valido("frontend", state) == 100


def test_heuristic_seguranca_is_0_when_a_blocking_finding_belongs_to_this_project():
    state = {"security_findings": [{"project_id": "frontend", "severity": "blocking"}]}
    assert heuristic_seguranca("frontend", state) == 0


def test_heuristic_seguranca_ignores_blocking_findings_from_other_projects():
    state = {"security_findings": [{"project_id": "backend", "severity": "blocking"}]}
    assert heuristic_seguranca("frontend", state) == 100


def test_blend_quality_score_inverts_alucinacao_and_ignores_missing_axes():
    axes = {"correta": 100, "relevante": 80, "fonte_utilizada": None, "alucinacao": 10,
            "formato_valido": 100, "cumprimento_regras": 90, "seguranca": 100}
    # fonte_utilizada excluded (None); alucinacao inverted 10 -> 90.
    # (100 + 80 + 90 + 100 + 90 + 100) / 6 = 93.333... -> rounds to 93.3
    assert blend_quality_score(axes) == 93.3


def test_blend_quality_score_is_none_when_every_axis_is_missing():
    assert blend_quality_score({"correta": None, "alucinacao": None}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q4 -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.quality'`.

- [ ] **Step 3: Create `src/orchestrator/quality.py` with the heuristics and blend function**

```python
from __future__ import annotations

from typing import Any

_PARSE_FAILURE_ERROR = "structured output could not be parsed"
_DOMAIN_AGENTS = {"frontend", "backend", "python_ai"}


def _producing_task(project_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent task run by this project's exclusive domain owner (frontend/
    backend/python_ai) — never a gate agent like reviewer/contracts/security/qa, since
    those review the project rather than producing its diff.
    """
    matches = [
        t for t in state.get("plan", [])
        if t.get("project_id") == project_id and t.get("agent") in _DOMAIN_AGENTS
    ]
    return matches[-1] if matches else None


def heuristic_correta(project_id: str, state: dict[str, Any]) -> int | None:
    task = _producing_task(project_id, state)
    if task is None:
        return None
    if task.get("status") in ("failed", "blocked"):
        return 0
    test_results = [t for t in state.get("test_results", []) if t.get("project_id") == project_id]
    if any(t.get("status") == "failed" for t in test_results):
        return 0
    return 100


def heuristic_formato_valido(project_id: str, state: dict[str, Any]) -> int | None:
    task = _producing_task(project_id, state)
    if task is None:
        return None
    errors = (task.get("result") or {}).get("errors", [])
    return 0 if _PARSE_FAILURE_ERROR in errors else 100


def heuristic_seguranca(project_id: str, state: dict[str, Any]) -> int:
    blocking = [
        f for f in state.get("security_findings", [])
        if f.get("project_id") == project_id and f.get("severity") == "blocking"
    ]
    return 0 if blocking else 100


def blend_quality_score(axes: dict[str, int | None]) -> float | None:
    """Average every scored axis; a missing (``None``) axis is excluded, not treated as
    0. ``alucinacao`` is inverted first, since for it lower is better.
    """
    values: list[float] = []
    for key, value in axes.items():
        if value is None:
            continue
        values.append(100 - value if key == "alucinacao" else value)
    if not values:
        return None
    return round(sum(values) / len(values), 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q4b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/quality.py tests/test_quality_scoring.py
git commit -m "feat: heuristic quality axes and score blending"
```

---

### Task 5: `build_quality_entry` — combine heuristic + reviewer axes into one record

**Files:**
- Modify: `src/orchestrator/quality.py`
- Test: `tests/test_quality_scoring.py`

**Interfaces:**
- Consumes: `heuristic_correta`, `heuristic_formato_valido`, `heuristic_seguranca`, `blend_quality_score`, `_producing_task` (Task 4); `AgentResult.quality` (Task 1/2).
- Produces: `build_quality_entry(project_id: str, state: dict, agent_result: AgentResult) -> dict[str, Any]` returning `{project_id, agent, prompt_version, axes, quality_score, estimated_cost, duration_seconds, computed_at}`. Task 8 calls this once per reviewed project.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_quality_scoring.py` (add `build_quality_entry` to the existing `from orchestrator.quality import (...)` line, and add `from schemas.models import AgentResult` at the top):

```python
def test_build_quality_entry_combines_heuristic_and_llm_axes():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed",
                  "result": {"prompt_version": "1.8", "estimated_cost": 0.02, "duration_seconds": 30.0, "errors": []}}],
        "test_results": [{"project_id": "frontend", "status": "passed"}],
        "security_findings": [],
    }
    agent_result = AgentResult(
        agent="reviewer", status="completed", summary="looks good",
        quality={"relevante": 95, "fonte_utilizada": 90, "alucinacao": 3, "cumprimento_regras": 92},
    )

    entry = build_quality_entry("frontend", state, agent_result)

    assert entry["project_id"] == "frontend"
    assert entry["agent"] == "frontend"
    assert entry["prompt_version"] == "1.8"
    assert entry["axes"] == {
        "correta": 100, "relevante": 95, "fonte_utilizada": 90, "alucinacao": 3,
        "formato_valido": 100, "cumprimento_regras": 92, "seguranca": 100,
    }
    assert entry["quality_score"] == 96.3
    assert entry["estimated_cost"] == 0.02
    assert entry["duration_seconds"] == 30.0
    assert "computed_at" in entry


def test_build_quality_entry_handles_a_reviewer_result_with_no_quality_object():
    state = {
        "plan": [{"agent": "backend", "project_id": "backend", "status": "completed",
                  "result": {"prompt_version": "1.0", "errors": []}}],
        "test_results": [], "security_findings": [],
    }
    agent_result = AgentResult(agent="reviewer", status="completed", summary="ok")

    entry = build_quality_entry("backend", state, agent_result)

    assert entry["axes"]["relevante"] is None
    assert entry["axes"]["correta"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q5 -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'build_quality_entry'`.

- [ ] **Step 3: Add `build_quality_entry` to `src/orchestrator/quality.py`**

Add these two lines to the top of `src/orchestrator/quality.py` (below the existing `from typing import Any`):

```python
from schemas.models import AgentResult, utc_now
```

Then append at the end of the file:

```python
def build_quality_entry(project_id: str, state: dict[str, Any], agent_result: AgentResult) -> dict[str, Any]:
    """One quality_scores entry for a project the reviewer just examined: heuristic axes
    (tests/security/parse) blended with the reviewer's own LLM judgement (relevante/
    fonte_utilizada/alucinacao/cumprimento_regras).
    """
    task = _producing_task(project_id, state)
    result = (task.get("result") or {}) if task else {}
    quality = agent_result.quality or {}
    axes = {
        "correta": heuristic_correta(project_id, state),
        "relevante": quality.get("relevante"),
        "fonte_utilizada": quality.get("fonte_utilizada"),
        "alucinacao": quality.get("alucinacao"),
        "formato_valido": heuristic_formato_valido(project_id, state),
        "cumprimento_regras": quality.get("cumprimento_regras"),
        "seguranca": heuristic_seguranca(project_id, state),
    }
    return {
        "project_id": project_id,
        "agent": task.get("agent") if task else None,
        "prompt_version": result.get("prompt_version"),
        "axes": axes,
        "quality_score": blend_quality_score(axes),
        "estimated_cost": result.get("estimated_cost", 0.0),
        "duration_seconds": result.get("duration_seconds", 0.0),
        "computed_at": utc_now(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q5b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/quality.py tests/test_quality_scoring.py
git commit -m "feat: build a combined quality entry per reviewed project"
```

---

### Task 6: `quality_by_execution` / `quality_by_version` — aggregation for the dashboard

**Files:**
- Modify: `src/orchestrator/quality.py`
- Test: `tests/test_quality_scoring.py`

**Interfaces:**
- Produces: `quality_by_execution(states: dict[str, dict]) -> list[dict]` (flattens every execution's `quality_scores`, newest `computed_at` first, each row also carrying `execution_id`); `quality_by_version(rows: list[dict]) -> list[dict]` (groups by `(agent, prompt_version)`, returns `{agent, prompt_version, runs, avg_quality, avg_cost_usd, avg_duration_seconds}` sorted by `(agent, prompt_version)`). Task 9 (`/quality-data`) calls both.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_quality_scoring.py` (add `quality_by_execution, quality_by_version` to the import):

```python
def test_quality_by_execution_flattens_and_sorts_newest_first():
    states = {
        "exec-a": {"quality_scores": [{"project_id": "frontend", "computed_at": "2026-01-01T00:00:00+00:00"}]},
        "exec-b": {"quality_scores": [{"project_id": "backend", "computed_at": "2026-02-01T00:00:00+00:00"}]},
    }

    rows = quality_by_execution(states)

    assert [r["execution_id"] for r in rows] == ["exec-b", "exec-a"]


def test_quality_by_version_averages_and_groups_by_agent_and_version():
    rows = [
        {"agent": "frontend", "prompt_version": "1.7", "quality_score": 84.0, "estimated_cost": 0.02, "duration_seconds": 30.0},
        {"agent": "frontend", "prompt_version": "1.8", "quality_score": 91.0, "estimated_cost": 0.0208, "duration_seconds": 27.6},
        {"agent": "frontend", "prompt_version": "1.8", "quality_score": 89.0, "estimated_cost": 0.0212, "duration_seconds": 28.4},
    ]

    result = quality_by_version(rows)

    by_version = {item["prompt_version"]: item for item in result}
    assert by_version["1.7"]["runs"] == 1
    assert by_version["1.7"]["avg_quality"] == 84.0
    assert by_version["1.8"]["runs"] == 2
    assert by_version["1.8"]["avg_quality"] == 90.0
    assert by_version["1.8"]["avg_cost_usd"] == 0.021
    assert by_version["1.8"]["avg_duration_seconds"] == 28.0
    assert [item["prompt_version"] for item in result] == ["1.7", "1.8"]


def test_quality_by_version_skips_rows_missing_agent_or_version():
    rows = [{"agent": None, "prompt_version": "1.0", "quality_score": 50.0}]
    assert quality_by_version(rows) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q6 -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'quality_by_execution'`.

- [ ] **Step 3: Append the aggregation functions to `src/orchestrator/quality.py`**

```python
def quality_by_execution(states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for execution_id, state in states.items():
        for entry in state.get("quality_scores", []):
            rows.append({"execution_id": execution_id, **entry})
    rows.sort(key=lambda r: r.get("computed_at", ""), reverse=True)
    return rows


def quality_by_version(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        agent, version = row.get("agent"), row.get("prompt_version")
        if not agent or not version:
            continue
        grouped.setdefault((agent, version), []).append(row)
    result = []
    for (agent, version), items in grouped.items():
        scores = [r["quality_score"] for r in items if r.get("quality_score") is not None]
        result.append({
            "agent": agent, "prompt_version": version, "runs": len(items),
            "avg_quality": round(sum(scores) / len(scores), 1) if scores else None,
            "avg_cost_usd": round(sum(r.get("estimated_cost", 0.0) for r in items) / len(items), 6),
            "avg_duration_seconds": round(sum(r.get("duration_seconds", 0.0) for r in items) / len(items), 1),
        })
    result.sort(key=lambda item: (item["agent"], item["prompt_version"]))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_quality_scoring.py -v --basetemp=.test-tmp-q6b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q --basetemp=.test-tmp-q6c -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/quality.py tests/test_quality_scoring.py
git commit -m "feat: aggregate quality scores by execution and by prompt version"
```

---

### Task 7: `dispatch_agents` stamps `prompt_version` onto each result

**Files:**
- Modify: `src/orchestrator/nodes.py:229-276` (`dispatch_agents`)
- Test: `tests/test_orchestration_noop.py`

**Interfaces:**
- Consumes: `AGENT_ROLES[agent]["prompt_version"]` (Task 3).
- Produces: `task["result"]["prompt_version"]` set for every frontend/backend/python_ai task result — this is exactly the key `_producing_task`/`build_quality_entry` (Task 5) reads.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestration_noop.py` (add `from agents.registry import AGENT_ROLES` to the imports):

```python
class StampRuntime:
    """Minimal runtime double — just enough surface for dispatch_agents to run one task."""

    def __init__(self) -> None:
        self.store = _StampStore()

    async def prepare_worktree(self, state, project_id):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")

    async def run_task(self, state, task):
        return AgentResult(agent=task["agent"], status="completed", summary="done")

    async def publish(self, event):
        return None

    async def persist(self, state, node, event=None, payload=None):
        return state


class _StampStore:
    def event(self, *args, **kwargs):
        return None

    def agent_run(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_dispatch_agents_stamps_the_current_prompt_version_onto_the_result():
    state = {
        "plan": [{"task_id": "T001", "agent": "frontend", "project_id": "frontend",
                  "status": "pending", "dependencies": [], "description": "add a button"}],
        "approvals": {}, "worktrees": {}, "token_usage": {}, "estimated_cost": 0.0,
    }

    result = await nodes.dispatch_agents(StampRuntime(), state)

    task = result["plan"][0]
    assert task["result"]["prompt_version"] == AGENT_ROLES["frontend"]["prompt_version"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestration_noop.py -k stamps_the_current_prompt_version -v --basetemp=.test-tmp-q7 -p no:cacheprovider`
Expected: FAIL — `KeyError: 'prompt_version'`.

- [ ] **Step 3: Stamp `prompt_version` in `dispatch_agents`**

In `src/orchestrator/nodes.py`, find (inside `dispatch_agents`):

```python
            else:
                task["status"] = "completed" if result.status == "completed" else result.status
                task["result"] = result.model_dump(mode="json")
                state.setdefault("files_changed", []).extend(result.files_changed)
```

Change to:

```python
            else:
                task["status"] = "completed" if result.status == "completed" else result.status
                task["result"] = result.model_dump(mode="json")
                task["result"]["prompt_version"] = AGENT_ROLES.get(task["agent"], {}).get("prompt_version")
                state.setdefault("files_changed", []).extend(result.files_changed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestration_noop.py -v --basetemp=.test-tmp-q7b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_orchestration_noop.py
git commit -m "feat: stamp the producing agent's prompt version onto its task result"
```

---

### Task 8: `code_review` records a `quality_scores` entry per reviewed project

**Files:**
- Modify: `src/orchestrator/nodes.py:11-24` (imports), `:318-343` (`_run_gate_agent_across_projects`), `:429-449` (`code_review`)
- Test: `tests/test_orchestration_noop.py`

**Interfaces:**
- Consumes: `build_quality_entry` (Task 5).
- Produces: `_run_gate_agent_across_projects(..., on_project_result: Callable[[str, AgentResult], Awaitable[None]] | None = None)` — an optional callback invoked once per project, with that project's own (pre-merge) `AgentResult`. `code_review` uses it to append to `state["quality_scores"]`; `run_contract_validation` does not pass it (unchanged behavior — covered by the existing regression test in this file).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestration_noop.py`:

```python
@pytest.mark.asyncio
async def test_code_review_records_a_quality_score_per_reviewed_project(monkeypatch):
    async def two_changed_projects(runtime, state):
        return ["frontend", "backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", two_changed_projects)
    runtime = RecordingRuntime()
    state = {
        "plan": [
            {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "status": "completed",
             "result": {"prompt_version": "1.2", "estimated_cost": 0.01, "duration_seconds": 12.0, "errors": []}},
            {"task_id": "T002", "agent": "backend", "project_id": "backend", "status": "completed",
             "result": {"prompt_version": "2.0", "estimated_cost": 0.02, "duration_seconds": 20.0, "errors": []}},
            {"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"},
        ],
        "approvals": {}, "worktrees": {}, "test_results": [], "security_findings": [],
    }

    result = await nodes.code_review(runtime, state)

    scores = result["quality_scores"]
    assert {s["project_id"] for s in scores} == {"frontend", "backend"}
    frontend = next(s for s in scores if s["project_id"] == "frontend")
    assert frontend["agent"] == "frontend"
    assert frontend["prompt_version"] == "1.2"
    assert frontend["axes"]["correta"] == 100
    assert frontend["axes"]["formato_valido"] == 100
    assert frontend["axes"]["seguranca"] == 100
    # RecordingRuntime's AgentResult carries no `.quality`, so the LLM axes stay unscored.
    assert frontend["axes"]["relevante"] is None
    assert frontend["quality_score"] is not None


@pytest.mark.asyncio
async def test_run_contract_validation_does_not_record_quality_scores(monkeypatch):
    """Regression: only the reviewer's pass records quality — contracts is a different
    gate agent and must not gain this side effect."""
    async def two_changed_projects(runtime, state):
        return ["frontend", "backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", two_changed_projects)
    runtime = RecordingRuntime()
    state = {
        "plan": [{"task_id": "T010", "agent": "contracts", "status": "pending", "description": "validate contracts"}],
        "contracts": [], "approvals": {}, "worktrees": {},
    }

    result = await nodes.run_contract_validation(runtime, state)

    assert result.get("quality_scores", []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_orchestration_noop.py -k "records_a_quality_score or does_not_record_quality" -v --basetemp=.test-tmp-q8 -p no:cacheprovider`
Expected: FAIL — `test_code_review_records_a_quality_score_per_reviewed_project` fails with `KeyError: 'quality_scores'` (`test_run_contract_validation_does_not_record_quality_scores` passes trivially already since `state["quality_scores"]` is never touched by `run_contract_validation` — that's expected and fine, it's a regression guard for the next step).

- [ ] **Step 3: Add the `on_project_result` callback to `_run_gate_agent_across_projects`**

In `src/orchestrator/nodes.py`, find:

```python
async def _run_gate_agent_across_projects(runtime: ExecutionRuntime, state: dict[str, Any], task: dict[str, Any],
                                          changed_projects: list[str]) -> AgentResult:
    """Dispatch a gate agent (contracts/reviewer) once per changed project and merge the
    outcomes. Taking only ``changed_projects[0]`` silently skipped every other project a
    feature touched — e.g. a feature that changes both frontend and backend in the same
    execution would only ever get one of the two actually reviewed/validated.
    """
    per_project: list[AgentResult] = []
    for project_id in changed_projects:
        task["project_id"] = project_id
        task["worktree"] = str(runtime.workdir_for(state, project_id))
        task["branch"] = state.get("worktrees", {}).get(project_id, {}).get("branch")
        await runtime.announce_agent_started(state, task)
        agent_result = await runtime.run_task(state, task)
        task["status"] = "completed" if agent_result.status == "completed" else agent_result.status
        task["result"] = agent_result.model_dump(mode="json")
        await runtime.announce_agent_finished(state, task)
        per_project.append(agent_result)
```

Change to:

```python
async def _run_gate_agent_across_projects(runtime: ExecutionRuntime, state: dict[str, Any], task: dict[str, Any],
                                          changed_projects: list[str],
                                          on_project_result: Callable[[str, AgentResult], Awaitable[None]] | None = None) -> AgentResult:
    """Dispatch a gate agent (contracts/reviewer) once per changed project and merge the
    outcomes. Taking only ``changed_projects[0]`` silently skipped every other project a
    feature touched — e.g. a feature that changes both frontend and backend in the same
    execution would only ever get one of the two actually reviewed/validated.

    ``on_project_result``, if given, is awaited once per project with that project's own
    (pre-merge) ``AgentResult`` — the merge below loses per-project detail, so anything
    that needs it (e.g. quality scoring) must observe it here.
    """
    per_project: list[AgentResult] = []
    for project_id in changed_projects:
        task["project_id"] = project_id
        task["worktree"] = str(runtime.workdir_for(state, project_id))
        task["branch"] = state.get("worktrees", {}).get(project_id, {}).get("branch")
        await runtime.announce_agent_started(state, task)
        agent_result = await runtime.run_task(state, task)
        task["status"] = "completed" if agent_result.status == "completed" else agent_result.status
        task["result"] = agent_result.model_dump(mode="json")
        await runtime.announce_agent_finished(state, task)
        per_project.append(agent_result)
        if on_project_result is not None:
            await on_project_result(project_id, agent_result)
```

- [ ] **Step 4: Wire `code_review` to record quality scores**

In `src/orchestrator/nodes.py`, find:

```python
async def code_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    task = next((t for t in state.get("plan", []) if t.get("agent") == "reviewer"), None)
    result = ReviewResult(status="not_run", summary="review skipped in dry-run")
    if task and not state.get("approvals", {}).get("dry_run"):
        task["status"] = "running"
        changed_projects = await _changed_project_ids(runtime, state)
        if not changed_projects:
            agent = AgentResult(
                agent="reviewer",
                status="completed",
                summary="No files changed; code review was not required.",
            )
        else:
            agent = await _run_gate_agent_across_projects(runtime, state, task, changed_projects)
```

Change to:

```python
async def code_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    task = next((t for t in state.get("plan", []) if t.get("agent") == "reviewer"), None)
    result = ReviewResult(status="not_run", summary="review skipped in dry-run")
    if task and not state.get("approvals", {}).get("dry_run"):
        task["status"] = "running"
        changed_projects = await _changed_project_ids(runtime, state)
        if not changed_projects:
            agent = AgentResult(
                agent="reviewer",
                status="completed",
                summary="No files changed; code review was not required.",
            )
        else:
            async def _record_quality(project_id: str, agent_result: AgentResult) -> None:
                state.setdefault("quality_scores", []).append(build_quality_entry(project_id, state, agent_result))

            agent = await _run_gate_agent_across_projects(runtime, state, task, changed_projects, on_project_result=_record_quality)
```

And add the import — in `src/orchestrator/nodes.py`, find:

```python
from orchestrator.config import Settings
from orchestrator.routing import gates_pass, ready_tasks
```

Change to:

```python
from orchestrator.config import Settings
from orchestrator.quality import build_quality_entry
from orchestrator.routing import gates_pass, ready_tasks
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestration_noop.py -v --basetemp=.test-tmp-q8b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q --basetemp=.test-tmp-q8c -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/nodes.py tests/test_orchestration_noop.py
git commit -m "feat: code_review records a quality score per reviewed project"
```

---

### Task 9: `/dashboard-data` exposes `quality_scores`; new `/quality-data` endpoint

**Files:**
- Modify: `src/api/app.py:29-34` (imports), `:245-272` (`dashboard_data`), add new route after `dashboard_data` (currently ends at line 322, before `/events/view` at line 324)
- Test: `tests/test_dashboard_data.py`

**Interfaces:**
- Consumes: `quality_by_execution`, `quality_by_version` (Task 6).
- Produces: `GET /dashboard-data` items gain `"quality_scores"`; new `GET /quality-data?limit=200` returning `{"by_execution": [...], "by_version": [...]}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard_data.py` (needs `utc_now` already imported — it is, on line 17):

```python
async def _seed_execution_with_quality_scores(settings: Settings, execution_id: str, quality_scores: list[dict]) -> dict:
    now = utc_now()
    state = {
        "execution_id": execution_id, "project_id": "bezalel", "feature_request": "add a button",
        "status": "completed", "started_at": now, "updated_at": now, "context_health": {}, "plan": [],
        "quality_scores": quality_scores,
    }
    SQLiteCheckpointer(settings.checkpoint_path).save(execution_id, state, "generate_final_report")
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = state
        await saver.aput(
            {"configurable": {"thread_id": execution_id, "checkpoint_ns": ""}}, checkpoint,
            {"source": "input", "step": -1, "writes": {}, "parents": {}}, {},
        )
    return state


@pytest.mark.asyncio
async def test_dashboard_data_exposes_quality_scores_per_execution(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await _seed_execution_with_quality_scores(settings, "exec-quality-1", [
        {"project_id": "frontend", "agent": "frontend", "prompt_version": "1.8",
         "axes": {"correta": 100}, "quality_score": 91.0, "estimated_cost": 0.02,
         "duration_seconds": 30.0, "computed_at": utc_now()},
    ])

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/dashboard-data")

    item = next(i for i in response.json()["items"] if i["execution_id"] == "exec-quality-1")
    assert item["quality_scores"][0]["project_id"] == "frontend"
    assert item["quality_scores"][0]["quality_score"] == 91.0


@pytest.mark.asyncio
async def test_quality_data_aggregates_by_agent_and_prompt_version(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await _seed_execution_with_quality_scores(settings, "exec-v17", [
        {"project_id": "frontend", "agent": "frontend", "prompt_version": "1.7",
         "axes": {}, "quality_score": 84.0, "estimated_cost": 0.02, "duration_seconds": 30.0,
         "computed_at": utc_now()},
    ])
    await _seed_execution_with_quality_scores(settings, "exec-v18", [
        {"project_id": "frontend", "agent": "frontend", "prompt_version": "1.8",
         "axes": {}, "quality_score": 91.0, "estimated_cost": 0.0208, "duration_seconds": 27.6,
         "computed_at": utc_now()},
    ])

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/quality-data")

    payload = response.json()
    assert len(payload["by_execution"]) == 2
    by_version = {item["prompt_version"]: item for item in payload["by_version"]}
    assert by_version["1.7"]["avg_quality"] == 84.0
    assert by_version["1.8"]["avg_quality"] == 91.0
    assert [item["prompt_version"] for item in payload["by_version"]] == ["1.7", "1.8"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dashboard_data.py -v --basetemp=.test-tmp-q9 -p no:cacheprovider`
Expected: FAIL — `test_dashboard_data_exposes_quality_scores_per_execution` fails with `KeyError: 'quality_scores'`; `test_quality_data_aggregates_by_agent_and_prompt_version` fails with 404 (route doesn't exist).

- [ ] **Step 3: Import the new aggregation functions in `src/api/app.py`**

Find:

```python
from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
```

Change to:

```python
from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
from orchestrator.quality import quality_by_execution, quality_by_version
```

- [ ] **Step 4: Expose `quality_scores` on each dashboard item**

Find:

```python
                "commits": state.get("commits", []),
                "merges": state.get("merges", []),
                "pull_requests": state.get("pull_requests", []),
            })
```

Change to:

```python
                "commits": state.get("commits", []),
                "merges": state.get("merges", []),
                "pull_requests": state.get("pull_requests", []),
                "quality_scores": state.get("quality_scores", []),
            })
```

- [ ] **Step 5: Add the `/quality-data` route**

Find the end of `dashboard_data` (the closing of its `return {...}` block, right before `@app.get("/events/view"...)`):

```python
            "source": {
                "checkpoint": str(settings.langgraph_checkpoint_path),
                "updated_from": "LangGraph native checkpointer",
            },
        }

    @app.get("/events/view", response_class=HTMLResponse)
```

Change to:

```python
            "source": {
                "checkpoint": str(settings.langgraph_checkpoint_path),
                "updated_from": "LangGraph native checkpointer",
            },
        }

    @app.get("/quality-data")
    async def quality_data(limit: int = 200) -> dict[str, Any]:
        """Per-project quality scores plus the prompt-version comparison table."""
        states = await langgraph_states()
        rows = quality_by_execution(states)
        return {"by_execution": rows[:limit], "by_version": quality_by_version(rows)}

    @app.get("/events/view", response_class=HTMLResponse)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard_data.py -v --basetemp=.test-tmp-q9b -p no:cacheprovider`
Expected: PASS — all tests in the file.

- [ ] **Step 7: Run the full suite and compile check**

Run: `python -m pytest -q --basetemp=.test-tmp-q9c -p no:cacheprovider`
Expected: PASS

Run: `python -m compileall -q src tests`
Expected: no output (success)

- [ ] **Step 8: Commit**

```bash
git add src/api/app.py tests/test_dashboard_data.py
git commit -m "feat: expose quality scores and prompt-version comparison via the API"
```

---

### Task 10: Panel — new "Qualidade" tab

**Files:**
- Modify: `web/Plataforma de Administração.dc.html` (and sync to `D:/Project/projeto-agents-plataform/Plataforma de Administração.dc.html`, same as every prior panel change this session)

**Interfaces:**
- Consumes: `GET /quality-data` (Task 9), `item.quality_scores` from `GET /dashboard-data` (unused directly by this task, but available if a later drill-down is added — YAGNI: not built now).

- [ ] **Step 1: Add the nav entry**

Find (in the `navItems` array built in the component's render method):

```javascript
        { key: "health", label: "SAÚDE" },
        { key: "projects", label: "PROJETOS" },
```

Change to:

```javascript
        { key: "health", label: "SAÚDE" },
        { key: "quality", label: "QUALIDADE" },
        { key: "projects", label: "PROJETOS" },
```

And find:

```javascript
      isHealth: this.state.view === "health",
      isProjects: this.state.view === "projects",
```

Change to:

```javascript
      isHealth: this.state.view === "health",
      isQuality: this.state.view === "quality",
      isProjects: this.state.view === "projects",
```

- [ ] **Step 2: Add quality state and polling**

Find the `state = {` block's `liveData: null,` line and add a sibling field:

```javascript
    liveData: null,
    qualityData: null,
```

Find `componentDidMount()`'s first line (`this.refreshDashboard();`) and add a call right after it, plus extend the same poll interval to also refresh quality:

```javascript
  componentDidMount() {
    this.refreshDashboard();
    this.refreshQuality();
    this._dashboardPoll = setInterval(() => { this.refreshDashboard(); this.refreshQuality(); }, 2000);
```

Add a new method right after `refreshDashboard()`'s closing brace (before `showToast`):

```javascript
  refreshQuality() {
    fetch("/quality-data")
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("quality-data unavailable")))
      .then((data) => { this.setState({ qualityData: data }); })
      .catch(() => {});
  }
```

(Errors are swallowed here, not toasted — this is a secondary panel, not the primary dashboard poll that already reports connectivity issues.)

- [ ] **Step 3: Add the render-data block for the new tab**

Find the render data object's `activeAgentCount: Object.keys(this.state.liveAgents).length,` line (near the other computed dashboard fields) and add above it:

```javascript
      qualityByExecution: (this.state.qualityData ? this.state.qualityData.by_execution : []).slice(0, 20).map((row) => ({
        key: row.execution_id + ":" + row.project_id + ":" + row.computed_at,
        project: row.project_id,
        agent: row.agent || "-",
        version: row.prompt_version || "-",
        score: row.quality_score === null || row.quality_score === undefined ? "—" : Math.round(row.quality_score) + "%",
        axes: ["correta", "relevante", "fonte_utilizada", "alucinacao", "formato_valido", "cumprimento_regras", "seguranca"].map((key) => ({
          label: key, value: (row.axes && row.axes[key] !== null && row.axes[key] !== undefined) ? row.axes[key] + "%" : "—",
        })),
      })),
      hasQualityByExecution: !!(this.state.qualityData && this.state.qualityData.by_execution && this.state.qualityData.by_execution.length),
      qualityByVersion: (this.state.qualityData ? this.state.qualityData.by_version : []).map((row, index, all) => {
        const prev = all.slice(0, index).reverse().find((p) => p.agent === row.agent);
        const pct = (curr, before) => (before === null || before === undefined || before === 0 || curr === null || curr === undefined) ? null : Math.round(((curr - before) / before) * 1000) / 10;
        const qualityDelta = prev && row.avg_quality !== null && prev.avg_quality !== null ? Math.round((row.avg_quality - prev.avg_quality) * 10) / 10 : null;
        const costDelta = prev ? pct(row.avg_cost_usd, prev.avg_cost_usd) : null;
        const durationDelta = prev ? pct(row.avg_duration_seconds, prev.avg_duration_seconds) : null;
        const fmtDelta = (v, suffix) => v === null ? "" : (v > 0 ? "+" : "") + v + suffix;
        return {
          key: row.agent + ":" + row.prompt_version,
          agent: row.agent, version: row.prompt_version, runs: row.runs,
          quality: row.avg_quality === null ? "—" : Math.round(row.avg_quality) + "%",
          qualityDelta: fmtDelta(qualityDelta, "pp"),
          cost: "US$" + row.avg_cost_usd.toFixed(4),
          costDelta: fmtDelta(costDelta, "%"),
          duration: row.avg_duration_seconds + "s",
          durationDelta: fmtDelta(durationDelta, "%"),
        };
      }),
      hasQualityByVersion: !!(this.state.qualityData && this.state.qualityData.by_version && this.state.qualityData.by_version.length),
      activeAgentCount: Object.keys(this.state.liveAgents).length,
```

- [ ] **Step 4: Add the tab's markup**

Find (the end of the "SAÚDE" screen — line 594-598 today — immediately followed by the "PROJETOS" screen's opening; this exact 5-line sequence is unique in the file because the `PROJETOS` comment marker only appears once):

```html
      </div>
    </sc-if>

    <!-- ===================== PROJETOS ===================== -->
    <sc-if value="{{ isProjects }}" hint-placeholder-val="{{ false }}">
```

Change to (the `</sc-if>` that was closing SAÚDE stays exactly where it is; QUALIDADE is inserted after it; PROJETOS then starts exactly as before):

```html
      </div>
    </sc-if>

    <!-- ===================== QUALIDADE ===================== -->
    <sc-if value="{{ isQuality }}" hint-placeholder-val="{{ false }}">
      <div data-screen-label="Qualidade" style="max-width:1180px;">
        <div style="margin-bottom:26px; border-bottom:2px solid #14140F; padding-bottom:14px;">
          <h1 style="font-size:26px; font-weight:800; letter-spacing:0.01em; margin:0 0 5px; text-transform:uppercase;">Qualidade</h1>
          <p style="font-size:12.5px; color:#5A564C; margin:0;">Nota por eixo de cada revisão, e como a qualidade/custo/latência mudam entre versões de prompt.</p>
        </div>

        <div style="border:1.5px solid #14140F; margin-bottom:24px;">
          <div style="background:#14140F; color:#fff; padding:8px 16px; font-size:11px; letter-spacing:0.08em; text-transform:uppercase; font-weight:700;">Últimas avaliações</div>
          <div style="padding:16px;">
            <sc-if value="{{ hasQualityByExecution }}" hint-placeholder-val="{{ false }}">
              <div style="display:flex; flex-direction:column; gap:10px;">
                <sc-for list="{{ qualityByExecution }}" as="q" hint-placeholder-count="3">
                  <div style="border:1px solid #14140F; padding:12px 14px;">
                    <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px;">
                      <span style="font-size:12px; font-weight:800; text-transform:uppercase;">{{ q.agent }} · {{ q.project }}</span>
                      <span style="font-size:10px; color:#5A564C; text-transform:uppercase;">prompt v{{ q.version }} · nota {{ q.score }}</span>
                    </div>
                    <div style="display:flex; flex-wrap:wrap; gap:8px;">
                      <sc-for list="{{ q.axes }}" as="ax" hint-placeholder-count="7">
                        <span style="font-size:10px; padding:4px 8px; border:1px solid #9B9689; color:#5A564C; text-transform:uppercase;">{{ ax.label }}: {{ ax.value }}</span>
                      </sc-for>
                    </div>
                  </div>
                </sc-for>
              </div>
            </sc-if>
            <sc-if value="{{ !hasQualityByExecution }}" hint-placeholder-val="{{ true }}">
              <div style="font-size:11.5px; color:#5A564C; font-style:italic;">Nenhuma avaliação registrada ainda.</div>
            </sc-if>
          </div>
        </div>

        <div style="border:1.5px solid #14140F;">
          <div style="background:#14140F; color:#fff; padding:8px 16px; font-size:11px; letter-spacing:0.08em; text-transform:uppercase; font-weight:700;">Comparação entre versões de prompt</div>
          <div style="padding:16px;">
            <sc-if value="{{ hasQualityByVersion }}" hint-placeholder-val="{{ false }}">
              <div style="display:flex; flex-direction:column; gap:8px;">
                <sc-for list="{{ qualityByVersion }}" as="v" hint-placeholder-count="2">
                  <div style="display:flex; justify-content:space-between; align-items:center; border:1px solid #14140F; padding:10px 14px;">
                    <span style="font-size:12px; font-weight:700; text-transform:uppercase;">{{ v.agent }} — prompt v{{ v.version }} ({{ v.runs }} exec.)</span>
                    <span style="font-size:11px; color:#5A564C;">qualidade {{ v.quality }} {{ v.qualityDelta }} · custo {{ v.cost }} {{ v.costDelta }} · latência {{ v.duration }} {{ v.durationDelta }}</span>
                  </div>
                </sc-for>
              </div>
            </sc-if>
            <sc-if value="{{ !hasQualityByVersion }}" hint-placeholder-val="{{ true }}">
              <div style="font-size:11.5px; color:#5A564C; font-style:italic;">Sem dado suficiente para comparar versões ainda.</div>
            </sc-if>
          </div>
        </div>
      </div>
    </sc-if>

    <!-- ===================== PROJETOS ===================== -->
    <sc-if value="{{ isProjects }}" hint-placeholder-val="{{ false }}">
```

- [ ] **Step 5: Syntax-check the inline JS**

Run:
```bash
node -e "
const fs = require('fs');
const html = fs.readFileSync('web/Plataforma de Administração.dc.html', 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
for (const s of scripts) {
  try { new Function(s); } catch (e) { console.log('SYNTAX ERROR:', e.message); process.exit(1); }
}
console.log('JS_OK, scripts checked:', scripts.length);
"
```
Expected: `JS_OK, scripts checked: 3`

- [ ] **Step 6: Run the full Python suite one more time (panel change touches no Python, but confirms nothing else drifted)**

Run: `python -m pytest -q --basetemp=.test-tmp-q10 -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7: Sync the panel file to its mirror path and commit**

```bash
cp "web/Plataforma de Administração.dc.html" "/d/Project/projeto-agents-plataform/Plataforma de Administração.dc.html"
git add "web/Plataforma de Administração.dc.html"
git commit -m "feat: add a Qualidade tab to the panel with per-review axes and version comparison"
```

---

## Manual verification (after all tasks)

1. Start the API (targeted restart if one is already running — never a broad `taskkill`).
2. Trigger a real execution that touches at least one project through the full pipeline (`code_review` must run, i.e. some file must actually change).
3. Open the panel's new "Qualidade" tab and confirm: at least one card appears under "Últimas avaliações" with the 7 axes (some may show "—" if the reviewer didn't populate `quality`, which is expected and not an error); after two executions of the same project, "Comparação entre versões de prompt" shows one row (version hasn't changed yet, so no delta is expected — bump `AGENT_ROLES["frontend"]["prompt_version"]` by hand and run again to see a second row with deltas).
