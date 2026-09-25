# tests/test_agent_cli_fallback.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from adapters.codex_cli import CodexCLI
from agents.registry import AGENT_ROLES
from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime


RATE_LIMIT_SCRIPT = r'''
import json
import sys
print(json.dumps({"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Upgrade to Pro or try again later."}}), flush=True)
sys.exit(1)
'''

COMPLETED_SCRIPT = r'''
import json
import sys
output = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(output, "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "status": "completed", "summary": "handled by fallback",
        "files_changed": [], "tests": [], "contracts_changed": [],
        "errors": [], "next_action": None, "tokens_input": 5, "tokens_output": 3,
    }))
sys.exit(0)
'''

FAILED_SCRIPT = r'''
import json
import sys
output = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(output, "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "status": "failed", "summary": "boom",
        "files_changed": [], "tests": [], "contracts_changed": [],
        "errors": ["boom"], "next_action": None, "tokens_input": 1, "tokens_output": 1,
    }))
sys.exit(1)
'''


def settings_with_command(tmp_path: Path, script_name: str, script_source: str) -> Settings:
    script = tmp_path / script_name
    script.write_text(script_source, encoding="utf-8")
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{script}"',
        checkpoint_sqlite_path=tmp_path / f"checkpoints-{script_name}.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / f"langgraph-{script_name}.sqlite3",
        max_retries=0, retry_backoff_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_run_task_falls_back_once_when_the_primary_hits_its_rate_limit(tmp_path: Path, monkeypatch):
    primary_settings = settings_with_command(tmp_path, "primary.py", RATE_LIMIT_SCRIPT)
    secondary_settings = settings_with_command(tmp_path, "secondary.py", COMPLETED_SCRIPT)
    # "codex" here only satisfies validate_agent_roles for the other 7 roles (all still
    # default to cli="codex") — "frontend" is patched below to "primary", so this entry is
    # never looked up or dispatched to in this test.
    clis = {"primary": CodexCLI(primary_settings), "secondary": CodexCLI(secondary_settings), "codex": CodexCLI(primary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", "secondary")
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.summary == "handled by fallback"
    assert result.cli_used == "secondary"
    # The rejected primary attempt's tokens must not leak into the task's outcome — only
    # the fallback's own usage (from COMPLETED_SCRIPT's tokens_input=5) counts.
    assert result.tokens_input == 5
    assert result.tokens_output == 3


@pytest.mark.asyncio
async def test_run_task_returns_the_rate_limited_result_unchanged_with_no_fallback_configured(tmp_path: Path, monkeypatch):
    primary_settings = settings_with_command(tmp_path, "primary.py", RATE_LIMIT_SCRIPT)
    # "codex" only satisfies validate_agent_roles for the other 7 roles; never dispatched to.
    clis = {"primary": CodexCLI(primary_settings), "codex": CodexCLI(primary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", None)
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "rate_limited"
    assert result.cli_used == "primary"


@pytest.mark.asyncio
async def test_run_task_does_not_fall_back_on_a_genuine_failure(tmp_path: Path, monkeypatch):
    """The fallback exists for usage limits specifically, not for every failure — a role
    that fails for an ordinary reason should not silently retry on a different CLI."""
    primary_settings = settings_with_command(tmp_path, "primary.py", FAILED_SCRIPT)
    secondary_settings = settings_with_command(tmp_path, "secondary.py", COMPLETED_SCRIPT)
    # "codex" only satisfies validate_agent_roles for the other 7 roles; never dispatched to.
    clis = {"primary": CodexCLI(primary_settings), "secondary": CodexCLI(secondary_settings), "codex": CodexCLI(primary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", "secondary")
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "failed"
    assert result.cli_used == "primary"


@pytest.mark.asyncio
async def test_run_task_returns_the_rate_limited_result_when_the_fallback_cli_is_unregistered(tmp_path: Path, monkeypatch):
    """A role may declare a fallback_cli that doesn't exist yet in self.clis (e.g. a typo, or
    naming a real adapter before it's built) — that must resolve to "no fallback available"
    lazily, at dispatch time, not raise anywhere."""
    primary_settings = settings_with_command(tmp_path, "primary.py", RATE_LIMIT_SCRIPT)
    clis = {"primary": CodexCLI(primary_settings), "codex": CodexCLI(primary_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "primary")
    monkeypatch.setitem(AGENT_ROLES["frontend"], "fallback_cli", "ghost")
    runtime = ExecutionRuntime(primary_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "rate_limited"
    assert result.cli_used == "primary"


@pytest.mark.asyncio
async def test_execution_runtime_raises_at_construction_for_an_unavailable_primary_cli(tmp_path: Path, monkeypatch):
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "totally_not_registered")

    with pytest.raises(ValueError, match="frontend.*totally_not_registered"):
        ExecutionRuntime(settings)
