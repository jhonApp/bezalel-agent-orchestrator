from __future__ import annotations

from typing import Any


def ready_tasks(state: dict[str, Any], roles: set[str] | None = None) -> list[dict[str, Any]]:
    completed = {task["task_id"] for task in state.get("plan", []) if task.get("status") == "completed"}
    selected = []
    for task in state.get("plan", []):
        if task.get("status") != "pending" or (roles and task.get("agent") not in roles):
            continue
        if all(dep in completed for dep in task.get("dependencies", [])):
            selected.append(task)
    return selected


def gates_pass(state: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if any(t.get("status") == "failed" for t in state.get("plan", [])):
        reasons.append("one or more tasks failed")
    if any(t.get("status") == "rate_limited" for t in state.get("plan", [])):
        reasons.append("one or more tasks hit the Codex usage limit and never ran")
    if any(c.get("severity") == "blocking" for c in state.get("contracts", [])):
        reasons.append("blocking contract finding")
    if any(t.get("blocking") or t.get("status") == "failed" for t in state.get("test_results", [])):
        reasons.append("mandatory tests failed")
    if any(r.get("blocking") or r.get("status") == "changes_requested" for r in state.get("review_results", [])):
        reasons.append("review requested changes")
    if state.get("security_blocking"):
        reasons.append("security review blocked")
    return not reasons, reasons
