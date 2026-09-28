from __future__ import annotations

from pathlib import Path

import pytest

from agents.registry import AGENT_ROLES
from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime
from persistence.checkpointer import SQLiteCheckpointer


def test_get_role_cli_overrides_is_empty_before_any_write(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    assert store.get_role_cli_overrides() == {}


def test_set_then_get_round_trips_an_override(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")

    assert store.get_role_cli_overrides() == {"frontend": {"cli": "claude_code", "fallback_cli": "codex"}}


def test_set_role_cli_override_accepts_a_null_fallback(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)

    assert store.get_role_cli_overrides()["frontend"] == {"cli": "claude_code", "fallback_cli": None}


def test_setting_the_same_role_twice_overwrites_rather_than_duplicates(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")
    store.set_role_cli_override("frontend", "codex", None)

    overrides = store.get_role_cli_overrides()
    assert overrides == {"frontend": {"cli": "codex", "fallback_cli": None}}


def test_overrides_for_different_roles_do_not_interfere(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)
    store.set_role_cli_override("backend", "codex", "claude_code")

    overrides = store.get_role_cli_overrides()
    assert overrides == {
        "frontend": {"cli": "claude_code", "fallback_cli": None},
        "backend": {"cli": "codex", "fallback_cli": "claude_code"},
    }


def runtime_settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / f"checkpoints-{name}.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / f"langgraph-{name}.sqlite3",
    )


def test_effective_role_config_falls_back_to_the_code_default_when_no_override_exists(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "no-override"))

    config = runtime._effective_role_config("frontend")

    assert config["cli"] == AGENT_ROLES["frontend"]["cli"]
    assert config["fallback_cli"] == AGENT_ROLES["frontend"]["fallback_cli"]


def test_effective_role_config_uses_the_persisted_cli_override(tmp_path: Path) -> None:
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "cli-override"))
    runtime.store.set_role_cli_override("frontend", "claude_code", None)

    config = runtime._effective_role_config("frontend")

    assert config["cli"] == "claude_code"


def test_effective_role_config_distinguishes_no_override_from_an_explicit_null_fallback(tmp_path: Path) -> None:
    """A role that has never been overridden must keep its code-default fallback_cli;
    a role explicitly overridden to have NO fallback must show None, not the default."""
    runtime = ExecutionRuntime(runtime_settings(tmp_path, "null-fallback"))
    runtime.store.set_role_cli_override("frontend", "codex", "claude_code")
    runtime.store.set_role_cli_override("backend", "codex", None)

    assert runtime._effective_role_config("frontend")["fallback_cli"] == "claude_code"
    assert runtime._effective_role_config("backend")["fallback_cli"] is None


def test_execution_runtime_raises_at_construction_for_a_persisted_override_naming_an_unavailable_cli(tmp_path: Path) -> None:
    from persistence.checkpointer import SQLiteCheckpointer

    settings = runtime_settings(tmp_path, "stale-override")
    store = SQLiteCheckpointer(settings.checkpoint_path)
    store.set_role_cli_override("frontend", "totally_not_registered", None)

    with pytest.raises(ValueError, match="frontend.*totally_not_registered"):
        ExecutionRuntime(settings, store=store)


@pytest.mark.asyncio
async def test_run_task_dispatches_through_an_override_to_a_different_cli(tmp_path: Path) -> None:
    """End-to-end: an override changes which CLI a role actually uses, with no
    AGENT_ROLES edit and no monkeypatching of the registry."""
    import sys

    from adapters.codex_cli import CodexCLI

    codex_script = tmp_path / "fake_codex.py"
    codex_script.write_text(
        "import sys\nsys.exit('codex should never run once frontend is overridden to claude_code')\n",
        encoding="utf-8",
    )
    claude_script = tmp_path / "fake_claude.py"
    claude_script.write_text(
        "import json\nprint(json.dumps({"
        "'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok', "
        "'total_cost_usd': 0.01, 'usage': {'input_tokens': 4, 'output_tokens': 2}, "
        "'structured_output': {'status': 'completed', 'summary': 'handled via override', "
        "'files_changed': [], 'tests': [], 'contracts_changed': [], 'errors': [], "
        "'next_action': None, 'tokens_input': 0, 'tokens_output': 0, 'quality': None}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        codex_command=f'"{sys.executable}" "{codex_script}"',
        claude_code_command=f'"{sys.executable}" "{claude_script}"',
        checkpoint_sqlite_path=tmp_path / "checkpoints-e2e.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph-e2e.sqlite3",
    )
    from adapters.claude_code_cli import ClaudeCodeCLI
    clis = {"codex": CodexCLI(settings), "claude_code": ClaudeCodeCLI(settings)}
    runtime = ExecutionRuntime(settings, clis=clis)
    runtime.store.set_role_cli_override("frontend", "claude_code", None)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.summary == "handled via override"
    assert result.cli_used == "claude_code"
