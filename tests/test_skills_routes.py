from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import api.app as app_module
from api.app import create_app
from orchestrator.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path / "frontend",
        backend_path=tmp_path / "backend", python_path=tmp_path / "python",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_get_project_skills_lists_installed_skills(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    seen_path = {}

    async def fake_list_skills(project_path, **kwargs):
        seen_path["path"] = project_path
        return {"skills": [{"name": "frontend-design", "source": "anthropics/skills"}], "error": None}

    monkeypatch.setattr(app_module, "list_skills", fake_list_skills)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/projects/frontend/skills")

    assert response.status_code == 200
    assert response.json() == {"project_id": "frontend", "skills": [{"name": "frontend-design", "source": "anthropics/skills"}], "error": None}
    assert seen_path["path"] == settings.frontend_path


def test_get_project_skills_rejects_unknown_project(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.get("/projects/not-a-project/skills")

    assert response.status_code == 404


def test_post_project_skills_installs_and_returns_result(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    calls = {}

    async def fake_install_skill(project_path, package, skill=None, **kwargs):
        calls["args"] = (project_path, package, skill)
        return {"ok": True, "result": {"installed": [skill]}}

    monkeypatch.setattr(app_module, "install_skill", fake_install_skill)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/projects/frontend/skills", json={"package": "anthropics/skills", "skill": "frontend-design"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": {"installed": ["frontend-design"]}}
    assert calls["args"] == (settings.frontend_path, "anthropics/skills", "frontend-design")


def test_delete_project_skill_removes_it(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    calls = {}

    async def fake_remove_skill(project_path, skill, **kwargs):
        calls["args"] = (project_path, skill)
        return {"ok": True, "output": "Removed frontend-design"}

    monkeypatch.setattr(app_module, "remove_skill", fake_remove_skill)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.delete("/projects/frontend/skills/frontend-design")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "output": "Removed frontend-design"}
    assert calls["args"] == (settings.frontend_path, "frontend-design")


def test_delete_project_skill_rejects_unknown_project(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.delete("/projects/not-a-project/skills/frontend-design")

    assert response.status_code == 404
