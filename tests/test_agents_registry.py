from __future__ import annotations

from pathlib import Path

from agents.registry import AGENT_ROLES, role_prompt


def test_every_agent_role_declares_a_non_empty_prompt_version():
    for role, config in AGENT_ROLES.items():
        version = config.get("prompt_version")
        assert isinstance(version, str) and version, f"{role} is missing prompt_version"


def test_classifier_role_exists_and_points_at_its_prompt_file():
    assert AGENT_ROLES["classifier"]["prompt"] == "classifier.md"


def test_classifier_prompt_file_loads_and_mentions_all_three_domains():
    prompts_root = Path(__file__).resolve().parents[1] / "prompts"
    text = role_prompt("classifier", prompts_root)
    assert "frontend" in text
    assert "backend" in text
    assert "python" in text
