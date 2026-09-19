from __future__ import annotations

import argparse
import asyncio
import json
import sys

from adapters.codex_cli import CodexCLI
from adapters.project_detector import discovery_markdown, discover_projects
from api.app import create_app
from orchestrator.config import Settings
from orchestrator.graph import OrchestrationGraph
from schemas.models import ExecutionRequest


def format_console_event(event: dict) -> str | None:
    """Render one orchestrator event as a single terminal line, or None to skip it.

    Skipped rather than shown: events with no operator-relevant content (a
    `state.updated` tick with no named milestone, a blank stdout/stderr line
    from Codex) — printing those just buries the signal a `run --feature`
    caller is watching for.
    """
    kind = event.get("type")
    if kind == "agent.started":
        return f">> {event.get('agent')} {event.get('task_id')} started: {event.get('description')}"
    if kind == "agent.finished":
        return f"<< {event.get('agent')} {event.get('task_id')} finished: {event.get('status')}"
    if kind == "agent.stream":
        raw = (event.get("raw") or "").strip()
        if not raw:
            return None
        return f"   [{event.get('agent')}] {raw}"
    if kind == "state.updated":
        name = event.get("event")
        if not name:
            return None
        return f"-- {event.get('node')}: {name}"
    return None


async def console_event_sink(event: dict) -> None:
    line = format_console_event(event)
    if line is not None:
        print(line, flush=True)


async def run_with_native_checkpointer(settings: Settings, request: ExecutionRequest) -> dict:
    """Use the same durable LangGraph state backend as the administration API."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.langgraph_checkpoint_path)) as saver:
        await saver.setup()
        runtime = __import__("orchestrator.nodes", fromlist=["ExecutionRuntime"]).ExecutionRuntime(
            settings, event_sink=console_event_sink)
        return await OrchestrationGraph(runtime, checkpointer=saver).run(request)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bezalel-orchestrator")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="detect project stacks and commands")
    sub.add_parser("check-codex", help="verify codex CLI and supported exec flags")
    run = sub.add_parser("run", help="run an orchestration")
    run.add_argument("--feature", required=True)
    run.add_argument("--project", default="bezalel")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument(
        "--analysis-only",
        action="store_true",
        help="run the agents read-only and disable commit, merge, and deploy",
    )
    api = sub.add_parser("api", help="start administration API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    return p


def cli(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.load()
    if args.command == "discover":
        projects = discover_projects(settings)
        print(discovery_markdown(projects, settings))
        return 0
    if args.command == "check-codex":
        print(json.dumps(asyncio.run(CodexCLI(settings).check()), indent=2, ensure_ascii=False))
        return 0
    if args.command == "run":
        state = asyncio.run(run_with_native_checkpointer(
            settings, ExecutionRequest(
                feature_request=args.feature,
                project_id=args.project,
                dry_run=args.dry_run,
                analysis_only=args.analysis_only,
            )
        ))
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(state.get("final_report", state), indent=2, ensure_ascii=False, default=str))
        return 0 if state.get("status") == "completed" else 1
    if args.command == "api":
        import uvicorn
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
