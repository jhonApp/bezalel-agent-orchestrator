from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.registry import AGENT_ROLES, GATE_ROLES, role_prompt
from agents.security import scan_project
from adapters.agent_cli import AgentCLIAdapter, build_cli, validate_agent_roles
from adapters.codex_cli import CLASSIFIER_SCHEMA
from adapters.command import run_command
from adapters.deploy import DeployAdapter
from adapters.git import GitError, GitManager
from adapters.github import create_pull_request, push_branch
from adapters.project_detector import discover_projects, discovery_markdown
from orchestrator.config import Settings
from orchestrator.quality import build_quality_entry
from orchestrator.routing import gates_pass, ready_tasks
from observability.context_health import ContextHealthMonitor
from observability.langsmith import LangSmithObserver
from persistence.checkpointer import SQLiteCheckpointer
from schemas.models import AgentResult, ContractFinding, DeployResult, ReviewResult, SecurityFinding, TaskSpec, TestResult, utc_now


class ExecutionRuntime:
    def __init__(self, settings: Settings, store: SQLiteCheckpointer | None = None,
                 clis: dict[str, AgentCLIAdapter] | None = None,
                 event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None):
        self.settings = settings
        self.store = store or SQLiteCheckpointer(settings.checkpoint_path)
        self.clis = clis or {"codex": build_cli("codex", settings)}
        validate_agent_roles(AGENT_ROLES, available_clis=set(self.clis.keys()))
        self.deploy = DeployAdapter(settings)
        self.health = ContextHealthMonitor(settings)
        self.tracing = LangSmithObserver(settings)
        self.event_sink = event_sink
        self.cancel_events: dict[str, asyncio.Event] = {}

    def cancel_event(self, execution_id: str) -> asyncio.Event:
        return self.cancel_events.setdefault(execution_id, asyncio.Event())

    async def persist(self, state: dict[str, Any], node: str, event: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        state = dict(state)
        state["updated_at"] = utc_now()
        state["message_count"] = int(state.get("message_count", 0)) + 1
        state["context_health"] = self.health.assess(state).model_dump(mode="json")
        if state["context_health"]["level"] != "healthy":
            state = self.health.compact(state)
        self.store.save(state["execution_id"], state, node)
        if event:
            self.store.event(state["execution_id"], event, payload or {}, state["updated_at"])
            self.tracing.event(state["execution_id"], event, payload or {})
        await self.publish({
            "type": "state.updated", "execution_id": state["execution_id"], "node": node,
            "event": event, "status": state.get("status"), "active_agent": state.get("active_agent"),
            "active_task": state.get("active_task"), "updated_at": state["updated_at"],
        })
        return state

    async def publish(self, event: dict[str, Any]) -> None:
        if self.event_sink:
            await self.event_sink(event)

    def workdir_for(self, state: dict[str, Any], project_id: str) -> Path:
        item = state.get("worktrees", {}).get(project_id)
        if item and item.get("path"):
            return Path(item["path"])
        project = next((p for p in state.get("detected_projects", []) if p.get("project_id") == project_id), None)
        return Path(project["path"]) if project else self.settings.orchestrator_root

    async def prepare_worktree(self, state: dict[str, Any], project_id: str) -> dict[str, Any]:
        state = dict(state)
        if not self.settings.use_worktrees:
            return state
        if project_id in state.get("worktrees", {}):
            return state
        project = next((p for p in state.get("detected_projects", []) if p.get("project_id") == project_id), None)
        if not project or not project.get("exists"):
            return state
        root = Path(project["path"])
        manager = GitManager(root)
        if not await manager.is_repo():
            return state
        branch = f"codex/{state['execution_id']}/{project_id}"
        path = self.settings.orchestrator_root / ".worktrees" / state["execution_id"] / project_id
        try:
            worktree, branch = await manager.create_worktree(branch, path)
            current = await manager.current_branch()
            state.setdefault("current_branch", {})[project_id] = current
            state.setdefault("worktrees", {})[project_id] = {"path": str(worktree), "branch": branch, "base": current}
        except (GitError, OSError) as exc:
            state.setdefault("errors", []).append(f"worktree {project_id}: {exc}")
        return state

    async def run_task(self, state: dict[str, Any], task: dict[str, Any]) -> AgentResult:
        role = task["agent"]
        if not task.get("project_id"):
            return AgentResult(agent=role, status="skipped", summary="no project assigned")
        workdir = self.workdir_for(state, task["project_id"])
        prompt = role_prompt(role, self.settings.orchestrator_root / "prompts")
        prompt += f"\n\nFeature request:\n{state['feature_request']}\n\nTask {task['task_id']}: {task['description']}\nAcceptance criteria:\n" + "\n".join(f"- {x}" for x in task.get("acceptance_criteria", []))
        project_context = self._known_project_context(state, task["project_id"])
        if project_context:
            prompt += "\n\n" + project_context
        if state.get("approvals", {}).get("analysis_only"):
            prompt += ("\n\nANALYSIS-ONLY MODE: Do not create, modify, or delete any file. Read the code and "
                       "return your findings, risks and recommendations in `summary`. `files_changed` must stay empty.")
        async def on_event(event: dict[str, Any]) -> None:
            await self.publish({
                "type": "agent.stream", "execution_id": state["execution_id"],
                "agent": role, "task_id": task["task_id"], **event,
            })

        role_config = AGENT_ROLES.get(role, {})
        cli = self.clis[role_config.get("cli", "codex")]
        fallback_cli_name = role_config.get("fallback_cli")
        reasoning_effort = self.settings.codex_reasoning_effort_gates if role in GATE_ROLES else None
        attempts = 0
        last: AgentResult | None = None
        while attempts <= self.settings.max_retries:
            attempts += 1
            task["attempts"] = attempts
            result = await cli.execute(prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                                       timeout=self.settings.agent_timeout_seconds, cancel_event=self.cancel_event(state["execution_id"]),
                                       trace_metadata={
                                           "orchestrator_execution_id": state["execution_id"],
                                           "orchestrator_task_id": task["task_id"],
                                           "orchestrator_project_id": task["project_id"],
                                           "context_health": state.get("context_health", {}),
                                       },
                                       on_event=on_event, reasoning_effort=reasoning_effort)
            last = result
            # A real usage-limit hit is not transient the way a timeout or a flaky tool call
            # is: the account-wide quota it reports against does not reset within this loop's
            # 2s/4s backoff, so retrying here only guarantees hitting the same wall two more
            # times, each burning a full prompt's worth of input tokens at the worst possible
            # moment. Stop after one attempt and let the gate/report surface it instead.
            if result.status in ("completed", "rate_limited"):
                break
            state["retries"] = int(state.get("retries", 0)) + 1
            if attempts <= self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (2 ** (attempts - 1)))

        # The fallback is a single attempt on a different CLI, never its own retry loop -
        # hammering a second rate-limited resource is not a recovery strategy. It only
        # fires for rate_limited specifically: a genuine failure is not evidence a
        # different CLI would have done better.
        if last is not None and last.status == "rate_limited" and fallback_cli_name:
            fallback = self.clis.get(fallback_cli_name)
            if fallback is not None:
                fallback_result = await fallback.execute(
                    prompt, workdir, AGENT_ROLES.get(role, {}).get("label", role),
                    timeout=self.settings.agent_timeout_seconds, cancel_event=self.cancel_event(state["execution_id"]),
                    trace_metadata={
                        "orchestrator_execution_id": state["execution_id"],
                        "orchestrator_task_id": task["task_id"],
                        "orchestrator_project_id": task["project_id"],
                        "context_health": state.get("context_health", {}),
                    },
                    on_event=on_event, reasoning_effort=reasoning_effort,
                )
                fallback_result.cli_used = fallback_cli_name
                return fallback_result

        if last is not None:
            last.cli_used = role_config.get("cli", "codex")
        return last or AgentResult(agent=role, status="failed", summary="agent did not return")

    @staticmethod
    def _known_project_context(state: dict[str, Any], project_id: str) -> str:
        """Render what `discover_projects` already learned about this project (framework,
        package manager, exact lint/test/build commands, important files) so the agent
        doesn't have to re-derive it via several rounds of file reads/greps on every single
        task — the dominant source of the large input-token-to-output-token ratio observed
        in production (e.g. python_ai: ~325k input vs ~3k output tokens over 17 executions).
        """
        project = next((p for p in state.get("detected_projects", []) if p.get("project_id") == project_id), None)
        if not project:
            return ""
        lines = ["Known project context (already detected — do not re-discover this by exploring the repo):"]
        if project.get("framework"):
            lines.append("- Framework: " + ", ".join(project["framework"]))
        if project.get("package_manager"):
            lines.append("- Package manager: " + project["package_manager"])
        if project.get("commands"):
            lines.append("- Commands: " + "; ".join(f"{k}={v}" for k, v in project["commands"].items()))
        if project.get("test_strategy"):
            lines.append("- Test strategy: " + project["test_strategy"])
        if project.get("important_files"):
            lines.append("- Important files: " + ", ".join(project["important_files"]))
        return "\n".join(lines) if len(lines) > 1 else ""

    async def announce_agent_started(self, state: dict[str, Any], task: dict[str, Any]) -> None:
        """Publish the same agent.started lifecycle event dispatch_agents emits for the
        parallel frontend/backend/python_ai batch — contracts and reviewer call run_task too,
        and without this their card in Manage Agents never leaves "idle" no matter how long
        their Codex session actually runs.
        """
        started_event = {
            "type": "agent.started", "execution_id": state["execution_id"], "agent": task["agent"],
            "task_id": task["task_id"], "description": task.get("description", ""), "status": "running",
        }
        self.store.event(state["execution_id"], "agent.started", started_event, utc_now())
        await self.publish(started_event)

    async def announce_agent_finished(self, state: dict[str, Any], task: dict[str, Any]) -> None:
        finished_event = {
            "type": "agent.finished", "execution_id": state["execution_id"], "agent": task["agent"],
            "task_id": task["task_id"], "status": task.get("status"), "result": task.get("result"),
        }
        self.store.event(state["execution_id"], "agent.finished", finished_event, utc_now())
        await self.publish(finished_event)

    async def classify_relevant_projects(self, feature_request: str, existing: set[str]) -> tuple[list[str], str]:
        """Ask Codex which domains this feature request actually touches, so create_plan
        doesn't dispatch a full coding-agent session for a project with nothing to do.
        Fails open (every existing project) on any classifier error — this call must
        never be able to shrink the pipeline by failing.

        Returns ``(relevant, source)`` where ``source`` is ``"classifier"`` when a real
        classifier response was used, or ``"fail_open"`` when ``execute_json`` returned
        ``None`` — so callers/observers can tell a deliberate "everything is relevant"
        decision apart from a silently-degraded classifier (missing CLI, drifted flags,
        timeout too short, etc.).
        """
        prompt = role_prompt("classifier", self.settings.orchestrator_root / "prompts")
        prompt += f"\n\nFeature request:\n{feature_request}"
        # validate_agent_roles already checks AGENT_ROLES["classifier"]["cli"] at boot (it
        # iterates every role, this one included) — honoring it here too, instead of pinning
        # to "codex" unconditionally, is what makes that validation mean something: a role's
        # cli must never be checked at startup and then silently ignored at dispatch.
        classifier_cli = self.clis[AGENT_ROLES.get("classifier", {}).get("cli", "codex")]
        result = await classifier_cli.execute_json(prompt, self.settings.workspace_root, CLASSIFIER_SCHEMA, "Classifier",
                                                    reasoning_effort=self.settings.codex_reasoning_effort_gates)
        if result is None:
            return sorted(existing), "fail_open"
        domains = {"frontend": "frontend", "backend": "backend", "python": "python"}
        relevant = [project for domain, project in domains.items() if result.get(domain, True) and project in existing]
        return relevant, "classifier"


def _cancelled(runtime: ExecutionRuntime, state: dict[str, Any]) -> bool:
    return runtime.cancel_event(state["execution_id"]).is_set()


async def analyze_request(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    if _cancelled(runtime, state):
        state["status"] = "cancelled"
        return await runtime.persist(state, "analyze_request")
    state["status"] = "running"
    state["architecture_summary"] = "Feature will be routed across detected frontend, .NET backend and Python/LangGraph projects; contracts gate consumers."
    state["next_action"] = "discover_projects"
    return await runtime.persist(state, "analyze_request", "supervisor.analyzed", {"feature_request": state["feature_request"]})


async def discover_projects_node(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    projects = discover_projects(runtime.settings)
    state["detected_projects"] = [p.model_dump(mode="json") for p in projects]
    report_dir = runtime.settings.orchestrator_root / "docs" / "discovery"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "project-discovery-latest.md").write_text(discovery_markdown(projects, runtime.settings), encoding="utf-8")
    missing = [p.expected_name for p in projects if not p.exists]
    if missing:
        state.setdefault("errors", []).append("missing project aliases: " + ", ".join(missing))
    state["next_action"] = "classify_projects"
    return await runtime.persist(state, "discover_projects", "projects.discovered", {"count": len(projects), "missing": missing})


async def classify_projects(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
    override = state.get("target_projects")
    if override is not None:
        relevant = [p for p in override if p in existing]
        source = "override"
    else:
        relevant, source = await runtime.classify_relevant_projects(state["feature_request"], existing)
    state["relevant_projects"] = relevant
    state["relevant_projects_source"] = source
    state["next_action"] = "create_plan"
    return await runtime.persist(state, "classify_projects", "projects.classified", {"relevant": relevant, "source": source})


def _reuse_if_completed(existing_by_id: dict[str, dict[str, Any]], task_id: str, role: str, project_id: str) -> dict[str, Any] | None:
    """A resumed execution's state["plan"] already holds the previous run's tasks — a task
    that already finished successfully must not be recreated (and re-dispatched to a real
    Codex agent) just because create_plan runs again.
    """
    prior = existing_by_id.get(task_id)
    if prior and prior.get("status") == "completed" and prior.get("agent") == role and prior.get("project_id") == project_id:
        return prior
    return None


async def create_plan(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    existing_by_id = {t["task_id"]: t for t in state.get("plan", [])}
    existing = {p["project_id"] for p in state.get("detected_projects", []) if p.get("exists")}
    relevant = existing if state.get("relevant_projects") is None else state["relevant_projects"]

    def build(task_id: str, role: str, project_id: str, description: str, dependencies: list[str], acceptance_criteria: list[str]) -> dict[str, Any]:
        reused = _reuse_if_completed(existing_by_id, task_id, role, project_id)
        if reused is not None:
            return reused
        return TaskSpec(task_id=task_id, agent=role, project_id=project_id, description=description,
                        dependencies=dependencies, acceptance_criteria=acceptance_criteria).model_dump(mode="json")

    tasks: list[dict[str, Any]] = []
    for number, (role, project, description) in enumerate([
        ("frontend", "frontend", "Implement the frontend portion of the feature using the detected stack and design system."),
        ("backend", "backend", "Implement backend/API/domain changes and preserve current AWS and authorization conventions."),
        ("python_ai", "python", "Implement Python/LangGraph workflow or prompt changes required by the feature."),
    ], 1):
        if project in existing and project in relevant:
            tasks.append(build(f"T{number:03d}", role, project, description, [],
                               ["Return changed files", "Return validation evidence", "Do not expose secrets"]))
    base_ids = [task["task_id"] for task in tasks]
    tasks.extend([
        build("T010", "contracts", "backend", "Validate API, event and serialized payload contracts across affected projects.", base_ids,
              ["No blocking breaking change remains", "Exact JSON casing and nullability checked"]),
        build("T011", "qa", "backend", "Run the relevant tests and add focused coverage only where required.", base_ids + ["T010"],
              ["Required tests pass", "Failures classified"]),
        build("T012", "security", "backend", "Review secrets, cloud permissions, logs, environment handling and deployment risk.", base_ids,
              ["No blocking secret or cloud finding"]),
        build("T013", "reviewer", "backend", "Review the resulting diff, tests, observability and compatibility.", ["T010", "T011", "T012"],
              ["No blocking review finding"]),
    ])
    state["plan"] = tasks
    state["dependencies"] = {task["task_id"]: task.get("dependencies", []) for task in tasks}
    state["next_action"] = "resolve_dependencies"
    return await runtime.persist(state, "create_plan", "plan.created", {"tasks": [task["task_id"] for task in tasks]})


async def resolve_dependencies(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    state["next_action"] = "create_contracts"
    return await runtime.persist(state, "resolve_dependencies", "dependencies.resolved", {"dependencies": state.get("dependencies", {})})


async def create_contracts(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    contract_file = runtime.settings.frontend_path / "docs" / "backend-integration-contract.md"
    findings: list[ContractFinding] = []
    if not contract_file.exists():
        findings.append(ContractFinding(resource="frontend-backend-contract", severity="warning", message="integration contract file not found"))
    else:
        text = contract_file.read_text(encoding="utf-8", errors="replace")
        if "HTTP 401" not in text or "Authorization" not in text:
            findings.append(ContractFinding(resource="frontend-backend-contract", severity="warning", message="contract lacks expected auth/error conventions"))
    state["contracts"] = [f.model_dump(mode="json") for f in findings]
    state["next_action"] = "dispatch_agents"
    return await runtime.persist(state, "create_contracts", "contracts.created", {"findings": len(findings)})


async def dispatch_agents(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    tasks = ready_tasks(state, {"frontend", "backend", "python_ai"})
    for task in tasks:
        task["status"] = "running"
        state["active_agent"] = task["agent"]
        state["active_task"] = task["task_id"]
        started_event = {
            "type": "agent.started", "execution_id": state["execution_id"], "agent": task["agent"],
            "task_id": task["task_id"], "description": task["description"], "status": "running",
        }
        runtime.store.event(state["execution_id"], "agent.started", started_event, utc_now())
        await runtime.publish(started_event)
        state = await runtime.prepare_worktree(state, task["project_id"])
        task["worktree"] = runtime.workdir_for(state, task["project_id"]).__str__()
        task["branch"] = state.get("worktrees", {}).get(task["project_id"], {}).get("branch")
    if tasks and not _cancelled(runtime, state) and not state.get("approvals", {}).get("dry_run"):
        results = await asyncio.gather(*(runtime.run_task(state, task) for task in tasks), return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                task["status"] = "failed"
                task["result"] = {"agent": task["agent"], "status": "failed", "errors": [str(result)]}
            else:
                task["status"] = "completed" if result.status == "completed" else result.status
                task["result"] = result.model_dump(mode="json")
                task["result"]["prompt_version"] = AGENT_ROLES.get(task["agent"], {}).get("prompt_version")
                state.setdefault("files_changed", []).extend(result.files_changed)
                state["token_usage"][task["agent"]] = result.tokens_input + result.tokens_output
                state["estimated_cost"] += result.estimated_cost
                runtime.store.agent_run(state["execution_id"], task["agent"], task["task_id"], task["result"], utc_now())
            finished_event = {
                "type": "agent.finished", "execution_id": state["execution_id"], "agent": task["agent"],
                "task_id": task["task_id"], "status": task["status"], "result": task["result"],
            }
            runtime.store.event(state["execution_id"], "agent.finished", finished_event, utc_now())
            await runtime.publish(finished_event)
    elif tasks:
        for task in tasks:
            task["status"] = "skipped"
            task["result"] = {"status": "skipped", "summary": "dry-run or cancelled"}
            finished_event = {
                "type": "agent.finished", "execution_id": state["execution_id"], "agent": task["agent"],
                "task_id": task["task_id"], "status": "skipped", "result": task["result"],
            }
            runtime.store.event(state["execution_id"], "agent.finished", finished_event, utc_now())
            await runtime.publish(finished_event)
    state["active_agent"] = None
    state["active_task"] = None
    state["next_action"] = "collect_agent_results"
    return await runtime.persist(state, "dispatch_agents", "agents.dispatched", {"tasks": [t["task_id"] for t in tasks]})


async def collect_agent_results(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    state["files_changed"] = sorted(set(state.get("files_changed", [])))
    for task in state.get("plan", []):
        if task.get("agent") in {"frontend", "backend", "python_ai"} and task.get("status") == "pending":
            task["status"] = "skipped"
    state["next_action"] = "run_contract_validation"
    return await runtime.persist(state, "collect_agent_results", "agents.collected", {"files_changed": len(state["files_changed"])})


async def _changed_paths(runtime: ExecutionRuntime, state: dict[str, Any], project_id: str) -> list[str] | None:
    """Return changed paths from the isolated worktree, or None if Git could not inspect it."""
    result = await run_command(
        ["git", "status", "--porcelain"],
        runtime.workdir_for(state, project_id),
        timeout=30,
    )
    if not result.ok:
        return None
    paths: list[str] = []
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.append(path.strip('"'))
    return paths


async def _changed_project_ids(runtime: ExecutionRuntime, state: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for project in state.get("detected_projects", []):
        if not project.get("exists"):
            continue
        paths = await _changed_paths(runtime, state, project["project_id"])
        if paths:
            changed.append(project["project_id"])
    return changed


async def _run_gate_agent_across_projects(runtime: ExecutionRuntime, state: dict[str, Any], task: dict[str, Any],
                                          changed_projects: list[str],
                                          on_project_result: Callable[[str, AgentResult], Awaitable[None]] | None = None) -> AgentResult:
    """Dispatch a gate agent (contracts/reviewer) once per changed project and merge the
    outcomes. Taking only ``changed_projects[0]`` silently skipped every other project a
    feature touched — e.g. a feature that changes both frontend and backend in the same
    execution would only ever get one of the two actually reviewed/validated.

    ``on_project_result``, if given, is awaited once per project with that project's own
    (pre-merge) ``AgentResult`` — the merge below loses per-project detail, so anything
    that needs it (e.g. quality scoring) must observe it here.
    """
    per_project: list[AgentResult] = []
    for project_id in changed_projects:
        task["project_id"] = project_id
        task["worktree"] = str(runtime.workdir_for(state, project_id))
        task["branch"] = state.get("worktrees", {}).get(project_id, {}).get("branch")
        await runtime.announce_agent_started(state, task)
        agent_result = await runtime.run_task(state, task)
        task["status"] = "completed" if agent_result.status == "completed" else agent_result.status
        task["result"] = agent_result.model_dump(mode="json")
        await runtime.announce_agent_finished(state, task)
        per_project.append(agent_result)
        if on_project_result is not None:
            await on_project_result(project_id, agent_result)
    statuses = [r.status for r in per_project]
    overall_status = "completed" if all(s == "completed" for s in statuses) else next(s for s in statuses if s != "completed")
    # A fallback can make different projects in the same gate run be served by different
    # CLIs — collapse to a single name only when every project actually agrees, otherwise
    # list which ones ran where rather than silently reporting just the first.
    clis_used = {r.cli_used or "?" for r in per_project}
    merged_cli_used = next(iter(clis_used)) if len(clis_used) == 1 else ", ".join(sorted(clis_used))
    return AgentResult(
        agent=task["agent"], status=overall_status,
        summary="; ".join(f"[{project}] {r.summary}" for project, r in zip(changed_projects, per_project)),
        errors=[err for r in per_project for err in r.errors],
        files_changed=[f for r in per_project for f in r.files_changed],
        tokens_input=sum(r.tokens_input for r in per_project),
        tokens_output=sum(r.tokens_output for r in per_project),
        estimated_cost=sum(r.estimated_cost for r in per_project),
        cli_used=merged_cli_used,
    )


async def run_contract_validation(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    task = next((t for t in state.get("plan", []) if t.get("agent") == "contracts"), None)
    if task:
        task["status"] = "running"
        if state.get("approvals", {}).get("dry_run"):
            task["status"] = "skipped"
        else:
            changed_projects = await _changed_project_ids(runtime, state)
            if not changed_projects:
                result = AgentResult(
                    agent="contracts",
                    status="completed",
                    summary="No files changed; contract validation was not required.",
                )
            else:
                result = await _run_gate_agent_across_projects(runtime, state, task, changed_projects)
            task["result"] = result.model_dump(mode="json")
            task["status"] = "completed" if result.status == "completed" else result.status
            if result.errors:
                state["contracts"].append(ContractFinding(resource="agent-contract-review", severity="blocking", message="contract agent reported an error", evidence=result.errors).model_dump(mode="json"))
    state["next_action"] = "run_tests"
    return await runtime.persist(state, "run_contract_validation", "contracts.validated", {"blocking": any(c.get("severity") == "blocking" for c in state.get("contracts", []))})


async def run_tests(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    results: list[TestResult] = []
    for project in state.get("detected_projects", []):
        if not project.get("exists"):
            results.append(TestResult(project_id=project["project_id"], status="not_configured", blocking=True, output="project missing"))
            continue
        command = None
        if project["project_id"] == "frontend":
            command = ["npm", "run", "test", "--", "--run"] if project.get("package_manager") == "npm" and "test" in project.get("commands", {}) else None
        elif project["project_id"] == "backend":
            command = ["dotnet", "test", "--no-restore"] if "test" in project.get("commands", {}) else None
        elif project["project_id"] == "python":
            command = ["python", "-m", "pytest", "-q"]
        changed_paths = await _changed_paths(runtime, state, project["project_id"])
        if changed_paths == []:
            results.append(TestResult(
                project_id=project["project_id"], status="skipped", command=command or [],
                output="No files changed in this project.", blocking=False,
            ))
            continue
        if not command or state.get("approvals", {}).get("dry_run"):
            results.append(TestResult(project_id=project["project_id"], status="skipped" if state.get("approvals", {}).get("dry_run") else "not_configured", command=command or []))
            continue
        result = await run_command(command, runtime.workdir_for(state, project["project_id"]), timeout=runtime.settings.agent_timeout_seconds)
        results.append(TestResult(project_id=project["project_id"], status="passed" if result.ok else "failed", command=command,
                                  output=(result.stdout + "\n" + result.stderr + ("\n" + result.error if result.error else ""))[-12000:],
                                  duration_seconds=result.duration_seconds, blocking=not result.ok))
    state["test_results"] = [r.model_dump(mode="json") for r in results]
    qa_task = next((t for t in state.get("plan", []) if t.get("agent") == "qa"), None)
    if qa_task:
        failed = [r for r in results if r.blocking or r.status == "failed"]
        qa_task["status"] = "failed" if failed else "completed"
        qa_task["result"] = {
            "status": qa_task["status"],
            "summary": f"{len(results) - len(failed)} project test gates passed or skipped; {len(failed)} failed",
        }
    state["next_action"] = "security_review"
    return await runtime.persist(state, "run_tests", "tests.completed", {"passed": sum(r.status == "passed" for r in results), "failed": sum(r.status == "failed" for r in results)})


async def security_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    findings: list[SecurityFinding] = []
    for project in state.get("detected_projects", []):
        if project.get("exists"):
            paths = await _changed_paths(runtime, state, project["project_id"])
            if paths:
                worktree_project = {**project, "path": str(runtime.workdir_for(state, project["project_id"]))}
                project_findings = scan_project(type("Project", (), worktree_project)(), paths)
                for finding in project_findings:
                    finding.project_id = project["project_id"]
                findings.extend(project_findings)
    blocking = any(f.severity == "blocking" for f in findings)
    state["security_findings"] = [f.model_dump(mode="json") for f in findings]
    state["security_blocking"] = blocking
    task = next((t for t in state.get("plan", []) if t.get("agent") == "security"), None)
    if task:
        task["status"] = "blocked" if blocking else "completed"
        task["result"] = {"status": task["status"], "findings": state["security_findings"]}
    state["next_action"] = "code_review"
    return await runtime.persist(state, "security_review", "security.completed", {"findings": len(findings), "blocking": blocking})


async def code_review(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    task = next((t for t in state.get("plan", []) if t.get("agent") == "reviewer"), None)
    result = ReviewResult(status="not_run", summary="review skipped in dry-run")
    if task and not state.get("approvals", {}).get("dry_run"):
        task["status"] = "running"
        changed_projects = await _changed_project_ids(runtime, state)
        if not changed_projects:
            agent = AgentResult(
                agent="reviewer",
                status="completed",
                summary="No files changed; code review was not required.",
            )
        else:
            async def _record_quality(project_id: str, agent_result: AgentResult) -> None:
                try:
                    state.setdefault("quality_scores", []).append(build_quality_entry(project_id, state, agent_result))
                except Exception as exc:
                    state.setdefault("errors", []).append(f"quality scoring {project_id}: {exc}")

            agent = await _run_gate_agent_across_projects(runtime, state, task, changed_projects, on_project_result=_record_quality)
        result = ReviewResult(status="approved" if agent.status == "completed" else "changes_requested", summary=agent.summary,
                              findings=[{"message": x} for x in agent.errors], blocking=agent.status != "completed")
        task["result"] = agent.model_dump(mode="json")
        task["status"] = "completed" if agent.status == "completed" else agent.status
    state["review_results"] = [result.model_dump(mode="json")]
    state["next_action"] = "commit_changes"
    return await runtime.persist(state, "code_review", "review.completed", {"status": result.status})


async def commit_changes(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    passed, reasons = gates_pass(state)
    commits = []
    if not passed:
        state["errors"].extend(["commit blocked: " + reason for reason in reasons])
    elif (runtime.settings.auto_commit and not state.get("approvals", {}).get("dry_run")
          and not state.get("approvals", {}).get("analysis_only")):
        for project_id, info in state.get("worktrees", {}).items():
            path = Path(info["path"])
            try:
                message = "feat: " + state["feature_request"].strip()[:60]
                commit = await GitManager(Path(next(p["path"] for p in state["detected_projects"] if p["project_id"] == project_id))).commit(path, message)
                if commit:
                    commits.append({"project_id": project_id, "branch": info.get("branch"), "sha": commit})
            except (GitError, StopIteration) as exc:
                state["errors"].append(f"commit {project_id}: {exc}")
    state["commits"] = commits
    state["next_action"] = "merge_changes"
    return await runtime.persist(state, "commit_changes", "git.commit", {"count": len(commits), "blocked": reasons})


async def merge_changes(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    passed, reasons = gates_pass(state)
    merges: list[dict[str, Any]] = []
    pull_requests: list[dict[str, Any]] = []
    can_run = (passed and not state.get("approvals", {}).get("dry_run")
               and not state.get("approvals", {}).get("analysis_only"))
    if can_run and runtime.settings.create_pull_request:
        for commit in state.get("commits", []):
            info = state.get("worktrees", {}).get(commit["project_id"], {})
            base = info.get("base")
            branch = info.get("branch")
            if not base or not branch:
                continue
            push_result = await push_branch(Path(info["path"]), branch)
            if not push_result.get("ok"):
                state["errors"].append(f"push {commit['project_id']}: {push_result.get('error')}")
                continue
            title = f"feat: {state['feature_request'].strip()[:60]}"
            body = f"Automated by bezalel-agent-orchestrator.\n\nExecution: {state['execution_id']}"
            pr_result = await create_pull_request(Path(info["path"]), base, branch, title, body)
            if pr_result.get("ok"):
                pull_requests.append({"project_id": commit["project_id"], "branch": branch, "url": pr_result["url"]})
            else:
                state["errors"].append(f"pr {commit['project_id']}: {pr_result.get('error')}")
    elif can_run and runtime.settings.auto_merge:
        for commit in state.get("commits", []):
            info = state.get("worktrees", {}).get(commit["project_id"], {})
            base = info.get("base")
            if not base or not info.get("branch"):
                continue
            try:
                sha = await GitManager(Path(next(p["path"] for p in state["detected_projects"] if p["project_id"] == commit["project_id"]))).merge(info["branch"], base)
                merges.append({"project_id": commit["project_id"], "sha": sha})
            except (GitError, StopIteration) as exc:
                state["errors"].append(f"merge {commit['project_id']}: {exc}")
    elif not passed:
        state["errors"].extend(["merge blocked: " + reason for reason in reasons])
    state["merges"] = merges
    state["pull_requests"] = pull_requests
    state["next_action"] = "deploy"
    return await runtime.persist(state, "merge_changes", "git.merge",
                                  {"count": len(merges), "prs": len(pull_requests), "blocked": reasons})


async def deploy(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    passed, reasons = gates_pass(state)
    results: list[DeployResult] = []
    if not passed:
        results = [DeployResult(project_id=p["project_id"], status="blocked", reason="; ".join(reasons)) for p in state.get("detected_projects", [])]
    elif (not runtime.settings.auto_deploy or state.get("approvals", {}).get("dry_run")
          or state.get("approvals", {}).get("analysis_only")):
        reason = "analysis_only" if state.get("approvals", {}).get("analysis_only") else "AUTO_DEPLOY=false or dry-run"
        results = [DeployResult(project_id=p["project_id"], status="skipped", reason=reason) for p in state.get("detected_projects", [])]
    else:
        for project in state.get("detected_projects", []):
            results.append(await runtime.deploy.execute(type("Project", (), project)(), runtime.workdir_for(state, project["project_id"])))
    state["deploy_results"] = [r.model_dump(mode="json") for r in results]
    state["next_action"] = "generate_final_report"
    return await runtime.persist(state, "deploy", "deploy.completed", {"statuses": [r.status for r in results]})


async def generate_final_report(runtime: ExecutionRuntime, state: dict[str, Any]) -> dict[str, Any]:
    blocking = not gates_pass(state)[0]
    state["status"] = "failed" if blocking and state.get("errors") else ("cancelled" if _cancelled(runtime, state) else "completed")
    state["next_action"] = "done"
    state["final_report"] = {
        "execution_id": state["execution_id"], "status": state["status"], "files_changed": sorted(set(state.get("files_changed", []))),
        "commits": state.get("commits", []), "tests": state.get("test_results", []), "contracts": state.get("contracts", []),
        "security": state.get("security_findings", []), "review": state.get("review_results", []),
        "deploy": state.get("deploy_results", []), "errors": state.get("errors", []), "context_health": state.get("context_health", {}),
    }
    report_dir = runtime.settings.orchestrator_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{state['execution_id']}.json").write_text(json.dumps(state["final_report"], ensure_ascii=False, indent=2), encoding="utf-8")
    return await runtime.persist(state, "generate_final_report", "execution.completed", state["final_report"])
