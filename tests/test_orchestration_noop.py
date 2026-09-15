from __future__ import annotations

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
