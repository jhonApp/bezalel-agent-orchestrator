# tests/test_wake_listener_app.py
from __future__ import annotations

import socket
import sys
from pathlib import Path

import httpx
import pytest

from wake_listener.activity import ActivityTracker
from wake_listener.app import _idle_tick, create_app, run_idle_check_once
from wake_listener.config import WakeListenerSettings
from wake_listener.process_manager import ProcessManager


FAKE_BACKEND_SCRIPT = r'''
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(0.1)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


port = int(sys.argv[1])
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_wired_app(tmp_path: Path, tracker: ActivityTracker | None = None):
    script = tmp_path / "fake_backend.py"
    script.write_text(FAKE_BACKEND_SCRIPT, encoding="utf-8")
    port = free_port()
    settings = WakeListenerSettings(backend_port=port, backend_health_path="/", auth_token="secret123")
    process_manager = ProcessManager(
        start_command=[sys.executable, str(script), str(port)],
        health_url=settings.health_url, health_timeout_seconds=15,
    )
    app = create_app(settings, process_manager=process_manager, tracker=tracker)
    return app, process_manager


@pytest.mark.asyncio
async def test_proxy_rejects_a_request_without_the_token(tmp_path: Path):
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/dashboard-data")
        assert response.status_code == 401
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_ping_never_requires_a_token(tmp_path: Path):
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ping")
        assert response.status_code == 200
        assert response.json() == {"status": "wake_listener_ok"}
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_proxy_wakes_the_backend_and_forwards_a_get_request(tmp_path: Path):
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/dashboard-data", headers={"Authorization": "Bearer secret123"})
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_events_path_accepts_the_token_as_a_query_param(tmp_path: Path):
    """EventSource cannot set custom headers — the one intentional exception."""
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("GET", "/events?token=secret123") as response:
                assert response.status_code == 200
                chunks = [chunk async for chunk in response.aiter_raw()]
        assert b"".join(chunks) == b"data: first\n\ndata: second\n\n"
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_streaming_proxy_tracks_the_open_stream_and_closes_it_when_drained(tmp_path: Path):
    tracker = ActivityTracker()
    app, process_manager = build_wired_app(tmp_path, tracker=tracker)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("GET", "/events", headers={"Authorization": "Bearer secret123"}) as response:
                assert response.status_code == 200
                _ = [chunk async for chunk in response.aiter_raw()]
        assert tracker.open_stream_count == 0
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_run_idle_check_once_stops_a_backend_that_has_been_idle_too_long(tmp_path: Path):
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    app, process_manager = build_wired_app(tmp_path, tracker=tracker)
    try:
        await process_manager.ensure_awake()
        now[0] = 10_000.0

        stopped = await run_idle_check_once(tracker, process_manager, idle_timeout_seconds=60)

        assert stopped is True
        assert not process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_run_idle_check_once_leaves_a_recently_active_backend_running(tmp_path: Path):
    tracker = ActivityTracker()
    app, process_manager = build_wired_app(tmp_path, tracker=tracker)
    try:
        await process_manager.ensure_awake()
        tracker.mark_active()

        stopped = await run_idle_check_once(tracker, process_manager, idle_timeout_seconds=600)

        assert stopped is False
        assert process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_query_param_token_is_rejected_on_a_non_streaming_path(tmp_path: Path):
    """The ?token= fallback exists only because EventSource can't set headers
    for /events — it must not become a general auth bypass on other paths."""
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/dashboard-data?token=secret123")
        assert response.status_code == 401
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_proxy_returns_502_when_the_upstream_request_fails(tmp_path: Path):
    script = tmp_path / "fake_backend.py"
    script.write_text(FAKE_BACKEND_SCRIPT, encoding="utf-8")
    healthy_port = free_port()
    dead_port = free_port()
    # settings point the actual proxied request at a port nothing is listening on...
    settings = WakeListenerSettings(backend_port=dead_port, backend_health_path="/", auth_token="secret123")
    # ...but the injected process manager's health check targets the real fake
    # backend, so ensure_awake() reports healthy and the request proceeds to the
    # (dead) proxy target, where it must fail with a real connection error.
    process_manager = ProcessManager(
        start_command=[sys.executable, str(script), str(healthy_port)],
        health_url=f"http://127.0.0.1:{healthy_port}/", health_timeout_seconds=15,
    )
    app = create_app(settings, process_manager=process_manager)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/dashboard-data", headers={"Authorization": "Bearer secret123"})
        assert response.status_code == 502
    finally:
        process_manager.stop_if_idle()


class _RaisingProcessManager:
    """Minimal stub whose stop_if_idle always raises, to prove the idle loop
    survives a single failing tick instead of dying silently forever."""

    def is_running(self) -> bool:
        return True

    def stop_if_idle(self) -> bool:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_idle_tick_swallows_exceptions_from_a_failing_stop_if_idle():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    now[0] = 10_000.0

    await _idle_tick(tracker, _RaisingProcessManager(), idle_timeout_seconds=60)
