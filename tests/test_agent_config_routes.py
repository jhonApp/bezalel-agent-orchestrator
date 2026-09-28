from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from orchestrator.config import Settings


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path / "frontend",
        backend_path=tmp_path / "backend", python_path=tmp_path / "python",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite3",
        langgraph_checkpoint_sqlite_path=tmp_path / "langgraph.sqlite3",
    )


def test_get_agents_config_returns_code_defaults_when_nothing_is_overridden(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.get("/agents/config")

    assert response.status_code == 200
    body = response.json()
    assert set(body["available"]) == {"codex", "claude_code"}
    assert body["roles"]["frontend"] == {
        "default_cli": "codex", "default_fallback_cli": None,
        "cli": "codex", "fallback_cli": None, "overridden": False,
    }


def test_put_then_get_reflects_the_override(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        put_response = client.put("/agents/frontend/config", json={"cli": "claude_code", "fallback_cli": "codex"})
        get_response = client.get("/agents/config")

    assert put_response.status_code == 200
    assert put_response.json() == {"role": "frontend", "cli": "claude_code", "fallback_cli": "codex"}
    assert get_response.json()["roles"]["frontend"] == {
        "default_cli": "codex", "default_fallback_cli": None,
        "cli": "claude_code", "fallback_cli": "codex", "overridden": True,
    }


def test_put_agent_config_rejects_an_unknown_role(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/not-a-role/config", json={"cli": "codex"})

    assert response.status_code == 404


def test_put_agent_config_rejects_an_unknown_cli_and_persists_nothing(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/frontend/config", json={"cli": "ghost_cli"})
        get_response = client.get("/agents/config")

    assert response.status_code == 400
    assert get_response.json()["roles"]["frontend"]["overridden"] is False


def test_put_agent_config_rejects_an_unknown_fallback_cli(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path))
    with TestClient(app) as client:
        response = client.put("/agents/frontend/config", json={"cli": "codex", "fallback_cli": "ghost_cli"})

    assert response.status_code == 400
