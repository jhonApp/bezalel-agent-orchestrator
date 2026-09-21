from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import nodes


class FakeRuntime:
    def __init__(self, *, create_pull_request: bool = False, auto_merge: bool = True) -> None:
        self.settings = SimpleNamespace(auto_commit=True, auto_merge=auto_merge, auto_deploy=True,
                                        create_pull_request=create_pull_request)

    async def persist(self, state, node, event=None, payload=None):
        return state

    def workdir_for(self, state, project_id):
        return Path(".")


def base_state() -> dict:
    return {
        "plan": [], "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
        "approvals": {}, "errors": [], "feature_request": "add a button", "execution_id": "exec-pr-1",
        "commits": [{"project_id": "frontend", "branch": "codex/exec-pr-1/frontend", "sha": "abc"}],
        "worktrees": {"frontend": {"path": "x", "branch": "codex/exec-pr-1/frontend", "base": "main"}},
        "detected_projects": [{"project_id": "frontend", "path": "x"}],
    }


@pytest.mark.asyncio
async def test_merge_changes_opens_a_pr_instead_of_merging_when_enabled(monkeypatch) -> None:
    class ExplodingGitManager:
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not merge locally when create_pull_request is enabled")

    monkeypatch.setattr(nodes, "GitManager", ExplodingGitManager)

    push_calls: list[tuple[str, str]] = []

    async def fake_push_branch(cwd, branch, **kwargs):
        push_calls.append((str(cwd), branch))
        return {"ok": True}

    pr_calls: list[tuple[str, str, str]] = []

    async def fake_create_pull_request(cwd, base, head, title, body, **kwargs):
        pr_calls.append((str(cwd), base, head))
        return {"ok": True, "url": "https://github.com/jhonApp/bezalel-app/pull/42"}

    monkeypatch.setattr(nodes, "push_branch", fake_push_branch)
    monkeypatch.setattr(nodes, "create_pull_request", fake_create_pull_request)

    runtime = FakeRuntime(create_pull_request=True)
    result = await nodes.merge_changes(runtime, base_state())

    assert result["merges"] == []
    assert result["pull_requests"] == [
        {"project_id": "frontend", "branch": "codex/exec-pr-1/frontend", "url": "https://github.com/jhonApp/bezalel-app/pull/42"},
    ]
    assert push_calls == [("x", "codex/exec-pr-1/frontend")]
    assert pr_calls == [("x", "main", "codex/exec-pr-1/frontend")]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_merge_changes_records_the_error_when_push_fails(monkeypatch) -> None:
    async def fake_push_branch(cwd, branch, **kwargs):
        return {"ok": False, "error": "remote: Permission denied"}

    monkeypatch.setattr(nodes, "push_branch", fake_push_branch)

    async def unexpected_pr_create(*args, **kwargs):  # pragma: no cover - a call is the failure
        raise AssertionError("must not open a PR when the push itself failed")

    monkeypatch.setattr(nodes, "create_pull_request", unexpected_pr_create)

    runtime = FakeRuntime(create_pull_request=True)
    result = await nodes.merge_changes(runtime, base_state())

    assert result["pull_requests"] == []
    assert any("Permission denied" in e for e in result["errors"])


@pytest.mark.asyncio
async def test_merge_changes_still_merges_locally_when_create_pull_request_is_disabled(monkeypatch) -> None:
    """Regression guard: the existing local-merge behavior must survive untouched for
    everyone who has not opted into create_pull_request."""
    merged: list[tuple[str, str]] = []

    class FakeGitManager:
        def __init__(self, root):
            self.root = root

        async def merge(self, branch, base):
            merged.append((branch, base))
            return "deadbeef"

    monkeypatch.setattr(nodes, "GitManager", FakeGitManager)

    async def unexpected_push(*args, **kwargs):  # pragma: no cover - a call is the failure
        raise AssertionError("must not push when create_pull_request is disabled")

    monkeypatch.setattr(nodes, "push_branch", unexpected_push)

    runtime = FakeRuntime(create_pull_request=False, auto_merge=True)
    result = await nodes.merge_changes(runtime, base_state())

    assert result["pull_requests"] == []
    assert result["merges"] == [{"project_id": "frontend", "sha": "deadbeef"}]
    assert merged == [("codex/exec-pr-1/frontend", "main")]
