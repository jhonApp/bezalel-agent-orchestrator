from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _base_url(base_url: str | None) -> str:
    return (base_url or os.getenv("ORCHESTRATOR_API_URL", DEFAULT_BASE_URL)).rstrip("/")


async def run_feature(feature_request: str, project_id: str = "bezalel", dry_run: bool = False,
                      analysis_only: bool = False, base_url: str | None = None) -> dict[str, Any]:
    """Dispatch a feature request to the running orchestrator API.

    This is a thin proxy, not a second orchestrator: it posts to the same
    `/executions` endpoint a human would call by hand, so the run lands on the
    same `LiveEventBroker` instance the Manage Agents panel and `/events/view`
    are already subscribed to. Without an orchestrator API up, there is no
    broker to publish into and nothing for the caller to watch, so that case
    is reported back as a clear error instead of a raw connection exception.

    `analysis_only=True` runs the real agents (they do read the code and
    respond) but the orchestrator refuses to commit, merge, or deploy no
    matter what `AUTO_COMMIT`/`AUTO_MERGE`/`AUTO_DEPLOY` say — use it for a
    review/audit request that must not touch the managed repositories.
    """
    url = _base_url(base_url)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
            response = await client.post("/executions", json={
                "feature_request": feature_request, "project_id": project_id,
                "dry_run": dry_run, "analysis_only": analysis_only,
            })
    except httpx.ConnectError:
        return {"error": f"Orchestrator API unreachable at {url}. Start it with `bezalel-orchestrator api`."}
    if response.status_code >= 400:
        return {"error": f"Orchestrator API returned {response.status_code}: {response.text[:500]}"}
    data = response.json()
    return {**data, "panel_url": f"{url}/", "events_url": f"{url}/events/view"}


async def check_execution(execution_id: str, base_url: str | None = None) -> dict[str, Any]:
    """Fetch the current state of a previously dispatched orchestrator execution."""
    url = _base_url(base_url)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
            response = await client.get(f"/executions/{execution_id}")
    except httpx.ConnectError:
        return {"error": f"Orchestrator API unreachable at {url}. Start it with `bezalel-orchestrator api`."}
    if response.status_code == 404:
        return {"error": f"execution {execution_id} not found"}
    return response.json()


mcp = FastMCP("bezalel-orchestrator")


@mcp.tool()
async def run_orchestrator_feature(feature_request: str, project_id: str = "bezalel", dry_run: bool = False,
                                   analysis_only: bool = False) -> dict[str, Any]:
    """Use this whenever the user asks to implement, build, fix, review, analyze, or audit
    something in one of the Bezalel projects (the bezalel-app frontend, the .NET backend, or
    the Workflow-IA Python/LangGraph pipeline) — instead of doing that work yourself in this
    session. This dispatches the request to the Bezalel 7-agent orchestrator (supervisor,
    frontend, backend, python_ai, contracts, qa, security, reviewer), which plans, implements,
    tests, reviews and gates the change end to end.

    Set `analysis_only=True` for a review/audit/"how could we improve X" request that must not
    change anything: the real agents still run and read the code, but the orchestrator refuses
    to commit, merge, or deploy regardless of the AUTO_COMMIT/AUTO_MERGE/AUTO_DEPLOY settings.
    Leave it False only when the user actually wants the change implemented.

    Progress (plan, agent start/finish, live Codex output, and each agent's findings) shows up
    immediately in the orchestrator's Manage Agents panel and its `/events/view` page — this
    call itself just returns the execution id to track, it does not wait for the run to finish.
    Use `check_orchestrator_execution` to poll that id for a final result.
    """
    return await run_feature(feature_request, project_id, dry_run, analysis_only)


@mcp.tool()
async def check_orchestrator_execution(execution_id: str) -> dict[str, Any]:
    """Check the current status of a previously dispatched orchestrator execution."""
    return await check_execution(execution_id)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
