from __future__ import annotations

from pathlib import Path

import pytest

from adapters.agent_cli import KNOWN_CLI_NAMES, build_cli, validate_agent_roles
from adapters.codex_cli import CodexCLI
from orchestrator.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_known_cli_names_contains_codex():
    assert "codex" in KNOWN_CLI_NAMES


def test_build_cli_returns_a_codex_cli_for_the_codex_name(tmp_path: Path):
    adapter = build_cli("codex", settings_for(tmp_path))

    assert isinstance(adapter, CodexCLI)


def test_build_cli_raises_for_an_unknown_name(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown agent CLI 'bogus'"):
        build_cli("bogus", settings_for(tmp_path))


def test_validate_agent_roles_passes_when_every_cli_is_available():
    roles = {"frontend": {"cli": "codex", "fallback_cli": None}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise


def test_validate_agent_roles_raises_for_an_unavailable_primary_cli():
    roles = {"frontend": {"cli": "bogus", "fallback_cli": None}}

    with pytest.raises(ValueError, match="frontend.*bogus"):
        validate_agent_roles(roles, available_clis={"codex"})


def test_validate_agent_roles_defaults_a_missing_cli_key_to_codex():
    roles = {"frontend": {}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise


def test_validate_agent_roles_never_checks_fallback_cli():
    """A role may declare a fallback_cli that doesn't exist yet (e.g. before
    a second real adapter is built) — that must not fail construction."""
    roles = {"frontend": {"cli": "codex", "fallback_cli": "not_built_yet"}}

    validate_agent_roles(roles, available_clis={"codex"})  # must not raise


def test_the_real_agent_roles_validate_cleanly_against_codex_only():
    from agents.registry import AGENT_ROLES

    validate_agent_roles(AGENT_ROLES, available_clis={"codex"})  # must not raise


def test_every_real_agent_role_defaults_to_codex_with_no_fallback():
    from agents.registry import AGENT_ROLES

    for role, config in AGENT_ROLES.items():
        assert config.get("cli") == "codex", f"role '{role}' should default to cli='codex'"
        assert config.get("fallback_cli") is None, f"role '{role}' should default to no fallback_cli"
