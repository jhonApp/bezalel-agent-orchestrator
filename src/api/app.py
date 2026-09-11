from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - exercised only before optional install
    FastAPI = None
    HTTPException = RuntimeError
    HTMLResponse = str
    StreamingResponse = None
    StaticFiles = None
    BaseModel = object

from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
from agents.registry import AGENT_ROLES
from observability.live_events import LiveEventBroker
from schemas.models import ExecutionRequest


class ProfileAssetRequest(BaseModel):
    """Small JSON contract used by the dashboard avatar uploader."""

    id: str
    data_url: str
    content_type: str


_PROFILE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
_PROFILE_DATA_URL = re.compile(r"^data:(?P<type>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$", re.I)
# Leave room for the base64/JSON envelope under Vercel's function body limit.
_PROFILE_MAX_BYTES = 3 * 1024 * 1024


def _upload_profile_blob(asset: ProfileAssetRequest, payload: bytes) -> dict[str, Any]:
    """Upload an avatar with the same server API used by ``@vercel/blob``.

    Keeping this call server-side means ``BLOB_READ_WRITE_TOKEN`` is never sent
    to the browser.  The standard-library client also keeps the Python runtime
    lightweight for Vercel Functions.
    """
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not configured")

    # Read-write tokens have the form vercel_blob_rw_<store-id>_<secret>.
    token_parts = token.split("_")
    store_id = token_parts[3] if len(token_parts) > 3 else ""
    if not store_id:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN has an invalid store id")

    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", asset.id).strip("-")[:80] or "agent"
    extension = asset.content_type.split("/", 1)[1].replace("jpeg", "jpg")
    pathname = f"profiles/{safe_id}.{extension}"
    query = urllib.parse.urlencode({"pathname": pathname})
    request = urllib.request.Request(
        f"https://vercel.com/api/blob/?{query}",
        data=payload,
        method="PUT",
        headers={
            "authorization": f"Bearer {token}",
            "x-vercel-blob-store-id": store_id,
            "x-api-version": "12",
            "x-api-blob-request-id": f"{store_id}:{os.urandom(8).hex()}",
            "x-api-blob-request-attempt": "0",
            "x-vercel-blob-access": "public",
            "x-content-type": asset.content_type,
            "x-add-random-suffix": "1",
            "content-type": asset.content_type,
            "content-length": str(len(payload)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not include response headers/body: they may contain token metadata.
        raise RuntimeError(f"Vercel Blob upload failed ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Vercel Blob upload failed") from exc

    if not isinstance(result, dict) or not result.get("url"):
        raise RuntimeError("Vercel Blob returned an invalid upload response")
    return {"url": result["url"], "pathname": result.get("pathname", pathname), "content_type": asset.content_type}

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:  # pragma: no cover - dependency validation happens at startup
    AsyncSqliteSaver = None


class ExecutionManager:
    def __init__(self, graph: OrchestrationGraph):
        self.graph = graph
        self.tasks: dict[str, asyncio.Task] = {}

    async def create(self, request: ExecutionRequest) -> dict[str, Any]:
        execution_id = request.execution_id or __import__("uuid").uuid4().hex
        self.tasks[execution_id] = asyncio.create_task(self.graph.run(request, execution_id))
        return {"execution_id": execution_id, "status": "started"}

    def state(self, execution_id: str) -> dict[str, Any]:
        state = self.graph.runtime.store.load(execution_id)
        if not state:
            raise KeyError(execution_id)
        return state


def create_app(settings: Settings | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed. Run pip install -e '.[dev]'.")
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: Any):
        if AsyncSqliteSaver is None:
            raise RuntimeError("langgraph-checkpoint-sqlite is not installed.")
        settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
            await saver.setup()
            broker = LiveEventBroker()
            graph = OrchestrationGraph(
                __import__("orchestrator.nodes", fromlist=["ExecutionRuntime"]).ExecutionRuntime(settings, event_sink=broker.publish),
                checkpointer=saver,
            )
            app.state.graph = graph
            app.state.manager = ExecutionManager(graph)
            app.state.event_broker = broker
            yield

    app = FastAPI(title="Bezalel Agent Orchestrator", version="0.1.0", lifespan=lifespan)
    design_root = settings.agent_platform_design_root
    design_file = next(design_root.glob("*.dc.html"), None) if design_root and design_root.is_dir() else None

    if design_root and design_root.is_dir() and StaticFiles:
        app.mount("/agent-platform-assets", StaticFiles(directory=str(design_root)), name="agent-platform-assets")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "project": settings.langsmith_project, "checkpoint": str(settings.checkpoint_path)}

    @app.post("/profile-assets")
    async def profile_asset(request: ProfileAssetRequest) -> dict[str, Any]:
        """Persist a dashboard avatar in the public Vercel Blob store."""
        content_type = request.content_type.strip().lower()
        match = _PROFILE_DATA_URL.fullmatch(request.data_url.strip())
        if not match or match.group("type").lower() != content_type:
            raise HTTPException(status_code=400, detail="data_url must be a base64 image matching content_type")
        if content_type not in _PROFILE_CONTENT_TYPES:
            raise HTTPException(status_code=415, detail="unsupported avatar image type")
        try:
            payload = base64.b64decode(match.group("data"), validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=400, detail="data_url contains invalid base64")
        if not payload or len(payload) > _PROFILE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="avatar must be between 1 byte and 3 MB")

        # Basic magic-byte validation prevents uploading arbitrary files with
        # an image MIME type.  The Blob store remains the source of the URL.
        signatures = {
            "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
            "image/webp": payload.startswith(b"RIFF") and payload[8:12] == b"WEBP",
            "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
            "image/avif": b"ftypavif" in payload[:32] or b"ftypavis" in payload[:32],
        }
        if not signatures[content_type]:
            raise HTTPException(status_code=400, detail="data_url bytes do not match content_type")
        try:
            result = await asyncio.to_thread(_upload_profile_blob, request, payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return result

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> Any:
        if design_file and design_file.is_file():
            content = design_file.read_text(encoding="utf-8", errors="replace")
            content = content.replace("<head>", '<head><base href="/agent-platform-assets/">', 1)
            return HTMLResponse(content=content)
        rows = "".join(f"<li><a href='/executions/{x['execution_id']}'>{x['execution_id']}</a> — {x['status']}</li>" for x in settings_store().list())
        return f"<html><body><h1>Bezalel Agent Orchestrator</h1><ul>{rows}</ul></body></html>"

    def settings_store():
        return app.state.graph.runtime.store

    def manager() -> ExecutionManager:
        return app.state.manager

    async def langgraph_states() -> dict[str, dict[str, Any]]:
        """Return the most recent native LangGraph state for each thread."""
        latest: dict[str, dict[str, Any]] = {}
        async for checkpoint in app.state.graph.checkpointer.alist(None):
            thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
            if thread_id and thread_id not in latest:
                latest[thread_id] = dict(checkpoint.checkpoint.get("channel_values", {}))
        return latest

    def langgraph_agent_metrics(states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate agent outcomes embedded in persisted LangGraph state."""
        metrics: dict[str, dict[str, Any]] = {}
        for state in states.values():
            for task in state.get("plan", []):
                agent = task.get("agent")
                result = task.get("result") or {}
                if not agent or not isinstance(result, dict):
                    continue
                item = metrics.setdefault(agent, {
                    "agent": agent, "runs": 0, "completed": 0, "failed": 0, "blocked": 0,
                    "tokens_input": 0, "tokens_output": 0, "estimated_cost": 0.0,
                    "duration_seconds": 0.0,
                })
                item["runs"] += 1
                status = str(result.get("status", task.get("status", "")))
                if status in {"completed", "failed", "blocked"}:
                    item[status] += 1
                item["tokens_input"] += int(result.get("tokens_input") or 0)
                item["tokens_output"] += int(result.get("tokens_output") or 0)
                item["estimated_cost"] += float(result.get("estimated_cost") or 0)
                item["duration_seconds"] += float(result.get("duration_seconds") or 0)
        return sorted(metrics.values(), key=lambda item: item["agent"])

    @app.get("/dashboard-data")
    async def dashboard_data() -> dict[str, Any]:
        """Live data source for the Agent Control UI."""
        states = await langgraph_states()
        executions = []
        for execution_id, state in states.items():
            durable_state = settings_store().load(execution_id) or {}
            executions.append({
                "execution_id": execution_id,
                "project_id": state.get("project_id", ""),
                "feature_request": state.get("feature_request", ""),
                "status": durable_state.get("status", state.get("status", "unknown")),
                "created_at": state.get("started_at", ""),
                "updated_at": durable_state.get("updated_at", state.get("updated_at", "")),
            })
        executions.sort(key=lambda item: item["updated_at"], reverse=True)
        metrics = [item for item in langgraph_agent_metrics(states) if item["agent"] in AGENT_ROLES]
        input_tokens = sum(item["tokens_input"] for item in metrics)
        output_tokens = sum(item["tokens_output"] for item in metrics)
        recorded_cost = sum(item["estimated_cost"] for item in metrics)
        estimated_cost = recorded_cost or (
            input_tokens * settings.cost_per_1m_input / 1_000_000
            + output_tokens * settings.cost_per_1m_output / 1_000_000
        )
        execution_items = []
        active_agents = [item for item in settings_store().active_agents() if item["agent"] in AGENT_ROLES]
        for execution in executions:
            execution_id = execution["execution_id"]
            state = states.get(execution_id, {})
            execution_items.append({
                **execution,
                "agent_keys": [task["agent"] for task in state.get("plan", []) if task.get("agent")],
                "context_health": state.get("context_health", {}),
            })
        return {
            "executions": {
                "total": len(executions),
                "completed": sum(item["status"] == "completed" for item in executions),
                "failed": sum(item["status"] == "failed" for item in executions),
                "running": sum(item["status"] == "running" for item in executions),
            },
            "agents": metrics,
            "items": execution_items,
            "active_agents": active_agents,
            "usage": {
                "tokens_input": input_tokens,
                "tokens_output": output_tokens,
                "recorded_cost_usd": round(recorded_cost, 6),
                "estimated_cost_usd": round(estimated_cost, 6),
            },
            "source": {
                "checkpoint": str(settings.langgraph_checkpoint_path),
                "updated_from": "LangGraph native checkpointer",
            },
        }

    @app.get("/events")
    async def events() -> Any:
        """Server-Sent Events for live LangGraph execution and agent updates."""
        async def stream() -> Any:
            async for event in app.state.event_broker.subscribe():
                yield f"event: orchestration\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
        })

    @app.post("/executions", status_code=202)
    async def create_execution(request: ExecutionRequest) -> dict[str, Any]:
        return await manager().create(request)

    @app.get("/executions")
    async def list_executions(limit: int = 100) -> list[dict[str, Any]]:
        return settings_store().list(limit)

    @app.get("/executions/{execution_id}")
    async def get_execution(execution_id: str) -> dict[str, Any]:
        try:
            return manager().state(execution_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="execution not found")

    @app.post("/executions/{execution_id}/cancel")
    async def cancel_execution(execution_id: str) -> dict[str, Any]:
        state = app.state.graph.cancel(execution_id)
        if not state:
            raise HTTPException(status_code=404, detail="execution not found")
        return state

    @app.post("/executions/{execution_id}/resume", status_code=202)
    async def resume_execution(execution_id: str) -> dict[str, Any]:
        if not settings_store().load(execution_id):
            raise HTTPException(status_code=404, detail="execution not found")
        manager().tasks[execution_id] = asyncio.create_task(app.state.graph.resume(execution_id))
        return {"execution_id": execution_id, "status": "resuming"}

    @app.get("/executions/{execution_id}/logs")
    async def execution_logs(execution_id: str) -> list[dict[str, Any]]:
        if not settings_store().load(execution_id):
            raise HTTPException(status_code=404, detail="execution not found")
        return settings_store().logs(execution_id)

    @app.get("/executions/{execution_id}/health")
    async def execution_health(execution_id: str) -> dict[str, Any]:
        try:
            return manager().state(execution_id).get("context_health", {})
        except KeyError:
            raise HTTPException(status_code=404, detail="execution not found")

    @app.get("/executions/{execution_id}/agents")
    async def agent_history(execution_id: str) -> list[dict[str, Any]]:
        if not settings_store().load(execution_id):
            raise HTTPException(status_code=404, detail="execution not found")
        return settings_store().agent_history(execution_id)

    return app
