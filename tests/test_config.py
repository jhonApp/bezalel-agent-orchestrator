from __future__ import annotations

import os
from pathlib import Path

from orchestrator.config import Settings


def test_codex_sandbox_defaults_to_full_access_on_windows(monkeypatch, tmp_path: Path) -> None:
    """workspace-write rejects even read-only commands (git status, ls) on this Codex CLI's
    Windows sandbox implementation — verified by hand against a real worktree. Defaulting to
    danger-full-access on Windows matches what the working `--dangerously-bypass-approvals-and-
    sandbox` interactive sessions already use; git worktree isolation is the real containment."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)

    settings = Settings.load(tmp_path)

    assert settings.codex_sandbox == "danger-full-access"


def test_codex_sandbox_keeps_workspace_write_off_windows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)

    settings = Settings.load(tmp_path)

    assert settings.codex_sandbox == "workspace-write"


def test_codex_sandbox_env_override_wins_even_on_windows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")

    settings = Settings.load(tmp_path)

    assert settings.codex_sandbox == "workspace-write"
