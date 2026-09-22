# Wake Listener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small always-on "Wake Listener" process that sits in front of the
existing orchestrator API, spawning it on demand and stopping it after an idle
timeout, so the real orchestrator can be reached over the internet (via ngrok)
without running 24/7 — and update the dashboard to talk to it directly.

**Architecture:** A new `wake_listener` package (config, activity tracking,
process spawn/health/stop, and a FastAPI reverse-proxy app) runs standalone on
its own port, in front of the existing `bezalel-orchestrator api` process. The
dashboard (`web/Plataforma de Administração.dc.html`) is changed to call the
listener's URL (via ngrok) instead of relative paths, with a one-time
browser-prompted token stored in `localStorage`.

**Tech Stack:** Python 3.11, FastAPI, httpx, uvicorn (all already project
dependencies — no new dependency is added), pytest/pytest-asyncio for tests.

## Global Constraints

- No new third-party dependency: use only `fastapi`, `httpx`, `uvicorn`,
  `pydantic`, stdlib — all already in `pyproject.toml`.
- Idle-stop must only ever terminate the process this listener itself spawned —
  never a process-name-based kill (existing project incident lesson, restated in
  the design spec's Error Handling section).
- An open `/events` (SSE) connection must count as activity for its entire
  duration; the idle timer must never fire while a client is still streaming.
- The auth token must never be committed into `web/Plataforma de
  Administração.dc.html` — it is entered once by the user and stored in
  `localStorage` only.
- `EventSource` (used for `/events`) cannot set custom headers — the listener
  must accept the token as a `?token=` query parameter as the one narrow
  exception to header-only auth, used only for that path.
- Tests spawn real subprocesses for fake backends (matching this project's
  existing testing style — see `tests/test_codex_yolo_integration.py`'s
  `FAKE_CODEX*` scripts) rather than mocking `subprocess.Popen`.

---

### Task 1: Config and activity-tracking primitives

**Files:**
- Create: `src/wake_listener/__init__.py`
- Create: `src/wake_listener/config.py`
- Create: `src/wake_listener/activity.py`
- Test: `tests/test_wake_listener_config.py`
- Test: `tests/test_wake_listener_activity.py`

**Interfaces:**
- Produces: `WakeListenerSettings` (pydantic `BaseModel`) with fields
  `listen_host: str`, `listen_port: int`, `backend_host: str`,
  `backend_port: int`, `backend_health_path: str`,
  `backend_start_command: list[str]`, `backend_health_timeout_seconds: float`,
  `idle_timeout_minutes: float`, `idle_check_interval_seconds: float`,
  `auth_token: str | None`, `cors_allowed_origins: list[str]`; properties
  `backend_base_url -> str`, `health_url -> str`, `idle_timeout_seconds -> float`;
  classmethod `WakeListenerSettings.load() -> WakeListenerSettings`.
- Produces: `ActivityTracker` with `__init__(self, clock: Callable[[], float] | None = None)`,
  methods `mark_active()`, `stream_opened()`, `stream_closed()`,
  `idle_seconds() -> float`, `is_idle(idle_timeout_seconds: float) -> bool`,
  property `open_stream_count -> int`.

- [ ] **Step 1: Create the package init**

```python
# src/wake_listener/__init__.py
```

(empty file — matches the existing `src/orchestrator/__init__.py` /
`src/adapters/__init__.py` pattern for a plain package marker.)

- [ ] **Step 2: Write the failing config tests**

```python
# tests/test_wake_listener_config.py
from __future__ import annotations

from wake_listener.config import WakeListenerSettings


def test_load_uses_defaults_when_no_env_vars_are_set(monkeypatch):
    for name in ("WAKE_LISTENER_HOST", "WAKE_LISTENER_PORT", "WAKE_BACKEND_HOST", "WAKE_BACKEND_PORT",
                "WAKE_BACKEND_HEALTH_PATH", "WAKE_BACKEND_START_COMMAND", "WAKE_IDLE_TIMEOUT_MINUTES",
                "WAKE_IDLE_CHECK_INTERVAL_SECONDS", "WAKE_LISTENER_TOKEN", "WAKE_CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)

    settings = WakeListenerSettings.load()

    assert settings.listen_host == "127.0.0.1"
    assert settings.listen_port == 8090
    assert settings.backend_base_url == "http://127.0.0.1:8000"
    assert settings.health_url == "http://127.0.0.1:8000/dashboard-data"
    assert settings.backend_start_command == ["bezalel-orchestrator", "api"]
    assert settings.idle_timeout_seconds == 1200.0
    assert settings.auth_token is None
    assert settings.cors_allowed_origins == ["https://vercel-deploy-orchestrator.vercel.app"]


def test_load_reads_every_override_from_env(monkeypatch):
    monkeypatch.setenv("WAKE_LISTENER_PORT", "9090")
    monkeypatch.setenv("WAKE_BACKEND_PORT", "8001")
    monkeypatch.setenv("WAKE_BACKEND_HEALTH_PATH", "/health")
    monkeypatch.setenv("WAKE_BACKEND_START_COMMAND", "python,-m,orchestrator")
    monkeypatch.setenv("WAKE_IDLE_TIMEOUT_MINUTES", "5")
    monkeypatch.setenv("WAKE_IDLE_CHECK_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("WAKE_LISTENER_TOKEN", "secret123")
    monkeypatch.setenv("WAKE_CORS_ALLOWED_ORIGINS", "https://a.example,https://b.example")

    settings = WakeListenerSettings.load()

    assert settings.listen_port == 9090
    assert settings.backend_base_url == "http://127.0.0.1:8001"
    assert settings.health_url == "http://127.0.0.1:8001/health"
    assert settings.backend_start_command == ["python", "-m", "orchestrator"]
    assert settings.idle_timeout_seconds == 300.0
    assert settings.idle_check_interval_seconds == 10.0
    assert settings.auth_token == "secret123"
    assert settings.cors_allowed_origins == ["https://a.example", "https://b.example"]
```

- [ ] **Step 3: Run the config tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wake_listener'`

- [ ] **Step 4: Implement the config module**

```python
# src/wake_listener/config.py
from __future__ import annotations

import os

from pydantic import BaseModel, Field


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


DEFAULT_CORS_ORIGIN = "https://vercel-deploy-orchestrator.vercel.app"


class WakeListenerSettings(BaseModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = 8090
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    backend_health_path: str = "/dashboard-data"
    backend_start_command: list[str] = Field(default_factory=lambda: ["bezalel-orchestrator", "api"])
    backend_health_timeout_seconds: float = 30.0
    idle_timeout_minutes: float = 20.0
    idle_check_interval_seconds: float = 60.0
    auth_token: str | None = None
    cors_allowed_origins: list[str] = Field(default_factory=lambda: [DEFAULT_CORS_ORIGIN])

    @property
    def backend_base_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"

    @property
    def health_url(self) -> str:
        return self.backend_base_url + self.backend_health_path

    @property
    def idle_timeout_seconds(self) -> float:
        return self.idle_timeout_minutes * 60

    @classmethod
    def load(cls) -> "WakeListenerSettings":
        command = os.getenv("WAKE_BACKEND_START_COMMAND", "").strip()
        origins = os.getenv("WAKE_CORS_ALLOWED_ORIGINS", "").strip()
        return cls(
            listen_host=os.getenv("WAKE_LISTENER_HOST", "127.0.0.1"),
            listen_port=_int("WAKE_LISTENER_PORT", 8090),
            backend_host=os.getenv("WAKE_BACKEND_HOST", "127.0.0.1"),
            backend_port=_int("WAKE_BACKEND_PORT", 8000),
            backend_health_path=os.getenv("WAKE_BACKEND_HEALTH_PATH", "/dashboard-data"),
            backend_start_command=command.split(",") if command else ["bezalel-orchestrator", "api"],
            backend_health_timeout_seconds=_float("WAKE_BACKEND_HEALTH_TIMEOUT_SECONDS", 30.0),
            idle_timeout_minutes=_float("WAKE_IDLE_TIMEOUT_MINUTES", 20.0),
            idle_check_interval_seconds=_float("WAKE_IDLE_CHECK_INTERVAL_SECONDS", 60.0),
            auth_token=(os.getenv("WAKE_LISTENER_TOKEN", "").strip() or None),
            cors_allowed_origins=origins.split(",") if origins else [DEFAULT_CORS_ORIGIN],
        )
```

- [ ] **Step 5: Run the config tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Write the failing activity-tracker tests**

```python
# tests/test_wake_listener_activity.py
from __future__ import annotations

from wake_listener.activity import ActivityTracker


def test_new_tracker_is_idle_once_the_timeout_elapses():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])

    now[0] = 100.0

    assert tracker.is_idle(idle_timeout_seconds=50)
    assert not tracker.is_idle(idle_timeout_seconds=200)


def test_mark_active_resets_the_idle_clock():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    now[0] = 100.0
    tracker.mark_active()

    now[0] = 110.0

    assert not tracker.is_idle(idle_timeout_seconds=50)


def test_an_open_stream_prevents_idle_regardless_of_elapsed_time():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    tracker.stream_opened()

    now[0] = 10_000.0

    assert not tracker.is_idle(idle_timeout_seconds=1)
    assert tracker.open_stream_count == 1


def test_closing_the_last_stream_lets_the_idle_clock_run_again():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    tracker.stream_opened()
    now[0] = 10.0
    tracker.stream_closed()

    now[0] = 10_100.0

    assert tracker.is_idle(idle_timeout_seconds=50)
    assert tracker.open_stream_count == 0


def test_stream_closed_never_goes_negative_if_called_without_a_matching_open():
    tracker = ActivityTracker(clock=lambda: 0.0)

    tracker.stream_closed()

    assert tracker.open_stream_count == 0
```

- [ ] **Step 7: Run the activity tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_activity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wake_listener.activity'`

- [ ] **Step 8: Implement the activity tracker**

```python
# src/wake_listener/activity.py
from __future__ import annotations

import time
from typing import Callable


class ActivityTracker:
    """Tracks when the backend was last used, so the idle loop knows when it is
    safe to stop it. An open SSE stream always counts as active for its whole
    duration — the idle timer must never fire while a client is still watching
    live events, even if no new bytes have been sent in a while.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._last_activity = self._clock()
        self._open_streams = 0

    def mark_active(self) -> None:
        self._last_activity = self._clock()

    def stream_opened(self) -> None:
        self._open_streams += 1
        self.mark_active()

    def stream_closed(self) -> None:
        self._open_streams = max(0, self._open_streams - 1)
        self.mark_active()

    def idle_seconds(self) -> float:
        return self._clock() - self._last_activity

    def is_idle(self, idle_timeout_seconds: float) -> bool:
        return self._open_streams == 0 and self.idle_seconds() >= idle_timeout_seconds

    @property
    def open_stream_count(self) -> int:
        return self._open_streams
```

- [ ] **Step 9: Run the activity tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_activity.py -v`
Expected: PASS (5 tests)

- [ ] **Step 10: Commit**

```bash
git add src/wake_listener/__init__.py src/wake_listener/config.py src/wake_listener/activity.py tests/test_wake_listener_config.py tests/test_wake_listener_activity.py
git commit -m "feat: add wake listener config and activity tracking"
```

---

### Task 2: Process manager (spawn on demand, health-wait, stop-if-idle)

**Files:**
- Create: `src/wake_listener/process_manager.py`
- Test: `tests/test_wake_listener_process_manager.py`

**Interfaces:**
- Consumes: nothing from Task 1 directly (this class is independent of
  `WakeListenerSettings`/`ActivityTracker` — it is wired together with them in
  Task 3).
- Produces: `ProcessManager` with
  `__init__(self, start_command: list[str], health_url: str, health_timeout_seconds: float = 30.0, health_poll_interval_seconds: float = 0.5)`,
  `is_running(self) -> bool`, `async def ensure_awake(self) -> None` (raises
  `TimeoutError` if the backend never becomes healthy), `def stop_if_idle(self) -> bool`.

- [ ] **Step 1: Write the failing process-manager tests**

```python
# tests/test_wake_listener_process_manager.py
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from wake_listener.process_manager import ProcessManager


FAKE_BACKEND_SCRIPT = r'''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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


@pytest.fixture
def fake_backend_command(tmp_path: Path) -> tuple[list[str], int]:
    script = tmp_path / "fake_backend.py"
    script.write_text(FAKE_BACKEND_SCRIPT, encoding="utf-8")
    port = free_port()
    return [sys.executable, str(script), str(port)], port


@pytest.mark.asyncio
async def test_ensure_awake_spawns_the_backend_when_it_is_not_already_running(fake_backend_command):
    command, port = fake_backend_command
    manager = ProcessManager(start_command=command, health_url=f"http://127.0.0.1:{port}/", health_timeout_seconds=10)
    assert not await manager._is_healthy()

    await manager.ensure_awake()

    assert manager.is_running()
    assert await manager._is_healthy()
    manager.stop_if_idle()


@pytest.mark.asyncio
async def test_ensure_awake_does_not_respawn_an_already_healthy_backend(fake_backend_command):
    command, port = fake_backend_command
    already_running = subprocess.Popen(command)
    try:
        health_url = f"http://127.0.0.1:{port}/"
        manager = ProcessManager(start_command=command, health_url=health_url, health_timeout_seconds=10)
        for _ in range(200):
            if await manager._is_healthy():
                break
            await asyncio.sleep(0.05)

        await manager.ensure_awake()

        assert not manager.is_running(), "must not track a process it did not spawn itself"
        assert manager.stop_if_idle() is False
        assert await manager._is_healthy(), "the pre-existing backend must still be running"
    finally:
        already_running.terminate()
        already_running.wait(timeout=10)


@pytest.mark.asyncio
async def test_ensure_awake_raises_timeout_error_when_the_backend_never_becomes_healthy():
    dead_port = free_port()
    manager = ProcessManager(
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
        health_url=f"http://127.0.0.1:{dead_port}/never-listens",
        health_timeout_seconds=0.5, health_poll_interval_seconds=0.1,
    )

    with pytest.raises(TimeoutError):
        await manager.ensure_awake()

    manager.stop_if_idle()


@pytest.mark.asyncio
async def test_stop_if_idle_stops_only_the_process_it_spawned(fake_backend_command):
    command, port = fake_backend_command
    manager = ProcessManager(start_command=command, health_url=f"http://127.0.0.1:{port}/", health_timeout_seconds=10)
    await manager.ensure_awake()

    stopped = manager.stop_if_idle()

    assert stopped is True
    assert not manager.is_running()
    assert not await manager._is_healthy()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_process_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wake_listener.process_manager'`

- [ ] **Step 3: Implement the process manager**

```python
# src/wake_listener/process_manager.py
from __future__ import annotations

import asyncio
import subprocess
import time

import httpx


class ProcessManager:
    """Spawns the real orchestrator backend on demand, waits for it to become
    healthy, and stops only the process it itself spawned — never a process
    that was already running before this manager touched it, and never by
    process name (see the project's earlier incident: a broad `taskkill` by
    image name once killed an unrelated in-progress execution).
    """

    def __init__(self, start_command: list[str], health_url: str,
                health_timeout_seconds: float = 30.0, health_poll_interval_seconds: float = 0.5) -> None:
        self.start_command = start_command
        self.health_url = health_url
        self.health_timeout_seconds = health_timeout_seconds
        self.health_poll_interval_seconds = health_poll_interval_seconds
        self._process: subprocess.Popen | None = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def ensure_awake(self) -> None:
        if self.is_running():
            return
        if await self._is_healthy():
            return
        self._process = subprocess.Popen(self.start_command)
        await self._wait_healthy()

    async def _is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(self.health_url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.health_timeout_seconds
        while time.monotonic() < deadline:
            if await self._is_healthy():
                return
            await asyncio.sleep(self.health_poll_interval_seconds)
        raise TimeoutError(f"backend did not become healthy within {self.health_timeout_seconds}s")

    def stop_if_idle(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            self._process = None
            return False
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_process_manager.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/wake_listener/process_manager.py tests/test_wake_listener_process_manager.py
git commit -m "feat: add wake listener process manager"
```

---

### Task 3: FastAPI app — token check, CORS, proxy, idle loop

**Files:**
- Create: `src/wake_listener/app.py`
- Test: `tests/test_wake_listener_app.py`

**Interfaces:**
- Consumes: `WakeListenerSettings` (Task 1), `ActivityTracker` (Task 1),
  `ProcessManager` (Task 2) — exact signatures as produced above.
- Produces: `create_app(settings: WakeListenerSettings, process_manager: ProcessManager | None = None, tracker: ActivityTracker | None = None) -> FastAPI`
  and `async def run_idle_check_once(tracker: ActivityTracker, process_manager: ProcessManager, idle_timeout_seconds: float) -> bool`
  (returns whatever `process_manager.stop_if_idle()` returned, or `False` if not idle).

- [ ] **Step 1: Write the failing app tests**

```python
# tests/test_wake_listener_app.py
from __future__ import annotations

import socket
import sys
from pathlib import Path

import httpx
import pytest

from wake_listener.activity import ActivityTracker
from wake_listener.app import create_app, run_idle_check_once
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wake_listener.app'`

- [ ] **Step 3: Implement the app**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest -p no:cacheprovider --basetemp="C:/betmp/pytest" tests/test_wake_listener_app.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/wake_listener/app.py tests/test_wake_listener_app.py
git commit -m "feat: add wake listener reverse-proxy app with idle shutdown"
```

---

### Task 4: CLI entrypoint, packaging, and setup runbook

**Files:**
- Create: `src/wake_listener/main.py`
- Modify: `pyproject.toml`
- Create: `docs/wake-listener-setup.md`

**Interfaces:**
- Consumes: `WakeListenerSettings.load()` (Task 1), `create_app` (Task 3).
- Produces: `cli(argv: list[str] | None = None) -> int`, console script
  `bezalel-wake-listener`.

- [ ] **Step 1: Implement the CLI**

```python
# src/wake_listener/main.py
from __future__ import annotations

import argparse

from wake_listener.app import create_app
from wake_listener.config import WakeListenerSettings


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bezalel-wake-listener")
    p.add_argument("--host", default=None, help="override WAKE_LISTENER_HOST")
    p.add_argument("--port", type=int, default=None, help="override WAKE_LISTENER_PORT")
    return p


def cli(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = WakeListenerSettings.load()
    if args.host:
        settings.listen_host = args.host
    if args.port:
        settings.listen_port = args.port
    import uvicorn
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
```

- [ ] **Step 2: Register the console script**

In `pyproject.toml`, find:

```toml
[project.scripts]
bezalel-orchestrator = "orchestrator.main:cli"
bezalel-orchestrator-mcp = "orchestrator.mcp_bridge:main"
```

Replace with:

```toml
[project.scripts]
bezalel-orchestrator = "orchestrator.main:cli"
bezalel-orchestrator-mcp = "orchestrator.mcp_bridge:main"
bezalel-wake-listener = "wake_listener.main:cli"
```

- [ ] **Step 3: Reinstall the package so the new script is on PATH**

Run: `pip install -e .`
Expected: exits 0; `bezalel-wake-listener --help` now runs without
`command not found`.

- [ ] **Step 4: Write the setup runbook**

```markdown
# docs/wake-listener-setup.md

# Wake Listener setup (on-premise, on-demand exposure)

Exposes the real orchestrator (this machine) to the internet through ngrok's
free static domain, without keeping the heavy orchestrator process running
24/7. See `docs/superpowers/specs/2026-09-22-on-premise-wake-listener-design.md`
for the design rationale.

## 1. Environment variables

Set these before starting the listener (e.g. in `.env` at the repo root,
picked up by the existing `python-dotenv` loading used by `Settings.load()`
in `orchestrator/config.py` — the wake listener does not use `.env` loading
itself, so export these in the shell/service environment instead):

| Variable | Default | Purpose |
|---|---|---|
| `WAKE_LISTENER_HOST` | `127.0.0.1` | interface the listener binds on |
| `WAKE_LISTENER_PORT` | `8090` | port the listener binds on (point ngrok here) |
| `WAKE_BACKEND_HOST` | `127.0.0.1` | where the real orchestrator API listens |
| `WAKE_BACKEND_PORT` | `8000` | must match `bezalel-orchestrator api`'s `--port` |
| `WAKE_LISTENER_TOKEN` | *(none — auth disabled)* | shared secret; **set this before exposing publicly** |
| `WAKE_IDLE_TIMEOUT_MINUTES` | `20` | how long with no activity before the backend is stopped |
| `WAKE_CORS_ALLOWED_ORIGINS` | `https://vercel-deploy-orchestrator.vercel.app` | comma-separated list of dashboard origins allowed to call this listener |

## 2. Run the listener

```bash
bezalel-wake-listener
```

Leave this running (Windows: Task Scheduler entry "at log on", or a small
wrapper service) — this is the one process meant to run 24/7. It does not run
Codex or touch git; it only spawns/stops `bezalel-orchestrator api` on demand.

## 3. Install ngrok and reserve a static domain

1. Install ngrok: https://ngrok.com/download
2. `ngrok config add-authtoken <your-authtoken>` (from the ngrok dashboard)
3. Reserve a free static domain from the ngrok dashboard (Domains -> New Domain)
4. Install as a Windows service pointed at the listener's port:
   ```
   ngrok service install --config <path-to-ngrok.yml>
   ```
   where `ngrok.yml` contains:
   ```yaml
   version: 3
   agent:
     authtoken: <your-authtoken>
   tunnels:
     wake-listener:
       proto: http
       addr: 8090
       domain: <your-static-domain>.ngrok-free.app
   ```
5. `ngrok service start`

## 4. Point the dashboard at it

Open `https://vercel-deploy-orchestrator.vercel.app/` — on first load (from a
non-localhost origin) it prompts once for the listener URL
(`https://<your-static-domain>.ngrok-free.app`) and the token, then stores
both in `localStorage`. See Task 5 of
`docs/superpowers/plans/2026-09-22-wake-listener.md` for what changed in the
dashboard itself.

## 5. Verify

```bash
curl https://<your-static-domain>.ngrok-free.app/ping
```

Expected: `{"status":"wake_listener_ok"}` — this alone does not require the
token and does not wake the backend. Then, with the token:

```bash
curl -H "Authorization: Bearer <token>" https://<your-static-domain>.ngrok-free.app/dashboard-data
```

The first call after a period of inactivity takes a few seconds (the listener
is spawning and health-checking the real backend); subsequent calls are fast
until the idle timeout elapses again.
```

- [ ] **Step 5: Commit**

```bash
git add src/wake_listener/main.py pyproject.toml docs/wake-listener-setup.md
git commit -m "feat: add wake listener CLI entrypoint and setup runbook"
```

---

### Task 5: Dashboard — call the wake listener instead of relative paths

**Files:**
- Modify: `web/Plataforma de Administração.dc.html`

**Interfaces:**
- Consumes: nothing from earlier tasks directly (this is a plain static-page
  change); the URL and token it sends are whatever the operator configured per
  `docs/wake-listener-setup.md` (Task 4).
- Produces: `Component.prototype.apiUrl(path)` and
  `Component.prototype.authHeaders(extra)` helper methods used by every fetch
  call site in this file.

- [ ] **Step 1: Add the one-time prompt and helper methods**

Find, inside `componentDidMount()`:

```js
  componentDidMount() {
    this.refreshDashboard();
```

Replace with:

```js
  componentDidMount() {
    this.apiBase = localStorage.getItem("wakeApiBase") || "";
    this.apiToken = localStorage.getItem("wakeApiToken") || "";
    if (!this.apiBase && location.hostname.endsWith("vercel.app")) {
      const base = window.prompt("URL do Wake Listener (ex: https://seunome.ngrok-free.app):", "");
      if (base) {
        const token = window.prompt("Token de acesso do Wake Listener:", "") || "";
        this.apiBase = base.replace(/\/$/, "");
        this.apiToken = token;
        localStorage.setItem("wakeApiBase", this.apiBase);
        localStorage.setItem("wakeApiToken", this.apiToken);
      }
    }
    this.refreshDashboard();
```

Find, right after `navBtnStyle(view) { ... }` closes (immediately before
`renderVals() {`):

```js
  renderVals() {
```

Replace with:

```js
  apiUrl(path) {
    return (this.apiBase || "") + path;
  }

  authHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    if (this.apiToken) headers["Authorization"] = "Bearer " + this.apiToken;
    return headers;
  }

  renderVals() {
```

- [ ] **Step 2: Point the SSE connection at the listener, with the token as a query param**

Find:

```js
    this.eventSource = new EventSource("/events");
```

Replace with:

```js
    const eventsUrl = this.apiUrl("/events") + (this.apiToken ? "?token=" + encodeURIComponent(this.apiToken) : "");
    this.eventSource = new EventSource(eventsUrl);
```

- [ ] **Step 3: Point `refreshDashboard` and `refreshQuality` fetch calls at the listener**

Find:

```js
    fetch("/dashboard-data")
```

Replace with:

```js
    fetch(this.apiUrl("/dashboard-data"), { headers: this.authHeaders() })
```

Find:

```js
    fetch("/quality-data")
```

Replace with:

```js
    fetch(this.apiUrl("/quality-data"), { headers: this.authHeaders() })
```

- [ ] **Step 4: Point the skills fetch calls at the listener**

Find:

```js
    fetch("/projects/" + projectId + "/skills")
```

Replace with:

```js
    fetch(this.apiUrl("/projects/" + projectId + "/skills"), { headers: this.authHeaders() })
```

Find:

```js
    fetch("/projects/" + projectId + "/skills", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package: pkg, skill: skillName || null }),
    })
```

Replace with:

```js
    fetch(this.apiUrl("/projects/" + projectId + "/skills"), {
      method: "POST", headers: this.authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ package: pkg, skill: skillName || null }),
    })
```

Find:

```js
    fetch("/projects/" + projectId + "/skills/" + encodeURIComponent(skillName), { method: "DELETE" })
```

Replace with:

```js
    fetch(this.apiUrl("/projects/" + projectId + "/skills/" + encodeURIComponent(skillName)), { method: "DELETE", headers: this.authHeaders() })
```

- [ ] **Step 5: Validate the file still parses and templating tags stay balanced**

Run:
```bash
node -e "
const fs = require('fs');
const html = fs.readFileSync('web/Plataforma de Administração.dc.html', 'utf8');
const re = /<script>([\s\S]*?)<\/script>/g;
let m, ok=true;
while ((m = re.exec(html))) { try { new Function(m[1]); } catch(e) { ok=false; console.log('FAIL: '+e.message); } }
console.log(ok ? 'JS OK' : 'JS ERRORS');
const count = (s) => (html.split(s).length - 1);
console.log('sc-if', count('<sc-if'), count('</sc-if>'));
console.log('sc-for', count('<sc-for'), count('</sc-for>'));
"
```
Expected: `JS OK`, and the `sc-if`/`sc-for` open/close counts equal on each line
(same counts as before this task's edits — this task adds no new `sc-if`/`sc-for`
tags, only changes JS inside `<script>`).

- [ ] **Step 6: Copy the updated file to the mirrored path**

Run: `cp -f "web/Plataforma de Administração.dc.html" "../projeto-agents-plataform/Plataforma de Administração.dc.html"`
(Skip this step with a note in the commit message if that path does not exist
in the environment running this task.)

- [ ] **Step 7: Commit**

```bash
git add "web/Plataforma de Administração.dc.html"
git commit -m "feat: point dashboard at the wake listener instead of relative paths"
```

---

## Manual end-to-end verification (not automated — run once after Task 5)

1. Start the listener: `bezalel-wake-listener` (do **not** also manually start
   `bezalel-orchestrator api` — the listener spawns it).
2. In another terminal: `curl http://127.0.0.1:8090/ping` -> expect
   `{"status":"wake_listener_ok"}`.
3. `curl http://127.0.0.1:8090/dashboard-data` (no token set yet, so no auth
   required) -> first call should take a few seconds (backend spawning) and
   then return real dashboard JSON; confirm `bezalel-orchestrator api`'s
   process actually started (e.g. `netstat -ano | findstr :8000` shows it
   listening).
4. Wait past `WAKE_IDLE_TIMEOUT_MINUTES` (or temporarily set it to `1` for this
   check) without further requests; confirm the port-8000 process is gone
   (`netstat -ano | findstr :8000` shows nothing).
5. Set `WAKE_LISTENER_TOKEN`, restart the listener, repeat step 3 without a
   token and confirm `401`; repeat with `-H "Authorization: Bearer <token>"`
   and confirm it works.
6. Only after the above passes locally: follow `docs/wake-listener-setup.md`
   to put ngrok in front of it and open the Vercel-hosted dashboard from a
   normal browser to confirm the end-to-end path for real.
