# src/wake_listener/app.py
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse

from wake_listener.activity import ActivityTracker
from wake_listener.config import WakeListenerSettings
from wake_listener.process_manager import ProcessManager

_EXCLUDED_REQUEST_HEADERS = {"host", "content-length", "authorization"}
_EXCLUDED_RESPONSE_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}
_STREAMED_PATH_PREFIXES = ("events",)


def create_app(settings: WakeListenerSettings, process_manager: ProcessManager | None = None,
              tracker: ActivityTracker | None = None) -> FastAPI:
    process_manager = process_manager or ProcessManager(
        start_command=settings.backend_start_command, health_url=settings.health_url,
        health_timeout_seconds=settings.backend_health_timeout_seconds,
    )
    tracker = tracker or ActivityTracker()

    @asynccontextmanager
    async def lifespan(app: Any):
        # Matches the existing orchestrator API's own lifespan pattern
        # (src/api/app.py) rather than the deprecated @app.on_event("startup").
        async def idle_loop() -> None:
            while True:
                await asyncio.sleep(settings.idle_check_interval_seconds)
                await run_idle_check_once(tracker, process_manager, settings.idle_timeout_seconds)

        task = asyncio.create_task(idle_loop())
        try:
            yield
        finally:
            task.cancel()

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
        _check_token(settings, request)
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

        if path.startswith(_STREAMED_PATH_PREFIXES):
            upstream_response = await client.send(upstream_request, stream=True)
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

        upstream_response = await client.send(upstream_request)
        await client.aclose()
        response_headers = {k: v for k, v in upstream_response.headers.items()
                           if k.lower() not in _EXCLUDED_RESPONSE_HEADERS}
        return Response(content=upstream_response.content, status_code=upstream_response.status_code,
                        headers=response_headers)

    return app


async def run_idle_check_once(tracker: ActivityTracker, process_manager: ProcessManager,
                              idle_timeout_seconds: float) -> bool:
    if tracker.is_idle(idle_timeout_seconds):
        return process_manager.stop_if_idle()
    return False


def _check_token(settings: WakeListenerSettings, request: Request) -> None:
    if not settings.auth_token:
        return
    if request.headers.get("authorization") == f"Bearer {settings.auth_token}":
        return
    # EventSource (used for the /events SSE path) cannot set custom headers — this
    # is the one intentional exception to header-only auth, scoped to that path
    # only by virtue of the dashboard only ever sending ?token= there.
    if request.query_params.get("token") == settings.auth_token:
        return
    raise HTTPException(status_code=401, detail="invalid or missing token")
