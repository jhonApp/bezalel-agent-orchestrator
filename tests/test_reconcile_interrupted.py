from __future__ import annotations

from pathlib import Path

import pytest

from persistence.checkpointer import SQLiteCheckpointer


def test_finish_active_agents_accepts_a_custom_summary(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    store.event("exec-1", "agent.started", {"agent": "frontend", "task_id": "T001"}, "t1")

    store.finish_active_agents("exec-1", status="interrupted", summary="orchestrator restarted mid-task")

    assert store.active_agents() == []
    events = store.logs("exec-1")
    finished = next(e for e in events if e["event_type"] == "agent.finished")
    assert finished["payload"]["status"] == "interrupted"
    assert finished["payload"]["result"]["summary"] == "orchestrator restarted mid-task"


def test_reconcile_interrupted_executions_closes_orphaned_started_events(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    # Simulates a process killed between "agent.started" and "agent.finished" for a real run,
    # plus one execution that finished normally (must be left untouched).
    store.event("exec-orphaned", "agent.started", {"agent": "frontend", "task_id": "T001"}, "t1")
    store.event("exec-orphaned", "agent.started", {"agent": "backend", "task_id": "T002"}, "t1")
    store.event("exec-clean", "agent.started", {"agent": "python_ai", "task_id": "T003"}, "t2")
    store.event("exec-clean", "agent.finished", {"agent": "python_ai", "task_id": "T003", "status": "completed"}, "t3")

    assert {a["agent"] for a in store.active_agents()} == {"frontend", "backend"}

    reconciled = store.reconcile_interrupted_executions()

    assert reconciled == ["exec-orphaned"]
    assert store.active_agents() == []


def test_reconcile_interrupted_executions_is_a_no_op_when_nothing_is_stale(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    store.event("exec-clean", "agent.started", {"agent": "python_ai", "task_id": "T003"}, "t2")
    store.event("exec-clean", "agent.finished", {"agent": "python_ai", "task_id": "T003", "status": "completed"}, "t3")

    assert store.reconcile_interrupted_executions() == []
    assert store.active_agents() == []


def test_reconcile_marks_a_durably_stuck_running_execution_as_failed(tmp_path: Path) -> None:
    """No agent is mid-flight (process died between graph nodes), but the last saved
    checkpoint still says status="running" — the summary tile's "em andamento" counter
    and the Executions list both read this durable status directly, so it must be closed
    too, not just the per-agent active_agents() tracking."""
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    store.save("exec-stuck", {
        "execution_id": "exec-stuck", "project_id": "bezalel", "feature_request": "add a button",
        "status": "running", "errors": [], "updated_at": "2026-01-01T00:00:00+00:00",
    }, "dispatch_agents")
    store.save("exec-done", {
        "execution_id": "exec-done", "project_id": "bezalel", "feature_request": "add a button",
        "status": "completed", "errors": [], "updated_at": "2026-01-01T00:00:00+00:00",
    }, "generate_final_report")

    store.reconcile_interrupted_executions()

    assert store.load("exec-stuck")["status"] == "failed"
    assert "orchestrator restarted while this agent was running" in store.load("exec-stuck")["errors"]
    assert store.load("exec-done")["status"] == "completed"
