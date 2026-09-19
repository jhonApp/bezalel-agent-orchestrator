from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from api.app import create_app
from orchestrator.config import Settings
from persistence.checkpointer import SQLiteCheckpointer
from schemas.models import utc_now


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_api_startup_clears_agents_orphaned_by_a_previous_process_death(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    # Simulate a prior run of the API that was killed mid-task: "agent.started" was logged,
    # the process died before "agent.finished" ever ran.
    SQLiteCheckpointer(settings.checkpoint_path).event(
        "exec-dead", "agent.started", {"agent": "frontend", "task_id": "T001"}, "2026-01-01T00:00:00+00:00",
    )

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/dashboard-data")

    assert response.status_code == 200
    assert response.json()["active_agents"] == []


@pytest.mark.asyncio
async def test_api_startup_stops_reporting_a_durably_stuck_execution_as_running(tmp_path: Path) -> None:
    """A process can die between graph nodes with no agent mid-flight at all — the dashboard's
    "em andamento" summary counter and the Executions list read the durable status directly,
    so a stuck "running" execution must self-heal on restart just like an orphaned agent."""
    settings = settings_for(tmp_path)
    now = utc_now()
    stuck_state = {
        "execution_id": "exec-stuck", "project_id": "bezalel", "feature_request": "add a button",
        "status": "running", "plan": [], "errors": [], "started_at": now, "updated_at": now,
    }
    SQLiteCheckpointer(settings.checkpoint_path).save("exec-stuck", stuck_state, "dispatch_agents")
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = stuck_state
        await saver.aput(
            {"configurable": {"thread_id": "exec-stuck", "checkpoint_ns": ""}}, checkpoint,
            {"source": "input", "step": -1, "writes": {}, "parents": {}}, {},
        )

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/dashboard-data")

    data = response.json()
    assert data["executions"]["running"] == 0
    item = next(i for i in data["items"] if i["execution_id"] == "exec-stuck")
    assert item["status"] == "failed"
