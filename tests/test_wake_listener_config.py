from __future__ import annotations

from wake_listener.config import WakeListenerSettings


def test_load_uses_defaults_when_no_env_vars_are_set(monkeypatch):
    for name in ("WAKE_LISTENER_HOST", "WAKE_LISTENER_PORT", "WAKE_BACKEND_HOST", "WAKE_BACKEND_PORT",
                "WAKE_BACKEND_HEALTH_PATH", "WAKE_BACKEND_START_COMMAND", "WAKE_IDLE_TIMEOUT_MINUTES",
                "WAKE_IDLE_CHECK_INTERVAL_SECONDS", "WAKE_LISTENER_TOKEN", "WAKE_CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)

    settings = WakeListenerSettings.load()

    assert settings.listen_host == "127.0.0.1"
    assert settings.listen_port == 8090
    assert settings.backend_base_url == "http://127.0.0.1:8000"
    assert settings.health_url == "http://127.0.0.1:8000/dashboard-data"
    assert settings.backend_start_command == ["bezalel-orchestrator", "api"]
    assert settings.idle_timeout_seconds == 1200.0
    assert settings.auth_token is None
    assert settings.cors_allowed_origins == ["https://vercel-deploy-orchestrator.vercel.app"]


def test_load_reads_every_override_from_env(monkeypatch):
    monkeypatch.setenv("WAKE_LISTENER_PORT", "9090")
    monkeypatch.setenv("WAKE_BACKEND_PORT", "8001")
    monkeypatch.setenv("WAKE_BACKEND_HEALTH_PATH", "/health")
    monkeypatch.setenv("WAKE_BACKEND_START_COMMAND", "python,-m,orchestrator")
    monkeypatch.setenv("WAKE_IDLE_TIMEOUT_MINUTES", "5")
    monkeypatch.setenv("WAKE_IDLE_CHECK_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("WAKE_LISTENER_TOKEN", "secret123")
    monkeypatch.setenv("WAKE_CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")

    settings = WakeListenerSettings.load()

    assert settings.listen_port == 9090
    assert settings.backend_base_url == "http://127.0.0.1:8001"
    assert settings.health_url == "http://127.0.0.1:8001/health"
    assert settings.backend_start_command == ["python", "-m", "orchestrator"]
    assert settings.idle_timeout_seconds == 300.0
    assert settings.idle_check_interval_seconds == 10.0
    assert settings.auth_token == "secret123"
    assert settings.cors_allowed_origins == ["https://a.example", "https://b.example"]
