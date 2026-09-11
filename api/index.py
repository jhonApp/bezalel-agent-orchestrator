"""Vercel entrypoint for the read-only administration panel/API."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Vercel Functions have ephemeral writable storage. Keep the checkpoint files
# in /tmp for warm-instance diagnostics; durable production execution belongs
# to the worker deployment, not this panel function.
os.environ.setdefault("CHECKPOINT_SQLITE_PATH", "/tmp/orchestrator.sqlite3")
os.environ.setdefault("LANGGRAPH_CHECKPOINT_SQLITE_PATH", "/tmp/langgraph-checkpoints.sqlite3")
os.environ.setdefault("AGENT_PLATFORM_DESIGN_ROOT", str(ROOT / "web"))

from src.api.app import create_app  # noqa: E402

app = create_app()
