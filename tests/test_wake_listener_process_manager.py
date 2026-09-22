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
async def test_ensure_awake_cleans_up_the_process_it_spawned_when_health_check_times_out():
    dead_port = free_port()
    manager = ProcessManager(
        start_command=[sys.executable, "-c", "import time; time.sleep(5)"],
        health_url=f"http://127.0.0.1:{dead_port}/never-listens",
        health_timeout_seconds=0.5, health_poll_interval_seconds=0.1,
    )

    try:
        with pytest.raises(TimeoutError):
            await manager.ensure_awake()

        assert not manager.is_running(), "process must be cleaned up when health-check times out"
    finally:
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


CWD_MARKER_BACKEND_SCRIPT = r'''
import os
import pathlib
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
marker_path = sys.argv[2]
pathlib.Path(marker_path).write_text(os.getcwd())
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


@pytest.mark.asyncio
async def test_ensure_awake_spawns_the_backend_with_the_configured_cwd(tmp_path: Path):
    """Reproduces Finding 1: the spawned backend must run with an explicit cwd,
    not whatever cwd the parent (e.g. Task Scheduler) happens to default to."""
    script_dir = tmp_path / "script_dir"
    script_dir.mkdir()
    script = script_dir / "cwd_marker_backend.py"
    script.write_text(CWD_MARKER_BACKEND_SCRIPT, encoding="utf-8")

    desired_cwd = tmp_path / "desired_cwd"
    desired_cwd.mkdir()
    marker_path = tmp_path / "marker.txt"

    port = free_port()
    command = [sys.executable, str(script), str(port), str(marker_path)]
    manager = ProcessManager(
        start_command=command, health_url=f"http://127.0.0.1:{port}/",
        health_timeout_seconds=10, cwd=str(desired_cwd),
    )

    try:
        await manager.ensure_awake()
        assert manager.is_running()
        recorded_cwd = marker_path.read_text().strip()
        assert Path(recorded_cwd).resolve() == desired_cwd.resolve()
    finally:
        manager.stop_if_idle()
