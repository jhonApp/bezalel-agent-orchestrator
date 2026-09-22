# src/wake_listener/app.py
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse

from wake_listener.activity import ActivityTracker
from wake_listener.config import WakeListenerSettings
from wake_listener.process_manager import ProcessManager

logger = logging.getLogger(__name__)

_EXCLUDED_REQUEST_HEADERS = {"host", "content-length", "authorization"}
_EXCLUDED_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}
_STREAMED_PATH_PREFIXES = ("events",)


def create_app(settings: WakeListenerSettings, process_manager: ProcessManager | None = None,
              tracker: ActivityTracker | None = None) -> FastAPI:
    process_manager = process_manager or ProcessManager(
        start_command=settings.backend_start_command, health_url=settings.health_url,
        health_timeout_seconds=settings.backend_health_timeout_seconds, cwd=settings.backend_cwd,
    )
    tracker = tracker or ActivityTracker()

    @asynccontextmanager
    async def lifespan(app: Any):
        # Matches the existing orchestrator API's own lifespan pattern
        # (src/api/app.py) rather than the deprecated @app.on_event("startup").
        async def idle_loop() -> None:
            while True:
                await asyncio.sleep(settings.idle_check_interval_seconds)
                await _idle_tick(tracker, process_manager, settings.idle_timeout_seconds, settings.backend_base_url)

        task = asyncio.create_task(idle_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            process_manager.stop_if_idle()

    app = FastAPI(lifespan=lifespan)
    app.state.process_manager = process_manager
    app.state.tracker = tracker

    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_allowed_origins,
        allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
    )

    @app.get("/ping")
    async def ping() -> dict:
        return {"status": "wake_listener_ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request) -> Response:
        _check_token(settings, request, path)
        tracker.mark_active()
        try:
            await process_manager.ensure_awake()
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        target_url = f"{settings.backend_base_url}/{path}"
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _EXCLUDED_REQUEST_HEADERS}
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            request.method, target_url, params=request.query_params, headers=headers, content=body,
        )

        is_streamed = path.startswith(_STREAMED_PATH_PREFIXES)
        try:
            upstream_response = await client.send(upstream_request, stream=is_streamed)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"upstream request failed: {exc}")

        if is_streamed:
            tracker.stream_opened()

            async def body_iterator() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream_response.aiter_raw():
                        yield chunk
                finally:
                    await upstream_response.aclose()
                    await client.aclose()
                    tracker.stream_closed()

            response_headers = {k: v for k, v in upstream_response.headers.items()
                               if k.lower() not in _EXCLUDED_RESPONSE_HEADERS}
            return StreamingResponse(body_iterator(), status_code=upstream_response.status_code,
                                     headers=response_headers,
                                     media_type=upstream_response.headers.get("content-type"))

        await client.aclose()
        response_headers = {k: v for k, v in upstream_response.headers.items()
                           if k.lower() not in _EXCLUDED_RESPONSE_HEADERS}
        return Response(content=upstream_response.content, status_code=upstream_response.status_code,
                        headers=response_headers)

    return app


async def _backend_has_active_work(backend_base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{backend_base_url}/dashboard-data")
        if response.status_code != 200:
            return False
        data = response.json()
        running = data.get("executions", {}).get("running", 0)
        return bool(running) or bool(data.get("active_agents"))
    except httpx.HTTPError:
        return False


async def run_idle_check_once(tracker: ActivityTracker, process_manager: ProcessManager,
                              idle_timeout_seconds: float, backend_base_url: str | None = None) -> bool:
    if not tracker.is_idle(idle_timeout_seconds):
        return False
    if backend_base_url and await _backend_has_active_work(backend_base_url):
        return False
    return process_manager.stop_if_idle()


async def _idle_tick(tracker: ActivityTracker, process_manager: ProcessManager, idle_timeout_seconds: float,
                     backend_base_url: str | None = None) -> None:
    try:
        await run_idle_check_once(tracker, process_manager, idle_timeout_seconds, backend_base_url)
    except Exception:
        logger.exception("wake listener idle check failed; will retry next interval")


def _check_token(settings: WakeListenerSettings, request: Request, path: str) -> None:
    if not settings.auth_token:
        return
    if request.headers.get("authorization") == f"Bearer {settings.auth_token}":
        return
    # EventSource (used for the /events SSE path) cannot set custom headers — this
    # is the one intentional exception to header-only auth, scoped to that path only.
    if path.startswith(_STREAMED_PATH_PREFIXES) and request.query_params.get("token") == settings.auth_token:
        return
    raise HTTPException(status_code=401, detail="invalid or missing token")
