from __future__ import annotations

from pathlib import Path

from persistence.checkpointer import SQLiteCheckpointer


def test_get_role_cli_overrides_is_empty_before_any_write(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    assert store.get_role_cli_overrides() == {}


def test_set_then_get_round_trips_an_override(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")

    assert store.get_role_cli_overrides() == {"frontend": {"cli": "claude_code", "fallback_cli": "codex"}}


def test_set_role_cli_override_accepts_a_null_fallback(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)

    assert store.get_role_cli_overrides()["frontend"] == {"cli": "claude_code", "fallback_cli": None}


def test_setting_the_same_role_twice_overwrites_rather_than_duplicates(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", "codex")
    store.set_role_cli_override("frontend", "codex", None)

    overrides = store.get_role_cli_overrides()
    assert overrides == {"frontend": {"cli": "codex", "fallback_cli": None}}


def test_overrides_for_different_roles_do_not_interfere(tmp_path: Path) -> None:
    store = SQLiteCheckpointer(tmp_path / "checkpoints.sqlite3")

    store.set_role_cli_override("frontend", "claude_code", None)
    store.set_role_cli_override("backend", "codex", "claude_code")

    overrides = store.get_role_cli_overrides()
    assert overrides == {
        "frontend": {"cli": "claude_code", "fallback_cli": None},
        "backend": {"cli": "codex", "fallback_cli": "claude_code"},
    }
