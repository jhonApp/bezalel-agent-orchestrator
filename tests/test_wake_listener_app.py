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
async def test_query_param_token_is_rejected_on_events_view_too(tmp_path: Path):
    """The ?token= exception is scoped to the exact "/events" path, not any path that
    merely starts with "events" — /events/view is a real, separate HTML page route on
    the orchestrator API and must not inherit the SSE-only auth exception."""
    app, process_manager = build_wired_app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/events/view?token=secret123")
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


@pytest.mark.asyncio
async def test_lifespan_shutdown_stops_a_backend_it_had_spawned(tmp_path: Path):
    """Finding 2: if the listener spawned the backend (e.g. from an earlier
    proxied request) and the listener process itself then restarts, shutdown
    must stop the backend it owns rather than leaking it permanently."""
    app, process_manager = build_wired_app(tmp_path)
    try:
        await process_manager.ensure_awake()
        assert process_manager.is_running()

        async with app.router.lifespan_context(app):
            pass

        assert not process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


DASHBOARD_DATA_BACKEND_SCRIPT = r'''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(RUNNING_BODY)

    def log_message(self, *args):
        pass


port = int(sys.argv[1])
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def build_dashboard_backend_command(tmp_path: Path, running: int) -> tuple[list[str], str]:
    body = ('{"executions": {"running": %d}, "active_agents": []}' % running).encode()
    script_source = DASHBOARD_DATA_BACKEND_SCRIPT.replace("RUNNING_BODY", repr(body))
    script = tmp_path / f"dashboard_backend_{running}.py"
    script.write_text(script_source, encoding="utf-8")
    port = free_port()
    return [sys.executable, str(script), str(port)], f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_run_idle_check_once_does_not_stop_a_backend_with_an_active_execution(tmp_path: Path):
    """Finding 4: even though the tracker itself is idle, a backend reporting
    an in-flight execution via /dashboard-data must not be stopped."""
    command, base_url = build_dashboard_backend_command(tmp_path, running=1)
    process_manager = ProcessManager(
        start_command=command, health_url=f"{base_url}/", health_timeout_seconds=10,
    )
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    try:
        await process_manager.ensure_awake()
        now[0] = 10_000.0

        stopped = await run_idle_check_once(
            tracker, process_manager, idle_timeout_seconds=0, backend_base_url=base_url,
        )

        assert stopped is False
        assert process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_run_idle_check_once_stops_a_genuinely_idle_backend_with_no_active_work(tmp_path: Path):
    """Finding 4 counterpart: a backend reporting zero running executions and no
    active agents must still be stopped once the tracker is idle."""
    command, base_url = build_dashboard_backend_command(tmp_path, running=0)
    process_manager = ProcessManager(
        start_command=command, health_url=f"{base_url}/", health_timeout_seconds=10,
    )
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    try:
        await process_manager.ensure_awake()
        now[0] = 10_000.0

        stopped = await run_idle_check_once(
            tracker, process_manager, idle_timeout_seconds=0, backend_base_url=base_url,
        )

        assert stopped is True
        assert not process_manager.is_running()
    finally:
        process_manager.stop_if_idle()


@pytest.mark.asyncio
async def test_run_idle_check_once_does_not_stop_when_the_active_work_check_itself_fails(tmp_path: Path):
    """Final-review finding: a failed /dashboard-data probe (timeout, connection error,
    non-200) must be treated as "assume busy, don't stop" — not "assume idle, go ahead" —
    since a backend genuinely busy running Codex is exactly the one most likely to miss a
    health-check deadline. Getting this backwards would silently disable the finding-4
    protection precisely when it matters most."""
    command, health_base_url = build_dashboard_backend_command(tmp_path, running=0)
    process_manager = ProcessManager(
        start_command=command, health_url=f"{health_base_url}/", health_timeout_seconds=10,
    )
    dead_base_url = f"http://127.0.0.1:{free_port()}"
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    try:
        await process_manager.ensure_awake()
        now[0] = 10_000.0

        stopped = await run_idle_check_once(
            tracker, process_manager, idle_timeout_seconds=0, backend_base_url=dead_base_url,
        )

        assert stopped is False, "an unreachable active-work check must fail toward 'busy', not 'idle'"
        assert process_manager.is_running()
    finally:
        process_manager.stop_if_idle()
