from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import empty_checkpoint

from adapters.codex_cli import resolve_codex_command
from orchestrator.config import Settings
from observability.langsmith import LangSmithObserver
from persistence.checkpointer import SQLiteCheckpointer
from schemas.models import utc_now


def _state(execution_id: str, feature: str, project: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "execution_id": execution_id, "thread_id": execution_id,
        "project_id": project, "feature_request": feature,
        "status": "running", "active_agent": "codex-yolo", "active_task": "CODEX-YOLO",
        "plan": [{"task_id": "CODEX-YOLO", "agent": "codex-yolo", "project_id": project,
                  "description": feature, "status": "running", "result": {}}],
        "started_at": now, "updated_at": now, "next_action": "codex-yolo",
        "errors": [], "approvals": {"yolo": True}, "context_health": {},
    }


async def _checkpoint(saver: Any, state: dict[str, Any], execution_id: str) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = state
    await saver.aput(
        {"configurable": {"thread_id": execution_id, "checkpoint_ns": ""}}, checkpoint,
        {"source": "input", "step": -1, "writes": {}, "parents": {}}, {},
    )


async def run_codex_yolo(settings: Settings, feature: str, project: str = "bezalel",
                         cwd: Path | None = None, timeout: int | None = None) -> dict[str, Any]:
    """Run a non-interactive Codex (yolo-equivalent) process with durable dashboard records."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    execution_id = uuid.uuid4().hex
    workdir = (cwd or settings.workspace_root).resolve()
    timeout = timeout or settings.agent_timeout_seconds
    store = SQLiteCheckpointer(settings.checkpoint_path)
    state = _state(execution_id, feature, project)
    store.save(execution_id, state, "codex_yolo.started")
    started = {"type": "agent.started", "execution_id": execution_id, "agent": "codex-yolo",
               "task_id": "CODEX-YOLO", "description": feature, "status": "running", "thread_id": execution_id}
    store.event(execution_id, "agent.started", started, state["updated_at"])
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish the running state before spawning Codex so the dashboard can see it.
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        await _checkpoint(saver, state, execution_id)
    command = resolve_codex_command(settings.codex_command) + [
        "exec", "--ephemeral", "--cd", str(workdir), "--skip-git-repo-check",
        "--ignore-user-config", "-c", 'approval_policy="never"',
        "-c", "features.plugin_hooks=true",
        "-c", 'plugins."tracing@langsmith-codex-plugins".enabled=true',
        "-s", "danger-full-access", "-",
    ]
    started_at = time.perf_counter()
    stdout = stderr = ""
    returncode: int | None = None
    error: str | None = None
    try:
        with LangSmithObserver(settings).codex_session(
            "orchestrator.codex_yolo", {"feature": feature},
            {"orchestrator_execution_id": execution_id, "orchestrator_project_id": project, "agent": "codex-yolo"},
        ) as environment:
            process = await asyncio.create_subprocess_exec(*command, cwd=str(workdir), env=environment,
                                                            stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(process.communicate(feature.encode("utf-8")), timeout=timeout)
                stdout, stderr, returncode = out.decode(errors="replace"), err.decode(errors="replace"), process.returncode
            except asyncio.TimeoutError:
                process.kill()
                out, err = await process.communicate()
                stdout, stderr, returncode, error = out.decode(errors="replace"), err.decode(errors="replace"), process.returncode, "Codex timeout"
    except OSError as exc:
        error = str(exc)
    duration = time.perf_counter() - started_at
    status = "completed" if returncode == 0 and not error else "failed"
    result = {"agent": "codex-yolo", "status": status, "summary": "Codex yolo execution finished" if status == "completed" else (error or "Codex failed"),
              "files_changed": [], "errors": ([error] if error else []) + ([stderr[-2000:]] if stderr and status == "failed" else []),
              "duration_seconds": duration, "returncode": returncode, "stdout": stdout[-settings.max_output_chars:], "stderr": stderr[-settings.max_output_chars:]}
    now = utc_now()
    state.update({"status": status, "active_agent": None, "active_task": None, "updated_at": now,
                  "next_action": "done", "final_report": result})
    state["plan"][0].update({"status": status, "result": result})
    store.save(execution_id, state, "codex_yolo.finished")
    store.agent_run(execution_id, "codex-yolo", "CODEX-YOLO", result, now)
    finished = {"type": "agent.finished", "execution_id": execution_id, "agent": "codex-yolo",
                "task_id": "CODEX-YOLO", "status": status, "result": result, "thread_id": execution_id}
    store.event(execution_id, "agent.finished", finished, now)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        await _checkpoint(saver, state, execution_id)
    return {"execution_id": execution_id, "thread_id": execution_id, **result}


async def run_codex_interactive_yolo(settings: Settings, project: str = "bezalel",
                                     cwd: Path | None = None) -> dict[str, Any]:
    """Open an interactive ``codex --yolo`` session tracked by the dashboard."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    execution_id = uuid.uuid4().hex
    workdir = (cwd or settings.workspace_root).resolve()
    feature = "Sessão interativa do Codex --yolo"
    store = SQLiteCheckpointer(settings.checkpoint_path)
    state = _state(execution_id, feature, project)
    store.save(execution_id, state, "codex_yolo.interactive_started")
    started = {"type": "agent.started", "execution_id": execution_id, "agent": "codex-yolo",
               "task_id": "CODEX-YOLO", "description": feature, "status": "running", "thread_id": execution_id}
    store.event(execution_id, "agent.started", started, state["updated_at"])
    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        await _checkpoint(saver, state, execution_id)

    returncode: int | None = None
    error: str | None = None
    started_at = time.perf_counter()
    try:
        # Do not capture stdio: the user gets Codex's normal interactive UI
        # and can submit as many prompts as desired in the same session.
        with LangSmithObserver(settings).codex_session(
            "orchestrator.codex_interactive_yolo", {"feature": feature},
            {"orchestrator_execution_id": execution_id, "orchestrator_project_id": project, "agent": "codex-yolo"},
        ) as environment:
            process = await asyncio.create_subprocess_exec(
                *resolve_codex_command(settings.codex_command), "--yolo",
                "-c", "features.plugin_hooks=true",
                "-c", 'plugins."tracing@langsmith-codex-plugins".enabled=true',
                cwd=str(workdir), env=environment,
            )
            returncode = await process.wait()
    except OSError as exc:
        error = str(exc)

    duration = time.perf_counter() - started_at
    status = "completed" if returncode == 0 and not error else "failed"
    result = {
        "agent": "codex-yolo", "status": status,
        "summary": "Sessão interativa do Codex encerrada" if status == "completed" else (error or "Codex encerrou com falha"),
        "files_changed": [], "errors": [error] if error else [],
        "duration_seconds": duration, "returncode": returncode,
    }
    now = utc_now()
    state.update({"status": status, "active_agent": None, "active_task": None, "updated_at": now,
                  "next_action": "done", "final_report": result})
    state["plan"][0].update({"status": status, "result": result})
    store.save(execution_id, state, "codex_yolo.interactive_finished")
    store.agent_run(execution_id, "codex-yolo", "CODEX-YOLO", result, now)
    finished = {"type": "agent.finished", "execution_id": execution_id, "agent": "codex-yolo",
                "task_id": "CODEX-YOLO", "status": status, "result": result, "thread_id": execution_id}
    store.event(execution_id, "agent.finished", finished, now)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        await _checkpoint(saver, state, execution_id)
    return {"execution_id": execution_id, "thread_id": execution_id, **result}
