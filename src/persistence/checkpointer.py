from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SQLiteCheckpointer:
    """Durable local execution/checkpoint store.

    SQLite is the development backend. The schema is intentionally portable so
    a PostgreSQL adapter can replace the connection layer without changing graph state.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS executions (
              execution_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
              feature_request TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
              node TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_execution ON checkpoints(execution_id, id);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
              event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
              agent TEXT NOT NULL, task_id TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)

    def save(self, execution_id: str, state: dict[str, Any], node: str) -> None:
        now = state.get("updated_at", "")
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO executions VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM executions WHERE execution_id=?), ?), ?)",
                       (execution_id, state.get("project_id", ""), state.get("feature_request", ""), state.get("status", "unknown"), execution_id, now, now))
            db.execute("INSERT INTO checkpoints(execution_id,node,state_json,created_at) VALUES(?,?,?,?)",
                       (execution_id, node, json.dumps(state, ensure_ascii=False, default=str), now))

    def load(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT state_json FROM checkpoints WHERE execution_id=? ORDER BY id DESC LIMIT 1", (execution_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM executions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def event(self, execution_id: str, event_type: str, payload: dict[str, Any], created_at: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO events(execution_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                       (execution_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), created_at))

    def logs(self, execution_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT event_type,payload_json,created_at FROM events WHERE execution_id=? ORDER BY id LIMIT ?", (execution_id, limit)).fetchall()
        return [{"event_type": row["event_type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in rows]

    def active_agents(self) -> list[dict[str, Any]]:
        """Return agents whose latest durable lifecycle event is ``started``."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT execution_id, event_type, payload_json, created_at FROM events "
                "WHERE event_type IN ('agent.started', 'agent.finished') ORDER BY id"
            ).fetchall()
        active: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            agent = payload.get("agent")
            if not agent:
                continue
            key = (row["execution_id"], agent)
            if row["event_type"] == "agent.started":
                active[key] = {"execution_id": row["execution_id"], "agent": agent, "task_id": payload.get("task_id"), "started_at": row["created_at"]}
            else:
                active.pop(key, None)
        return list(active.values())

    def finish_active_agents(self, execution_id: str, status: str = "cancelled", summary: str | None = None) -> None:
        """Close durable agent lifecycle entries when an execution is cancelled."""
        now = datetime.now(timezone.utc).isoformat()
        summary = summary or "execution cancelled"
        for active in self.active_agents():
            if active["execution_id"] != execution_id:
                continue
            payload = {
                "type": "agent.finished",
                "execution_id": execution_id,
                "agent": active["agent"],
                "task_id": active.get("task_id"),
                "status": status,
                "result": {"status": status, "summary": summary},
            }
            self.event(execution_id, "agent.finished", payload, now)

    def reconcile_interrupted_executions(self, summary: str = "orchestrator restarted while this agent was running") -> list[str]:
        """Close out agents left dangling ``started`` by a process that died mid-flight.

        ``active_agents()`` only knows about durable started/finished events — a crash or a
        manual restart between the two leaves a phantom "running" agent forever, which is
        exactly what the dashboard's live-agent overlay reads. Call this once at API startup,
        before anything reads the store, so a restart self-heals instead of accumulating ghosts.
        """
        stale_execution_ids = sorted({active["execution_id"] for active in self.active_agents()})
        for execution_id in stale_execution_ids:
            self.finish_active_agents(execution_id, status="interrupted", summary=summary)

        # A process can also die between graph nodes, with no agent mid-flight at all — the
        # durable "running" status itself is then just as stuck, and the dashboard's summary
        # tile and Executions list read that status directly, independent of active_agents().
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            rows = db.execute("SELECT execution_id FROM executions WHERE status = 'running'").fetchall()
        for execution_id in {row["execution_id"] for row in rows}:
            state = self.load(execution_id)
            if not state or state.get("status") != "running":
                continue
            state = dict(state)
            state["status"] = "failed"
            state["next_action"] = "done"
            state["updated_at"] = now
            errors = list(state.get("errors") or [])
            if summary not in errors:
                errors.append(summary)
            state["errors"] = errors
            self.save(execution_id, state, "startup_reconcile")
        return stale_execution_ids

    def agent_run(self, execution_id: str, agent: str, task_id: str | None, result: dict[str, Any], created_at: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO agent_runs(execution_id,agent,task_id,result_json,created_at) VALUES(?,?,?,?,?)",
                       (execution_id, agent, task_id, json.dumps(result, ensure_ascii=False, default=str), created_at))

    def agent_history(self, execution_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT agent,task_id,result_json,created_at FROM agent_runs WHERE execution_id=? ORDER BY id", (execution_id,)).fetchall()
        return [{"agent": row["agent"], "task_id": row["task_id"], "result": json.loads(row["result_json"]), "created_at": row["created_at"]} for row in rows]

    def agent_metrics(self) -> list[dict[str, Any]]:
        """Return durable, presentation-ready agent totals across executions."""
        with self._connect() as db:
            rows = db.execute("SELECT agent, result_json FROM agent_runs ORDER BY id").fetchall()

        metrics: dict[str, dict[str, Any]] = {}
        for row in rows:
            result = json.loads(row["result_json"])
            item = metrics.setdefault(row["agent"], {
                "agent": row["agent"], "runs": 0, "completed": 0, "failed": 0,
                "blocked": 0, "tokens_input": 0, "tokens_output": 0,
                "estimated_cost": 0.0, "duration_seconds": 0.0,
            })
            item["runs"] += 1
            status = str(result.get("status", ""))
            if status in {"completed", "failed", "blocked"}:
                item[status] += 1
            item["tokens_input"] += int(result.get("tokens_input") or 0)
            item["tokens_output"] += int(result.get("tokens_output") or 0)
            item["estimated_cost"] += float(result.get("estimated_cost") or 0)
            item["duration_seconds"] += float(result.get("duration_seconds") or 0)
        return list(metrics.values())
