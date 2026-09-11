from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from adapters.codex_cli import CodexCLI
from orchestrator.config import Settings
from orchestrator.nodes import (ExecutionRuntime, analyze_request, code_review, collect_agent_results, commit_changes,
                                create_contracts, create_plan, deploy, discover_projects_node, dispatch_agents,
                                generate_final_report, merge_changes, resolve_dependencies, run_contract_validation,
                                run_tests, security_review)
from orchestrator.state import ExecutionState
from persistence.checkpointer import SQLiteCheckpointer
from schemas.models import ExecutionRequest, initial_state


class OrchestrationGraph:
    def __init__(self, runtime: ExecutionRuntime, checkpointer: Any | None = None):
        self.runtime = runtime
        builder = StateGraph(ExecutionState)
        nodes: list[tuple[str, Callable]] = [
            ("analyze_request", analyze_request), ("discover_projects", discover_projects_node),
            ("create_plan", create_plan), ("resolve_dependencies", resolve_dependencies),
            ("create_contracts", create_contracts), ("dispatch_agents", dispatch_agents),
            ("collect_agent_results", collect_agent_results), ("run_contract_validation", run_contract_validation),
            ("run_tests", run_tests), ("security_review", security_review), ("code_review", code_review),
            ("commit_changes", commit_changes), ("merge_changes", merge_changes), ("deploy", deploy),
            ("generate_final_report", generate_final_report),
        ]
        for name, function in nodes:
            async def invoke(state, fn=function):
                return await fn(runtime, state)
            builder.add_node(name, invoke)
        builder.add_edge(START, "analyze_request")
        for first, second in zip([name for name, _ in nodes], [name for name, _ in nodes][1:]):
            builder.add_edge(first, second)
        builder.add_edge("generate_final_report", END)
        # The native LangGraph checkpointer is the source of truth for a
        # thread's state and checkpoint history. The runtime store continues
        # to hold presentation-specific event and agent-run records.
        self.checkpointer = checkpointer
        self.compiled = builder.compile(checkpointer=checkpointer)

    async def run(self, request: ExecutionRequest, execution_id: str | None = None) -> dict[str, Any]:
        execution_id = execution_id or str(uuid.uuid4())
        state = self.runtime.store.load(execution_id) or initial_state(request, execution_id)
        if state.get("status") in {"completed", "failed"} and state.get("next_action") == "done":
            return state
        return await self.compiled.ainvoke(state, config={"configurable": {"thread_id": execution_id}})

    async def resume(self, execution_id: str) -> dict[str, Any]:
        state = self.runtime.store.load(execution_id)
        if not state:
            raise KeyError(execution_id)
        state["status"] = "running"
        state["approvals"] = {**state.get("approvals", {}), "resumed": True}
        self.runtime.cancel_event(execution_id).clear()
        request = ExecutionRequest(feature_request=state["feature_request"], project_id=state.get("project_id", "bezalel"), execution_id=execution_id,
                                   dry_run=bool(state.get("approvals", {}).get("dry_run", False)))
        return await self.run(request, execution_id)

    def cancel(self, execution_id: str) -> dict[str, Any] | None:
        self.runtime.cancel_event(execution_id).set()
        state = self.runtime.store.load(execution_id)
        if state:
            state["status"] = "cancelled"
            state["next_action"] = "resume"
            self.runtime.store.save(execution_id, state, "cancel")
            self.runtime.store.finish_active_agents(execution_id)
        return state


def create_graph(settings: Settings | None = None) -> OrchestrationGraph:
    settings = settings or Settings.load()
    runtime = ExecutionRuntime(settings)
    return OrchestrationGraph(runtime)
