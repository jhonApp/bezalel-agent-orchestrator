from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from orchestrator import nodes
from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
from orchestrator.nodes import ExecutionRuntime


class NoAgentRuntime:
    async def run_task(self, state, task):  # pragma: no cover - a call is the failure
        raise AssertionError(f"agent {task['agent']} must not run without changed files")

    async def persist(self, state, node, event=None, payload=None):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")


@pytest.mark.asyncio
async def test_contract_validation_completes_without_agent_when_no_files_changed(monkeypatch):
    async def no_changed_projects(runtime, state):
        return []

    monkeypatch.setattr(nodes, "_changed_project_ids", no_changed_projects)
    state = {
        "plan": [{"task_id": "T010", "agent": "contracts", "status": "pending"}],
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
        "plan": [{"task_id": "T013", "agent": "reviewer", "status": "pending"}],
        "approvals": {},
    }

    result = await nodes.code_review(NoAgentRuntime(), state)

    task = result["plan"][0]
    assert task["status"] == "completed"
    assert task["result"]["status"] == "completed"
    assert result["review_results"][0]["status"] == "approved"
    assert result["review_results"][0]["blocking"] is False


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
