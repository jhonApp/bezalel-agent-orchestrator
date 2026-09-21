from pathlib import Path
import os

from adapters.codex_cli import AGENT_SCHEMA, CodexCLI, resolve_codex_command
from orchestrator.config import Settings


def test_codex_windows_resolution(tmp_path: Path):
    settings = Settings(orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path, backend_path=tmp_path, python_path=tmp_path, codex_command="codex")
    resolved = resolve_codex_command(settings.codex_command)
    assert Path(resolved[-1]).name.lower() in {"codex", "codex.cmd", "codex.exe"}


def test_exec_command_uses_config_override_for_approval_policy(tmp_path: Path):
    settings = Settings(
        orchestrator_root=tmp_path,
        workspace_root=tmp_path,
        frontend_path=tmp_path,
        backend_path=tmp_path,
        python_path=tmp_path,
        codex_command="codex",
        codex_approval_policy="never",
    )
    command = CodexCLI(settings)._exec_command(
        tmp_path, tmp_path / "schema.json", tmp_path / "output.txt"
    )

    assert "--ask-for-approval" not in command
    assert "--ignore-user-config" in command
    assert "approval_policy=\"never\"" in command
    assert "features.plugin_hooks=true" in command
    assert 'plugins."tracing@langsmith-codex-plugins".enabled=true' in command
    assert command[-1] == "-"


def test_agent_schema_has_an_optional_quality_object_for_the_reviewer():
    quality = AGENT_SCHEMA["properties"]["quality"]
    assert AGENT_SCHEMA["additionalProperties"] is False
    assert "quality" not in AGENT_SCHEMA["required"]
    assert quality["additionalProperties"] is False
    assert set(quality["required"]) == {"relevante", "fonte_utilizada", "alucinacao", "cumprimento_regras"}


def test_agent_schema_is_strict_for_structured_outputs():
    assert AGENT_SCHEMA["additionalProperties"] is False
    # "quality" is the one deliberately optional property — only the reviewer role
    # populates it (see prompts/reviewer.md); every other property stays required.
    assert set(AGENT_SCHEMA["required"]) == set(AGENT_SCHEMA["properties"]) - {"quality"}
    assert AGENT_SCHEMA["properties"]["tests"]["items"]["additionalProperties"] is False
    assert AGENT_SCHEMA["properties"]["contracts_changed"]["items"]["additionalProperties"] is False


USAGE_LIMIT_RAW = (
    '{"type":"thread.started","thread_id":"01a0babc-af46-7192-870f-7f0e01cf2cac"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro '
    '(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage '
    'to purchase more credits or try again at 5:57 PM."}\n'
    '{"type":"turn.failed","error":{"message":"You’ve hit your usage limit. Upgrade to Pro '
    '(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage '
    'to purchase more credits or try again at 5:57 PM."}}\n'
)

GENERIC_TURN_FAILURE_RAW = (
    '{"type":"thread.started","thread_id":"x"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"turn.failed","error":{"message":"network error contacting model provider"}}\n'
)


def test_parse_response_flags_a_usage_limit_turn_failure_as_rate_limited():
    result = CodexCLI._parse_response(USAGE_LIMIT_RAW, USAGE_LIMIT_RAW)

    assert result.status == "rate_limited"
    assert "usage limit" in result.summary.lower()
    assert result.errors and "usage limit" in result.errors[0].lower()


def test_parse_response_surfaces_the_real_reason_for_a_non_usage_turn_failure():
    result = CodexCLI._parse_response(GENERIC_TURN_FAILURE_RAW, GENERIC_TURN_FAILURE_RAW)

    assert result.status == "failed"
    assert "network error contacting model provider" in result.summary


def test_parse_response_falls_back_to_the_generic_message_with_no_turn_failure_event():
    result = CodexCLI._parse_response("not json at all", "not json at all")

    assert result.status == "failed"
    assert result.summary == "Codex returned no valid structured result"
