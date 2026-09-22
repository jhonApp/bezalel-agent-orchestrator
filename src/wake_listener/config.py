from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


DEFAULT_CORS_ORIGIN = "https://vercel-deploy-orchestrator.vercel.app"


class WakeListenerSettings(BaseModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = 8090
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    backend_health_path: str = "/dashboard-data"
    backend_start_command: list[str] = Field(default_factory=lambda: ["bezalel-orchestrator", "api"])
    backend_health_timeout_seconds: float = 30.0
    idle_timeout_minutes: float = 20.0
    idle_check_interval_seconds: float = 60.0
    auth_token: str | None = None
    cors_allowed_origins: list[str] = Field(default_factory=lambda: [DEFAULT_CORS_ORIGIN])
    backend_cwd: str = Field(default_factory=lambda: str(Path(__file__).resolve().parents[2]))

    @property
    def backend_base_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"

    @property
    def health_url(self) -> str:
        return self.backend_base_url + self.backend_health_path

    @property
    def idle_timeout_seconds(self) -> float:
        return self.idle_timeout_minutes * 60

    @classmethod
    def load(cls) -> "WakeListenerSettings":
        command = os.getenv("WAKE_BACKEND_START_COMMAND", "").strip()
        origins = os.getenv("WAKE_CORS_ALLOWED_ORIGINS", "").strip()
        return cls(
            listen_host=os.getenv("WAKE_LISTENER_HOST", "127.0.0.1"),
            listen_port=_int("WAKE_LISTENER_PORT", 8090),
            backend_host=os.getenv("WAKE_BACKEND_HOST", "127.0.0.1"),
            backend_port=_int("WAKE_BACKEND_PORT", 8000),
            backend_health_path=os.getenv("WAKE_BACKEND_HEALTH_PATH", "/dashboard-data"),
            backend_start_command=command.split(",") if command else ["bezalel-orchestrator", "api"],
            backend_health_timeout_seconds=_float("WAKE_BACKEND_HEALTH_TIMEOUT_SECONDS", 30.0),
            idle_timeout_minutes=_float("WAKE_IDLE_TIMEOUT_MINUTES", 20.0),
            idle_check_interval_seconds=_float("WAKE_IDLE_CHECK_INTERVAL_SECONDS", 60.0),
            auth_token=(os.getenv("WAKE_LISTENER_TOKEN", "").strip() or None),
            cors_allowed_origins=origins.split(",") if origins else [DEFAULT_CORS_ORIGIN],
            backend_cwd=os.getenv("WAKE_BACKEND_CWD", "").strip() or str(Path(__file__).resolve().parents[2]),
        )
