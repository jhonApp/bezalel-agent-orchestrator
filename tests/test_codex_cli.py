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


def test_agent_schema_is_strict_for_structured_outputs():
    assert AGENT_SCHEMA["additionalProperties"] is False
    assert set(AGENT_SCHEMA["required"]) == set(AGENT_SCHEMA["properties"])
    assert AGENT_SCHEMA["properties"]["tests"]["items"]["additionalProperties"] is False
    assert AGENT_SCHEMA["properties"]["contracts_changed"]["items"]["additionalProperties"] is False
