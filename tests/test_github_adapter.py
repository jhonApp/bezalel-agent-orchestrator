from __future__ import annotations

import sys
from pathlib import Path

import pytest

from adapters.github import create_pull_request, push_branch


FAKE_GIT_PUSH_OK = r'''
import sys
assert sys.argv[1:] == ["push", "-u", "origin", "codex/exec-1/frontend"]
sys.exit(0)
'''

FAKE_GIT_PUSH_FAILS = r'''
import sys
sys.stderr.write("remote: Permission denied\n")
sys.exit(1)
'''

FAKE_GH_PR_CREATE_OK = r'''
import pathlib
import sys
pathlib.Path("received-argv.json").write_text(repr(sys.argv[1:]), encoding="utf-8")
print("Creating pull request for codex/exec-1/frontend into main in jhonApp/bezalel-app")
print("https://github.com/jhonApp/bezalel-app/pull/42")
sys.exit(0)
'''

FAKE_GH_PR_CREATE_FAILS = r'''
import sys
sys.stderr.write("pull request create failed: GraphQL: A pull request already exists\n")
sys.exit(1)
'''


def fake_command(tmp_path: Path, source: str, name: str) -> list[str]:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_push_branch_succeeds(tmp_path: Path) -> None:
    result = await push_branch(tmp_path, "codex/exec-1/frontend", command=fake_command(tmp_path, FAKE_GIT_PUSH_OK, "fake_git.py"))

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_push_branch_reports_failure(tmp_path: Path) -> None:
    result = await push_branch(tmp_path, "codex/exec-1/frontend", command=fake_command(tmp_path, FAKE_GIT_PUSH_FAILS, "fake_git.py"))

    assert result["ok"] is False
    assert "Permission denied" in result["error"]


@pytest.mark.asyncio
async def test_create_pull_request_extracts_the_url_and_builds_expected_arguments(tmp_path: Path) -> None:
    result = await create_pull_request(
        tmp_path, base="main", head="codex/exec-1/frontend", title="feat: add a button", body="Automated PR",
        command=fake_command(tmp_path, FAKE_GH_PR_CREATE_OK, "fake_gh.py"),
    )

    received = (tmp_path / "received-argv.json").read_text(encoding="utf-8")
    assert received == (
        "['pr', 'create', '--base', 'main', '--head', 'codex/exec-1/frontend', "
        "'--title', 'feat: add a button', '--body', 'Automated PR']"
    )
    assert result == {"ok": True, "url": "https://github.com/jhonApp/bezalel-app/pull/42"}


@pytest.mark.asyncio
async def test_create_pull_request_reports_failure(tmp_path: Path) -> None:
    result = await create_pull_request(
        tmp_path, base="main", head="codex/exec-1/frontend", title="t", body="b",
        command=fake_command(tmp_path, FAKE_GH_PR_CREATE_FAILS, "fake_gh.py"),
    )

    assert result["ok"] is False
    assert "already exists" in result["error"]
