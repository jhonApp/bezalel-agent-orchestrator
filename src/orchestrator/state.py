from __future__ import annotations

from typing import Any, TypedDict


class ExecutionState(TypedDict, total=False):
    execution_id: str
    project_id: str
    feature_request: str
    detected_projects: list[dict[str, Any]]
    architecture_summary: str
    plan: list[dict[str, Any]]
    active_task: str | None
    active_agent: str | None
    dependencies: dict[str, list[str]]
    contracts: list[dict[str, Any]]
    files_changed: list[str]
    commits: list[dict[str, Any]]
    merges: list[dict[str, Any]]
    test_results: list[dict[str, Any]]
    security_findings: list[dict[str, Any]]
    security_blocking: bool
    review_results: list[dict[str, Any]]
    deploy_results: list[dict[str, Any]]
    errors: list[str]
    retries: int
    token_usage: dict[str, int]
    estimated_cost: float
    latency: dict[str, float]
    context_health: dict[str, Any]
    approvals: dict[str, bool]
    current_branch: dict[str, str]
    worktrees: dict[str, dict[str, str]]
    status: str
    next_action: str
    final_report: dict[str, Any]
    started_at: str
    updated_at: str
    message_count: int
