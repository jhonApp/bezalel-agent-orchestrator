"""Contract tests for running Codex as a managed subprocess."""

from pathlib import Path
import sys

import pytest

from adapters.codex_cli import CodexCLI
from orchestrator.config import Settings
from orchestrator.codex_launcher import run_codex_yolo
from persistence.checkpointer import SQLiteCheckpointer


FAKE_CODEX = r'''
import json
import pathlib
import sys
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
failed = "--fail" in sys.argv
output.write_text(json.dumps({
    "status": "failed" if failed else "completed",
    "summary": "fake codex run",
    "files_changed": [], "tests": [], "contracts_changed": [],
    "errors": ["fake process failure"] if failed else [],
    "next_action": None, "tokens_input": 3, "tokens_output": 2,
}), encoding="utf-8")
sys.exit(7 if failed else 0)
'''


def settings_for(tmp_path: Path, command: str) -> Settings:
    return Settings(
        orchestrator_root=tmp_path,
        workspace_root=tmp_path,
        frontend_path=tmp_path,
        backend_path=tmp_path,
        python_path=tmp_path,
        codex_command=command,
        agent_timeout_seconds=10,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


@pytest.fixture
def fake_codex(tmp_path: Path) -> str:
    path = tmp_path / "fake_codex.py"
    path.write_text(FAKE_CODEX, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


@pytest.mark.asyncio
async def test_managed_codex_subprocess_returns_structured_result(tmp_path: Path, fake_codex: str):
    result = await CodexCLI(settings_for(tmp_path, fake_codex)).execute(
        "exercise", tmp_path, "frontend"
    )
    assert result.status == "completed"
    assert result.summary == "fake codex run"
    assert result.tokens_input == 3
    assert result.tokens_output == 2


@pytest.mark.asyncio
async def test_managed_codex_subprocess_preserves_nonzero_failure(tmp_path: Path, fake_codex: str):
    result = await CodexCLI(settings_for(tmp_path, fake_codex + " --fail")).execute(
        "exercise", tmp_path, "frontend"
    )
    assert result.status == "failed"
    assert result.errors


def test_dashboard_lifecycle_is_durable_across_store_instances(tmp_path: Path):
    path = tmp_path / "events.sqlite3"
    first = SQLiteCheckpointer(path)
    first.event("execution-1", "agent.started", {"agent": "frontend", "task_id": "T001"}, "t1")
    second = SQLiteCheckpointer(path)
    assert second.active_agents() == [{
        "execution_id": "execution-1", "agent": "frontend",
        "task_id": "T001", "started_at": "t1",
    }]
    second.event("execution-1", "agent.finished", {"agent": "frontend", "task_id": "T001"}, "t2")
    assert second.active_agents() == []


@pytest.mark.asyncio
async def test_codex_yolo_launcher_persists_dashboard_lifecycle(tmp_path: Path):
    script = tmp_path / "fake_yolo.py"
    script.write_text("import sys; sys.stdin.read(); sys.exit(0)\n", encoding="utf-8")
    settings = settings_for(tmp_path, f'"{sys.executable}" "{script}"')

    result = await run_codex_yolo(settings, "managed task", cwd=tmp_path, timeout=10)

    assert result["status"] == "completed"
    store = SQLiteCheckpointer(settings.checkpoint_path)
    events = store.logs(result["execution_id"])
    assert [event["event_type"] for event in events] == ["agent.started", "agent.finished"]
    assert store.active_agents() == []
