from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn

from api.app import create_app
from orchestrator.config import Settings
from orchestrator.mcp_bridge import check_execution, resume_execution, run_feature


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


@asynccontextmanager
async def running_api(tmp_path: Path) -> AsyncIterator[str]:
    """Boot the real FastAPI app on an OS-assigned port for a genuine HTTP round trip."""
    app = create_app(settings_for(tmp_path))
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


async def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_run_feature_dispatches_a_dry_run_execution(tmp_path: Path) -> None:
    async with running_api(tmp_path) as base_url:
        result = await run_feature("teste da ponte mcp", dry_run=True, base_url=base_url)

    assert "execution_id" in result
    assert result["status"] == "started"
    assert result["panel_url"] == base_url + "/"
    assert result["events_url"] == base_url + "/events/view"


@pytest.mark.asyncio
async def test_run_feature_reports_unreachable_api_clearly() -> None:
    port = await _free_port()
    result = await run_feature("teste", base_url=f"http://127.0.0.1:{port}")

    assert "error" in result
    assert "unreachable" in result["error"]
    assert f"127.0.0.1:{port}" in result["error"]


@pytest.mark.asyncio
async def test_check_execution_returns_the_dispatched_status(tmp_path: Path) -> None:
    async with running_api(tmp_path) as base_url:
        dispatched = await run_feature("teste de consulta", dry_run=True, base_url=base_url)
        await asyncio.sleep(0.2)
        status = await check_execution(dispatched["execution_id"], base_url=base_url)

    assert status["execution_id"] == dispatched["execution_id"]
    assert "status" in status


@pytest.mark.asyncio
async def test_check_execution_reports_unknown_id_as_error(tmp_path: Path) -> None:
    async with running_api(tmp_path) as base_url:
        status = await check_execution("does-not-exist", base_url=base_url)

    assert "error" in status
    assert "not found" in status["error"]


@pytest.mark.asyncio
async def test_resume_execution_resumes_a_previously_dispatched_execution(tmp_path: Path) -> None:
    async with running_api(tmp_path) as base_url:
        dispatched = await run_feature("teste de resume", dry_run=True, base_url=base_url)
        await asyncio.sleep(0.2)
        result = await resume_execution(dispatched["execution_id"], base_url=base_url)

    assert result["execution_id"] == dispatched["execution_id"]
    assert result["status"] == "resuming"


@pytest.mark.asyncio
async def test_resume_execution_reports_unknown_id_as_error(tmp_path: Path) -> None:
    async with running_api(tmp_path) as base_url:
        result = await resume_execution("does-not-exist", base_url=base_url)

    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_resume_execution_reports_unreachable_api_clearly() -> None:
    port = await _free_port()
    result = await resume_execution("some-id", base_url=f"http://127.0.0.1:{port}")

    assert "error" in result
    assert "unreachable" in result["error"]
