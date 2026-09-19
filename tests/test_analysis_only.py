from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from api.app import create_app
from orchestrator import nodes
from orchestrator.config import Settings
from orchestrator.main import parser
from orchestrator.nodes import ExecutionRuntime
from orchestrator.mcp_bridge import run_feature
from schemas.models import AgentResult, ExecutionRequest, initial_state


def test_initial_state_carries_analysis_only_approval() -> None:
    request = ExecutionRequest(feature_request="analisa a segurança do frontend", analysis_only=True)
    state = initial_state(request, "exec-1")

    assert state["approvals"] == {"dry_run": False, "analysis_only": True}


def test_initial_state_defaults_analysis_only_to_false() -> None:
    request = ExecutionRequest(feature_request="implementa um botão")
    state = initial_state(request, "exec-2")

    assert state["approvals"]["analysis_only"] is False


def test_run_cli_accepts_analysis_only() -> None:
    args = parser().parse_args(["run", "--feature", "review the frontend", "--analysis-only"])

    assert args.analysis_only is True


def test_module_cli_help_works_from_repository_root() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-m", "orchestrator", "run", "--help"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--analysis-only" in result.stdout


class FakeRuntime:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(auto_commit=True, auto_merge=True, auto_deploy=True)

    async def persist(self, state, node, event=None, payload=None):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")


@pytest.mark.asyncio
async def test_commit_changes_never_touches_git_in_analysis_only_mode(monkeypatch) -> None:
    class ExplodingGitManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not instantiate GitManager in analysis_only mode")

    monkeypatch.setattr(nodes, "GitManager", ExplodingGitManager)
    state = {
        "plan": [], "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
        "approvals": {"analysis_only": True}, "errors": [], "feature_request": "analyze",
        "worktrees": {"frontend": {"path": "x", "branch": "b"}},
        "detected_projects": [{"project_id": "frontend", "path": "x"}],
    }

    result = await nodes.commit_changes(FakeRuntime(), state)

    assert result["commits"] == []
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_merge_changes_never_touches_git_in_analysis_only_mode(monkeypatch) -> None:
    class ExplodingGitManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not instantiate GitManager in analysis_only mode")

    monkeypatch.setattr(nodes, "GitManager", ExplodingGitManager)
    state = {
        "plan": [], "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
        "approvals": {"analysis_only": True}, "errors": [], "feature_request": "analyze",
        "commits": [{"project_id": "frontend", "branch": "b", "sha": "abc"}],
        "worktrees": {"frontend": {"path": "x", "branch": "b", "base": "main"}},
        "detected_projects": [{"project_id": "frontend", "path": "x"}],
    }

    result = await nodes.merge_changes(FakeRuntime(), state)

    assert result["merges"] == []
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_deploy_is_skipped_with_analysis_only_reason() -> None:
    state = {
        "plan": [], "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
        "approvals": {"analysis_only": True}, "errors": [], "feature_request": "analyze",
        "detected_projects": [{"project_id": "frontend", "path": "x"}],
    }

    result = await nodes.deploy(FakeRuntime(), state)

    assert result["deploy_results"][0]["status"] == "skipped"
    assert "analysis_only" in result["deploy_results"][0]["reason"]


@pytest.mark.asyncio
async def test_run_task_adds_read_only_instruction_in_analysis_only_mode(tmp_path: Path) -> None:
    class CapturingCodex:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def execute(self, prompt, workdir, role, **kwargs):
            self.prompts.append(prompt)
            return AgentResult(agent="frontend", status="completed", summary="found issues")

    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    codex = CapturingCodex()
    runtime = ExecutionRuntime(settings, codex=codex)
    state = {"execution_id": "exec-3", "feature_request": "analisa a segurança do frontend", "approvals": {"analysis_only": True}}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "analyze security", "acceptance_criteria": []}

    await runtime.run_task(state, task)

    assert codex.prompts, "codex.execute was never called"
    assert "ANALYSIS-ONLY" in codex.prompts[0]


@pytest.mark.asyncio
async def test_run_task_omits_read_only_instruction_outside_analysis_only_mode(tmp_path: Path) -> None:
    class CapturingCodex:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def execute(self, prompt, workdir, role, **kwargs):
            self.prompts.append(prompt)
            return AgentResult(agent="frontend", status="completed", summary="done")

    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    codex = CapturingCodex()
    runtime = ExecutionRuntime(settings, codex=codex)
    state = {"execution_id": "exec-4", "feature_request": "implementa um botão", "approvals": {}}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "build it", "acceptance_criteria": []}

    await runtime.run_task(state, task)

    assert "ANALYSIS-ONLY" not in codex.prompts[0]


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


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
async def test_mcp_bridge_forwards_analysis_only_flag(tmp_path: Path) -> None:
    async with running_api(settings_for(tmp_path)) as base_url:
        dispatched = await run_feature("analisa a segurança do frontend", analysis_only=True, base_url=base_url)
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            response = await client.get(f"/executions/{dispatched['execution_id']}")

    assert response.json()["approvals"]["analysis_only"] is True
