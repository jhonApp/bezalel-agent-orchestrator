from __future__ import annotations

import logging

import pytest

from wake_listener import main as wake_listener_main


_WAKE_ENV_VARS = (
    "WAKE_LISTENER_HOST", "WAKE_LISTENER_PORT", "WAKE_BACKEND_HOST", "WAKE_BACKEND_PORT",
    "WAKE_BACKEND_HEALTH_PATH", "WAKE_BACKEND_START_COMMAND", "WAKE_BACKEND_HEALTH_TIMEOUT_SECONDS",
    "WAKE_IDLE_TIMEOUT_MINUTES", "WAKE_IDLE_CHECK_INTERVAL_SECONDS", "WAKE_LISTENER_TOKEN",
    "WAKE_CORS_ALLOWED_ORIGINS", "WAKE_BACKEND_CWD",
)


def _clear_wake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _WAKE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_cli_refuses_to_start_without_a_token_on_a_non_loopback_host(monkeypatch: pytest.MonkeyPatch):
    _clear_wake_env(monkeypatch)
    monkeypatch.setenv("WAKE_LISTENER_HOST", "0.0.0.0")
    monkeypatch.delenv("WAKE_LISTENER_TOKEN", raising=False)

    def fake_run(*args, **kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = wake_listener_main.cli([])

    assert result == 1


def test_cli_starts_with_a_warning_when_token_is_unset_on_loopback(monkeypatch: pytest.MonkeyPatch, caplog):
    _clear_wake_env(monkeypatch)
    monkeypatch.delenv("WAKE_LISTENER_HOST", raising=False)
    monkeypatch.delenv("WAKE_LISTENER_TOKEN", raising=False)

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_run)

    with caplog.at_level(logging.WARNING):
        result = wake_listener_main.cli([])

    assert len(calls) == 1
    assert result == 0
    assert "WAKE_LISTENER_TOKEN" in caplog.text


def test_cli_starts_normally_when_a_token_is_set(monkeypatch: pytest.MonkeyPatch):
    _clear_wake_env(monkeypatch)
    monkeypatch.setenv("WAKE_LISTENER_TOKEN", "secret123")

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = wake_listener_main.cli([])

    assert len(calls) == 1
    assert result == 0
