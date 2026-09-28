# tests/test_claude_code_cli.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from adapters.claude_code_cli import ClaudeCodeCLI, resolve_claude_code_command
from orchestrator.config import Settings


def settings_for(tmp_path: Path, command: str) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        claude_code_command=command,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def write_script(tmp_path: Path, name: str, source: str) -> str:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_resolve_claude_code_command_splits_a_quoted_path_with_spaces():
    command = resolve_claude_code_command('"C:\\Program Files\\Python\\python.exe" "C:\\my scripts\\claude.py"')

    assert command == ["C:\\Program Files\\Python\\python.exe", "C:\\my scripts\\claude.py"]


def test_resolve_claude_code_command_defaults_to_bare_claude():
    resolved = resolve_claude_code_command("claude")

    assert Path(resolved[-1]).name.lower() in {"claude", "claude.cmd", "claude.exe"}


FAKE_CLAUDE_SUCCESS = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "{\"status\": \"completed\", \"summary\": \"ok\"}",
    "total_cost_usd": 0.0537833,
    "usage": {"input_tokens": 4, "output_tokens": 145},
    "structured_output": {
        "status": "completed", "summary": "ok", "files_changed": [], "tests": [],
        "contracts_changed": [], "errors": [], "next_action": None,
        "tokens_input": 0, "tokens_output": 0, "quality": None,
    },
}))
sys.exit(0)
'''

FAKE_CLAUDE_404_ERROR = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 404,
    "result": "There's an issue with the selected model. It may not exist or you may not have access to it.",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
}))
sys.exit(1)
'''

FAKE_CLAUDE_429_ERROR = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 429,
    "result": "Rate limit exceeded. Please try again later.",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
}))
sys.exit(1)
'''

FAKE_CLAUDE_MALFORMED = r'''
import sys
print("not valid json at all")
sys.exit(1)
'''

FAKE_CLAUDE_EMPTY = r'''
import sys
sys.exit(1)
'''

FAKE_CLAUDE_HANGS = r'''
import time
time.sleep(30)
'''


@pytest.mark.asyncio
async def test_execute_populates_agent_result_from_the_real_usage_not_the_models_self_report(tmp_path: Path):
    """AGENT_SCHEMA requires the model to self-report tokens_input/tokens_output inside
    structured_output too - this proves the real usage-derived values win, exactly like
    the equivalent Codex fix earlier this session."""
    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "completed"
    assert result.summary == "ok"
    assert result.tokens_input == 4
    assert result.tokens_output == 145
    assert result.estimated_cost == pytest.approx(0.0537833)


@pytest.mark.asyncio
async def test_execute_maps_a_404_style_error_to_failed(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_404.py", FAKE_CLAUDE_404_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert "issue with the selected model" in result.summary
    assert result.errors == [result.summary]


@pytest.mark.asyncio
async def test_execute_maps_a_429_style_error_to_rate_limited(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_429.py", FAKE_CLAUDE_429_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "rate_limited"
    assert "Rate limit exceeded" in result.summary


@pytest.mark.asyncio
async def test_execute_fails_cleanly_on_malformed_output(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_malformed.py", FAKE_CLAUDE_MALFORMED)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert result.errors == ["structured output could not be parsed"]


@pytest.mark.asyncio
async def test_execute_fails_cleanly_on_empty_stdout(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_empty.py", FAKE_CLAUDE_EMPTY)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert result.errors == ["structured output could not be parsed"]


@pytest.mark.asyncio
async def test_execute_kills_the_process_and_returns_failed_on_timeout(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend", timeout=1)

    assert result.status == "failed"
    assert result.errors == ["agent timeout"]


@pytest.mark.asyncio
async def test_execute_kills_the_process_and_returns_blocked_on_cancellation(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)
    cancel_event = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.3)
        cancel_event.set()

    result, _ = await asyncio.gather(
        ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend", timeout=30, cancel_event=cancel_event),
        cancel_soon(),
    )

    assert result.status == "blocked"
    assert result.errors == ["execution cancelled"]


@pytest.mark.asyncio
async def test_execute_json_returns_structured_output_on_success(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result == {
        "status": "completed", "summary": "ok", "files_changed": [], "tests": [],
        "contracts_changed": [], "errors": [], "next_action": None,
        "tokens_input": 0, "tokens_output": 0, "quality": None,
    }


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_error(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_404.py", FAKE_CLAUDE_404_ERROR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result is None


@pytest.mark.asyncio
async def test_execute_json_returns_none_on_timeout(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_hangs.py", FAKE_CLAUDE_HANGS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier", timeout=1)

    assert result is None


@pytest.mark.asyncio
async def test_execute_returns_failed_when_the_claude_binary_does_not_exist(tmp_path: Path):
    settings = settings_for(tmp_path, "C:/definitely/not/a/real/path/claude-nonexistent.exe")

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert "unavailable" in result.summary.lower()


@pytest.mark.asyncio
async def test_execute_json_returns_none_when_the_claude_binary_does_not_exist(tmp_path: Path):
    settings = settings_for(tmp_path, "C:/definitely/not/a/real/path/claude-nonexistent.exe")

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result is None


def test_build_cli_returns_a_claude_code_cli_for_the_claude_code_name(tmp_path: Path):
    from adapters.agent_cli import KNOWN_CLI_NAMES, build_cli

    settings = settings_for(tmp_path, "claude")

    adapter = build_cli("claude_code", settings)

    assert isinstance(adapter, ClaudeCodeCLI)
    assert "claude_code" in KNOWN_CLI_NAMES
    assert "codex" in KNOWN_CLI_NAMES


@pytest.mark.asyncio
async def test_run_task_dispatches_to_a_real_claude_code_adapter_when_a_role_is_configured_for_it(tmp_path: Path, monkeypatch):
    """Proves the Spec-1 dispatch path (ExecutionRuntime.run_task resolving
    AGENT_ROLES[role]["cli"] through self.clis) actually reaches a real ClaudeCodeCLI,
    not just that ClaudeCodeCLI works when called directly."""
    from agents.registry import AGENT_ROLES
    from orchestrator.nodes import ExecutionRuntime

    command = write_script(tmp_path, "fake_claude_success.py", FAKE_CLAUDE_SUCCESS)
    claude_settings = settings_for(tmp_path, command)
    codex_settings = settings_for(tmp_path, "codex")  # never invoked; satisfies validate_agent_roles for the other 7 roles
    clis = {"codex": ClaudeCodeCLI(codex_settings), "claude_code": ClaudeCodeCLI(claude_settings)}
    monkeypatch.setitem(AGENT_ROLES["frontend"], "cli", "claude_code")
    runtime = ExecutionRuntime(claude_settings, clis=clis)
    state = {"execution_id": "execution-1", "feature_request": "add a button", "worktrees": {}, "detected_projects": []}
    task = {"task_id": "T001", "agent": "frontend", "project_id": "frontend", "description": "do it", "acceptance_criteria": []}

    result = await runtime.run_task(state, task)

    assert result.status == "completed"
    assert result.cli_used == "claude_code"
    assert result.tokens_input == 4


# Final-review findings: real response shapes CodexCLI already degrades gracefully but
# ClaudeCodeCLI's _parse used to raise an uncaught ValidationError/AttributeError on.

FAKE_CLAUDE_SUCCESS_NO_STRUCTURED_OUTPUT = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "plain text answer, no structured_output field at all",
    "total_cost_usd": 0.01,
    "usage": {"input_tokens": 2, "output_tokens": 3},
}))
sys.exit(0)
'''

FAKE_CLAUDE_NULL_STRUCTURED_OUTPUT = r'''
import json
import sys
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "null structured_output",
    "total_cost_usd": 0.01,
    "usage": {"input_tokens": 2, "output_tokens": 3},
    "structured_output": None,
}))
sys.exit(0)
'''

FAKE_CLAUDE_LIST_RESPONSE = r'''
import json
import sys
print(json.dumps([{"unexpected": "top-level list, not an object"}]))
sys.exit(0)
'''

FAKE_CLAUDE_ERROR_WITH_STDERR = r'''
import json
import sys
print("diagnostic detail on stderr", file=sys.stderr)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 500,
    "result": "internal error",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
}))
sys.exit(1)
'''


@pytest.mark.asyncio
async def test_execute_degrades_gracefully_when_a_success_response_has_no_structured_output(tmp_path: Path):
    """A real shape this CLI emits (design spec finding #1: a success response with only
    `result`, no `structured_output`) must not raise — AgentResult.status has no default,
    so building it from an empty structured dict used to raise an uncaught ValidationError."""
    command = write_script(tmp_path, "fake_claude_no_structured.py", FAKE_CLAUDE_SUCCESS_NO_STRUCTURED_OUTPUT)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_execute_degrades_gracefully_when_structured_output_is_explicitly_null(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_null_structured.py", FAKE_CLAUDE_NULL_STRUCTURED_OUTPUT)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_execute_degrades_gracefully_when_the_top_level_response_is_a_list(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_list.py", FAKE_CLAUDE_LIST_RESPONSE)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert result.errors == ["structured output could not be parsed"]


@pytest.mark.asyncio
async def test_execute_json_returns_none_when_the_top_level_response_is_a_list(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_list.py", FAKE_CLAUDE_LIST_RESPONSE)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute_json("classify this", tmp_path, {"type": "object"}, "Classifier")

    assert result is None


@pytest.mark.asyncio
async def test_execute_captures_stderr_and_raw_response_on_failure(tmp_path: Path):
    """This adapter's first real use is the middle of an outage on the primary CLI — the
    worst possible moment to have zero diagnostics. A failure must carry stderr and a raw
    response tail, not just a generic message."""
    command = write_script(tmp_path, "fake_claude_stderr.py", FAKE_CLAUDE_ERROR_WITH_STDERR)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "failed"
    assert result.raw_response != ""
    assert any("diagnostic detail on stderr" in err for err in result.errors)


@pytest.mark.asyncio
async def test_execute_sets_raw_response_on_success_too(tmp_path: Path):
    command = write_script(tmp_path, "fake_claude_success2.py", FAKE_CLAUDE_SUCCESS)
    settings = settings_for(tmp_path, command)

    result = await ClaudeCodeCLI(settings).execute("do it", tmp_path, "frontend")

    assert result.status == "completed"
    assert "structured_output" in result.raw_response or "completed" in result.raw_response
