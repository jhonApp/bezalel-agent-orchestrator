from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from orchestrator.config import Settings
from persistence.checkpointer import SQLiteCheckpointer


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path / "frontend",
        backend_path=tmp_path / "backend", python_path=tmp_path / "python",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def _save(store: SQLiteCheckpointer, execution_id: str, project_id: str, status: str, updated_at: str) -> None:
    store.save(execution_id, {
        "execution_id": execution_id, "project_id": project_id, "feature_request": "some feature",
        "status": status, "updated_at": updated_at,
    }, "some_node")


def test_get_executions_resumable_excludes_completed_executions(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    store = SQLiteCheckpointer(settings.checkpoint_path)
    _save(store, "e-done", "bezalel", "completed", "2026-09-29T00:00:00+00:00")
    _save(store, "e-stuck", "bezalel", "failed", "2026-09-29T00:01:00+00:00")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/executions/resumable")

    assert response.status_code == 200
    ids = [row["execution_id"] for row in response.json()]
    assert ids == ["e-stuck"]


def test_get_executions_resumable_filters_by_project_id(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    store = SQLiteCheckpointer(settings.checkpoint_path)
    _save(store, "e-bezalel", "bezalel", "failed", "2026-09-29T00:00:00+00:00")
    _save(store, "e-other", "other-project", "failed", "2026-09-29T00:01:00+00:00")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/executions/resumable", params={"project_id": "bezalel"})

    ids = [row["execution_id"] for row in response.json()]
    assert ids == ["e-bezalel"]


def test_get_executions_resumable_path_is_not_swallowed_by_the_execution_id_route(tmp_path: Path) -> None:
    """/executions/resumable must resolve to the resumable-list route, not be interpreted
    as /executions/{execution_id} with execution_id="resumable" (which would 404)."""
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.get("/executions/resumable")

    assert response.status_code == 200
    assert response.json() == []
