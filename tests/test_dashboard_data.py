from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
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


async def _seed_completed_execution(settings: Settings, execution_id: str) -> dict:
    """Write a durable state as if `generate_final_report` had already run for it."""
    now = utc_now()
    state = {
        "execution_id": execution_id, "project_id": "bezalel", "feature_request": "add a button",
        "status": "completed", "started_at": now, "updated_at": now, "context_health": {},
        "plan": [
            {"task_id": "T001", "agent": "frontend", "status": "completed", "result": {
                "agent": "frontend", "status": "completed", "summary": "Added the Button component",
                "files_changed": ["src/Button.tsx"], "errors": [], "tests": [], "contracts_changed": [],
                "next_action": None, "tokens_input": 10, "tokens_output": 5, "duration_seconds": 1.2,
                "estimated_cost": 0.0001, "raw_response": "x" * 5000,
            }},
            {"task_id": "T012", "agent": "security", "status": "blocked", "result": {
                "agent": "security", "status": "blocked", "summary": "Found a hard-coded secret",
                "files_changed": [], "errors": ["AWS access key identifier"], "tests": [], "contracts_changed": [],
                "next_action": None, "tokens_input": 3, "tokens_output": 2, "duration_seconds": 0.4,
                "estimated_cost": 0.0, "raw_response": "y" * 5000,
            }},
        ],
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


async def _seed_failed_execution(settings: Settings, execution_id: str, security_findings=None, contracts=None) -> dict:
    """Write a durable state as if a real gate had blocked commit/merge, matching production."""
    now = utc_now()
    state = {
        "execution_id": execution_id, "project_id": "bezalel", "feature_request": "remove the retry button",
        "status": "failed", "started_at": now, "updated_at": now, "context_health": {}, "plan": [],
        "errors": ["commit blocked: mandatory tests failed", "merge blocked: mandatory tests failed"],
        "security_findings": security_findings or [],
        "contracts": contracts or [],
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


@asynccontextmanager
async def running_api(settings: Settings) -> AsyncIterator[str]:
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_dashboard_data_includes_trimmed_per_agent_task_summaries(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await _seed_completed_execution(settings, "exec-completed-1")

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/dashboard-data")

    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["execution_id"] == "exec-completed-1")
    tasks = {t["task_id"]: t for t in item["tasks"]}

    assert tasks["T001"] == {
        "task_id": "T001", "agent": "frontend", "status": "completed",
        "summary": "Added the Button component", "files_changed": ["src/Button.tsx"], "errors": [],
    }
    assert tasks["T012"]["status"] == "blocked"
    assert tasks["T012"]["errors"] == ["AWS access key identifier"]
    # The large raw Codex transcript must not ride along on a 2-second dashboard poll.
    assert "raw_response" not in tasks["T001"]


@pytest.mark.asyncio
async def test_dashboard_data_exposes_the_real_gate_block_reasons_for_a_failed_execution(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await _seed_failed_execution(settings, "exec-failed-1")

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/dashboard-data")

    item = next(i for i in response.json()["items"] if i["execution_id"] == "exec-failed-1")
    assert item["status"] == "failed"
    assert item["errors"] == ["commit blocked: mandatory tests failed", "merge blocked: mandatory tests failed"]


@pytest.mark.asyncio
async def test_dashboard_data_exposes_only_blocking_security_and_contract_findings(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await _seed_failed_execution(
        settings, "exec-incident-1",
        security_findings=[
            {"severity": "blocking", "path": "src/config.py", "message": "AWS access key identifier", "evidence": "redacted"},
            {"severity": "warning", "path": ".env", "message": ".env exists; verify it is ignored", "evidence": ""},
        ],
        contracts=[
            {"resource": "carousel-job-shape", "severity": "blocking", "message": "field removed without mirroring backend", "producer": "python_ai"},
            {"resource": "frontend-backend-contract", "severity": "warning", "message": "contract lacks expected auth/error conventions"},
        ],
    )

    async with running_api(settings) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get("/dashboard-data")

    item = next(i for i in response.json()["items"] if i["execution_id"] == "exec-incident-1")
    assert item["security_findings"] == [
        {"path": "src/config.py", "message": "AWS access key identifier"},
    ]
    assert item["contract_findings"] == [
        {"resource": "carousel-job-shape", "message": "field removed without mirroring backend"},
    ]


def test_persisted_agent_metrics_support_dashboard_data(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    store.agent_run("exec-1", "frontend", "T001", {
        "status": "completed", "tokens_input": 1000, "tokens_output": 100,
        "estimated_cost": 0.0, "duration_seconds": 12,
    }, "2026-01-01T00:00:00+00:00")
    metrics = store.agent_metrics()

    assert metrics[0]["agent"] == "frontend"
    assert metrics[0]["tokens_input"] == 1000
    assert metrics[0]["tokens_output"] == 100
    assert metrics[0]["completed"] == 1
