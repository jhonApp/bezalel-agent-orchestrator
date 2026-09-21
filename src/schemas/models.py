from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandResult(BaseModel):
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None


class ProjectDetection(BaseModel):
    project_id: str
    expected_name: str
    path: str
    exists: bool
    language: str | None = None
    framework: list[str] = Field(default_factory=list)
    package_manager: str | None = None
    dependency_manifests: list[str] = Field(default_factory=list)
    lockfiles: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)
    important_files: list[str] = Field(default_factory=list)
    test_strategy: str | None = None
    deploy_strategy: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    existing_agents_or_workflows: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    task_id: str
    agent: str
    project_id: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed", "blocked", "skipped", "rate_limited"] = "pending"
    attempts: int = 0
    worktree: str | None = None
    branch: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent: str
    status: Literal["completed", "failed", "blocked", "skipped", "rate_limited"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    contracts_changed: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_action: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    duration_seconds: float = 0.0
    estimated_cost: float = 0.0
    raw_response: str = ""
    quality: dict[str, Any] | None = None


class ContractFinding(BaseModel):
    resource: str
    severity: Literal["info", "warning", "blocking"]
    message: str
    producer: str | None = None
    consumer: str | None = None
    evidence: list[str] = Field(default_factory=list)


class TestResult(BaseModel):
    project_id: str
    command: list[str] = Field(default_factory=list)
    status: Literal["passed", "failed", "skipped", "not_configured"]
    output: str = ""
    duration_seconds: float = 0.0
    blocking: bool = False


class SecurityFinding(BaseModel):
    severity: Literal["info", "warning", "blocking"]
    path: str
    message: str
    evidence: str = ""
    project_id: str | None = None


class ReviewResult(BaseModel):
    status: Literal["approved", "changes_requested", "blocked", "not_run"]
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    blocking: bool = False


class DeployResult(BaseModel):
    project_id: str
    strategy: str | None = None
    status: Literal["deployed", "blocked", "failed", "not_configured", "skipped"]
    command: list[str] = Field(default_factory=list)
    output: str = ""
    duration_seconds: float = 0.0
    rollback: str | None = None
    reason: str | None = None


class ContextHealth(BaseModel):
    level: Literal["healthy", "warning", "critical"] = "healthy"
    message_count: int = 0
    estimated_tokens: int = 0
    file_chars: int = 0
    tool_calls: int = 0
    repeated_information: int = 0
    stale_information: int = 0
    contract_conflicts: int = 0
    retries: int = 0
    consecutive_errors: int = 0
    estimated_cost: float = 0.0
    duration_seconds: float = 0.0
    compacted: bool = False
    summary_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    feature_request: str
    project_id: str = "bezalel"
    execution_id: str | None = None
    dry_run: bool = False
    analysis_only: bool = False
    target_projects: list[str] | None = None


class ExecutionStateModel(BaseModel):
    execution_id: str
    project_id: str
    feature_request: str
    target_projects: list[str] | None = None
    detected_projects: list[ProjectDetection] = Field(default_factory=list)
    relevant_projects: list[str] = Field(default_factory=list)
    architecture_summary: str = ""
    plan: list[TaskSpec] = Field(default_factory=list)
    active_task: str | None = None
    active_agent: str | None = None
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    contracts: list[ContractFinding] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    commits: list[dict[str, Any]] = Field(default_factory=list)
    pull_requests: list[dict[str, Any]] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    review_results: list[ReviewResult] = Field(default_factory=list)
    quality_scores: list[dict[str, Any]] = Field(default_factory=list)
    deploy_results: list[DeployResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    retries: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    estimated_cost: float = 0.0
    latency: dict[str, float] = Field(default_factory=dict)
    context_health: ContextHealth = Field(default_factory=ContextHealth)
    approvals: dict[str, bool] = Field(default_factory=dict)
    current_branch: dict[str, str] = Field(default_factory=dict)
    worktrees: dict[str, dict[str, str]] = Field(default_factory=dict)
    status: str = "created"
    next_action: str = "analyze_request"
    final_report: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    message_count: int = 0


def initial_state(request: ExecutionRequest, execution_id: str) -> dict[str, Any]:
    return ExecutionStateModel(
        execution_id=execution_id,
        project_id=request.project_id,
        feature_request=request.feature_request,
        target_projects=request.target_projects,
        status="created",
        approvals={"dry_run": request.dry_run, "analysis_only": request.analysis_only},
    ).model_dump(mode="json")
