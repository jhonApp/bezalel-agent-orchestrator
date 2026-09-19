from __future__ import annotations

import sys
from pathlib import Path

import pytest

from adapters.skills import install_skill, list_skills, remove_skill


FAKE_SKILLS_LIST = r'''
import json
import sys
if sys.argv[1:] == ["list", "--json"]:
    print(json.dumps([{"name": "frontend-design", "source": "anthropics/skills", "agents": ["Codex"]}]))
    sys.exit(0)
sys.exit(1)
'''

FAKE_SKILLS_LIST_FAILS = r'''
import sys
sys.stderr.write("network error reaching registry\n")
sys.exit(1)
'''

FAKE_SKILLS_ADD = r'''
import json
import pathlib
import sys
pathlib.Path("received-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(json.dumps({"installed": ["frontend-design"], "agent": "Codex"}))
sys.exit(0)
'''

FAKE_SKILLS_ADD_NON_JSON = r'''
print("Installed frontend-design for Codex (legacy CLI, no --json support)")
'''


def fake_command(tmp_path: Path, source: str) -> list[str]:
    script = tmp_path / "fake_skills.py"
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_list_skills_parses_json_output(tmp_path: Path) -> None:
    result = await list_skills(tmp_path, command=fake_command(tmp_path, FAKE_SKILLS_LIST))

    assert result["error"] is None
    assert result["skills"] == [{"name": "frontend-design", "source": "anthropics/skills", "agents": ["Codex"]}]


@pytest.mark.asyncio
async def test_list_skills_reports_error_when_command_fails(tmp_path: Path) -> None:
    result = await list_skills(tmp_path, command=fake_command(tmp_path, FAKE_SKILLS_LIST_FAILS))

    assert result["skills"] == []
    assert "network error reaching registry" in result["error"]


@pytest.mark.asyncio
async def test_install_skill_builds_expected_arguments_and_parses_result(tmp_path: Path) -> None:
    result = await install_skill(tmp_path, "anthropics/skills", skill="frontend-design",
                                 command=fake_command(tmp_path, FAKE_SKILLS_ADD))

    received = (tmp_path / "received-argv.json").read_text(encoding="utf-8")
    assert received == '["add", "anthropics/skills", "-y", "--json", "--skill", "frontend-design"]'
    assert result["ok"] is True
    assert result["result"] == {"installed": ["frontend-design"], "agent": "Codex"}


@pytest.mark.asyncio
async def test_install_skill_omits_skill_flag_when_not_given(tmp_path: Path) -> None:
    await install_skill(tmp_path, "anthropics/skills", command=fake_command(tmp_path, FAKE_SKILLS_ADD))

    received = (tmp_path / "received-argv.json").read_text(encoding="utf-8")
    assert received == '["add", "anthropics/skills", "-y", "--json"]'


@pytest.mark.asyncio
async def test_install_skill_handles_non_json_stdout_without_crashing(tmp_path: Path) -> None:
    result = await install_skill(tmp_path, "anthropics/skills", skill="frontend-design",
                                 command=fake_command(tmp_path, FAKE_SKILLS_ADD_NON_JSON))

    assert result["ok"] is True
    assert result["result"] is None


FAKE_SKILLS_REMOVE = r'''
import pathlib
import sys
pathlib.Path("received-argv.json").write_text(repr(sys.argv[1:]), encoding="utf-8")
print("Removed frontend-design")
sys.exit(0)
'''

FAKE_SKILLS_REMOVE_FAILS = r'''
import sys
sys.stderr.write("skill 'frontend-design' is not installed\n")
sys.exit(1)
'''


@pytest.mark.asyncio
async def test_remove_skill_builds_expected_arguments(tmp_path: Path) -> None:
    result = await remove_skill(tmp_path, "frontend-design", command=fake_command(tmp_path, FAKE_SKILLS_REMOVE))

    received = (tmp_path / "received-argv.json").read_text(encoding="utf-8")
    assert received == "['remove', '--skill', 'frontend-design', '-y']"
    assert result["ok"] is True
    assert "Removed frontend-design" in result["output"]


@pytest.mark.asyncio
async def test_remove_skill_reports_error_when_command_fails(tmp_path: Path) -> None:
    result = await remove_skill(tmp_path, "frontend-design", command=fake_command(tmp_path, FAKE_SKILLS_REMOVE_FAILS))

    assert result["ok"] is False
    assert "not installed" in result["error"]
