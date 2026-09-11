from __future__ import annotations

from pathlib import Path

from persistence.checkpointer import SQLiteCheckpointer


def test_persisted_agent_metrics_support_dashboard_data(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    store.agent_run("exec-1", "frontend", "T001", {
        "status": "completed", "tokens_input": 1000, "tokens_output": 100,
        "estimated_cost": 0.0, "duration_seconds": 12,
    }, "2026-01-01T00:00:00+00:00")
    metrics = store.agent_metrics()

    assert metrics[0]["agent"] == "frontend"
    assert metrics[0]["tokens_input"] == 1000
    assert metrics[0]["tokens_output"] == 100
    assert metrics[0]["completed"] == 1
