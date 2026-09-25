from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.registry import AGENT_ROLES
from orchestrator import nodes
from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
from orchestrator.nodes import ExecutionRuntime
from schemas.models import AgentResult, SecurityFinding


class NoAgentRuntime:
    async def run_task(self, state, task):  # pragma: no cover - a call is the failure
        raise AssertionError(f"agent {task['agent']} must not run without changed files")

    async def persist(self, state, node, event=None, payload=None):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")

    async def announce_agent_started(self, state, task):
        return None

    async def announce_agent_finished(self, state, task):
        return None


class RecordingRuntime:
    """Records which project_id run_task was actually dispatched against, per call — proves
    a gate agent reviews/validates every changed project, not just the first one."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.announced_started: list[str] = []
        self.announced_finished: list[str] = []

    async def run_task(self, state, task):
        self.calls.append(task["project_id"])
        failed = task["project_id"] == "backend"
        return AgentResult(
            agent=task["agent"], status="failed" if failed else "completed",
            summary=f"reviewed {task['project_id']}",
            errors=[f"{task['project_id']} needs changes"] if failed else [],
        )

    async def persist(self, state, node, event=None, payload=None):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")

    async def announce_agent_started(self, state, task):
        self.announced_started.append(task["project_id"])

    async def announce_agent_finished(self, state, task):
        self.announced_finished.append(task["project_id"])


class TokenAwareRuntime:
    """Stub whose run_task returns a distinct real token/cost figure per project, to prove
    _run_gate_agent_across_projects sums them into the merged result instead of discarding
    them — the same class of bug already fixed for the `quality` field."""

    def __init__(self) -> None:
        self._by_project = {
            "frontend": AgentResult(agent="reviewer", status="completed", summary="ok frontend",
                                    tokens_input=1000, tokens_output=100, estimated_cost=0.01),
            "backend": AgentResult(agent="reviewer", status="completed", summary="ok backend",
                                   tokens_input=2000, tokens_output=200, estimated_cost=0.02),
        }

    async def run_task(self, state, task):
        return self._by_project[task["project_id"]]

    def workdir_for(self, state, project_id):
        return Path(".")

    async def announce_agent_started(self, state, task):
        return None

    async def announce_agent_finished(self, state, task):
        return None


@pytest.mark.asyncio
async def test_run_gate_agent_across_projects_sums_real_tokens_and_cost_across_projects():
    task = {"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"}
    state = {"worktrees": {}}

    result = await nodes._run_gate_agent_across_projects(TokenAwareRuntime(), state, task, ["frontend", "backend"])

    assert result.tokens_input == 3000
    assert result.tokens_output == 300
    assert result.estimated_cost == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_contract_validation_completes_without_agent_when_no_files_changed(monkeypatch):
    async def no_changed_projects(runtime, state):
        return []

    monkeypatch.setattr(nodes, "_changed_project_ids", no_changed_projects)
    state = {
        "plan": [{"task_id": "T010", "agent": "contracts", "status": "pending", "description": "validate contracts"}],
        "contracts": [],
        "approvals": {},
    }

    result = await nodes.run_contract_validation(NoAgentRuntime(), state)

    task = result["plan"][0]
    assert task["status"] == "completed"
    assert task["result"]["status"] == "completed"
    assert "No files changed" in task["result"]["summary"]
    assert result["contracts"] == []


@pytest.mark.asyncio
async def test_code_review_approves_without_agent_when_no_files_changed(monkeypatch):
    async def no_changed_projects(runtime, state):
        return []

    monkeypatch.setattr(nodes, "_changed_project_ids", no_changed_projects)
    state = {
        "plan": [{"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"}],
        "approvals": {},
    }

    result = await nodes.code_review(NoAgentRuntime(), state)

    task = result["plan"][0]
    assert task["status"] == "completed"
    assert task["result"]["status"] == "completed"
    assert result["review_results"][0]["status"] == "approved"
    assert result["review_results"][0]["blocking"] is False


@pytest.mark.asyncio
async def test_run_contract_validation_validates_every_changed_project_not_just_the_first(monkeypatch):
    async def two_changed_projects(runtime, state):
        return ["frontend", "backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", two_changed_projects)
    runtime = RecordingRuntime()
    state = {
        "plan": [{"task_id": "T010", "agent": "contracts", "status": "pending", "description": "validate contracts"}],
        "contracts": [], "approvals": {}, "worktrees": {},
    }

    result = await nodes.run_contract_validation(runtime, state)

    assert runtime.calls == ["frontend", "backend"], "must validate every changed project, not just changed_projects[0]"
    task = result["plan"][0]
    assert task["status"] == "failed"
    assert runtime.announced_started == ["frontend", "backend"]
    assert runtime.announced_finished == ["frontend", "backend"]


@pytest.mark.asyncio
async def test_code_review_reviews_every_changed_project_not_just_the_first(monkeypatch):
    async def two_changed_projects(runtime, state):
        return ["frontend", "backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", two_changed_projects)
    runtime = RecordingRuntime()
    state = {
        "plan": [{"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"}],
        "approvals": {}, "worktrees": {},
    }

    result = await nodes.code_review(runtime, state)

    assert runtime.calls == ["frontend", "backend"], "must review every changed project, not just changed_projects[0]"
    task = result["plan"][0]
    assert task["status"] == "failed"
    assert result["review_results"][0]["blocking"] is True
    assert "frontend" in result["review_results"][0]["summary"]
    assert "backend" in result["review_results"][0]["summary"]


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path,
        workspace_root=tmp_path,
        frontend_path=tmp_path,
        backend_path=tmp_path,
        python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


@pytest.mark.asyncio
async def test_cancel_updates_durable_and_native_checkpoints(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings)
    execution_id = "execution-1"
    runtime.store.save(execution_id, {
        "execution_id": execution_id,
        "project_id": "bezalel",
        "feature_request": "read-only check",
        "status": "running",
    }, "started")
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        graph = object.__new__(OrchestrationGraph)
        graph.runtime = runtime
        graph.checkpointer = saver

        state = await graph.cancel(execution_id)
        checkpoints = [
            item async for item in saver.alist({"configurable": {"thread_id": execution_id}})
        ]

    assert state is not None
    assert state["status"] == "cancelled"
    assert state["updated_at"]
    assert runtime.store.load(execution_id)["status"] == "cancelled"
    assert checkpoints[0].checkpoint["channel_values"]["status"] == "cancelled"


class FakeCompiled:
    """Stands in for the compiled LangGraph so the test observes exactly what state resume()
    hands to a real invocation, without needing real project detection or Codex subprocesses."""

    def __init__(self) -> None:
        self.invoked_with: dict | None = None

    async def ainvoke(self, state, config):
        self.invoked_with = state
        return {**state, "status": "completed", "next_action": "done"}


@pytest.mark.asyncio
async def test_resume_reruns_an_execution_that_was_previously_marked_finished(tmp_path: Path) -> None:
    """A reconciled/failed execution (status=failed, next_action=done — exactly what
    reconcile_interrupted_executions() and generate_final_report both leave behind) must
    actually restart the graph on resume, not silently no-op because resume()'s in-memory
    status flip never reached the store before run() re-read it."""
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings)
    execution_id = "execution-resume-1"
    runtime.store.save(execution_id, {
        "execution_id": execution_id, "project_id": "bezalel", "feature_request": "retry this",
        "status": "failed", "next_action": "done", "approvals": {},
    }, "generate_final_report")

    graph = object.__new__(OrchestrationGraph)
    graph.runtime = runtime
    graph.checkpointer = None
    fake_compiled = FakeCompiled()
    graph.compiled = fake_compiled

    await graph.resume(execution_id)

    assert fake_compiled.invoked_with is not None, "resume() must not silently no-op on a finished execution"
    assert fake_compiled.invoked_with["status"] == "running"


FAKE_CODEX_PROGRESS = r'''
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
print(json.dumps({"kind": "tool_call", "tool": "apply_patch"}), flush=True)
output.write_text(json.dumps({
    "status": "completed", "summary": "ok",
    "files_changed": [], "tests": [], "contracts_changed": [],
    "errors": [], "next_action": None, "tokens_input": 1, "tokens_output": 1,
}), encoding="utf-8")
sys.exit(0)
'''


@pytest.mark.asyncio
async def test_run_contract_validation_announces_agent_lifecycle_when_dispatched(tmp_path: Path, monkeypatch) -> None:
    """contracts calls run_task exactly like the parallel frontend/backend/python_ai dispatch
    does, but never published agent.started/agent.finished — its card in Manage Agents could
    never show "Executando agora", always looking idle even while genuinely running."""
    script = tmp_path / "fake_codex_progress.py"
    script.write_text(FAKE_CODEX_PROGRESS, encoding="utf-8")
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    published: list[dict] = []

    async def capture(event: dict) -> None:
        published.append(event)

    async def one_changed_project(runtime, state):
        return ["backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", one_changed_project)
    runtime = ExecutionRuntime(settings, event_sink=capture)
    state = {
        "execution_id": "execution-contracts-1", "feature_request": "add a button",
        "worktrees": {}, "detected_projects": [], "contracts": [], "approvals": {},
        "plan": [{"task_id": "T010", "agent": "contracts", "status": "pending", "description": "validate contracts"}],
    }

    result = await nodes.run_contract_validation(runtime, state)

    task = result["plan"][0]
    assert task["status"] == "completed"
    lifecycle_types = [e.get("type") for e in published if e.get("type") in {"agent.started", "agent.finished"}]
    assert lifecycle_types == ["agent.started", "agent.finished"]
    assert all(e.get("agent") == "contracts" for e in published if e.get("type") in {"agent.started", "agent.finished"})
    assert runtime.store.active_agents() == []


@pytest.mark.asyncio
async def test_code_review_announces_agent_lifecycle_when_dispatched(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "fake_codex_progress.py"
    script.write_text(FAKE_CODEX_PROGRESS, encoding="utf-8")
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    published: list[dict] = []

    async def capture(event: dict) -> None:
        published.append(event)

    async def one_changed_project(runtime, state):
        return ["backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", one_changed_project)
    runtime = ExecutionRuntime(settings, event_sink=capture)
    state = {
        "execution_id": "execution-reviewer-1", "feature_request": "add a button",
        "worktrees": {}, "detected_projects": [], "approvals": {},
        "plan": [{"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"}],
    }

    result = await nodes.code_review(runtime, state)

    task = result["plan"][0]
    assert task["status"] == "completed"
    lifecycle_types = [e.get("type") for e in published if e.get("type") in {"agent.started", "agent.finished"}]
    assert lifecycle_types == ["agent.started", "agent.finished"]
    assert all(e.get("agent") == "reviewer" for e in published if e.get("type") in {"agent.started", "agent.finished"})
    assert runtime.store.active_agents() == []


@pytest.mark.asyncio
async def test_run_task_publishes_agent_stream_events(tmp_path: Path):
    script = tmp_path / "fake_codex_progress.py"
    script.write_text(FAKE_CODEX_PROGRESS, encoding="utf-8")
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    published: list[dict] = []

    async def capture(event: dict) -> None:
        published.append(event)

    runtime = ExecutionRuntime(settings, event_sink=capture)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    stream_events = [event for event in published if event.get("type") == "agent.stream"]
    assert stream_events, "expected at least one agent.stream event while the agent was running"
    first = stream_events[0]
    assert first["execution_id"] == "execution-1"
    assert first["agent"] == "frontend"
    assert first["task_id"] == "T001"
    assert first["parsed"] == {"kind": "tool_call", "tool": "apply_patch"}


RATE_LIMIT_SCRIPT_TEMPLATE = r'''
import json
import pathlib
import sys
calls_dir = pathlib.Path(r"{calls_dir}")
(calls_dir / str(len(list(calls_dir.iterdir())))).write_text("call")
print(json.dumps({{"type": "turn.failed", "error": {{"message": "You’ve hit your usage limit. Upgrade to Pro or try again later."}}}}), flush=True)
sys.exit(1)
'''

GENUINE_FAILURE_SCRIPT_TEMPLATE = r'''
import json
import pathlib
import sys
calls_dir = pathlib.Path(r"{calls_dir}")
(calls_dir / str(len(list(calls_dir.iterdir())))).write_text("call")
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps({{
    "status": "failed", "summary": "boom",
    "files_changed": [], "tests": [], "contracts_changed": [],
    "errors": ["boom"], "next_action": None, "tokens_input": 1, "tokens_output": 1,
}}), encoding="utf-8")
sys.exit(1)
'''


@pytest.mark.asyncio
async def test_run_task_does_not_retry_a_rate_limited_result(tmp_path: Path):
    """A real usage-limit hit does not reset within this loop's 2s/4s backoff — retrying it
    only guarantees hitting the same wall again, burning another full prompt's worth of input
    tokens right when the account is already out of quota."""
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    script = tmp_path / "fake_codex_rate_limit.py"
    script.write_text(RATE_LIMIT_SCRIPT_TEMPLATE.format(calls_dir=str(calls_dir)), encoding="utf-8")
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
        max_retries=2, retry_backoff_seconds=0.01,
    )
    runtime = ExecutionRuntime(settings)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "rate_limited"
    assert len(list(calls_dir.iterdir())) == 1, "rate_limited must not be retried"


@pytest.mark.asyncio
async def test_run_task_still_retries_a_genuine_failure(tmp_path: Path):
    """The rate_limited carve-out must not turn into "never retry anything" — a plain
    failure (a bad edit, a flaky tool call) keeps the existing retry budget."""
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    script = tmp_path / "fake_codex_genuine_failure.py"
    script.write_text(GENUINE_FAILURE_SCRIPT_TEMPLATE.format(calls_dir=str(calls_dir)), encoding="utf-8")
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
        max_retries=2, retry_backoff_seconds=0.01,
    )
    runtime = ExecutionRuntime(settings)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "failed"
    assert len(list(calls_dir.iterdir())) == 3, "a genuine failure keeps retrying up to max_retries"


@pytest.mark.asyncio
async def test_run_task_injects_known_project_context_into_the_prompt(tmp_path: Path):
    """discover_projects already learns framework/package manager/exact commands/important
    files once per execution — the agent must receive that directly instead of re-deriving
    it via several rounds of file reads on every single task."""
    captured_prompts: list[str] = []

    class CapturingCodex:
        async def execute(self, prompt, workdir, role, **kwargs):
            captured_prompts.append(prompt)
            return AgentResult(agent=role, status="completed", summary="ok")

    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    runtime = ExecutionRuntime(settings, clis={"codex": CapturingCodex()})
    state = {
        "execution_id": "execution-1", "feature_request": "add a button", "worktrees": {},
        "detected_projects": [{
            "project_id": "frontend", "expected_name": "bezalel-app", "path": str(tmp_path), "exists": True,
            "framework": ["React", "Vite"], "package_manager": "npm",
            "commands": {"test": "npm run test", "lint": "npm run lint"},
            "test_strategy": "Vitest", "important_files": ["package.json", "vite.config.ts"],
        }],
    }
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    await runtime.run_task(state, task)

    assert captured_prompts, "expected the agent to be invoked"
    prompt = captured_prompts[0]
    assert "React, Vite" in prompt
    assert "npm" in prompt
    assert "npm run test" in prompt
    assert "Vitest" in prompt
    assert "package.json" in prompt


@pytest.mark.asyncio
async def test_run_task_lowers_reasoning_effort_only_for_gate_roles(tmp_path: Path):
    """contracts/reviewer judge an already-produced diff — frontend/backend/python_ai still
    write the code, so they keep the CLI's own default reasoning effort (no override)."""
    from agents.registry import GATE_ROLES

    captured: list[dict] = []

    class CapturingCodex:
        async def execute(self, prompt, workdir, role, **kwargs):
            captured.append(kwargs)
            return AgentResult(agent=role, status="completed", summary="ok")

    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    runtime = ExecutionRuntime(settings, clis={"codex": CapturingCodex()})
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}

    for role in ("frontend", "contracts"):
        task = {"task_id": "T-" + role, "agent": role, "project_id": "backend", "description": "do it", "acceptance_criteria": []}
        await runtime.run_task(state, task)

    assert "contracts" in GATE_ROLES and "frontend" not in GATE_ROLES
    assert captured[0]["reasoning_effort"] is None  # frontend
    assert captured[1]["reasoning_effort"] == settings.codex_reasoning_effort_gates  # contracts


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


class StampRuntime:
    """Minimal runtime double — just enough surface for dispatch_agents to run one task."""

    def __init__(self) -> None:
        self.store = _StampStore()

    def cancel_event(self, execution_id):
        return asyncio.Event()

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
        "execution_id": "execution-t007",
        "plan": [{"task_id": "T001", "agent": "frontend", "project_id": "frontend",
                  "status": "pending", "dependencies": [], "description": "add a button"}],
        "approvals": {}, "worktrees": {}, "token_usage": {}, "estimated_cost": 0.0,
    }

    result = await nodes.dispatch_agents(StampRuntime(), state)

    task = result["plan"][0]
    assert task["result"]["prompt_version"] == AGENT_ROLES["frontend"]["prompt_version"]


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
async def test_code_review_does_not_abort_when_quality_scoring_raises(monkeypatch):
    async def two_changed_projects(runtime, state):
        return ["frontend", "backend"]

    monkeypatch.setattr(nodes, "_changed_project_ids", two_changed_projects)

    def exploding_build_quality_entry(project_id, state, agent_result):
        raise ValueError(f"boom for {project_id}")

    monkeypatch.setattr(nodes, "build_quality_entry", exploding_build_quality_entry)
    runtime = RecordingRuntime()
    state = {
        "plan": [{"task_id": "T013", "agent": "reviewer", "status": "pending", "description": "review the diff"}],
        "approvals": {}, "worktrees": {}, "errors": [],
    }

    result = await nodes.code_review(runtime, state)

    # Both projects still got reviewed (the exception in quality-scoring didn't abort the loop);
    # the review's own status/results are unaffected by the quality-scoring failure.
    assert runtime.calls == ["frontend", "backend"]
    assert result["review_results"][0]["status"] in ("approved", "changes_requested")
    assert any("quality scoring frontend" in e for e in result["errors"])
    assert any("quality scoring backend" in e for e in result["errors"])
    assert result.get("quality_scores", []) == []


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
