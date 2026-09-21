from __future__ import annotations

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
