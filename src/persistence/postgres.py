"""Optional PostgreSQL persistence hook for shared environments.

SQLite is the default local checkpointer. Install ``psycopg[binary]`` and
provide ``ORCHESTRATOR_DATABASE_URL`` to enable a PostgreSQL implementation;
the explicit error keeps local installs dependency-light and safe.
"""
from __future__ import annotations

from typing import Any


class PostgresCheckpointer:
    def __init__(self, dsn: str):
        self.dsn = dsn
        try:
            import psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("PostgreSQL requer a dependência opcional psycopg[binary].") from exc
        self._psycopg = psycopg

    def initialize(self) -> None:
        with self._psycopg.connect(self.dsn) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS orchestrator_checkpoints (execution_id TEXT, node TEXT, state JSONB, created_at TIMESTAMPTZ DEFAULT now())")

    def save(self, execution_id: str, state: dict[str, Any], node: str) -> None:
        import json
        with self._psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO orchestrator_checkpoints (execution_id,node,state) VALUES (%s,%s,%s)", (execution_id, node, json.dumps(state)))

