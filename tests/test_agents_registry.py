from __future__ import annotations

from agents.registry import AGENT_ROLES


def test_every_agent_role_declares_a_non_empty_prompt_version():
    for role, config in AGENT_ROLES.items():
        version = config.get("prompt_version")
        assert isinstance(version, str) and version, f"{role} is missing prompt_version"
