from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import nodes
from orchestrator.config import Settings
from orchestrator.nodes import ExecutionRuntime
from persistence.checkpointer import SQLiteCheckpointer
from schemas.models import ExecutionRequest, initial_state


def test_initial_state_carries_target_projects_override_through():
    request = ExecutionRequest(feature_request="add a button", target_projects=["frontend"])
    state = initial_state(request, "exec-1")
    assert state["target_projects"] == ["frontend"]


def test_initial_state_defaults_target_projects_to_none():
    request = ExecutionRequest(feature_request="add a button")
    state = initial_state(request, "exec-1")
    assert state["target_projects"] is None


def test_initial_state_defaults_relevant_projects_to_empty_list():
    request = ExecutionRequest(feature_request="add a button")
    state = initial_state(request, "exec-1")
    assert state["relevant_projects"] == []


def test_initial_state_preserves_an_explicit_empty_target_projects_list():
    request = ExecutionRequest(feature_request="tweak a config file only", target_projects=[])
    state = initial_state(request, "exec-1")
    assert state["target_projects"] == []


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path,
        backend_path=tmp_path, python_path=tmp_path,
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


class FakeCodexForClassifier:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute_json(self, prompt, workdir, schema, label, timeout=120):
        self.calls.append({"prompt": prompt, "workdir": workdir, "schema": schema, "label": label})
        return self._result


@pytest.mark.asyncio
async def test_classify_relevant_projects_filters_by_the_classifier_result(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True, "backend": False, "python": False}))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert relevant == ["frontend"]


@pytest.mark.asyncio
async def test_classify_relevant_projects_fails_open_when_the_classifier_returns_none(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier(None))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend"})

    assert relevant == ["backend", "frontend"]


@pytest.mark.asyncio
async def test_classify_relevant_projects_defaults_a_missing_field_to_relevant(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = ExecutionRuntime(settings, store=SQLiteCheckpointer(settings.checkpoint_path),
                               codex=FakeCodexForClassifier({"frontend": True}))

    relevant = await runtime.classify_relevant_projects("add a button", {"frontend", "backend", "python"})

    assert set(relevant) == {"frontend", "backend", "python"}


class StubClassifyRuntime:
    def __init__(self, relevant):
        self._relevant = relevant
        self.classify_calls = []

    async def classify_relevant_projects(self, feature_request, existing):
        self.classify_calls.append((feature_request, existing))
        return self._relevant

    async def persist(self, state, node, event=None, payload=None):
        return state


@pytest.mark.asyncio
async def test_classify_projects_node_uses_the_override_and_skips_the_classifier():
    runtime = StubClassifyRuntime(relevant=["frontend", "backend", "python"])
    state = {
        "feature_request": "add a button", "target_projects": ["frontend"],
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["frontend"]
    assert runtime.classify_calls == []


@pytest.mark.asyncio
async def test_classify_projects_node_calls_the_classifier_when_no_override_is_given():
    runtime = StubClassifyRuntime(relevant=["backend"])
    state = {
        "feature_request": "fix the endpoint", "target_projects": None,
        "detected_projects": [
            {"project_id": "frontend", "exists": True},
            {"project_id": "backend", "exists": True},
        ],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["backend"]
    assert runtime.classify_calls == [("fix the endpoint", {"frontend", "backend"})]


@pytest.mark.asyncio
async def test_classify_projects_node_filters_the_override_by_existing_projects():
    runtime = StubClassifyRuntime(relevant=[])
    state = {
        "feature_request": "add a button", "target_projects": ["frontend", "python"],
        "detected_projects": [{"project_id": "frontend", "exists": True}],
    }

    result = await nodes.classify_projects(runtime, state)

    assert result["relevant_projects"] == ["frontend"]
