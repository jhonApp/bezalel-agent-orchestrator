from __future__ import annotations

from pathlib import Path

from persistence.checkpointer import SQLiteCheckpointer


def _save(store: SQLiteCheckpointer, execution_id: str, project_id: str, status: str, updated_at: str) -> None:
    store.save(execution_id, {
        "execution_id": execution_id, "project_id": project_id, "feature_request": "some feature",
        "status": status, "updated_at": updated_at,
    }, "some_node")


def test_list_resumable_excludes_completed_executions(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    _save(store, "e1", "bezalel", "completed", "2026-09-29T00:00:00+00:00")
    _save(store, "e2", "bezalel", "failed", "2026-09-29T00:01:00+00:00")

    result = store.list_resumable()

    assert [r["execution_id"] for r in result] == ["e2"]


def test_list_resumable_includes_running_and_cancelled_executions_too(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    _save(store, "e-running", "bezalel", "running", "2026-09-29T00:00:00+00:00")
    _save(store, "e-cancelled", "bezalel", "cancelled", "2026-09-29T00:01:00+00:00")
    _save(store, "e-created", "bezalel", "created", "2026-09-29T00:02:00+00:00")

    result = store.list_resumable()

    assert {r["execution_id"] for r in result} == {"e-running", "e-cancelled", "e-created"}


def test_list_resumable_filters_by_project_id(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    _save(store, "e-bezalel", "bezalel", "failed", "2026-09-29T00:00:00+00:00")
    _save(store, "e-other", "other-project", "failed", "2026-09-29T00:01:00+00:00")

    result = store.list_resumable(project_id="bezalel")

    assert [r["execution_id"] for r in result] == ["e-bezalel"]


def test_list_resumable_orders_most_recently_updated_first(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    _save(store, "e-old", "bezalel", "failed", "2026-09-29T00:00:00+00:00")
    _save(store, "e-new", "bezalel", "failed", "2026-09-29T01:00:00+00:00")

    result = store.list_resumable()

    assert [r["execution_id"] for r in result] == ["e-new", "e-old"]


def test_list_resumable_respects_the_limit(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")
    for i in range(5):
        _save(store, f"e{i}", "bezalel", "failed", f"2026-09-29T00:0{i}:00+00:00")

    result = store.list_resumable(limit=2)

    assert len(result) == 2
    assert [r["execution_id"] for r in result] == ["e4", "e3"]
